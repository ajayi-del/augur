import asyncio
import json
import os
import time
import structlog
from pathlib import Path
from typing import Dict

from core.config import config as settings
from data.valuechain_bridge import ValueChainBridge
from data.solana_bridge import SolanaBridge
from data.bybit_feed import BybitFeed
from kingdom.state_sync import KingdomStateSync, AugurState, AgentBet
from intelligence.prediction_market import CrossAgentBetEngine
from intelligence.prediction_market import AgentBet as PredictionBet
from memory.trade_journal import TradeJournal
from memory.augur_hist_wr import augur_hist_wr
from memory.outcome_resolver import OutcomeResolver
from memory.cross_agent_feedback import CrossAgentFeedback
from execution.venues.mexc_client import MexcClient
from execution.bybit_client import BybitClient
from execution.routing_client import RoutingClient

logger = structlog.get_logger()

_POLY_JOURNAL  = Path(settings.augur_log_path) / "polymarket_journal.jsonl"
_AUGUR_JOURNAL = Path(settings.augur_log_path) / "augur_journal.jsonl"

# Execution gate thresholds
# Dual-agent agreement required: confidence > 0.7 AND not single-agent
# single_aria max score = confidence × 5.0 → market_confidence ≤ 0.5 → never fires here
_CONFIDENCE_GATE         = 0.70   # minimum market_confidence to execute
_CONFIDENCE_LOG_FLOOR    = 0.60   # log position_deferred_low_conviction below this
_SINGLE_AGENT_TYPES      = ("single_aria", "single_augur", "silence", "disagreement")
_EXECUTION_COOLDOWN_MS   = 30 * 60 * 1000   # 30-minute per-symbol cooldown

# Native signal loop weights (Solana on-chain as primary evidence)
_TPS_WEIGHT     = 0.30
_PRICE_WEIGHT   = 0.50
_FUNDING_WEIGHT = 0.20
_MIN_BET_CONFIDENCE = 0.15   # below this AUGUR stays silent on the symbol


