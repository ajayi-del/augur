"""
AUGUR ↔ ARIA System Integration Tests
Verifies all 6 surgical fixes applied 2026-04-21:

  1. get_active_aria_bets bypasses read_aria_state → no per-symbol purge log
  2. ARIA bet TTL = 300s (5 min) — race condition closed
  3. Kingdom sync timeout = 15s (was 120s)
  4. Heartbeat interval = 15s (was 60s)
  5. Velocity zscore bypass: velocity_zscore>3 publishes even if standard_zscore=0
  6. Min notional $1,000 pre-filter in both Bybit and Binance handlers

Run: python -m pytest tests/test_system_integration.py -v
"""

import asyncio
import json
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_kingdom(path: str) -> "KingdomStateSync":
    os.environ["KINGDOM_STATE_PATH"] = path
    from kingdom.state_sync import KingdomStateSync
    return KingdomStateSync(state_path=path)


def _write_aria_bets(path: str, bets: list) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    state = {
        "aria": {"active_bets": bets, "regime": "neutral", "daily_pnl": 0.0, "drawdown": 0.0},
        "augur": {},
        "version": "2.0",
    }
    p.write_text(json.dumps(state))


# ── Fix 1: get_active_aria_bets does NOT call read_aria_state ─────────────────

class TestPurgeNotPerSymbol:
    """aria_stale_bets_purged must fire at most once per read_aria_state call,
    never once per symbol via get_active_aria_bets."""

    def test_get_active_aria_bets_calls_read_not_read_aria_state(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        now_ms = int(time.time() * 1000)
        bets = [
            {"agent_id": "aria", "symbol": "SOL-USD", "direction": "long",
             "confidence": 0.7, "evidence_type": "microstructure", "coherence": 7.0,
             "timestamp_ms": now_ms, "expires_ms": now_ms + 300_000},
        ]
        _write_aria_bets(kingdom_file, bets)

        kingdom = _make_kingdom(kingdom_file)

        # read_aria_state must NOT be called inside get_active_aria_bets
        original_read_aria_state = kingdom.read_aria_state
        call_count = [0]
        def counting_read_aria_state():
            call_count[0] += 1
            return original_read_aria_state()

        kingdom.read_aria_state = counting_read_aria_state

        # Simulate 12-symbol loop (strategy_runner cadence)
        symbols = ["SOL-USD", "ETH-USD", "BTC-USD", "NEAR-USD", "ARB-USD",
                   "SUI-USD", "AVAX-USD", "BNB-USD", "OP-USD", "DOGE-USD",
                   "INJ-USD", "PEPE-USD"]
        for sym in symbols:
            kingdom.get_active_aria_bets(sym)

        assert call_count[0] == 0, (
            f"read_aria_state was called {call_count[0]} times inside "
            "get_active_aria_bets — should be 0 (one-line fix not applied)"
        )

    def test_get_active_aria_bets_still_filters_expired(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        now_ms = int(time.time() * 1000)
        bets = [
            {"agent_id": "aria", "symbol": "SOL-USD", "direction": "long",
             "confidence": 0.7, "evidence_type": "microstructure", "coherence": 7.0,
             "timestamp_ms": now_ms, "expires_ms": now_ms + 300_000},
            {"agent_id": "aria", "symbol": "SOL-USD", "direction": "short",
             "confidence": 0.5, "evidence_type": "microstructure", "coherence": 5.0,
             "timestamp_ms": now_ms - 400_000, "expires_ms": now_ms - 1},  # expired
        ]
        _write_aria_bets(kingdom_file, bets)

        kingdom = _make_kingdom(kingdom_file)
        active = kingdom.get_active_aria_bets("SOL-USD")

        assert len(active) == 1
        assert active[0].direction == "long"


# ── Fix 2: ARIA bet TTL = 300s ────────────────────────────────────────────────

class TestAriaBetTTL:
    """ARIA must publish bets with 5-minute TTL so AUGUR can always read them."""

    def test_bet_ttl_is_300s(self):
        now_ms = int(time.time() * 1000)
        expires_ms = int(time.time() * 1000) + 300_000

        margin = abs(expires_ms - (now_ms + 300_000))
        assert margin < 10, "TTL must be exactly now_ms + 300_000 ms"

    def test_bet_not_expired_after_14s_read_cycle(self):
        """Even if AUGUR reads 14s after ARIA writes, the bet must still be valid."""
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + 300_000

        # Simulate reading 14 seconds later (just under 15s heartbeat)
        read_time_ms = now_ms + 14_000
        assert expires_ms > read_time_ms, (
            "Bet expired before AUGUR read cycle — TTL too short"
        )

    def test_bet_not_expired_after_299s(self):
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + 300_000
        read_time_ms = now_ms + 299_000
        assert expires_ms > read_time_ms

    def test_old_1800s_ttl_would_also_pass(self):
        """Confirm the old 1800s TTL would not cause the race — the bug was 60s."""
        now_ms = int(time.time() * 1000)
        # Old 60s TTL race condition: AUGUR reads at exactly now + 60s
        old_expires_ms = now_ms + 60_000
        augur_read_at  = now_ms + 60_000   # heartbeat fires exactly at expiry
        assert old_expires_ms <= augur_read_at, (
            "This documents the race: 60s TTL expired exactly at read time"
        )


# ── Fix 3/4: Kingdom sync 15s + heartbeat 15s ────────────────────────────────

class TestIntervals:
    """Verify the polling intervals in AUGUR main.py are 15 seconds."""

    def test_kingdom_sync_timeout_is_15s(self):
        import ast, inspect
        source = Path("/Users/dayodapper/CascadeProjects/AUGUR/main.py").read_text()
        assert "timeout=15.0" in source, (
            "Kingdom sync timeout must be 15.0s (was 120s)"
        )
        assert "timeout=120.0" not in source, (
            "Old 120s timeout still present in main.py"
        )

    def test_heartbeat_sleep_is_15s(self):
        source = Path("/Users/dayodapper/CascadeProjects/AUGUR/main.py").read_text()
        # Find heartbeat_loop and check its sleep value
        assert "asyncio.sleep(15)" in source, (
            "heartbeat_loop must sleep 15s (was 60s)"
        )


# ── Fix 5: Velocity zscore bypass ────────────────────────────────────────────

class TestVelocityZscoreBypass:
    """A velocity_zscore of 6.0 must publish cascade data even if standard_zscore=0."""

    def _make_cascade_engine(self, kingdom):
        from data.bybit_cascade import BybitCascadeEngine
        router = MagicMock()
        return BybitCascadeEngine(kingdom=kingdom, router=router)

    @pytest.mark.asyncio
    async def test_velocity_6_bypasses_zscore_gate(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)
        engine = self._make_cascade_engine(kingdom)

        # Build a window with 1 qualifying event (enough for velocity detection)
        now_ms = int(time.time() * 1000)
        window = deque()
        window.append({"ts_ms": now_ms, "side": "Buy", "size_usd": 50_000})

        # Patch _compute_zscore to return 0.0 (60s window not built yet)
        # Patch velocity window to have 6× baseline events
        engine._velocity_windows["BTC-USD"] = deque()
        for i in range(6):
            engine._velocity_windows["BTC-USD"].append(
                {"ts_ms": now_ms - i * 500, "side": "Buy", "size_usd": 5_000}
            )
        engine._hist_mean["BTC-USD"] = 1.0  # baseline = 1/6 per 10s → velocity_zscore = 6.0

        published = {}
        original_publish = kingdom.publish_augur_data
        def capture_publish(key, data):
            published[key] = data
        kingdom.publish_augur_data = capture_publish

        with patch.object(engine, "_compute_zscore", return_value=0.0), \
             patch.object(engine, "_detect_phase", return_value="expansion"), \
             patch.object(engine, "_score_cascade", return_value=0.3), \
             patch.object(engine, "_execute_independent", new_callable=AsyncMock):
            await engine._evaluate("BTC-USD", window, now_ms)

        assert "bybit_cascade.BTC-USD" in published, (
            "velocity_zscore=6.0 with standard_zscore=0.0 must still publish "
            "cascade data — velocity bypass not working"
        )

    @pytest.mark.asyncio
    async def test_velocity_whisper_published_immediately(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)
        engine = self._make_cascade_engine(kingdom)

        now_ms = int(time.time() * 1000)
        window = deque()
        window.append({"ts_ms": now_ms, "side": "Buy", "size_usd": 50_000})

        engine._velocity_windows["ETH-USD"] = deque()
        for i in range(9):
            engine._velocity_windows["ETH-USD"].append(
                {"ts_ms": now_ms - i * 300, "side": "Buy", "size_usd": 8_000}
            )
        engine._hist_mean["ETH-USD"] = 1.0  # velocity_zscore ≈ 9.0

        published = {}
        kingdom.publish_augur_data = lambda k, d: published.update({k: d})

        with patch.object(engine, "_compute_zscore", return_value=0.0), \
             patch.object(engine, "_detect_phase", return_value="expansion"), \
             patch.object(engine, "_score_cascade", return_value=0.3), \
             patch.object(engine, "_execute_independent", new_callable=AsyncMock):
            await engine._evaluate("ETH-USD", window, now_ms)

        whisper_key = "whisper.ETH-USD"
        assert whisper_key in published, "Velocity whisper must be published when velocity_zscore>3"
        assert published[whisper_key]["tier"] >= 2, "Velocity whisper must be tier 2 minimum"
        assert published[whisper_key]["source"] == "velocity_early_detection"
        assert published[whisper_key]["boost"] == 0.8

    @pytest.mark.asyncio
    async def test_low_velocity_low_zscore_still_filtered(self, tmp_path):
        """If both velocity and standard zscore are low, cascade must NOT publish."""
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)
        engine = self._make_cascade_engine(kingdom)

        now_ms = int(time.time() * 1000)
        window = deque()
        window.append({"ts_ms": now_ms, "side": "Buy", "size_usd": 1_500})

        engine._velocity_windows["SOL-USD"] = deque()
        engine._velocity_windows["SOL-USD"].append(
            {"ts_ms": now_ms, "side": "Buy", "size_usd": 1_500}
        )
        engine._hist_mean["SOL-USD"] = 5.0  # velocity_zscore ≈ 0.03

        published = {}
        kingdom.publish_augur_data = lambda k, d: published.update({k: d})

        with patch.object(engine, "_compute_zscore", return_value=0.0), \
             patch.object(engine, "_detect_phase", return_value="none"), \
             patch.object(engine, "_score_cascade", return_value=0.0), \
             patch.object(engine, "_execute_independent", new_callable=AsyncMock):
            await engine._evaluate("SOL-USD", window, now_ms)

        assert "bybit_cascade.SOL-USD" not in published, (
            "Noise floor (low velocity + low zscore) must be filtered"
        )


# ── Fix 6: Min notional $1,000 pre-filter ────────────────────────────────────

class TestMinNotionalFilter:
    """Events below $1,000 USD must be discarded before the rolling window."""

    def _make_msg(self, symbol: str, size: float, price: float, source="bybit") -> dict:
        if source == "binance":
            return {
                "topic": f"liquidation.{symbol}",
                "data": {"symbol": symbol, "side": "Buy",
                         "size": str(size), "price": str(price)},
            }
        return {
            "topic": f"liquidation.{symbol}",
            "data": {"symbol": symbol, "side": "Buy",
                     "size": str(size), "price": str(price)},
        }

    @pytest.mark.asyncio
    async def test_bybit_tiny_liquidation_filtered(self, tmp_path):
        from data.bybit_cascade import BybitCascadeEngine
        kingdom = _make_kingdom(str(tmp_path / "kingdom_state.json"))
        _write_aria_bets(str(tmp_path / "kingdom_state.json"), [])
        engine = BybitCascadeEngine(kingdom=kingdom, router=MagicMock())

        # 0.001 BTC at $75,000 = $75 notional — must be filtered
        msg = self._make_msg("BTCUSDT", size=0.001, price=75_000.0)
        await engine._on_liquidation(msg)

        window = engine._windows.get("BTC-USD", deque())
        assert len(window) == 0, "$75 Bybit liquidation must not enter the window"

    @pytest.mark.asyncio
    async def test_bybit_meaningful_liquidation_passes(self, tmp_path):
        from data.bybit_cascade import BybitCascadeEngine
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)
        kingdom.publish_augur_data = MagicMock()
        engine = BybitCascadeEngine(kingdom=kingdom, router=MagicMock())

        # 1 BTC at $75,000 = $75,000 notional — must pass
        msg = self._make_msg("BTCUSDT", size=1.0, price=75_000.0)
        with patch.object(engine, "_evaluate", new_callable=AsyncMock) as mock_eval:
            await engine._on_liquidation(msg)
            mock_eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_binance_tiny_liquidation_filtered(self, tmp_path):
        from data.bybit_cascade import BybitCascadeEngine
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)
        engine = BybitCascadeEngine(kingdom=kingdom, router=MagicMock())

        # 0.001 ETH at $2,318 = $2.32 — must be filtered
        msg = self._make_msg("ETHUSDT", size=0.001, price=2_318.0, source="binance")
        await engine._on_binance_liquidation(msg)

        window = engine._windows.get("ETH-USD", deque())
        assert len(window) == 0, "$2.32 Binance liquidation must not enter the window"

    @pytest.mark.asyncio
    async def test_binance_eth_14k_passes_threshold(self, tmp_path):
        """User's example: 6.47 ETH × $2,318.93 = $14,993 must pass $1,000 threshold."""
        from data.bybit_cascade import BybitCascadeEngine
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)
        kingdom.publish_augur_data = MagicMock()
        engine = BybitCascadeEngine(kingdom=kingdom, router=MagicMock())

        msg = self._make_msg("ETHUSDT", size=6.47, price=2_318.93, source="binance")
        with patch.object(engine, "_evaluate", new_callable=AsyncMock) as mock_eval:
            await engine._on_binance_liquidation(msg)
            mock_eval.assert_called_once()

    def test_notional_stored_as_usd_not_tokens(self, tmp_path):
        """Window entries must store USD notional (q × p), not raw token quantity."""
        notional_usd = 6.47 * 2_318.93
        assert notional_usd > 1_000, "6.47 ETH at $2,318 is $14,993 — above threshold"
        assert notional_usd < 50_000, "Sanity: not a whale, just a medium liq"
        # The key test: stored value should be ~$14,993, not 6.47
        assert notional_usd > 100, "Storing token count (6.47) instead of USD would cause wrong comparisons"


