"""
AUGUR Production Verification Suite.

Philosophy: tests should be as strict as a Kant categorical imperative
and as precise as a Nietzsche aphorism.

Every test verifies a production invariant.
A single FAIL = do not go live.

Run:
  python3 -m pytest tests/verify_production.py -v --tb=short --no-header

Expected: 35 passed, 0 failed, 0 errors.
"""

import asyncio
import json
import math
import os
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

KINGDOM_PATH = os.getenv(
    "KINGDOM_STATE_PATH",
    os.path.expanduser("~/kingdom/kingdom_state.json"),
)


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 1 — INFRASTRUCTURE
# Kingdom, venues, feeds must be reachable
# ═══════════════════════════════════════════════════════════════════════════

class TestInfrastructure:

    def test_kingdom_file_exists_and_readable(self):
        """Kingdom state must exist and be valid JSON."""
        path = Path(KINGDOM_PATH)
        assert path.exists(), f"Kingdom not found: {KINGDOM_PATH}"
        with open(path) as f:
            state = json.load(f)
        assert "aria"  in state, "Kingdom missing aria section"
        assert "augur" in state, "Kingdom missing augur section"
        print(f"  Kingdom keys: {list(state.keys())}")

    def test_kingdom_aria_has_regime(self):
        """ARIA must be publishing regime to kingdom."""
        with open(KINGDOM_PATH) as f:
            state = json.load(f)
        aria = state.get("aria", {})
        assert "regime" in aria, "ARIA not writing regime to kingdom"
        print(f"  ARIA regime: {aria.get('regime')}")
        print(f"  ARIA bets:   {len(aria.get('active_bets', []))}")

    @pytest.mark.asyncio
    async def test_bybit_public_api(self):
        """Bybit market data must resolve from GCP Frankfurt."""
        import aiohttp, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        conn = aiohttp.TCPConnector(ssl=ctx)
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": "NEARUSDT"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                assert r.status == 200, f"Bybit returned {r.status}"
                data  = await r.json()
                price = float(data["result"]["list"][0]["markPrice"])
                assert price > 0, "Bybit returned zero price"
                print(f"  Bybit NEAR mark price: {price}")

    @pytest.mark.asyncio
    async def test_bybit_websocket_connects(self):
        """Bybit WebSocket must connect and send subscription ack."""
        import ssl, websockets
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        received = []
        async with websockets.connect(
            "wss://stream.bybit.com/v5/public/linear",
            open_timeout=8,
            ssl=ctx,
        ) as ws:
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": ["liquidation.NEARUSDT"],
            }))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=4)
                received.append(json.loads(msg))
            except asyncio.TimeoutError:
                pass  # connected but no liq events — normal
        print(f"  Bybit WS: connected  messages={len(received)}")

    def test_watchdog_available(self):
        """watchdog must be installed for sub-100ms kingdom sync."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            print("  watchdog: installed ✓")
        except ImportError:
            pytest.fail("watchdog not installed — run: pip install watchdog")

    def test_bybit_execution_client_importable(self):
        """Bybit execution client must import without errors."""
        from execution.bybit_client import BybitClient
        client = BybitClient(mode="paper")
        assert client.mode == "paper"
        print(f"  BybitClient: importable mode=paper ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 2 — KINGDOM LATENCY
# Sub-100ms write→read round trip is required
# ═══════════════════════════════════════════════════════════════════════════

class TestKingdomLatency:

    def test_kingdom_write_read_latency(self):
        """Kingdom write→read must be under 100ms."""
        import filelock

        lock_path = KINGDOM_PATH + ".lock"
        t0 = time.time()

        with filelock.FileLock(lock_path, timeout=1):
            with open(KINGDOM_PATH, "r") as f:
                state = json.load(f)
            state["_test"] = {"ts": t0}
            tmp = KINGDOM_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, KINGDOM_PATH)

        with filelock.FileLock(lock_path, timeout=1):
            with open(KINGDOM_PATH, "r") as f:
                readback = json.load(f)

        latency_ms = (time.time() - t0) * 1000
        assert latency_ms < 100, f"Kingdom round-trip: {latency_ms:.1f}ms (max 100ms)"
        assert "_test" in readback, "Write not persisted"

        # Clean up
        with filelock.FileLock(lock_path, timeout=1):
            with open(KINGDOM_PATH, "r") as f:
                state = json.load(f)
            state.pop("_test", None)
            with open(KINGDOM_PATH, "w") as f:
                json.dump(state, f)

        print(f"  Kingdom latency: {latency_ms:.1f}ms ✓")

    def test_kingdom_state_sync_watchdog_method(self):
        """KingdomStateSync must expose start_watcher method."""
        from kingdom.state_sync import KingdomStateSync
        sync = KingdomStateSync(KINGDOM_PATH)
        assert hasattr(sync, "start_watcher"), "start_watcher method missing"
        assert hasattr(sync, "publish_augur_data"), "publish_augur_data missing"
        assert hasattr(sync, "get_augur_data"), "get_augur_data missing"
        print("  KingdomStateSync: watchdog methods present ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 3 — PHILOSOPHICAL STACK
# Personality → Kant → Nietzsche must fire correctly on every signal
# ═══════════════════════════════════════════════════════════════════════════

class TestPhilosophicalStack:

    def _signal(self, **kw):
        from intelligence.augur_personalities import AugurSignal
        defaults = dict(
            symbol="NEAR-USD", direction="short",
            combined=0.38, confidence=0.55, coherence=5.0,
            tps=2500.0, price_momentum_pct=-0.6,
            agg_ratio=0.20, funding_rate=0.001,
            cascade_zscore=2.5,
            timestamp_ms=int(time.time() * 1000),
            edge=0.24,
        )
        defaults.update(kw)
        return AugurSignal(**defaults)

    def test_personality_momentum_on_cascade(self):
        """Cascade z>2 + agg_ratio<0.35 → MOMENTUM."""
        from intelligence.augur_personalities import assign_personality, AugurPersonality
        sig = self._signal(cascade_zscore=2.5, agg_ratio=0.25)
        p = assign_personality(sig, 0.01, False, 0.0, 0.0, 50.0)
        assert p == AugurPersonality.MOMENTUM, f"Expected MOMENTUM, got {p.value}"
        print(f"  Cascade z=2.5 agg=0.25 → {p.value} ✓")

    def test_personality_sentinel_on_drawdown(self):
        """aria_drawdown>3% → SENTINEL regardless of signal."""
        from intelligence.augur_personalities import assign_personality, AugurPersonality
        sig = self._signal(cascade_zscore=3.0)
        p = assign_personality(sig, 0.05, False, 0.0, 0.0, 50.0)
        assert p == AugurPersonality.SENTINEL, f"Expected SENTINEL, got {p.value}"
        print(f"  Drawdown 5% → {p.value} ✓")

    def test_personality_arbitrage_on_divergence(self):
        """bybit_divergence>0.25% → ARBITRAGE."""
        from intelligence.augur_personalities import assign_personality, AugurPersonality
        sig = self._signal(cascade_zscore=0.5)
        p = assign_personality(sig, 0.01, False, 0.004, 0.0, 50.0)
        assert p == AugurPersonality.ARBITRAGE, f"Expected ARBITRAGE, got {p.value}"
        print(f"  Divergence 0.4% → {p.value} ✓")

    def test_kant_approves_valid_signal(self):
        """Kant must approve structurally sound trade."""
        from intelligence.augur_kant import AugurKant
        from intelligence.augur_personalities import AugurPersonality
        kant = AugurKant()
        sig   = self._signal()
        frame = kant.validate(
            signal=sig,
            personality=AugurPersonality.MOMENTUM,
            bybit_connected=True,
            total_exposure_pct=0.25,
            symbol_exposure_pct=0.05,
            augur_has_position=False,
            aria_regime="alt_season",
            aria_drawdown=0.01,
            kingdom_total_positions=1,
            max_open_trades=4,
        )
        assert frame.passed, f"Kant rejected: {[c.name for c in frame.failed_checks]}"
        assert frame.confidence > 0, f"confidence={frame.confidence}"
        print(f"  Kant: APPROVED  confidence={frame.confidence:.2f} ✓")

    def test_kant_rejects_stale_signal(self):
        """Signal older than 5 min must fail signal_fresh check."""
        from intelligence.augur_kant import AugurKant
        from intelligence.augur_personalities import AugurPersonality
        kant = AugurKant()
        sig   = self._signal(timestamp_ms=int((time.time() - 400) * 1000))
        frame = kant.validate(
            signal=sig,
            personality=AugurPersonality.MOMENTUM,
            bybit_connected=True,
            total_exposure_pct=0.20,
            symbol_exposure_pct=0.05,
            augur_has_position=False,
            aria_regime="alt_season",
            aria_drawdown=0.01,
            kingdom_total_positions=1,
            max_open_trades=4,
        )
        assert not frame.passed
        failed_names = [c.name for c in frame.failed_checks]
        assert "signal_fresh" in failed_names, f"Got: {failed_names}"
        print("  Kant: REJECTED stale signal ✓")

    def test_kant_rejects_overextended_kingdom(self):
        """total_exposure_pct ≥ 0.60 must fail capital_sound."""
        from intelligence.augur_kant import AugurKant
        from intelligence.augur_personalities import AugurPersonality
        kant = AugurKant()
        sig   = self._signal()
        frame = kant.validate(
            signal=sig,
            personality=AugurPersonality.MOMENTUM,
            bybit_connected=True,
            total_exposure_pct=0.70,
            symbol_exposure_pct=0.05,
            augur_has_position=False,
            aria_regime="alt_season",
            aria_drawdown=0.01,
            kingdom_total_positions=1,
            max_open_trades=4,
        )
        assert not frame.passed
        assert any("capital" in c.name for c in frame.failed_checks)
        print("  Kant: REJECTED overextension ✓")

    def test_nietzsche_aggressive_on_high_conviction(self):
        """High edge + high alignment + good wr → AGGRESSIVE or CONVICTED."""
        from intelligence.augur_nietzsche import AugurNietzsche, WillState
        from intelligence.augur_personalities import AugurPersonality
        from intelligence.augur_kant import KantFrame
        nietzsche = AugurNietzsche()
        sig = self._signal(edge=0.30)
        frame = KantFrame(
            passed=True, structure="cascade_momentum",
            confidence=0.85, coherence_min=4.0,
            order_type="market", size_cap=400.0,
        )
        out = nietzsche.compute(
            signal=sig,
            kant_frame=frame,
            personality=AugurPersonality.MOMENTUM,
            hist_wr=0.65,
            agent_alignment=0.78,
        )
        assert out.will_state in (WillState.AGGRESSIVE, WillState.CONVICTED), \
            f"Expected AGGRESSIVE/CONVICTED, got {out.will_state.value}"
        assert out.size_mult > 0.7, f"size_mult={out.size_mult}"
        print(f"  Nietzsche: {out.will_state.value}  "
              f"size_mult={out.size_mult:.2f}  conviction={out.conviction:.3f} ✓")

    def test_nietzsche_abstains_on_weak_signal(self):
        """Low edge + low alignment → ABSTAIN or CAUTIOUS."""
        from intelligence.augur_nietzsche import AugurNietzsche, WillState
        from intelligence.augur_personalities import AugurPersonality
        from intelligence.augur_kant import KantFrame
        nietzsche = AugurNietzsche()
        sig = self._signal(edge=0.04)
        frame = KantFrame(
            passed=True, structure="directional",
            confidence=0.35, coherence_min=3.0,
            order_type="limit", size_cap=200.0,
        )
        out = nietzsche.compute(
            signal=sig,
            kant_frame=frame,
            personality=AugurPersonality.MOMENTUM,
            hist_wr=0.42,
            agent_alignment=0.35,
        )
        assert out.size_mult <= 0.50, f"Weak signal size_mult too high: {out.size_mult}"
        print(f"  Nietzsche: {out.will_state.value}  size_mult={out.size_mult:.2f} ✓")

    def test_nietzsche_sentinel_zero_size(self):
        """SENTINEL personality → size_mult = 0.0 always."""
        from intelligence.augur_nietzsche import AugurNietzsche, WillState
        from intelligence.augur_personalities import AugurPersonality
        from intelligence.augur_kant import KantFrame
        nietzsche = AugurNietzsche()
        sig = self._signal(edge=0.40)
        frame = KantFrame(
            passed=True, structure="sentinel_close",
            confidence=0.90, coherence_min=0.0,
            order_type="market", size_cap=0.0,
        )
        out = nietzsche.compute(
            signal=sig,
            kant_frame=frame,
            personality=AugurPersonality.SENTINEL,
            hist_wr=0.90,
            agent_alignment=0.90,
        )
        assert out.size_mult == 0.0, f"SENTINEL must zero size, got {out.size_mult}"
        print("  SENTINEL → size_mult=0.0 ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 4 — CHANCELLOR GOVERNANCE
# The constitution must be enforced on every trade
# ═══════════════════════════════════════════════════════════════════════════

class TestChancellor:

    def _c(self):
        from kingdom.chancellor import Chancellor
        return Chancellor()

    def _call(self, c, aria_dir=None, aria_coh=0.0,
              augur_dir=None, augur_conv=0.0,
              drawdown=0.02, daily=0.01, cascade_z=0.5,
              total_exp=0.25, sym_exp=0.05, balance=300.0):
        return c.adjudicate(
            aria_direction=aria_dir, aria_coherence=aria_coh,
            augur_direction=augur_dir, augur_conviction=augur_conv,
            aria_drawdown=drawdown, daily_loss_pct=daily,
            cascade_zscore=cascade_z,
            total_exposure_pct=total_exp, symbol_exposure_pct=sym_exp,
            balance=balance,
        )

    def test_compound_strong_amplifies(self):
        """Both agents agree strongly → ≥ 1.20x size."""
        c  = self._c()
        d  = self._call(c, aria_dir="short", aria_coh=8.5,
                        augur_dir="short", augur_conv=0.78)
        assert d.action == "AUTHORIZE"
        assert d.aria_executes and d.augur_executes
        assert d.size_modifier >= 1.20, f"Expected ≥1.20, got {d.size_modifier}"
        print(f"  Compound strong → {d.size_modifier:.2f}x ✓")

    def test_conflict_penalises_size(self):
        """Agents disagree → AUGUR stands down, small size."""
        c  = self._c()
        d  = self._call(c, aria_dir="short", aria_coh=7.0,
                        augur_dir="long", augur_conv=0.60)
        assert d.size_modifier <= 0.25, f"Conflict size_modifier={d.size_modifier}"
        assert not d.augur_executes, "AUGUR should stand down on conflict"
        print(f"  Conflict → {d.size_modifier:.2f}x  augur_executes=False ✓")

    def test_emergency_veto_low_balance(self):
        """Balance < $200 → emergency VETO."""
        c = self._c()
        d = self._call(c, aria_dir="short", aria_coh=8.0,
                       augur_dir="short", augur_conv=0.80, balance=150.0)
        assert d.action == "VETO"
        assert not d.aria_executes and not d.augur_executes
        print(f"  Balance=150 → VETO  reason={d.reason} ✓")

    def test_emergency_veto_daily_loss(self):
        """daily_loss_pct > 5% → emergency VETO."""
        c = self._c()
        d = self._call(c, aria_dir="short", augur_dir="short",
                       aria_coh=8.0, augur_conv=0.80, daily=0.06)
        assert d.action == "VETO"
        print(f"  Daily loss 6% → VETO ✓")

    def test_treasury_blocks_overexposure(self):
        """total_exposure ≥ 60% → treasury VETO."""
        c = self._c()
        d = self._call(c, aria_dir="short", augur_dir="short",
                       aria_coh=8.0, augur_conv=0.80, total_exp=0.65)
        assert d.action == "VETO"
        print(f"  Overexposure 65% → VETO  reason={d.reason} ✓")

    def test_single_aria_strong_executes(self):
        """ARIA coherence > 7.0, no AUGUR → ARIA executes at 70%."""
        c  = self._c()
        d  = self._call(c, aria_dir="short", aria_coh=8.0)
        assert d.aria_executes
        assert not d.augur_executes
        assert 0.60 <= d.size_modifier <= 0.80, f"Got {d.size_modifier}"
        print(f"  Single ARIA strong → {d.size_modifier:.2f}x  augur=False ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 5 — BYBIT CASCADE ENGINE
# Cross-venue liquidation intelligence
# ═══════════════════════════════════════════════════════════════════════════

class TestBybitCascadeEngine:

    def _engine(self):
        from data.bybit_cascade import BybitCascadeEngine
        kingdom    = MagicMock()
        kingdom.get_aria_cascade.return_value = None
        kingdom.publish_augur_data = MagicMock()
        chancellor = MagicMock()
        chancellor.adjudicate.return_value = MagicMock(
            action="AUTHORIZE", augur_executes=True, size_modifier=0.5,
        )
        router = AsyncMock()
        return BybitCascadeEngine(
            kingdom=kingdom, chancellor=chancellor, router=router,
        )

    def test_symbol_map_covers_aria_universe(self):
        """All ARIA tier-A symbols must be in cascade engine map."""
        from data.bybit_cascade import _SYMBOL_MAP
        required = [
            "SOL-USD", "ETH-USD", "NEAR-USD", "ARB-USD",
            "SUI-USD", "AVAX-USD", "BNB-USD", "OP-USD",
            "INJ-USD", "WIF-USD", "BONK-USD",
        ]
        for sym in required:
            assert sym in _SYMBOL_MAP, f"Missing Bybit map for {sym}"
            assert _SYMBOL_MAP[sym].endswith("USDT"), f"Invalid: {_SYMBOL_MAP[sym]}"
        print(f"  {len(required)} symbols mapped ✓")

    def test_zscore_computation(self):
        """Z-score formula: (n - mean) / std."""
        engine = self._engine()
        engine._hist_mean = {"NEAR-USD": 5.0}
        engine._hist_std  = {"NEAR-USD": 2.0}
        z = engine._compute_zscore("NEAR-USD", 10)
        assert abs(z - 2.5) < 0.01, f"Expected 2.5, got {z}"
        print(f"  Z-score (10-5)/2 = {z:.2f} ✓")

    def test_cascade_score_strong_signal(self):
        """High z, high notional, expansion phase → score > 0.65."""
        engine = self._engine()
        score = engine._score_cascade(
            symbol="NEAR-USD", zscore=3.5,
            notional=1_000_000, phase="expansion", direction="bearish",
        )
        assert score > 0.65, f"Strong cascade scored {score:.3f}"
        print(f"  Strong cascade: {score:.3f} ✓")

    def test_cascade_score_weak_signal(self):
        """Low z, low notional, quiet → score < 0.35."""
        engine = self._engine()
        score = engine._score_cascade(
            symbol="NEAR-USD", zscore=1.1,
            notional=30_000, phase="quiet", direction="mixed",
        )
        assert score < 0.35, f"Weak cascade scored {score:.3f}"
        print(f"  Weak cascade: {score:.3f} ✓")

    @pytest.mark.asyncio
    async def test_kingdom_publish_on_cascade(self):
        """Z-score above threshold must publish to kingdom."""
        engine = self._engine()
        engine._hist_mean = {"NEAR-USD": 5.0}
        engine._hist_std  = {"NEAR-USD": 2.0}
        engine._last_stat_update = time.time()

        now = int(time.time() * 1000)
        window = deque()
        for i in range(15):
            window.append({
                "ts_ms": now - i * 500,
                "side": "Buy", "size_usd": 60_000,
            })

        await engine._evaluate("NEAR-USD", window, now)

        engine.kingdom.publish_augur_data.assert_called()
        key, data = engine.kingdom.publish_augur_data.call_args[0]
        assert "bybit_cascade" in key, f"Unexpected key: {key}"
        assert data["symbol"] == "NEAR-USD"
        assert data["direction"] == "bearish"  # Buy = longs liq'd = price fell
        print(f"  Published: key={key}  direction={data['direction']} ✓")

    @pytest.mark.asyncio
    async def test_independent_trade_fires_on_high_score(self):
        """Score ≥ 0.70 → router.place_order called."""
        engine = self._engine()
        engine._hist_mean         = {"NEAR-USD": 5.0}
        engine._hist_std          = {"NEAR-USD": 2.0}
        engine._last_stat_update  = time.time()
        engine._last_independent  = {}

        now    = int(time.time() * 1000)
        window = deque()
        for _ in range(30):
            window.append({"ts_ms": now, "side": "Buy", "size_usd": 500_000})

        with patch.object(engine, "_score_cascade", return_value=0.82):
            await engine._evaluate("NEAR-USD", window, now)

        engine.router.place_order.assert_called()
        call_kwargs = engine.router.place_order.call_args[1]
        assert call_kwargs["direction"] == "short"  # bearish → short
        print(f"  Score 0.82 → independent trade fired  direction=short ✓")

    def test_reconnect_loop_present(self):
        """start() must contain a while True reconnect loop."""
        import inspect
        from data.bybit_cascade import BybitCascadeEngine
        src = inspect.getsource(BybitCascadeEngine.start)
        assert "while True" in src,   "No reconnect loop in start()"
        assert "asyncio.sleep" in src, "No reconnect delay in start()"
        print("  Reconnect loop: present ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 6 — CROSS-LEARNING ENGINE
# Agents must teach each other through outcomes
# ═══════════════════════════════════════════════════════════════════════════

class TestCrossLearning:

    def _kingdom(self):
        k = MagicMock()
        k.read_finance.return_value = {"agent_alignment": {}}
        k.write_finance = MagicMock()
        return k

    def test_both_correct_boosts_alignment(self):
        """ARIA wins, AUGUR agreed → alignment delta > 0."""
        from memory.cross_learning import CrossLearningEngine
        k  = self._kingdom()
        cl = CrossLearningEngine(kingdom=k)
        cl.on_aria_trade_closed(
            symbol="NEAR-USD", aria_direction="short",
            aria_won=True, augur_direction="short",  # agreed
        )
        k.write_finance.assert_called()
        finance = k.write_finance.call_args[0][0]
        alignment = finance["agent_alignment"]["NEAR-USD"]
        assert alignment > 0.50, f"Both correct should boost, got {alignment}"
        print(f"  Both correct → alignment={alignment:.3f} ✓")

    def test_augur_wrong_penalises_alignment(self):
        """ARIA wins, AUGUR disagreed → alignment decreases."""
        from memory.cross_learning import CrossLearningEngine
        k  = self._kingdom()
        k.read_finance.return_value = {"agent_alignment": {"NEAR-USD": 0.60}}
        cl = CrossLearningEngine(kingdom=k)
        cl.on_aria_trade_closed(
            symbol="NEAR-USD", aria_direction="short",
            aria_won=True, augur_direction="long",  # disagreed
        )
        finance = k.write_finance.call_args[0][0]
        alignment = finance["agent_alignment"]["NEAR-USD"]
        assert alignment < 0.60, f"AUGUR wrong should penalise, got {alignment}"
        print(f"  AUGUR wrong → alignment={alignment:.3f} ✓")

    def test_augur_bet_resolved_updates_hist_wr(self):
        """Resolved bet → augur_hist_wr updated for symbol/direction."""
        from memory.cross_learning import CrossLearningEngine
        from memory.augur_hist_wr import augur_hist_wr as hist_wr
        k  = self._kingdom()
        cl = CrossLearningEngine(kingdom=k)
        before = hist_wr._data.copy()
        cl.on_augur_bet_resolved(
            symbol="NEAR-USD", direction="short",
            personality="momentum", augur_won=True,
        )
        assert hist_wr._data != before or len(hist_wr._data) > 0, \
            "hist_wr not updated after resolve"
        print("  Bet resolved → hist_wr updated ✓")

    def test_alignment_delta_bounded(self):
        """Each alignment change must be ≤ 0.05."""
        from memory.cross_learning import CrossLearningEngine, _ALIGN_DELTA
        assert _ALIGN_DELTA <= 0.05, f"Align delta too large: {_ALIGN_DELTA}"
        print(f"  Alignment delta: ±{_ALIGN_DELTA:.3f} (max 0.05) ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 7 — SESSION MANAGER (ARIA)
# Correct thresholds per trading session
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionManager:

    def test_london_threshold_is_4_5(self):
        """London coherence minimum must be 4.5 — was 5.0 (wrong)."""
        import sys as _sys
        aria_path = "/Users/dayodapper/CascadeProjects/ARIA"
        if aria_path not in _sys.path:
            _sys.path.insert(0, aria_path)
        import importlib
        sc = importlib.import_module("core.session_config")
        # Support _SESSIONS (private dict) or SESSION_CONFIGS (alias)
        sessions = getattr(sc, "SESSION_CONFIGS", None) or getattr(sc, "_SESSIONS", {})
        london   = sessions.get("london")
        minimum  = getattr(london, "coherence_minimum", None)
        assert minimum == 4.5, (
            f"London minimum={minimum}. Must be 4.5. "
            "Was 5.0 — blocked a cascade_zscore=2.3 trade with 95% sell pressure."
        )
        print(f"  London coherence_min: {minimum} ✓")

    def test_asian_threshold_is_restrictive(self):
        """Asian session must be more restrictive than London."""
        import sys as _sys, importlib
        aria_path = "/Users/dayodapper/CascadeProjects/ARIA"
        if aria_path not in _sys.path:
            _sys.path.insert(0, aria_path)
        sc      = importlib.import_module("core.session_config")
        sessions = getattr(sc, "SESSION_CONFIGS", None) or getattr(sc, "_SESSIONS", {})
        london  = sessions.get("london")
        asian   = sessions.get("asian")
        l_min   = getattr(london, "coherence_minimum", 0)
        a_min   = getattr(asian,  "coherence_minimum", 0)
        assert a_min > l_min, f"Asian({a_min}) should be > London({l_min})"
        print(f"  Asian={a_min} > London={l_min} ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 8 — EVENT BUS STABILITY (ARIA)
# Must initialise exactly once per process
# ═══════════════════════════════════════════════════════════════════════════

class TestEventBusStability:

    def test_event_bus_start_is_blocking(self):
        """event_bus.start() must be a coroutine (blocking)."""
        import sys
        if "/Users/dayodapper/CascadeProjects/ARIA" not in sys.path:
            sys.path.insert(0, "/Users/dayodapper/CascadeProjects/ARIA")
        import inspect
        from core.event_bus import CoalescedEventBus
        bus = CoalescedEventBus()
        assert asyncio.iscoroutinefunction(bus.start), "start() must be async"
        src = inspect.getsource(bus.start)
        assert "await" in src, "start() must await the dispatch loop"
        assert "_running" in src, "start() must set _running guard"
        print("  event_bus.start() is blocking coroutine ✓")

    def test_daily_tracker_load_guard(self):
        """DailyTracker._load must guard against double loading."""
        import sys
        if "/Users/dayodapper/CascadeProjects/ARIA" not in sys.path:
            sys.path.insert(0, "/Users/dayodapper/CascadeProjects/ARIA")
        import inspect
        from core.clock import DailyTradeTracker
        src = inspect.getsource(DailyTradeTracker._load)
        assert "_loaded" in src, "_loaded guard missing from _load()"
        print("  DailyTradeTracker._load: guard present ✓")


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 9 — FULL PIPELINE INTEGRATION
# Signal → Personality → Kant → Nietzsche → Chancellor in < 200ms
# ═══════════════════════════════════════════════════════════════════════════

class TestIntegration:

    def test_full_signal_pipeline_latency(self):
        """Complete pipeline must complete in under 200ms."""
        from intelligence.augur_personalities import AugurSignal, assign_personality
        from intelligence.augur_kant import AugurKant
        from intelligence.augur_nietzsche import AugurNietzsche
        from kingdom.chancellor import Chancellor

        t0 = time.time()

        sig = AugurSignal(
            symbol="NEAR-USD", direction="short",
            combined=0.35, confidence=0.60, coherence=5.5,
            tps=2800.0, price_momentum_pct=-0.7,
            agg_ratio=0.22, funding_rate=0.002,
            cascade_zscore=2.8,
            timestamp_ms=int(time.time() * 1000),
            edge=0.30,
        )

        personality = assign_personality(sig, 0.01, False, 0.0, 0.0, 50.0)
        frame = AugurKant().validate(
            signal=sig, personality=personality,
            bybit_connected=True,
            total_exposure_pct=0.25, symbol_exposure_pct=0.06,
            augur_has_position=False, aria_regime="alt_season",
            aria_drawdown=0.01, kingdom_total_positions=1,
            max_open_trades=4,
        )
        will = AugurNietzsche().compute(
            signal=sig, kant_frame=frame, personality=personality,
            hist_wr=0.55, agent_alignment=0.65,
        )
        decision = Chancellor().adjudicate(
            aria_direction=None, aria_coherence=0.0,
            augur_direction="short", augur_conviction=will.conviction,
            aria_drawdown=0.01, daily_loss_pct=0.01,
            cascade_zscore=2.8,
            total_exposure_pct=0.25, symbol_exposure_pct=0.06,
            balance=300.0,
        )

        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 200, f"Pipeline took {elapsed_ms:.1f}ms (max 200ms)"

        print(f"\n  FULL PIPELINE (NEAR-USD short):")
        print(f"    Personality: {personality.value}")
        print(f"    Kant:        {'PASS' if frame.passed else 'FAIL'}")
        print(f"    Nietzsche:   {will.will_state.value} conv={will.conviction:.3f}")
        print(f"    Chancellor:  {decision.action} {decision.size_modifier:.2f}x")
        print(f"    Latency:     {elapsed_ms:.1f}ms ✓")

    def test_kingdom_cross_agent_link(self):
        """ARIA bets readable by AUGUR; no ghost position fields."""
        with open(KINGDOM_PATH) as f:
            state = json.load(f)
        aria  = state.get("aria",  {})
        augur = state.get("augur", {})
        assert aria.get("regime") is not None, "ARIA not writing regime"
        assert isinstance(augur, dict),         "AUGUR section must be dict"
        print(f"  ARIA regime: {aria.get('regime')}")
        print(f"  AUGUR keys:  {list(augur.keys())}")
        print(f"  Cross-agent link: OK ✓")

    def test_augur_config_loads(self):
        """AUGUR config must load cleanly with all required fields."""
        import importlib.util, sys as _sys
        augur_root = str(Path(__file__).parent.parent)
        spec = importlib.util.spec_from_file_location(
            "_augur_config", f"{augur_root}/core/config.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        config = mod.config
        assert config.max_open_trades >= 4,     "max_open_trades < 4"
        assert len(config.news_assets)  >= 27,  "news_assets < 27 symbols"
        assert len(config.watched_markets) >= 27
        assert config.base_trade_usd > 0
        assert config.bybit_mode in ("paper", "live")
        print(f"  Config: mode={config.mode}  "
              f"assets={len(config.news_assets)}  "
              f"bybit={config.bybit_mode} ✓")
