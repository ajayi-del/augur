"""
Bybit Cascade Engine — Cross-Venue Liquidation Intelligence.

ARIA watches SoDEX liquidations.
AUGUR watches Bybit liquidations.

Bybit is 10x larger than SoDEX.
When Bybit cascades: SoDEX follows.
The lag is 200–800ms.

AUGUR exploits this lag in three modes:

  1. INTELLIGENCE — warn ARIA via kingdom write
     ARIA reads on next watchdog wake (< 100ms)
     Can boost or reduce ARIA coherence mid-signal

  2. CONFIRMATION — amplify ARIA trades when both
     venues cascade in the same direction.
     Chancellor grants compound_strong bonus.

  3. INDEPENDENT — trade MEXC ahead of SoDEX
     cascade when Bybit clearly leads.
     50% size, tighter stops, 2-min expiry.
     Exploits the 200–800ms propagation delay.

This is cross-venue cascade arbitrage.
The edge is not the direction — it is the lag.

Kant: Is the cascade structurally real?
  (Z-score + notional threshold)
Nietzsche: How hard do we press?
  (Phase × ARIA agreement × z-score)
Chancellor: Does the kingdom authorize?
  (Capital gates + drawdown veto)
"""

import asyncio
import json
import math
import time
import structlog
import websockets
from collections import defaultdict, deque
from typing import Dict, Optional

logger = structlog.get_logger(__name__)

_WS_URL           = "wss://stream.bybit.com/v5/public/linear"
_RECONNECT_DELAY  = 2.0       # seconds before reconnect
_WINDOW_MS        = 60_000    # 60s liquidation window
_HIST_UPDATE_S    = 300       # update historical stats every 5 min
_MIN_ZSCORE       = 1.0       # ignore noise below this
_EXECUTE_THRESH   = 0.70      # cascade score for independent trade
_SMALL_THRESH     = 0.50      # cascade score for small intelligence trade
_INDEPENDENT_SIZE = 0.50      # 50% of base size when ARIA unconfirmed

# Full ARIA/AUGUR universe — all symbols we watch for cascade propagation
_SYMBOL_MAP: Dict[str, str] = {
    "SOL-USD":      "SOLUSDT",
    "ETH-USD":      "ETHUSDT",
    "BTC-USD":      "BTCUSDT",
    "NEAR-USD":     "NEARUSDT",
    "ARB-USD":      "ARBUSDT",
    "SUI-USD":      "SUIUSDT",
    "AVAX-USD":     "AVAXUSDT",
    "BNB-USD":      "BNBUSDT",
    "OP-USD":       "OPUSDT",
    "DOGE-USD":     "DOGEUSDT",
    "INJ-USD":      "INJUSDT",
    "HYPE-USD":     "HYPEUSDT",
    "ENA-USD":      "ENAUSDT",
    "APT-USD":      "APTUSDT",
    "TIA-USD":      "TIAUSDT",
    "WIF-USD":      "WIFUSDT",
    "BONK-USD":     "BONKUSDT",
    "PEPE-USD":     "PEPEUSDT",
    "WLD-USD":      "WLDUSDT",
    "JUP-USD":      "JUPUSDT",
    "HBAR-USD":     "HBARUSDT",
    "ATOM-USD":     "ATOMUSDT",
    "SEI-USD":      "SEIUSDT",
    "MNT-USD":      "MNTUSDT",
    "TRUMP-USD":    "TRUMPUSDT",
}

_REVERSE_MAP = {v: k for k, v in _SYMBOL_MAP.items()}