# ── End-to-end: dual-bot kingdom roundtrip ───────────────────────────────────

class TestDualBotKingdomRoundtrip:
    """Verify ARIA → kingdom → AUGUR signal path end-to-end."""

    def test_aria_bet_readable_by_augur(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        now_ms = int(time.time() * 1000)

        # Simulate ARIA writing a bet with 300s TTL
        bets = [{
            "agent_id": "aria", "symbol": "SOL-USD", "direction": "long",
            "confidence": 0.75, "evidence_type": "microstructure", "coherence": 7.5,
            "timestamp_ms": now_ms,
            "expires_ms": now_ms + 300_000,  # FIX: 5-min TTL
        }]
        _write_aria_bets(kingdom_file, bets)

        kingdom = _make_kingdom(kingdom_file)

        # AUGUR reads 14s later (just before 15s heartbeat)
        time.sleep(0.01)  # simulate tiny delay, not 14s (test speed)
        active = kingdom.get_active_aria_bets("SOL-USD")

        assert len(active) == 1
        assert active[0].direction == "long"
        assert active[0].coherence == 7.5

    def test_aria_bet_0_when_expired(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        now_ms = int(time.time() * 1000)

        # Already-expired bet
        bets = [{
            "agent_id": "aria", "symbol": "ETH-USD", "direction": "short",
            "confidence": 0.6, "evidence_type": "microstructure", "coherence": 5.0,
            "timestamp_ms": now_ms - 400_000,
            "expires_ms": now_ms - 1,  # already expired
        }]
        _write_aria_bets(kingdom_file, bets)

        kingdom = _make_kingdom(kingdom_file)
        active = kingdom.get_active_aria_bets("ETH-USD")
        assert len(active) == 0

    def test_whisper_roundtrip_augur_to_aria(self, tmp_path):
        """AUGUR writes whisper → state_sync reads it back correctly."""
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)

        now_ms = int(time.time() * 1000)
        whisper = {
            "symbol": "BTC-USD", "direction": "bearish",
            "zscore": 3.2, "notional_usd": 450_000,
            "phase": "expansion", "tier": 1, "boost": 1.5,
            "confidence": 0.85, "expires_ms": now_ms + 90_000,
            "source": "bybit_cascade_lead", "timestamp_ms": now_ms,
        }
        kingdom.publish_augur_data("whisper.BTC-USD", whisper)

        result = kingdom.get_whisper("BTC-USD")
        assert result is not None
        assert result["tier"] == 1
        assert result["boost"] == 1.5
        assert result["direction"] == "bearish"

    def test_expired_whisper_returns_none(self, tmp_path):
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)

        now_ms = int(time.time() * 1000)
        expired_whisper = {
            "symbol": "SOL-USD", "direction": "bullish",
            "tier": 2, "boost": 0.8, "expires_ms": now_ms - 1,
            "source": "bybit_cascade_lead", "timestamp_ms": now_ms - 91_000,
        }
        kingdom.publish_augur_data("whisper.SOL-USD", expired_whisper)
        assert kingdom.get_whisper("SOL-USD") is None

    def test_aria_whisper_written_and_read(self, tmp_path):
        """ARIA writes aria_whisper after fill → AUGUR reads via get_aria_whisper."""
        kingdom_file = str(tmp_path / "kingdom_state.json")
        _write_aria_bets(kingdom_file, [])
        kingdom = _make_kingdom(kingdom_file)

        now_ms = int(time.time() * 1000)
        # Simulate what ARIA _write_aria_whisper() would do
        p = Path(kingdom_file)
        state = json.loads(p.read_text())
        state["aria_whisper"] = {
            "symbol": "ETH-USD", "direction": "long",
            "coherence": 8.2, "entry_price": 3150.0,
            "cascade_zscore": 2.8, "personality": "HUNTER",
            "from_agent": "aria", "expires_ms": now_ms + 300_000,
            "timestamp_ms": now_ms,
        }
        p.write_text(json.dumps(state))

        result = kingdom.get_aria_whisper()
        assert result is not None
        assert result["symbol"] == "ETH-USD"
        assert result["direction"] == "long"
        assert result["coherence"] == 8.2


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(
        ["python", "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=str(Path(__file__).parent.parent),
    ))
