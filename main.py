"""
AUGUR — Solana-Native Sovereign Agent.

Philosophy:
  ARIA  — empiricist. Trades what IS. Microstructure. Cascade. Flow.
  AUGUR — rationalist. Trades what WILL BE. Probability. Signal. Prediction.

Both governed by the same constitution:
  Kant:       Is the trade structurally sound?
  Nietzsche:  Is the will strong enough to act?
  Chancellor: Does the kingdom authorise it?

Signal pipeline (every 30s):
  Bybit mark price + OB + funding + Solana TPS
  → AugurSignal
  → AugurPersonality   (WHO trades this?)
  → AugurKant          (IS the trade sound?)
  → AugurNietzsche     (HOW STRONGLY to trade?)
  → Chancellor         (DOES the kingdom authorise?)
  → RoutingClient      (Bybit primary, MEXC fallback)
  → CrossLearningEngine (outcome feeds back into hist_wr + alignment)

Kingdom sync:
  Watchdog filesystem event (< 100ms) → immediate read of ARIA state
  Fallback: 120s polling when watchdog fires nothing
"""

import asyncio
import json
import os
import time
import structlog
from pathlib import Path
from typing import Dict, Optional

from core.config import config as settings
from data.valuechain_bridge import ValueChainBridge
from data.solana_bridge import SolanaBridge
from data.bybit_feed import BybitFeed
from data.bybit_cascade import BybitCascadeEngine
from data.solana_liq_feed import LiquidationFeedManager
from kingdom.state_sync import KingdomStateSync, AugurState, AgentBet
from kingdom.chancellor import Chancellor
from intelligence.prediction_market import CrossAgentBetEngine
from intelligence.prediction_market import AgentBet as PredictionBet
from intelligence.augur_personalities import (
    AugurPersonality, AugurSignal, assign_personality,
)
from intelligence.augur_kant import AugurKant
from intelligence.augur_nietzsche import AugurNietzsche, WillState
from memory.trade_journal import TradeJournal
from memory.augur_hist_wr import augur_hist_wr
from memory.outcome_resolver import OutcomeResolver
from memory.cross_agent_feedback import CrossAgentFeedback
from memory.cross_learning import CrossLearningEngine
from execution.bybit_client import BybitClient
from execution.routing_client import RoutingClient
from intelligence.strategy_runner import StrategyRunner
from intelligence.deep_intelligence import DeepIntelligenceAgent

logger = structlog.get_logger()

_AUGUR_JOURNAL = Path(settings.augur_log_path) / "augur_journal.jsonl"

# Execution gate thresholds
# Dual-agent agreement (confidence > 0.7, not single-agent) is the standard.
# Nietzsche ABSTAIN overrides regardless of confidence gate.
_CONFIDENCE_GATE       = 0.70
_CONFIDENCE_LOG_FLOOR  = 0.60
_SINGLE_AGENT_TYPES    = ("single_aria", "single_augur", "silence", "disagreement")
_EXECUTION_COOLDOWN_MS = 30 * 60 * 1000   # 30-minute per-symbol cooldown

# Native signal loop weights
_TPS_WEIGHT     = 0.20
_PRICE_WEIGHT   = 0.45
_OB_WEIGHT      = 0.20
_FUNDING_WEIGHT = 0.15
_MIN_BET_CONFIDENCE = 0.15