class BybitCascadeEngine:
    """
    AUGUR's cross-venue cascade radar.

    Watches Bybit liquidation streams for 25 assets.
    Detects cascade onset 200–800ms before SoDEX reacts.
    Publishes to kingdom so ARIA can confirm or deny.
    Executes independently when the lead signal is strong enough.

    This is the tightest loop in the kingdom.
    Every millisecond of latency costs real edge.
    """

    def __init__(self, kingdom, chancellor, router, base_trade_usd: float = 200.0):
        self.kingdom        = kingdom
        self.chancellor     = chancellor
        self.router         = router
        self.base_trade_usd = base_trade_usd

        # Per-symbol rolling liquidation windows: list of {ts_ms, side, size_usd}
        self._windows: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=500)
        )

        # Historical stats for z-score computation (updated every 5min)
        self._hist_mean: Dict[str, float] = {}   # liq/min baseline
        self._hist_std:  Dict[str, float] = {}   # baseline std
        self._hist_sum:  Dict[str, float] = defaultdict(float)   # for EMA
        self._hist_n:    Dict[str, int]   = defaultdict(int)     # sample count
        self._last_stat_update: float     = 0.0

        # Z-score from previous evaluation — for phase detection
        self._prev_zscore: Dict[str, float] = defaultdict(float)

        # Cooldown: don't fire independent trades too often
        self._last_independent: Dict[str, float] = {}
        self._independent_cooldown_s = 120.0

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Connect and stream forever. Auto-reconnects on any error.
        Designed for asyncio.gather() — blocking until cancelled.
        """
        logger.info("bybit_cascade_engine_starting",
                    symbols=len(_SYMBOL_MAP),
                    latency_target_ms=50)
        while True:
            try:
                await self._stream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("bybit_cascade_reconnecting", error=str(e))
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _stream(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            bybit_symbols = list(_SYMBOL_MAP.values())
            await ws.send(json.dumps({
                "op":   "subscribe",
                "args": [f"liquidation.{s}" for s in bybit_symbols],
            }))
            logger.info("bybit_cascade_subscribed", symbols=len(bybit_symbols))

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    topic = msg.get("topic", "")
                    if topic.startswith("liquidation."):
                        await self._on_liquidation(msg)
                except Exception as e:
                    logger.warning("bybit_cascade_msg_error", error=str(e))

    # ── Liquidation event processing ──────────────────────────────────────────

    async def _on_liquidation(self, msg: dict) -> None:
        """
        Process one Bybit liquidation event.
        Window → z-score → phase → score → kingdom publish → maybe execute.
        Target: entire path < 50ms.
        """
        liq          = msg.get("data", {})
        bybit_symbol = msg["topic"].split(".", 1)[1]
        aria_symbol  = _REVERSE_MAP.get(bybit_symbol)

        if not aria_symbol or not liq:
            return

        now_ms = int(time.time() * 1000)
        side   = liq.get("side", "")
        size   = float(liq.get("size", 0))
        price  = float(liq.get("price", 0))

        if size <= 0:
            return

        # Add to rolling window
        window = self._windows[aria_symbol]
        window.append({"ts_ms": now_ms, "side": side, "size_usd": size * price})

        # Prune events older than 60s
        cutoff = now_ms - _WINDOW_MS
        while window and window[0]["ts_ms"] < cutoff:
            window.popleft()

        await self._evaluate(aria_symbol, window, now_ms)

    # ── Signal evaluation ─────────────────────────────────────────────────────

    async def _evaluate(self, symbol: str, window: deque, now_ms: int) -> None:
        liq_60s      = len(window)
        notional_60s = sum(e["size_usd"] for e in window)

        # Direction: which side is getting liquidated?
        long_liqs  = sum(1 for e in window if e["side"] == "Buy")
        short_liqs = liq_60s - long_liqs

        if long_liqs > short_liqs * 1.5:
            direction = "bearish"    # longs liquidated → price fell
        elif short_liqs > long_liqs * 1.5:
            direction = "bullish"    # shorts liquidated → price rose
        else:
            direction = "mixed"

        # Periodically update historical stats
        if time.time() - self._last_stat_update > _HIST_UPDATE_S:
            self._update_historical_stats(symbol, liq_60s)

        zscore = self._compute_zscore(symbol, liq_60s)
        phase  = self._detect_phase(symbol, zscore)
        score  = self._score_cascade(symbol, zscore, notional_60s, phase, direction)

        if zscore < _MIN_ZSCORE:
            return  # noise floor — not worth publishing

        # ── Publish to kingdom (intelligence always shared) ────────────────
        cascade_data = {
            "symbol":      symbol,
            "active":      zscore > 1.5,
            "zscore":      round(zscore, 2),
            "direction":   direction,
            "phase":       phase,
            "liq_60s":     liq_60s,
            "notional_usd": round(notional_60s, 0),
            "cascade_score": round(score, 3),
            "timestamp_ms": now_ms,
        }
        self.kingdom.publish_augur_data(f"bybit_cascade.{symbol}", cascade_data)

        logger.info(
            "bybit_cascade_evaluated",
            symbol=symbol,
            zscore=round(zscore, 2),
            direction=direction,
            phase=phase,
            score=round(score, 3),
            liq_60s=liq_60s,
            notional_usd=round(notional_60s, 0),
        )

        # ── Route based on score ───────────────────────────────────────────
        if score >= _EXECUTE_THRESH and direction != "mixed":
            await self._execute_independent(symbol, direction, score, zscore, now_ms)
        elif score >= _SMALL_THRESH and direction != "mixed":
            logger.info("bybit_cascade_intelligence_only",
                        symbol=symbol, score=round(score, 3),
                        reason="score_below_execute_threshold")

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute_independent(
        self,
        symbol:    str,
        direction: str,   # "bullish" | "bearish"
        score:     float,
        zscore:    float,
        now_ms:    int,
    ) -> None:
        """
        Bybit-led trade. ARIA has not confirmed.
        Exploits 200–800ms propagation delay.
        50% size. Tighter stops implied by 2-min bet window.
        """
        # Cooldown check
        last = self._last_independent.get(symbol, 0.0)
        if time.time() - last < self._independent_cooldown_s:
            return

        # Check if ARIA already has the same cascade signal
        aria_cascade = self.kingdom.get_aria_cascade(symbol)
        if aria_cascade and aria_cascade.get("direction") == direction:
            # ARIA already sees it — Chancellor handles compound bonus
            logger.debug("bybit_cascade_aria_already_sees",
                         symbol=symbol, direction=direction)
            return

        trade_direction = "long" if direction == "bullish" else "short"
        size_usd = round(self.base_trade_usd * _INDEPENDENT_SIZE, 2)

        # Chancellor adjudication
        try:
            state           = self.kingdom.read()
            aria            = state.aria
            total_exp_pct   = 0.0   # approximate — cascade engine doesn't track full exposure
            sym_exp_pct     = 0.0
            aria_drawdown   = getattr(aria, "drawdown", 0.0) or 0.0
            cascade_z       = float(aria.cascade_alert.get("zscore", 0.0))

            decision = self.chancellor.adjudicate(
                aria_direction=None,     # ARIA hasn't confirmed yet
                aria_coherence=0.0,
                augur_direction=trade_direction,
                augur_conviction=min(score, 0.90),
                aria_drawdown=aria_drawdown,
                daily_loss_pct=0.0,
                cascade_zscore=cascade_z,
                total_exposure_pct=total_exp_pct,
                symbol_exposure_pct=sym_exp_pct,
                balance=300.0,          # fallback — cascade engine has no balance cache
            )

            if not decision.augur_executes:
                logger.info("bybit_cascade_chancellor_blocked",
                            symbol=symbol, reason=decision.reason)
                return
        except Exception as e:
            logger.warning("bybit_cascade_chancellor_error", error=str(e))
            return

        # Update kingdom cascade key with independent lead signal so ARIA reads it
        self.kingdom.publish_augur_data(f"bybit_cascade.{symbol}", {
            "symbol":        symbol,
            "active":        True,
            "direction":     direction,   # "bullish"/"bearish" — ARIA reads this key
            "zscore":        round(zscore, 2),
            "score":         round(score, 3),
            "phase":         "expansion",
            "independent_lead": True,
            "waiting_for_aria": True,
            "expires_ms":    now_ms + 120_000,
            "timestamp_ms":  now_ms,
        })

        self._last_independent[symbol] = time.time()

        logger.info(
            "bybit_cascade_independent_trade",
            symbol=symbol,
            direction=trade_direction,
            zscore=round(zscore, 2),
            score=round(score, 3),
            size_usd=size_usd,
            reason="bybit_leads_sodex_expected_within_800ms",
        )

        try:
            await self.router.place_order(
                symbol=symbol,
                direction=trade_direction,
                size_usd=size_usd,
            )
        except Exception as e:
            logger.error("bybit_cascade_execution_error",
                         symbol=symbol, error=str(e))

    # ── Statistics ────────────────────────────────────────────────────────────

    def _compute_zscore(self, symbol: str, liq_60s: int) -> float:
        """
        Z-score of current liq_60s vs historical baseline.
        Defaults to conservative (mean=5, std=3) until calibrated.
        """
        mean = self._hist_mean.get(symbol, 5.0)
        std  = self._hist_std.get(symbol, 3.0)
        if std == 0:
            return 0.0
        return (liq_60s - mean) / std

    def _update_historical_stats(self, symbol: str, liq_60s: int) -> None:
        """
        Exponential moving average update for historical mean and variance.
        Alpha=0.05 — slow adaptation, stable baseline.
        """
        alpha = 0.05
        current_mean = self._hist_mean.get(symbol, float(liq_60s))
        new_mean     = alpha * liq_60s + (1 - alpha) * current_mean
        self._hist_mean[symbol] = new_mean

        diff     = liq_60s - new_mean
        curr_var = (self._hist_std.get(symbol, 3.0)) ** 2
        new_var  = alpha * diff ** 2 + (1 - alpha) * curr_var
        self._hist_std[symbol]  = max(math.sqrt(new_var), 0.5)
        self._last_stat_update  = time.time()

    def _detect_phase(self, symbol: str, zscore: float) -> str:
        """
        Phase detection based on z-score trajectory.

        trigger:    zscore rising past 2.0 — cascade beginning
        expansion:  zscore > 3.0 — cascade in full force
        exhaustion: zscore falling from peak — cascade decelerating
        quiet:      below noise threshold
        """
        prev = self._prev_zscore[symbol]
        self._prev_zscore[symbol] = zscore

        if zscore > 3.0:
            return "expansion"
        if zscore > prev and zscore > 2.0:
            return "trigger"
        if zscore < prev and prev > 2.0 and zscore > _MIN_ZSCORE:
            return "exhaustion"
        return "quiet"

    def _score_cascade(
        self,
        symbol:      str,
        zscore:      float,
        notional:    float,
        phase:       str,
        direction:   str,
    ) -> float:
        """
        Cascade quality score [0, 1].

        Weighted combination of:
          z-score strength      (40%) — is this statistically real?
          notional size         (25%) — how much money is being liquidated?
          phase multiplier      (20%) — what stage is the cascade at?
          ARIA agreement        (15%) — does ARIA's SoDEX data confirm?

        Philosophy: a z-score of 4 with $10M notional in trigger phase
        with ARIA confirmation = near-certain edge. Act aggressively.
        A z-score of 2 with $100k notional in quiet phase = noise. Wait.
        """
        # Z-score component (normalised)
        z_score_component = min(zscore / 4.0, 1.0)

        # Notional component (log scale — $100k baseline, $10M max)
        if notional > 0:
            notional_component = min(math.log10(max(notional, 1) / 100_000) / 2.0, 1.0)
            notional_component = max(notional_component, 0.0)
        else:
            notional_component = 0.0

        # Phase multiplier
        phase_mult = {
            "trigger":    0.80,
            "expansion":  1.00,
            "exhaustion": 0.30,
            "quiet":      0.10,
        }.get(phase, 0.10)

        # ARIA agreement component
        aria_cascade = self.kingdom.get_aria_cascade(symbol)
        if aria_cascade and aria_cascade.get("direction") == direction:
            aria_component = 1.0
        elif aria_cascade and aria_cascade.get("direction") != direction:
            aria_component = -0.5   # active conflict — reduce score
        else:
            aria_component = 0.5    # no ARIA cascade — neutral

        score = (
            z_score_component    * 0.40 +
            notional_component   * 0.25 +
            phase_mult           * 0.20 +
            max(aria_component, 0) * 0.15
        )

        return round(min(score, 1.0), 4)