def _journal_append(path: Path, entry: dict) -> None:
    """Append a JSON line to a journal file. Never crashes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning("journal_write_error", path=str(path), error=str(e))


class AugurApplication:
    """
    AUGUR — Solana-native Sovereign Agent. Trades Bybit linear perps.
    Signal sources: ARIA kingdom state + Solana on-chain + CoinGecko prices + Drift funding.
    Execution: Bybit primary, MEXC futures fallback; MEXC prediction markets when live.
    """

    def __init__(self, cfg):
        self.config      = cfg
        self._start_time = time.time()
        # Live requires Bybit keys (Bybit is primary — MEXC is geo-blocked on GCP)
        self._is_live    = (
            cfg.mode == "live" and
            cfg.live_mode_confirmed and
            bool(cfg.bybit_api_key)
        )

        # Kingdom / Bridge
        self.kingdom = KingdomStateSync(cfg.kingdom_state_path)
        self.bridge  = ValueChainBridge(self.kingdom)

        # Solana on-chain signals (CoinGecko prices, TPS)
        self.solana = SolanaBridge()

        # Bybit real-time market data (mark prices, OB imbalance, liquidations)
        self.bybit_feed = BybitFeed(symbols=cfg.news_assets)

        # Execution layer
        self.mexc = MexcClient(
            api_key=cfg.mexc_api_key,
            secret=cfg.mexc_secret_key,
            leverage=min(cfg.mexc_futures_leverage, 5),
            max_position_usdt=cfg.mexc_max_position_usdt,
            prediction_bankroll=cfg.mexc_prediction_bankroll,
            prediction_max_bet_pct=cfg.mexc_prediction_max_bet_pct,
        )
        self.bybit = BybitClient(
            mode="live" if self._is_live else "paper",
            api_key=cfg.bybit_api_key,
            api_secret=cfg.bybit_api_secret,
        )
        self.router = RoutingClient(
            mexc=self.mexc,
            bybit=self.bybit,
            mode="live" if self._is_live else "paper",
        )

        # Cross-agent bet engine
        self.bet_engine = CrossAgentBetEngine()

        # Journal
        self.journal = TradeJournal()
        self.journal.load()

        # Learning loop components
        self.outcome_resolver  = OutcomeResolver(bybit_client=self.bybit)
        self.cross_feedback    = CrossAgentFeedback(kingdom=self.kingdom)

        # Cached ValueChain state — refreshed every 600s
        self._cascade_alert: dict  = {"active": False, "zscore": 0.0, "phase": "none"}
        self._regime: str          = "unknown"
        self._funding_rates: dict  = {}
        self._solana_snapshot: dict = {}

        # Execution dedup — {symbol: last_executed_timestamp_ms}
        self._executed_signals: Dict[str, int] = {}

        # Previous-cycle prices for native signal momentum computation
        self._price_prev: Dict[str, float] = {}

    # ── Startup banner ────────────────────────────────────────────────────────

    def _print_startup_banner(self) -> None:
        _C = "\033[1;36m"
        _G = "\033[0;32m"
        _Y = "\033[1;33m"
        _R = "\033[0m"
        mode_label = "LIVE" if self._is_live else "PAPER"
        mode_color = _Y if self._is_live else _G

        print(f"\n{_C}{'='*58}{_R}")
        print(f"{_C}   AUGUR — Solana-Native Sovereign Agent{_R}")
        print(f"{_C}{'='*58}{_R}")
        print(f"{mode_color}  MODE: {mode_label}{_R}")

        if self._is_live:
            print(f"{_Y}  Primary venue: Bybit V5 linear perps (5× leverage){_R}")
            print(f"{_Y}  Fallback venue: MEXC futures (active when IP whitelisted){_R}")
            print(f"{_Y}  Max open trades: {self.config.max_open_trades}{_R}")
            print(f"{_Y}  Confidence gate: >{_CONFIDENCE_GATE:.0%} dual-agent agreement{_R}")
            print(f"{_Y}  MEXC prediction bankroll: ${self.mexc.prediction_bankroll:.0f} "
                  f"(max bet {self.mexc.max_bet_pct*100:.0f}% per signal){_R}")
        else:
            print(f"{_G}  Execution: paper simulation (no real orders){_R}")

        try:
            aria = self.kingdom.read_aria_state()
            bets = len(aria.active_bets)
            print(f"{_G}  Kingdom sync: connected  ({bets} active ARIA bets){_R}")
        except Exception:
            print(f"  Kingdom sync: waiting for ARIA state")

        print(f"{_G}  ValueChain bridge: ready{_R}")
        print(f"{_G}  Solana bridge: ready (CoinGecko prices, public RPC){_R}")
        print(f"{_G}  Cross-agent bet engine: ready{_R}")
        Path(settings.augur_log_path).mkdir(parents=True, exist_ok=True)
        print(f"{_G}  Journal: {settings.augur_log_path}{_R}")
        print(f"{_C}{'='*58}{_R}\n")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_signal_on_cooldown(self, symbol: str, bet_ts_ms: int) -> bool:
        last = self._executed_signals.get(symbol, 0)
        return (bet_ts_ms - last) < _EXECUTION_COOLDOWN_MS

    def _mark_executed(self, symbol: str, bet_ts_ms: int) -> None:
        self._executed_signals[symbol] = bet_ts_ms

    def _compute_size(self, coherence: float, resolution_score: float) -> float:
        """
        Position size in USDT.
        Scales with conviction; always ≤ max_trade_usd.
        """
        base = self.config.base_trade_usd
        # Coherence multiplier: 5.0→0.5×, 8.0→1.0×, 10.0→1.0× (capped)
        coh_mult = min((coherence - 4.0) / 6.0, 1.0)
        # Resolution score multiplier: 5.0→0.7×, 7.0→1.0×, 9.0→1.2×
        score_mult = min(0.5 + (resolution_score / 10.0), 1.2)
        size = base * coh_mult * score_mult
        return round(min(max(size, self.config.min_trade_usd), self.config.max_trade_usd), 2)

    # ── Loops ─────────────────────────────────────────────────────────────────

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
                    "prices_n":       len(self._solana_snapshot.get("jupiter_prices", {})),
                    "timestamp_ms":   int(time.time() * 1000),
                })
                logger.info(
                    "valuechain_refreshed",
                    cascade_active=self._cascade_alert.get("active"),
                    cascade_zscore=round(self._cascade_alert.get("zscore", 0.0), 2),
                    regime=self._regime,
                    solana_tps=round(self._solana_snapshot.get("tps", 0), 0),
                    tps_mult=self._solana_snapshot.get("tps_multiplier"),
                )
            except Exception as e:
                logger.error("valuechain_loop_error", error=str(e))
            await asyncio.sleep(600)

    async def augur_signal_loop(self) -> None:
        """
        Every 30s — AUGUR's independent on-chain signal evaluation.

        Signal stack (Solana-native + Bybit market data):
          1. Solana TPS trend       (weight 0.20) — network health/congestion
          2. Bybit mark price momentum (weight 0.45) — venue-native price action
          3. Bybit order book imbalance (weight 0.20) — real-time buy/sell pressure
          4. Drift/Bybit funding rate   (weight 0.15) — carry signal

        AUGUR generates microstructure PredictionBets. When ARIA (narrative) and
        AUGUR (microstructure) agree, EVIDENCE_INDEPENDENCE=1.0 gives max bonus.
        Confidence is calibrated by hist_wr — after 10+ trades the priors update.
        """
        logger.info("augur_signal_loop_started", interval_s=30)

        while True:
            try:
                tps   = await self.solana.get_network_tps()
                now_ms = int(time.time() * 1000)

                for symbol in self.config.news_assets:
                    aria_sym = f"{symbol}-USD"
                    try:
                        # ── Signal 1: Solana TPS ──────────────────────────────
                        if tps >= 3000:    tps_signal = 0.58
                        elif tps >= 1500:  tps_signal = 0.53
                        elif tps > 0 and tps < 800: tps_signal = 0.42
                        else:              tps_signal = 0.50

                        # ── Signal 2: Bybit mark price momentum ───────────────
                        pct_30s = self.bybit_feed.get_price_momentum(aria_sym, 30.0)
                        if pct_30s > 0.5:   price_signal = 0.72
                        elif pct_30s > 0.2: price_signal = 0.62
                        elif pct_30s > 0.0: price_signal = 0.53
                        elif pct_30s < -0.5: price_signal = 0.28
                        elif pct_30s < -0.2: price_signal = 0.38
                        elif pct_30s < 0.0:  price_signal = 0.47
                        else:                price_signal = 0.50   # no data yet

                        # ── Signal 3: Bybit orderbook imbalance ───────────────
                        agg = self.bybit_feed.get_agg_ratio(aria_sym)
                        if agg > 0.65:   ob_signal = 0.68
                        elif agg > 0.55: ob_signal = 0.58
                        elif agg < 0.35: ob_signal = 0.32
                        elif agg < 0.45: ob_signal = 0.42
                        else:            ob_signal = 0.50

                        # ── Signal 4: Funding rate (Bybit > Drift fallback) ───
                        fund_rate = (
                            self.bybit_feed.get_funding_rate(aria_sym) or
                            self._funding_rates.get(symbol, 0.0)
                        )
                        if fund_rate > 0.02:    fund_signal = 0.38
                        elif fund_rate > 0.005: fund_signal = 0.45
                        elif fund_rate < -0.02: fund_signal = 0.62
                        elif fund_rate < -0.005: fund_signal = 0.55
                        else:                   fund_signal = 0.50

                        combined = (
                            0.20 * tps_signal +
                            0.45 * price_signal +
                            0.20 * ob_signal +
                            0.15 * fund_signal
                        )

                        deviation = abs(combined - 0.50)
                        if deviation < 0.04:
                            continue

                        direction  = "long" if combined > 0.50 else "short"
                        raw_conf   = min(deviation * 2.5, 0.85)

                        if raw_conf < _MIN_BET_CONFIDENCE:
                            continue

                        # Calibrate confidence with historical win rate
                        wr_mult    = augur_hist_wr.confidence_multiplier(symbol, direction)
                        confidence = round(min(raw_conf * wr_mult, 0.90), 3)

                        # Alignment score from cross-agent feedback (replaces 0.5 default)
                        alignment  = self.cross_feedback.get_alignment(aria_sym)

                        # Coherence: TPS/600 (Solana), boosted by alignment
                        coherence  = round(
                            min((tps / 600.0 if tps > 0 else 3.0) * (0.5 + alignment), 10.0), 2
                        )

                        augur_bet = PredictionBet(
                            agent_id="augur",
                            symbol=aria_sym,
                            direction=direction,
                            confidence=confidence,
                            evidence_type="microstructure",
                            coherence=coherence,
                            timestamp_ms=now_ms,
                            expires_ms=now_ms + 30 * 60 * 1000,
                        )
                        self.bet_engine.place_bet(augur_bet)
                        logger.debug(
                            "augur_native_bet",
                            symbol=aria_sym,
                            direction=direction,
                            confidence=confidence,
                            raw_conf=round(raw_conf, 3),
                            wr_mult=wr_mult,
                            alignment=round(alignment, 3),
                            agg_ratio=round(agg, 3),
                            price_pct=round(pct_30s, 3),
                        )

                    except Exception as e:
                        logger.warning("augur_signal_symbol_error",
                                       symbol=symbol, error=str(e))

            except Exception as e:
                logger.error("augur_signal_loop_error", error=str(e))
            await asyncio.sleep(30)

    async def kingdom_sync_loop(self) -> None:
        """
        Every 60s — read ARIA bets, compute cross-agent resolution,
        execute live when dual-agent confidence ≥ 0.7 (live mode only).
        """
        logger.info("kingdom_sync_loop_started", interval_s=60)
        while True:
            try:
                aria = self.kingdom.read_aria_state()

                if not aria.active_bets:
                    logger.info("waiting_for_aria_state")
                    _journal_append(_AUGUR_JOURNAL, {
                        "event": "waiting_for_aria_state",
                        "timestamp_ms": int(time.time() * 1000),
                    })
                else:
                    now_ms = int(time.time() * 1000)

                    for bet_dict in aria.active_bets:
                        try:
                            aria_bet = AgentBet(**{
                                k: v for k, v in bet_dict.items()
                                if k in AgentBet.__dataclass_fields__
                            })
                            pm_bet = PredictionBet.model_validate(bet_dict)

                            self.bet_engine.place_bet(pm_bet)
                            resolution = self.bet_engine.resolve(pm_bet.symbol)

                            log_entry = {
                                "event":              "cross_bet_resolution",
                                "symbol":             aria_bet.symbol,
                                "aria_direction":     aria_bet.direction,
                                "aria_coherence":     aria_bet.coherence,
                                "agreement_type":     resolution.agreement_type,
                                "market_confidence":  round(resolution.market_confidence, 3),
                                "resolution_score":   round(resolution.resolution_score, 3),
                                "recommended_action": resolution.recommended_action,
                                "timestamp_ms":       now_ms,
                            }
                            _journal_append(_AUGUR_JOURNAL, log_entry)
                            logger.info(
                                "cross_bet_resolved",
                                symbol=aria_bet.symbol,
                                agreement=resolution.agreement_type,
                                confidence=round(resolution.market_confidence, 3),
                                score=round(resolution.resolution_score, 3),
                            )

                            # ── Live execution gate ──────────────────────────
                            if not self._is_live:
                                continue
                            if aria_bet.direction == "neutral":
                                continue

                            # Conviction gate: dual-agent agreement only
                            if resolution.market_confidence < _CONFIDENCE_LOG_FLOOR:
                                logger.info(
                                    "position_deferred_low_conviction",
                                    symbol=aria_bet.symbol,
                                    confidence=round(resolution.market_confidence, 3),
                                    agreement=resolution.agreement_type,
                                )
                                continue
                            if resolution.market_confidence < _CONFIDENCE_GATE:
                                continue
                            if resolution.agreement_type in _SINGLE_AGENT_TYPES:
                                logger.info(
                                    "position_deferred_single_agent",
                                    symbol=aria_bet.symbol,
                                    agreement=resolution.agreement_type,
                                )
                                continue

                            if aria_bet.coherence < self.config.min_coherence:
                                continue
                            if self._is_signal_on_cooldown(aria_bet.symbol, aria_bet.timestamp_ms):
                                continue

                            # Max open trades gate
                            open_count = self.kingdom.count_open_positions("augur")
                            if open_count >= self.config.max_open_trades:
                                logger.info(
                                    "max_trades_reached",
                                    symbol=aria_bet.symbol,
                                    open_count=open_count,
                                    limit=self.config.max_open_trades,
                                )
                                continue

                            size_usd = self._compute_size(
                                aria_bet.coherence, resolution.resolution_score
                            )
                            tps_mult = self._solana_snapshot.get("tps_multiplier", 1.0)
                            size_usd = round(size_usd * tps_mult, 2)

                            logger.info(
                                "augur_executing_signal",
                                symbol=aria_bet.symbol,
                                direction=aria_bet.direction,
                                coherence=aria_bet.coherence,
                                confidence=round(resolution.market_confidence, 3),
                                agreement=resolution.agreement_type,
                                size_usd=size_usd,
                                tps_mult=tps_mult,
                            )

                            order = await self.router.place_order(
                                symbol=aria_bet.symbol,
                                direction=aria_bet.direction,
                                size_usd=size_usd,
                            )

                            self._mark_executed(aria_bet.symbol, aria_bet.timestamp_ms)
                            self.kingdom.write_position(
                                "augur", aria_bet.symbol, aria_bet.direction,
                                size_usd, order.venue,
                            )
                            # Register with outcome resolver for learning loop
                            self.outcome_resolver.register_position(
                                symbol=aria_bet.symbol,
                                direction=aria_bet.direction,
                                size_usd=size_usd,
                                entry_price=order.entry,
                                venue=order.venue,
                            )

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
                                "tps_mult":          tps_mult,
                                "timestamp_ms":      now_ms,
                            })

                        except Exception as e:
                            logger.warning(
                                "cross_bet_error",
                                symbol=bet_dict.get("symbol"), error=str(e),
                            )

                # Publish AUGUR state to kingdom
                augur_state = AugurState(
                    active_bets=[],
                    active_polymarket_bets=[],
                    etf_flow_direction=self._regime,
                )
                self.kingdom.write_augur_state(augur_state)

            except Exception as e:
                logger.error("kingdom_sync_error", error=str(e))
            await asyncio.sleep(60)

    async def mexc_prediction_loop(self) -> None:
        """
        Every 300s — scan MEXC prediction markets for edges.
        Uses ARIA coherence + Solana TPS conviction multiplier.
        Paper: journal only. Live: execute via MEXC prediction API.
        """
        logger.info("mexc_prediction_loop_started", interval_s=300)
        min_edge = self.config.mexc_min_prediction_edge

        while True:
            try:
                markets = await self.mexc.get_prediction_markets()

                if not markets:
                    logger.info("mexc_no_prediction_markets")
                    await asyncio.sleep(300)
                    continue

                aria_coherence = self.bridge.get_aria_coherence("BTC-USD")
                tps_mult       = self._solana_snapshot.get("tps_multiplier", 1.0)
                placed         = 0

                for market in markets[:50]:
                    try:
                        yes_price = float(market.get("yesPrice", market.get("yes_price", 0.5)))
                        market_id = market.get("marketId") or market.get("id", "")
                        question  = market.get("question", market.get("title", ""))

                        if not market_id or yes_price <= 0 or yes_price >= 1:
                            continue

                        if aria_coherence and self._regime in ("bull", "risk_on"):
                            p_augur = min(yes_price + 0.12 * tps_mult, 0.95)
                        elif self._regime in ("bear", "risk_off", "liquidation"):
                            p_augur = max(yes_price - 0.12 * tps_mult, 0.05)
                        else:
                            continue

                        edge = abs(p_augur - yes_price)
                        if edge < min_edge:
                            continue

                        outcome   = "YES" if p_augur > yes_price else "NO"
                        size_usdt = round(
                            self.mexc.prediction_bankroll * self.mexc.max_bet_pct, 2
                        )

                        entry = {
                            "event":        "prediction_signal",
                            "market_id":    market_id,
                            "question":     question[:80],
                            "yes_price":    yes_price,
                            "p_augur":      round(p_augur, 3),
                            "edge":         round(edge, 3),
                            "outcome":      outcome,
                            "size_usdt":    size_usdt,
                            "regime":       self._regime,
                            "tps_mult":     tps_mult,
                            "mode":         "live" if self._is_live else "paper",
                            "timestamp_ms": int(time.time() * 1000),
                        }

                        if self._is_live:
                            result = await self.mexc.place_prediction_bet(
                                market_id, outcome, size_usdt
                            )
                            entry["result"] = result
                            entry["event"]  = "prediction_bet_placed"

                        _journal_append(_AUGUR_JOURNAL, entry)
                        placed += 1
                        if placed >= 5:
                            break

                    except Exception as e:
                        logger.warning("prediction_item_error", error=str(e))

                logger.info(
                    "mexc_prediction_scan_done",
                    total_markets=len(markets), placed=placed,
                )

            except Exception as e:
                logger.error("mexc_prediction_loop_error", error=str(e))
            await asyncio.sleep(300)

    async def heartbeat_loop(self) -> None:
        """Every 60s — operational pulse with balance check in live mode."""
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
                        balance_str = f"  bybit_usdt={bal:.2f}"
                        if bal < 20.0:
                            logger.warning("bybit_low_balance", usdt=bal)
                    except Exception:
                        balance_str = "  bybit_usdt=err"

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
                    "timestamp_ms":   int(time.time() * 1000),
                })

            except Exception as e:
                logger.error("heartbeat_error", error=str(e))
            await asyncio.sleep(60)

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.journal.start_writer()
        self._print_startup_banner()

        if self._is_live:
            await asyncio.gather(
                self.bybit.health_check(),
                self.mexc.health_check(),
                return_exceptions=True,
            )

        try:
            await asyncio.gather(
                self.valuechain_loop(),
                self.bybit_feed.start(),
                self.augur_signal_loop(),
                self.kingdom_sync_loop(),
                self.mexc_prediction_loop(),
                self.outcome_resolver.resolve_loop(),
                self.cross_feedback.feedback_loop(),
                self.heartbeat_loop(),
            )
        finally:
            await self.journal.stop_writer()


if __name__ == "__main__":
    app = AugurApplication(settings)
    asyncio.run(app.run())