def _journal_append(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning("journal_write_error", path=str(path), error=str(e))


class AugurApplication:
    """
    AUGUR is the rationalist twin.
    It does not react to what IS. It anticipates what WILL BE.
    Every decision flows through Kant, Nietzsche, and the Chancellor.
    No exceptions. No shortcuts. No overrides.
    """

    def __init__(self, cfg):
        self.config      = cfg
        self._start_time = time.time()
        self._is_live    = (
            cfg.mode == "live" and
            cfg.live_mode_confirmed and
            bool(cfg.bybit_api_key)
        )

        # Kingdom / Bridge
        self.kingdom = KingdomStateSync(cfg.kingdom_state_path)
        self.bridge  = ValueChainBridge(self.kingdom)

        # Solana on-chain signals
        self.solana = SolanaBridge()

        # Bybit real-time market data (mark prices, OB imbalance, liquidations)
        # news_assets are bare symbols ("SOL") — feed expects "SOL-USD" format
        self.bybit_feed = BybitFeed(symbols=[f"{s}-USD" for s in cfg.news_assets])

        # Execution layer — Bybit only
        self.bybit = BybitClient(
            mode="live" if self._is_live else "paper",
            api_key=cfg.bybit_api_key,
            api_secret=cfg.bybit_api_secret,
        )
        self.router = RoutingClient(
            bybit=self.bybit,
            mode="live" if self._is_live else "paper",
        )

        # Cross-agent bet engine (prediction market consensus)
        self.bet_engine = CrossAgentBetEngine()

        # Philosophy layer
        self.kant       = AugurKant()
        self.nietzsche  = AugurNietzsche()
        self.chancellor = Chancellor()

        # Journal & learning loop
        self.journal          = TradeJournal()
        self.journal.load()
        self.outcome_resolver = OutcomeResolver(bybit_client=self.bybit)
        self.cross_feedback   = CrossAgentFeedback(kingdom=self.kingdom)
        self.cross_learning   = CrossLearningEngine(kingdom=self.kingdom)

        # Cached state — refreshed by their respective loops
        self._cascade_alert:     dict        = {"active": False, "zscore": 0.0, "phase": "none"}
        self._regime:            str         = "unknown"
        self._funding_rates:     dict        = {}
        self._solana_snapshot:   dict        = {}
        self._cached_balance:    float       = 300.0  # sentinel until first fetch
        self._daily_loss_pct:    float       = 0.0   # updated from kingdom drawdown

        # Cross-venue cascade engine — Bybit leads SoDEX by 200–800ms
        self.bybit_cascade = BybitCascadeEngine(
            kingdom=self.kingdom,
            chancellor=self.chancellor,
            router=self.router,
            base_trade_usd=cfg.base_trade_usd,
        )

        # Solana liquidation sources - Drift + Pyth velocity
        self.liquidation_manager = LiquidationFeedManager(
            kingdom=self.kingdom,
            bybit_cascade_engine=self.bybit_cascade,
        )

        # Deep intelligence — DeepSeek-powered smart money analysis (6h cycle)
        self.intel_agent = DeepIntelligenceAgent(
            log_path = cfg.augur_log_path,
            kingdom  = self.kingdom,
            bridge   = self.bridge,
        )

        # Strategy runner — PERP_CASCADE + PERP_MOMENTUM, 30s cycle
        self.strategy_runner = StrategyRunner(
            bybit_feed      = self.bybit_feed,
            kingdom         = self.kingdom,
            chancellor      = self.chancellor,
            router          = self.router,
            get_balance     = lambda: self._cached_balance,
            get_daily_loss  = lambda: self._daily_loss_pct,
            intel_agent     = self.intel_agent,
        )

        # Execution dedup — {symbol: last_executed_ms}
        self._executed_signals: Dict[str, int] = {}

        # Watchdog kingdom event — set by filesystem watcher, cleared after processing
        self._kingdom_event  = asyncio.Event()
        self._kingdom_observer = None

    # ── Startup banner ────────────────────────────────────────────────────────

    def _print_startup_banner(self) -> None:
        _C = "\033[1;36m"
        _G = "\033[0;32m"
        _Y = "\033[1;33m"
        _R = "\033[0m"
        mode_label = "LIVE" if self._is_live else "PAPER"
        mode_color = _Y if self._is_live else _G

        print(f"\n{_C}{'='*60}{_R}")
        print(f"{_C}   AUGUR — Rationalist Sovereign. Will to Power.{_R}")
        print(f"{_C}{'='*60}{_R}")
        print(f"{mode_color}  MODE: {mode_label}{_R}")
        print(f"{_G}  Philosophy: Kant → Nietzsche → Chancellor → Execute{_R}")
        print(f"{_G}  Personalities: Oracle Scout Arb Momentum Sentinel Hedger{_R}")
        print(f"{_G}  Kingdom sync: watchdog < 100ms (fallback 120s){_R}")

        if self._is_live:
            print(f"{_Y}  Venue: Bybit V5 linear perps (5× leverage){_R}")
            print(f"{_Y}  Fallback: MEXC futures (when IP whitelisted){_R}")
            print(f"{_Y}  Max open trades: {self.config.max_open_trades}{_R}")
            print(f"{_Y}  Confidence gate: >{_CONFIDENCE_GATE:.0%} dual-agent{_R}")
        else:
            print(f"{_G}  Execution: paper simulation (no real orders){_R}")

        try:
            aria = self.kingdom.read_aria_state()
            print(f"{_G}  Kingdom: connected  ({len(aria.active_bets)} ARIA bets){_R}")
        except Exception:
            print(f"  Kingdom: waiting for ARIA state")

        Path(settings.augur_log_path).mkdir(parents=True, exist_ok=True)
        print(f"{_G}  Journal: {settings.augur_log_path}{_R}")
        print(f"{_C}{'='*60}{_R}\n")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_signal_on_cooldown(self, symbol: str, bet_ts_ms: int) -> bool:
        return (bet_ts_ms - self._executed_signals.get(symbol, 0)) < _EXECUTION_COOLDOWN_MS

    def _mark_executed(self, symbol: str, bet_ts_ms: int) -> None:
        self._executed_signals[symbol] = bet_ts_ms

    def _get_exposure_pcts(self, symbol: str) -> tuple:
        """
        Compute kingdom-wide and per-symbol exposure as fractions of balance.
        Used by Kant's capital_sound check.
        """
        try:
            state    = self.kingdom.read()
            registry = state.position_registry
            now_ms   = int(time.time() * 1000)
            balance  = max(self._cached_balance, 1.0)

            all_pos = [
                p for positions in registry.values()
                for p in positions
                if now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000
            ]
            total_usd  = sum(p.get("size_usd", 0) for p in all_pos)
            symbol_usd = sum(p.get("size_usd", 0) for p in all_pos
                             if p.get("symbol") == symbol)
            return (total_usd / balance, symbol_usd / balance)
        except Exception:
            return (0.0, 0.0)

    def _has_augur_position(self, symbol: str) -> bool:
        try:
            state = self.kingdom.read()
            now_ms = int(time.time() * 1000)
            for p in state.position_registry.get("augur", []):
                if (p.get("symbol") == symbol and
                        now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000):
                    return True
        except Exception:
            pass
        return False

    def _compute_size(self, coherence: float, resolution_score: float) -> float:
        base      = self.config.base_trade_usd
        coh_mult  = min((coherence - 4.0) / 6.0, 1.0)
        sc_mult   = min(0.5 + (resolution_score / 10.0), 1.2)
        size      = base * coh_mult * sc_mult
        return round(min(max(size, self.config.min_trade_usd), self.config.max_trade_usd), 2)

    def _get_aria_max_position_usd(self, state) -> float:
        """Largest single ARIA position in the registry."""
        try:
            now_ms = int(time.time() * 1000)
            return max(
                (p.get("size_usd", 0)
                 for p in state.position_registry.get("aria", [])
                 if now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000),
                default=0.0
            )
        except Exception:
            return 0.0

    # ── Phase 1: ValueChain + Solana on-chain refresh ─────────────────────────

    async def valuechain_loop(self) -> None:
        """Every 600s — refresh ARIA kingdom state + Solana on-chain signals."""
        logger.info("valuechain_loop_started", interval_s=600)
        while True:
            try:
                self._cascade_alert   = self.bridge.get_cascade_signal()
                self._regime          = self.bridge.get_regime()
                self._funding_rates   = await self.bridge.get_funding_rates()
                self._solana_snapshot = await self.solana.get_full_snapshot()

                _journal_append(_AUGUR_JOURNAL, {
                    "event":          "valuechain_refresh",
                    "cascade_active": self._cascade_alert.get("active"),
                    "cascade_zscore": self._cascade_alert.get("zscore"),
                    "regime":         self._regime,
                    "funding_n":      len(self._funding_rates),
                    "solana_tps":     round(self._solana_snapshot.get("tps", 0), 0),
                    "tps_mult":       self._solana_snapshot.get("tps_multiplier"),
                    "timestamp_ms":   int(time.time() * 1000),
                })
                logger.info(
                    "valuechain_refreshed",
                    cascade_active=self._cascade_alert.get("active"),
                    cascade_zscore=round(self._cascade_alert.get("zscore", 0.0), 2),
                    regime=self._regime,
                    solana_tps=round(self._solana_snapshot.get("tps", 0), 0),
                )
            except Exception as e:
                logger.error("valuechain_loop_error", error=str(e))
            await asyncio.sleep(600)

    # ── Phase 2: AUGUR signal loop — Kant → Nietzsche → execute ──────────────

    async def augur_signal_loop(self) -> None:
        """
        Every 30s — independent signal evaluation with full philosophy pipeline.

        Signal stack:
          TPS (0.20) + mark price momentum (0.45) + OB imbalance (0.20) + funding (0.15)

        Pipeline:
          AugurSignal → assign_personality → AugurKant → AugurNietzsche → execute
        """
        logger.info("augur_signal_loop_started", interval_s=30)

        while True:
            try:
                tps    = await self.solana.get_network_tps()
                now_ms = int(time.time() * 1000)

                cascade_zscore = float(self._cascade_alert.get("zscore", 0.0))
                aria_state     = self.kingdom.read_aria_state()
                aria_regime    = self._regime or "unknown"
                aria_drawdown  = getattr(aria_state, "drawdown", 0.0) or 0.0
                aria_max_pos   = self._get_aria_max_position_usd(self.kingdom.read())
                open_count     = self.kingdom.count_open_positions("augur")

                for symbol in self.config.news_assets:
                    aria_sym = f"{symbol}-USD"
                    try:
                        # ── Raw signal computation ─────────────────────────────
                        if tps >= 3000:         tps_signal = 0.58
                        elif tps >= 1500:       tps_signal = 0.53
                        elif 0 < tps < 800:     tps_signal = 0.42
                        else:                   tps_signal = 0.50

                        pct_30s = self.bybit_feed.get_price_momentum(aria_sym, 30.0)
                        if pct_30s > 0.5:        price_signal = 0.72
                        elif pct_30s > 0.2:      price_signal = 0.62
                        elif pct_30s > 0.0:      price_signal = 0.53
                        elif pct_30s < -0.5:     price_signal = 0.28
                        elif pct_30s < -0.2:     price_signal = 0.38
                        elif pct_30s < 0.0:      price_signal = 0.47
                        else:                    price_signal = 0.50

                        agg = self.bybit_feed.get_agg_ratio(aria_sym)
                        if agg > 0.65:           ob_signal = 0.68
                        elif agg > 0.55:         ob_signal = 0.58
                        elif agg < 0.35:         ob_signal = 0.32
                        elif agg < 0.45:         ob_signal = 0.42
                        else:                    ob_signal = 0.50

                        fund_rate = (
                            self.bybit_feed.get_funding_rate(aria_sym)
                            or self._funding_rates.get(symbol, 0.0)
                        )
                        if fund_rate > 0.02:     fund_signal = 0.38
                        elif fund_rate > 0.005:  fund_signal = 0.45
                        elif fund_rate < -0.02:  fund_signal = 0.62
                        elif fund_rate < -0.005: fund_signal = 0.55
                        else:                    fund_signal = 0.50

                        combined = (
                            _TPS_WEIGHT     * tps_signal  +
                            _PRICE_WEIGHT   * price_signal +
                            _OB_WEIGHT      * ob_signal    +
                            _FUNDING_WEIGHT * fund_signal
                        )

                        deviation = abs(combined - 0.50)
                        if deviation < 0.04:
                            continue

                        direction = "long" if combined > 0.50 else "short"
                        raw_conf  = min(deviation * 2.5, 0.85)

                        if raw_conf < _MIN_BET_CONFIDENCE:
                            continue

                        # Calibrate with historical win rate
                        wr_mult    = augur_hist_wr.confidence_multiplier(symbol, direction)
                        confidence = round(min(raw_conf * wr_mult, 0.90), 3)

                        # Alignment from cross-feedback. When no history exists
                        # (neutral ≤ 0.52), proxy from ARIA's coherence — a strong
                        # ARIA signal is implicit agreement until AUGUR earns its own record.
                        alignment = self.cross_feedback.get_alignment(aria_sym)
                        if alignment <= 0.52:
                            # Patch 4 — AUGUR reads ARIA execution whisper for alignment.
                            # ARIA writes aria_whisper to kingdom after confirmed fill.
                            # If a fresh, direction-matching whisper exists, derive alignment
                            # from ARIA's confirmed coherence instead of the active_bets proxy.
                            _aria_whisper = self.kingdom.get_aria_whisper() or {}
                            _wh_sym = _aria_whisper.get("symbol", "")
                            _wh_dir = _aria_whisper.get("direction", "")
                            _wh_exp = _aria_whisper.get("expires_ms", 0)
                            _wh_coh = float(_aria_whisper.get("coherence", 0.0))
                            _now_ms = int(time.time() * 1000)
                            _dir_match = (
                                (direction == "long"  and _wh_dir == "long") or
                                (direction == "short" and _wh_dir == "short")
                            )
                            if (_wh_sym == aria_sym and _dir_match
                                    and _wh_exp > _now_ms and _wh_coh > 0.0):
                                alignment = round(min(0.50 + (_wh_coh / 20.0), 0.85), 3)
                                logger.info("augur_aria_whisper_used",
                                            symbol=aria_sym, aria_coherence=_wh_coh,
                                            alignment=alignment,
                                            personality=_aria_whisper.get("personality", "?"))
                            else:
                                # Fallback: proxy from ARIA's active bet coherence
                                aria_bet_dict = next(
                                    (b for b in aria_state.active_bets
                                     if b.get("symbol") == aria_sym),
                                    None,
                                )
                                aria_coh = (aria_bet_dict.get("coherence", 0.0)
                                            if aria_bet_dict else 0.0)
                                if aria_coh > 7.0:
                                    alignment = 0.65
                                elif aria_coh > 5.0:
                                    alignment = 0.55
                        # ── AUGUR coherence — Kantian structural soundness ────
                        # Per-symbol Bybit cascade data (more precise than global alert)
                        _bc = self.kingdom.get_augur_data(f"bybit_cascade.{aria_sym}")
                        _bybit_z = float(_bc.get("zscore", 0.0)) if _bc and _bc.get("active") else 0.0

                        # Kant factors — four dimensions of structural truth [0, 1]
                        _ob_conviction  = abs(agg - 0.50) * 2.0                   # OB one-sided?
                        _mom_strength   = min(abs(pct_30s) / 0.30, 1.0)           # price moving ≥0.3%?
                        _cascade_factor = min(_bybit_z / 4.0, 1.0)               # Bybit cascade real?
                        _fund_clarity   = max(0.0, 1.0 - abs(fund_rate) * 40.0)  # funding clean?

                        # Weighted Kantian base [0, 10]
                        _kant_score = (
                            _ob_conviction  * 3.5 +   # 35% — what is market doing?
                            _mom_strength   * 2.5 +   # 25% — is price confirming?
                            _cascade_factor * 2.5 +   # 25% — is cascade structural?
                            _fund_clarity   * 1.5      # 15% — is structure clean?
                        )

                        # DeepSeek errand-bird whisper — ambient probability boost
                        # Boosts kant_score when the external observer agrees with AUGUR's direction
                        _ds_whisper = self.kingdom.get_deepseek_bias(aria_sym)
                        if (_ds_whisper and
                                _ds_whisper.get("bias") == direction and
                                _ds_whisper.get("strength", 0) >= 0.55):
                            _ds_boost = min(_ds_whisper.get("strength", 0) * 0.5, 0.5)
                            _kant_score = min(_kant_score + _ds_boost, 10.0)
                            logger.debug("deepseek_whisper_kant_boost",
                                         symbol=aria_sym, boost=round(_ds_boost, 3))

                        # Nietzsche will multiplier — TPS health × alignment track record
                        _tps_will  = max(0.50, min(tps / 3000.0, 1.20)) if tps > 0 else 0.60
                        _will_mult = _tps_will * (0.70 + alignment * 0.30)

                        coherence = round(min(_kant_score * _will_mult, 10.0), 2)

                        # ── Build AugurSignal ──────────────────────────────────
                        edge = round(deviation * 2.0, 4)  # probability edge
                        signal = AugurSignal(
                            symbol=aria_sym,
                            direction=direction,
                            combined=combined,
                            confidence=confidence,
                            coherence=coherence,
                            tps=tps,
                            price_momentum_pct=pct_30s,
                            agg_ratio=agg,
                            funding_rate=fund_rate,
                            cascade_zscore=cascade_zscore,
                            timestamp_ms=now_ms,
                            edge=edge,
                        )

                        # ── Phase 2a: Personality ──────────────────────────────
                        personality = assign_personality(
                            signal=signal,
                            aria_drawdown=aria_drawdown,
                            calendar_block_active=False,   # TODO: wire calendar
                            bybit_divergence_pct=0.0,      # single-venue for now
                            bybit_funding_diff=0.0,
                            aria_max_position_usd=aria_max_pos,
                        )

                        # ── Phase 2b: Kant validation ──────────────────────────
                        total_exp, sym_exp = self._get_exposure_pcts(aria_sym)
                        kant_frame = self.kant.validate(
                            signal=signal,
                            personality=personality,
                            bybit_connected=self.bybit_feed.is_connected(),
                            total_exposure_pct=total_exp,
                            symbol_exposure_pct=sym_exp,
                            augur_has_position=self._has_augur_position(aria_sym),
                            aria_regime=aria_regime,
                            aria_drawdown=aria_drawdown,
                            kingdom_total_positions=open_count,
                            max_open_trades=self.config.max_open_trades,
                        )

                        if not kant_frame.passed:
                            logger.info(
                                "augur_kant_blocked",
                                symbol=aria_sym,
                                personality=personality.value,
                                failed=[c.name for c in kant_frame.failed_checks],
                            )
                            # Still place the bet for prediction market consensus
                        else:
                            # ── Phase 2c: Nietzsche will computation ───────────
                            hist_wr  = augur_hist_wr.get(symbol, direction)
                            will_out = self.nietzsche.compute(
                                signal=signal,
                                kant_frame=kant_frame,
                                personality=personality,
                                hist_wr=hist_wr,
                                agent_alignment=alignment,
                            )

                            if will_out.will_state == WillState.ABSTAIN:
                                logger.info(
                                    "augur_nietzsche_abstain",
                                    symbol=aria_sym,
                                    conviction=will_out.conviction,
                                )
                            else:
                                # Annotate signal with Nietzsche sizing intent
                                signal.confidence = round(
                                    min(confidence * will_out.size_mult, 0.90), 3
                                )

                        # Always place the bet so dual-agent engine can resolve
                        augur_bet = PredictionBet(
                            agent_id="augur",
                            symbol=aria_sym,
                            direction=direction,
                            confidence=signal.confidence,
                            evidence_type="microstructure",
                            coherence=coherence,
                            timestamp_ms=now_ms,
                            expires_ms=now_ms + 30 * 60 * 1000,
                        )
                        self.bet_engine.place_bet(augur_bet)
                        logger.info(
                            "augur_native_bet",
                            symbol=aria_sym,
                            direction=direction,
                            confidence=signal.confidence,
                            personality=personality.value,
                            will=(will_out.will_state.value
                                  if kant_frame.passed else "kant_blocked"),
                            combined=round(combined, 3),
                            agg_ratio=round(agg, 3),
                            price_momentum=round(pct_30s, 3),
                        )

                    except Exception as e:
                        logger.warning("augur_signal_symbol_error",
                                       symbol=symbol, error=str(e))

            except Exception as e:
                logger.error("augur_signal_loop_error", error=str(e))
            await asyncio.sleep(30)

    # ── Phase 3: Kingdom sync — watchdog event-driven ─────────────────────────

    async def kingdom_sync_loop(self) -> None:
        """
        Event-driven kingdom sync — wakes within 50ms of ARIA writing.

        Watchdog filesystem observer notifies this loop via asyncio.Event.
        Fallback poll fires every 120s when no events arrive.

        On every wake: read ARIA bets → cross-agent resolution → Chancellor gate → execute.
        """
        loop = asyncio.get_event_loop()
        self._kingdom_observer = self.kingdom.start_watcher(
            callback=lambda: self._kingdom_event.set(),
            loop=loop,
        )
        logger.info("kingdom_sync_loop_started",
                    watchdog=self._kingdom_observer is not None)

        while True:
            try:
                # Wait for ARIA write event or fallback timeout
                try:
                    await asyncio.wait_for(self._kingdom_event.wait(), timeout=15.0)
                    # Measure latency from file mtime to our processing
                    try:
                        mtime_ms = int(self.kingdom.state_path.stat().st_mtime * 1000)
                        latency_ms = int(time.time() * 1000) - mtime_ms
                        logger.debug("kingdom_signal_received",
                                     latency_ms=latency_ms,
                                     target_ms=100)
                    except Exception:
                        pass
                except asyncio.TimeoutError:
                    logger.debug("kingdom_sync_fallback_poll")
                finally:
                    self._kingdom_event.clear()

                aria = self.kingdom.read_aria_state()

                if not aria.active_bets:
                    logger.debug("waiting_for_aria_state")
                    continue

                now_ms     = int(time.time() * 1000)
                open_count = self.kingdom.count_open_positions("augur")

                for bet_dict in aria.active_bets:
                    try:
                        aria_bet = AgentBet(**{
                            k: v for k, v in bet_dict.items()
                            if k in AgentBet.__dataclass_fields__
                        })
                        pm_bet = PredictionBet.model_validate(bet_dict)

                        self.bet_engine.place_bet(pm_bet)
                        resolution = self.bet_engine.resolve(pm_bet.symbol)

                        _journal_append(_AUGUR_JOURNAL, {
                            "event":              "cross_bet_resolution",
                            "symbol":             aria_bet.symbol,
                            "aria_direction":     aria_bet.direction,
                            "aria_coherence":     aria_bet.coherence,
                            "agreement_type":     resolution.agreement_type,
                            "market_confidence":  round(resolution.market_confidence, 3),
                            "resolution_score":   round(resolution.resolution_score, 3),
                            "recommended_action": resolution.recommended_action,
                            "timestamp_ms":       now_ms,
                        })
                        logger.info(
                            "cross_bet_resolved",
                            symbol=aria_bet.symbol,
                            agreement=resolution.agreement_type,
                            confidence=round(resolution.market_confidence, 3),
                            score=round(resolution.resolution_score, 3),
                        )

                        if not self._is_live:
                            continue
                        if aria_bet.direction == "neutral":
                            continue

                        # Conviction gate — threshold scales with agreement strength.
                        # single_aria: ARIA carries the signal, 0.35 is enough.
                        # compound:    both agents must be convinced, 0.55 required.
                        # single_augur/silence/disagreement: deferred.
                        atype = resolution.agreement_type
                        if atype in ("silence", "disagreement"):
                            continue
                        if atype == "single_augur":
                            # AUGUR alone must be highly convinced
                            if resolution.market_confidence < 0.55:
                                logger.info("position_deferred_low_conviction",
                                            symbol=aria_bet.symbol,
                                            confidence=round(resolution.market_confidence, 3),
                                            agreement=atype)
                                continue
                        elif atype in ("weak_agreement", "strong_agreement"):
                            if resolution.market_confidence < 0.55:
                                logger.info("position_deferred_low_conviction",
                                            symbol=aria_bet.symbol,
                                            confidence=round(resolution.market_confidence, 3),
                                            agreement=atype)
                                continue
                        else:  # single_aria — ARIA carries the weight
                            if resolution.market_confidence < 0.35:
                                logger.info("position_deferred_low_conviction",
                                            symbol=aria_bet.symbol,
                                            confidence=round(resolution.market_confidence, 3),
                                            agreement=atype)
                                continue
                        if aria_bet.coherence < self.config.min_coherence:
                            continue
                        if self._is_signal_on_cooldown(aria_bet.symbol,
                                                        aria_bet.timestamp_ms):
                            continue

                        # ── Phase 5: Chancellor adjudication ──────────────────
                        # Pull AUGUR's current conviction on this symbol
                        augur_bets = self.bet_engine.get_active_bets(aria_bet.symbol)
                        augur_bet  = augur_bets.get("augur")
                        augur_conv = augur_bet.confidence if augur_bet else 0.0
                        augur_dir  = augur_bet.direction  if augur_bet else None

                        total_exp, sym_exp = self._get_exposure_pcts(aria_bet.symbol)

                        # Institutional backing: check if AUGUR's direction has hot signal
                        has_institutional = False
                        if augur_dir and hasattr(self, "intel_agent") and self.intel_agent:
                            hot = self.intel_agent.get_hot_signal(aria_bet.symbol)
                            has_institutional = (
                                hot is not None
                                and hot.direction == augur_dir
                                and hot.conviction >= 0.65
                            )

                        chancellor_decision = self.chancellor.adjudicate(
                            aria_direction=aria_bet.direction,
                            aria_coherence=aria_bet.coherence,
                            augur_direction=augur_dir,
                            augur_conviction=augur_conv,
                            aria_drawdown=self._daily_loss_pct,  # AUGUR's own P&L only
                            daily_loss_pct=self._daily_loss_pct,
                            cascade_zscore=float(self._cascade_alert.get("zscore", 0.0)),
                            total_exposure_pct=total_exp,
                            symbol_exposure_pct=sym_exp,
                            balance=max(self._cached_balance, 0.0),
                            has_institutional_signal=has_institutional,
                        )

                        if not chancellor_decision.augur_executes:
                            logger.info(
                                "augur_chancellor_blocked",
                                symbol=aria_bet.symbol,
                                reason=chancellor_decision.reason,
                                action=chancellor_decision.action,
                            )
                            continue

                        if open_count >= self.config.max_open_trades:
                            logger.info("max_trades_reached",
                                        symbol=aria_bet.symbol,
                                        open_count=open_count,
                                        limit=self.config.max_open_trades)
                            continue

                        # Final size with Chancellor modifier + Solana TPS multiplier
                        base_size  = self._compute_size(aria_bet.coherence,
                                                        resolution.resolution_score)
                        tps_mult   = self._solana_snapshot.get("tps_multiplier", 1.0)
                        size_usd   = round(
                            base_size
                            * chancellor_decision.size_modifier
                            * tps_mult,
                            2
                        )
                        size_usd   = min(max(size_usd, self.config.min_trade_usd),
                                         self.config.max_trade_usd)

                        logger.info(
                            "augur_executing_signal",
                            symbol=aria_bet.symbol,
                            direction=aria_bet.direction,
                            coherence=aria_bet.coherence,
                            confidence=round(resolution.market_confidence, 3),
                            agreement=resolution.agreement_type,
                            chancellor=chancellor_decision.reason,
                            size_usd=size_usd,
                            tps_mult=tps_mult,
                        )

                        # Hedge case: AUGUR trades its own direction (opposite to ARIA)
                        # Normal case: AUGUR follows ARIA's direction
                        trade_direction = aria_bet.direction
                        if chancellor_decision.augur_hedges and augur_dir:
                            trade_direction = augur_dir
                            logger.info(
                                "augur_institutional_hedge",
                                symbol=aria_bet.symbol,
                                aria_direction=aria_bet.direction,
                                augur_direction=augur_dir,
                                size_usd=size_usd,
                            )

                        mark = self.bybit_feed.get_mark_price(aria_bet.symbol)
                        is_long = trade_direction == "long"
                        tp_mark = round(mark * 1.015 if is_long else mark * 0.985, 6) if mark > 0 else 0.0
                        sl_mark = round(mark * 0.990 if is_long else mark * 1.010, 6) if mark > 0 else 0.0

                        order = await self.router.place_order(
                            symbol=aria_bet.symbol,
                            direction=trade_direction,
                            size_usd=size_usd,
                            entry=mark if mark > 0 else 0.0,
                            tp1=tp_mark,
                            stop=sl_mark,
                        )

                        self._mark_executed(aria_bet.symbol, aria_bet.timestamp_ms)
                        self.kingdom.write_position(
                            "augur", aria_bet.symbol, trade_direction,
                            size_usd, order.venue,
                        )
                        self.outcome_resolver.register_position(
                            symbol=aria_bet.symbol,
                            direction=aria_bet.direction,
                            size_usd=size_usd,
                            entry_price=order.entry,
                            venue=order.venue,
                        )
                        open_count += 1

                        _journal_append(_AUGUR_JOURNAL, {
                            "event":             "live_order_placed",
                            "symbol":            aria_bet.symbol,
                            "direction":         aria_bet.direction,
                            "size_usd":          size_usd,
                            "order_id":          order.order_id,
                            "venue":             order.venue,
                            "resolution_score":  resolution.resolution_score,
                            "market_confidence": resolution.market_confidence,
                            "agreement":         resolution.agreement_type,
                            "chancellor":        chancellor_decision.reason,
                            "tps_mult":          tps_mult,
                            "timestamp_ms":      now_ms,
                        })

                    except Exception as e:
                        logger.warning("cross_bet_error",
                                       symbol=bet_dict.get("symbol"), error=str(e))

                # Publish AUGUR state to kingdom
                self.kingdom.write_augur_state(AugurState(
                    active_bets=[],
                    active_polymarket_bets=[],
                    etf_flow_direction=self._regime,
                ))

            except Exception as e:
                logger.error("kingdom_sync_error", error=str(e))


    # ── Phase 6: Heartbeat ────────────────────────────────────────────────────

    async def heartbeat_loop(self) -> None:
        """Every 60s — operational pulse. Updates cached_balance for Chancellor."""
        while True:
            try:
                uptime_s   = int(time.time() - self._start_time)
                aria       = self.kingdom.read_aria_state()
                mode_str   = "LIVE" if self._is_live else "PAPER"
                open_count = self.kingdom.count_open_positions("augur")

                balance_str = ""
                if self._is_live:
                    try:
                        bal = await self.bybit.get_balance()
                        if bal > 0:
                            self._cached_balance = bal
                        balance_str = f"  bybit_usdt={bal:.2f}"
                        if bal < 20.0:
                            logger.warning("bybit_low_balance", usdt=bal)
                    except Exception:
                        balance_str = "  bybit_usdt=err"
                else:
                    self._cached_balance = 300.0  # paper sentinel value

                # AUGUR tracks its own daily loss — not ARIA's drawdown.
                # ARIA drawdown is already checked via aria_drawdown in Chancellor.
                # Until AUGUR has journal-backed P&L, daily_loss_pct stays 0.
                self._daily_loss_pct = 0.0

                print(
                    f"\n\033[1;36m[AUGUR HEARTBEAT]\033[0m"
                    f"  mode={mode_str}"
                    f"  uptime={uptime_s}s"
                    f"  regime={self._regime}"
                    f"  cascade={'ON' if self._cascade_alert.get('active') else 'off'}"
                    f"  aria_bets={len(aria.active_bets)}"
                    f"  open_pos={open_count}/{self.config.max_open_trades}"
                    f"  tps={self._solana_snapshot.get('tps', 0):.0f}"
                    f"{balance_str}"
                )

                _journal_append(_AUGUR_JOURNAL, {
                    "event":          "heartbeat",
                    "uptime_s":       uptime_s,
                    "mode":           mode_str.lower(),
                    "regime":         self._regime,
                    "cascade_active": self._cascade_alert.get("active"),
                    "aria_bets":      len(aria.active_bets),
                    "open_positions": open_count,
                    "solana_tps":     self._solana_snapshot.get("tps", 0),
                    "balance":        self._cached_balance,
                    "timestamp_ms":   int(time.time() * 1000),
                })

            except Exception as e:
                logger.error("heartbeat_error", error=str(e))
            await asyncio.sleep(15)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.journal.start_writer()
        self._print_startup_banner()

        if self._is_live:
            await asyncio.gather(
                self.bybit.health_check(),
                return_exceptions=True,
            )

        try:
            await asyncio.gather(
                self.valuechain_loop(),
                self.bybit_feed.start(),
                self.bybit_cascade.start(),
                self.liquidation_manager.start(),
                self.augur_signal_loop(),
                self.kingdom_sync_loop(),
                self.outcome_resolver.resolve_loop(),
                self.cross_feedback.feedback_loop(),
                self.heartbeat_loop(),
                self.strategy_runner.run_forever(),
                self.intel_agent.run_forever(),
            )
        finally:
            if self._kingdom_observer:
                try:
                    self._kingdom_observer.stop()
                    self._kingdom_observer.join()
                except Exception:
                    pass
            await self.journal.stop_writer()


if __name__ == "__main__":
    app = AugurApplication(settings)
    asyncio.run(app.run())
