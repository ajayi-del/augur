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

_BYBIT_WS_URL     = "wss://stream.bybit.com/v5/public/linear"
_BINANCE_WS_URL   = "wss://fstream.binance.com/ws/!forceOrder@arr"
_RECONNECT_DELAY  = 2.0       # seconds before reconnect
_WINDOW_MS        = 60_000    # 60s liquidation window
_HIST_UPDATE_S    = 300       # update historical stats every 5 min
_MIN_ZSCORE       = 0.1       # TEMPORARY: lowered for testing - was 1.0
_EXECUTE_THRESH   = 0.70      # cascade score for independent trade
_SMALL_THRESH     = 0.50      # cascade score for small intelligence trade
_INDEPENDENT_SIZE = 0.50      # 50% of base size when ARIA unconfirmed

# Binance to ARIA symbol mapping (BTCUSDT -> BTC-USD format)
_BINANCE_MAP: Dict[str, str] = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD", 
    "SOLUSDT": "SOL-USD",
    "DOGEUSDT": "DOGE-USD",
    "WIFUSDT": "WIF-USD",
    "BONKUSDT": "BONK-USD",
    "PEPEUSDT": "PEPE-USD",
    "INJUSDT": "INJ-USD",
    "SUIUSDT": "SUI-USD",
    "ARBUSDT": "ARB-USD",
    "OPUSDT": "OP-USD",
    "AVAXUSDT": "AVAX-USD",
    "BNBUSDT": "BNB-USD",
    "NEARUSDT": "NEAR-USD",
    "APTUSDT": "APT-USD",
    "SEIUSDT": "SEI-USD",
    "TIAUSDT": "TIA-USD",
    "ATOMUSDT": "ATOM-USD",
    "WLDUSDT": "WLD-USD",
    "JUPUSDT": "JUP-USD",
    "HBARUSDT": "HBAR-USD",
    "HYPEUSDT": "HYPE-USD",
    "ENAUSDT": "ENA-USD",
    "MNTUSDT": "MNT-USD",
    "TRUMPUSDT": "TRUMP-USD",
    "TRIAUSDT": "TRIA-USD",
}

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
    "PEPE-USD":     "1000PEPEUSDT",
    "WLD-USD":      "WLDUSDT",
    "JUP-USD":      "JUPUSDT",
    "HBAR-USD":     "HBARUSDT",
    "ATOM-USD":     "ATOMUSDT",
    "SEI-USD":      "SEIUSDT",
    "MNT-USD":      "MNTUSDT",
    "TRUMP-USD":    "TRUMPUSDT",
    "TRIA-USD":     "TRIAUSDT",
}

_REVERSE_MAP = {v: k for k, v in _SYMBOL_MAP.items()}

# ARIA universe bybit symbols for filtering
_ARIA_UNIVERSE_BYBIT: Dict[str, str] = _SYMBOL_MAP


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

        # 10-second velocity windows for early cascade detection
        self._velocity_windows: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100)
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
        Connect and stream forever with fixed backoff reconnect.
        Production-grade reconnection: [2, 3, 5, 10, 10] seconds.
        Never gives up. Never exponential backoff.
        """
        logger.info("bybit_cascade_engine_starting",
                    symbols=len(_SYMBOL_MAP),
                    latency_target_ms=50,
                    reconnect_delays=[2, 3, 5, 10, 10],
                    version="production_grade_v2")
        
        attempt = 0
        delays = [2, 3, 5, 10, 10]  # Fixed delays - never reconnect immediately
        
        while True:
            try:
                await self._stream()
                # Success - reset attempt counter
                if attempt > 0:
                    logger.info("bybit_cascade_recovered", 
                               attempt=attempt,
                               total_downtime=sum(delays[:attempt]))
                attempt = 0
                
            except asyncio.CancelledError:
                logger.info("bybit_cascade_cancelled")
                raise
            except Exception as e:
                attempt += 1
                delay = delays[min(attempt - 1, len(delays) - 1)]
                
                logger.warning("bybit_cascade_reconnect_attempt",
                           attempt=attempt,
                           delay=delay,
                           error=str(e))
                
                await asyncio.sleep(delay)

    async def _stream(self) -> None:
        """Try Bybit first, fallback to Binance if Bybit fails after 3 attempts."""
        bybit_attempts = 0
        max_bybit_attempts = 3
        
        while bybit_attempts < max_bybit_attempts:
            try:
                await self._bybit_stream()
                # Success - reset attempts and log recovery if we were on Binance
                if hasattr(self, '_on_binance') and self._on_binance:
                    logger.info("liq_feed_restored_bybit", attempts=bybit_attempts)
                    self._on_binance = False
                return
            except Exception as e:
                bybit_attempts += 1
                logger.warning("bybit_stream_failed_attempt", 
                           attempt=bybit_attempts,
                           max_attempts=max_bybit_attempts,
                           error=str(e))
                
                if bybit_attempts < max_bybit_attempts:
                    # Fixed backoff delay
                    delays = [0, 1, 2, 5]
                    delay = delays[min(bybit_attempts - 1, len(delays) - 1)]
                    await asyncio.sleep(delay)
        
        # All Bybit attempts failed - switch to Binance
        logger.error("liq_feed_fallback_binance", 
                   bybit_attempts=bybit_attempts,
                   reason="all_bybit_attempts_failed")
        self._on_binance = True
        
        try:
            await self._binance_stream()
        except Exception as e:
            logger.error("binance_fallback_failed", error=str(e))
            # Both failed - wait and retry Bybit
            await asyncio.sleep(10)
            logger.info("liq_feed_retrying_bybit_after_binance_failure")
            await self._stream()  # Recursive retry

    async def _bybit_stream(self) -> None:
        """Bybit per-symbol liquidation stream (liquidation.{symbol} topics)."""
        bybit_symbols = list(_SYMBOL_MAP.values())
        logger.info("bybit_cascade_connecting",
                    symbols_count=len(bybit_symbols),
                    all_symbols=bybit_symbols)

        async with websockets.connect(
            _BYBIT_WS_URL,
            ping_interval=10,
            ping_timeout=5,
            close_timeout=5,
        ) as ws:
            logger.info("bybit_cascade_connected", url=_BYBIT_WS_URL)

            # Subscribe per-symbol individually so one invalid symbol can't block others
            subscribed = 0
            for sym in bybit_symbols:
                topic = f"liquidation.{sym}"
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                try:
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    resp = json.loads(resp_raw)
                    if resp.get("success"):
                        subscribed += 1
                    else:
                        logger.debug("bybit_cascade_symbol_rejected",
                                     symbol=sym, reason=resp.get("ret_msg", ""))
                except asyncio.TimeoutError:
                    logger.debug("bybit_cascade_symbol_timeout", symbol=sym)

            if subscribed == 0:
                raise RuntimeError("bybit_cascade: no per-symbol subscriptions confirmed")

            logger.info("bybit_cascade_per_symbol_subscribed",
                        subscribed=subscribed, total=len(bybit_symbols))

            # Start message queue and processor
            message_queue = asyncio.Queue(maxsize=1000)

            receiver_task  = asyncio.create_task(self._message_receiver(ws, message_queue))
            processor_task = asyncio.create_task(self._message_processor(message_queue))
            keepalive_task = asyncio.create_task(self._keepalive(ws))

            try:
                await asyncio.gather(receiver_task, processor_task, keepalive_task)
            except Exception as e:
                logger.error("bybit_cascade_task_error", error=str(e))
            finally:
                for task in [receiver_task, processor_task, keepalive_task]:
                    if not task.done():
                        task.cancel()

    async def _binance_stream(self) -> None:
        """Binance liquidation stream as fallback - all symbols in one stream."""
        logger.info("binance_liquidation_stream_starting", url=_BINANCE_WS_URL)
        
        async with websockets.connect(_BINANCE_WS_URL) as ws:
            logger.info("binance_liquidation_connected", receiving_all_symbols=True)
            
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    
                    # Binance liquidation format: {"o": {"s": "BTCUSDT", "S": "BUY", ...}}
                    if "o" in data:
                        liq = data["o"]
                        symbol = liq.get("s", "")
                        
                        # Map to ARIA format
                        aria_symbol = _BINANCE_MAP.get(symbol)
                        if not aria_symbol:
                            continue  # Skip symbols not in our universe
                        
                        # Convert Binance format to our liquidation handler format
                        liquidation_msg = {
                            "topic": f"liquidation.{symbol}",
                            "data": {
                                "symbol": symbol,
                                "side": liq.get("S", ""),  # BUY/SELL -> our side format
                                "size": str(liq.get("q", "0")),  # quantity
                                "price": str(liq.get("p", "0")),  # price
                                "time": liq.get("T", 0)  # timestamp
                            }
                        }
                        
                        logger.info("binance_liquidation_received", 
                                   binance_symbol=symbol,
                                   aria_symbol=aria_symbol,
                                   side=liq.get("S"),
                                   size=liq.get("q"),
                                   price=liq.get("p"))
                        
                        await self._on_binance_liquidation(liquidation_msg)
                        
                except Exception as e:
                    logger.warning("binance_liquidation_msg_error", error=str(e))

    async def _on_binance_liquidation(self, msg: dict) -> None:
        """
        Process Binance liquidation event using same pipeline as Bybit.
        Converts Binance format to our standard processing.
        """
        # CRITICAL: Log every Binance liquidation event
        logger.info("binance_liquidation_raw", 
                   topic=msg.get("topic"),
                   symbol=msg.get("data", {}).get("symbol"))
        
        liq          = msg.get("data", {})
        binance_symbol = msg["topic"].split(".", 1)[1]
        aria_symbol   = _BINANCE_MAP.get(binance_symbol)

        if not aria_symbol or not liq:
            logger.debug("binance_liquidation_filtered", 
                        reason="missing_symbol_or_data",
                        aria_symbol=aria_symbol,
                        has_liq=bool(liq))
            return

        now_ms = int(time.time() * 1000)
        side   = liq.get("side", "")
        size   = float(liq.get("size", 0))
        price  = float(liq.get("price", 0))

        if size <= 0:
            logger.debug("binance_liquidation_filtered", 
                        reason="invalid_size",
                        size=size,
                        symbol=binance_symbol)
            return

        # Add to rolling window (same as Bybit processing)
        window = self._windows[aria_symbol]
        window.append({"ts_ms": now_ms, "side": side, "size_usd": size * price})

        # Prune events older than 60s
        cutoff = now_ms - _WINDOW_MS
        while window and window[0]["ts_ms"] < cutoff:
            window.popleft()

        await self._evaluate(aria_symbol, window, now_ms)

    async def _keepalive(self, ws) -> None:
        """Manual keepalive task - sends ping every 10 seconds."""
        logger.info("bybit_keepalive_started")
        while True:
            await asyncio.sleep(10)
            try:
                await ws.send(json.dumps({"op": "ping"}))
                logger.debug("bybit_ws_ping_sent")
            except Exception as e:
                logger.warning("bybit_keepalive_failed", error=str(e))
                break

    async def _message_receiver(self, ws, queue: asyncio.Queue) -> None:
        """Receiver task - only queues messages, no processing."""
        logger.info("bybit_message_receiver_started")
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    queue.put_nowait(msg)
                    logger.debug("bybit_message_queued", queue_size=queue.qsize())
                except asyncio.TimeoutError:
                    # Timeout is expected - continue loop
                    continue
                except Exception as e:
                    logger.error("bybit_receiver_error", error=str(e))
                    break
        except Exception as e:
            logger.error("bybit_receiver_fatal_error", error=str(e))

    async def _message_processor(self, queue: asyncio.Queue) -> None:
        """Processor task - only evaluates queued messages."""
        logger.info("bybit_message_processor_started")
        first_message = True
        
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                
                if first_message:
                    logger.info("bybit_first_message_received", 
                               timestamp=int(time.time()))
                    first_message = False
                
                data = json.loads(msg)
                topic = data.get("topic", "")
                
                # Handle per-symbol liquidation topics: liquidation.{SYMBOL}
                if topic.startswith("liquidation."):
                    await self._on_liquidation(data)
                    
            except asyncio.TimeoutError:
                logger.debug("bybit_processor_queue_timeout")
                continue
            except Exception as e:
                logger.error("bybit_processor_error", error=str(e))
                break

    # ── Liquidation event processing ──────────────────────────────────────────

    async def _on_liquidation(self, msg: dict) -> None:
        """
        Process one Bybit liquidation event.
        Window → z-score → phase → score → kingdom publish → maybe execute.
        Target: entire path < 50ms.
        """
        # CRITICAL: Log every liquidation event that reaches this handler
        logger.info("bybit_liquidation_raw", 
                   topic=msg.get("topic"),
                   symbol=msg.get("data", {}).get("symbol"))
        
        liq          = msg.get("data", {})
        bybit_symbol = msg["topic"].split(".", 1)[1]
        aria_symbol  = _REVERSE_MAP.get(bybit_symbol)

        if not aria_symbol or not liq:
            logger.debug("bybit_liquidation_filtered", 
                        reason="missing_symbol_or_data",
                        aria_symbol=aria_symbol,
                        has_liq=bool(liq))
            return

        now_ms = int(time.time() * 1000)
        side   = liq.get("side", "")
        size   = float(liq.get("size", 0))
        price  = float(liq.get("price", 0))

        if size <= 0:
            logger.debug("bybit_liquidation_filtered", 
                        reason="invalid_size",
                        size=size,
                        symbol=bybit_symbol)
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

        # === VELOCITY DETECTION (10-second window) ===
        # Use the latest event already stored in window (appended by _on_liquidation)
        velocity_window = self._velocity_windows[symbol]
        if window:
            last = window[-1]
            velocity_window.append({"ts_ms": now_ms, "side": last.get("side", ""),
                                    "size_usd": last.get("size_usd", 0.0)})
        
        # Prune velocity window to 10 seconds
        velocity_cutoff = now_ms - 10_000  # 10 seconds
        while velocity_window and velocity_window[0]["ts_ms"] < velocity_cutoff:
            velocity_window.popleft()
        
        # Calculate velocity z-score
        liq_10s = len(velocity_window)
        hist_mean_60s = self._hist_mean.get(symbol, 5.0)
        expected_10s = hist_mean_60s / 6.0  # Expected events in 10s
        
        if expected_10s > 0:
            velocity_zscore = liq_10s / expected_10s
        else:
            velocity_zscore = 0.0
        
        # Early cascade detection
        fire_cascade_early = False
        if velocity_zscore > 3.0:
            fire_cascade_early = True
            logger.info("bybit_velocity_cascade",
                       symbol=symbol,
                       velocity_zscore=round(velocity_zscore, 2),
                       liq_10s=liq_10s,
                       expected_10s=round(expected_10s, 1),
                       note="early_detection_30s_ahead")

        # Periodically update historical stats
        if time.time() - self._last_stat_update > _HIST_UPDATE_S:
            self._update_historical_stats(symbol, liq_60s)

        zscore = self._compute_zscore(symbol, liq_60s)
        phase  = self._detect_phase(symbol, zscore)
        score  = self._score_cascade(symbol, zscore, notional_60s, phase, direction)

        # Enhanced threshold: use lower threshold for early velocity detection
        effective_threshold = _MIN_ZSCORE
        if fire_cascade_early:
            effective_threshold = 0.05  # Very low threshold for velocity triggers

        if zscore < effective_threshold:
            logger.debug("bybit_liquidation_filtered", 
                        reason="below_zscore_threshold",
                        symbol=symbol,
                        zscore=round(zscore, 2),
                        threshold=effective_threshold,
                        velocity_zscore=round(velocity_zscore, 2))
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
            "velocity_zscore": round(velocity_zscore, 2) if 'velocity_zscore' in locals() else 0.0,
            "early_detection": fire_cascade_early if 'fire_cascade_early' in locals() else False,
        }
        self.kingdom.publish_augur_data(f"bybit_cascade.{symbol}", cascade_data)

        # ── Whisper to ARIA — tier-classified cascade intelligence ─────────────
        # ARIA reads this on every signal cycle and applies a coherence boost
        # if the symbol, direction, and tier match its current signal.
        _WHISPER_BOOST = {1: 1.5, 2: 0.8, 3: 0.3}
        tier = self._classify_tier(zscore, notional_60s, phase, direction)
        if tier is not None:
            whisper = {
                "symbol":       symbol,
                "direction":    direction,   # "bullish" | "bearish"
                "zscore":       round(zscore, 2),
                "notional_usd": round(notional_60s, 0),
                "phase":        phase,
                "tier":         tier,
                "boost":        _WHISPER_BOOST[tier],
                "confidence":   round(score, 3),
                "expires_ms":   now_ms + 90_000,   # 90s — propagation window
                "source":       "bybit_cascade_lead",
                "timestamp_ms": now_ms,
            }
            self.kingdom.publish_augur_data(f"whisper.{symbol}", whisper)
            logger.info(
                "augur_whisper_published",
                symbol    = symbol,
                tier      = tier,
                boost     = _WHISPER_BOOST[tier],
                direction = direction,
                zscore    = round(zscore, 2),
                notional  = round(notional_60s, 0),
                expires_s = 90,
            )

        logger.info(
            "bybit_cascade_evaluated",
            symbol=symbol,
            zscore=round(zscore, 2),
            direction=direction,
            phase=phase,
            score=round(score, 3),
            liq_60s=liq_60s,
            notional_usd=round(notional_60s, 0),
            velocity_zscore=round(velocity_zscore, 2) if 'velocity_zscore' in locals() else 0.0,
            early_detection=fire_cascade_early if 'fire_cascade_early' in locals() else False,
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

    @staticmethod
    def _classify_tier(zscore: float, notional: float, phase: str, direction: str) -> Optional[int]:
        """
        Tier 1 — Act immediately: zscore>3.5, notional>$500k, expansion phase, clear direction.
        Tier 2 — Act with confirmation: zscore≥2.5, notional>$200k, clear direction.
        Tier 3 — Monitor: zscore≥1.5, any notional, clear direction.
        None   — Ignore: mixed direction, exhaustion phase, or below noise floor.

        Exhaustion is AUGUR's domain (reversal trade) — ARIA should not follow.
        """
        if direction == "mixed" or phase == "exhaustion":
            return None
        if zscore > 3.5 and notional > 500_000 and phase == "expansion":
            return 1
        if zscore >= 2.5 and notional > 200_000:
            return 2
        if zscore >= 1.5:
            return 3
        return None

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
