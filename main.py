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
from kingdom.state_sync import KingdomStateSync, AugurState, AgentBet
from intelligence.prediction_market import CrossAgentBetEngine
from intelligence.prediction_market import AgentBet as PredictionBet
from memory.trade_journal import TradeJournal
from execution.venues.mexc_client import MexcClient
from execution.bybit_client import BybitClient
from execution.routing_client import RoutingClient

logger = structlog.get_logger()

_POLY_JOURNAL  = Path(settings.augur_log_path) / "polymarket_journal.jsonl"
_AUGUR_JOURNAL = Path(settings.augur_log_path) / "augur_journal.jsonl"

# Minimum cross-agent resolution score to trigger live execution.
# single_aria max score = confidence × 5.0 (~2.5 for typical ARIA bets)
# strong_agreement: 7.0+ | weak_agreement: 5.0+ | single_aria: 1.5+
_EXECUTION_SCORE_FLOOR  = 1.5
# Per-symbol cooldown — prevents re-entering the same signal every 60s
_EXECUTION_COOLDOWN_MS  = 30 * 60 * 1000   # 30 minutes


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
    AUGUR — Sovereign Prediction Agent.
    Live mode: MEXC primary execution, Bybit fallback.
    Signal sources: ARIA kingdom state + Solana on-chain + MEXC prediction markets.
    """

    def __init__(self, cfg):
        self.config     = cfg
        self._start_time = time.time()
        self._is_live   = (
            cfg.mode == "live" and
            cfg.live_mode_confirmed and
            bool(cfg.mexc_api_key)
        )

        # Kingdom / Bridge
        self.kingdom = KingdomStateSync(cfg.kingdom_state_path)
        self.bridge  = ValueChainBridge(self.kingdom)

        # Solana on-chain signals
        self.solana = SolanaBridge()

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

        # Cached ValueChain state — refreshed every 600s
        self._cascade_alert: dict         = {"active": False, "zscore": 0.0, "phase": "none"}
        self._regime: str                  = "unknown"
        self._funding_rates: dict          = {}
        self._solana_snapshot: dict        = {}

        # Execution dedup — {symbol: last_executed_timestamp_ms}
        self._executed_signals: Dict[str, int] = {}

    # ── Startup banner ────────────────────────────────────────────────────────

    def _print_startup_banner(self) -> None:
        _C = "\033[1;36m"
        _G = "\033[0;32m"
        _Y = "\033[1;33m"
        _R = "\033[0m"
        mode_label = "LIVE" if self._is_live else "PAPER"
        mode_color = _Y if self._is_live else _G

        print(f"\n{_C}{'='*58}{_R}")
        print(f"{_C}   AUGUR — Sovereign Prediction Agent{_R}")
        print(f"{_C}{'='*58}{_R}")
        print(f"{mode_color}  MODE: {mode_label}{_R}")

        if self._is_live:
            print(f"{_Y}  Primary venue: MEXC futures (leverage {self.mexc.leverage}×, "
                  f"max ${self.mexc.max_position_usdt:.0f}){_R}")
            print(f"{_Y}  Fallback venue: Bybit V5 linear perps{_R}")
            print(f"{_Y}  Prediction bankroll: ${self.mexc.prediction_bankroll:.0f} "
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
        print(f"{_G}  Solana bridge: ready (public RPC + Jupiter){_R}")
        print(f"{_G}  Cross-agent bet engine: ready{_R}")
        Path(settings.augur_log_path).mkdir(parents=True, exist_ok=True)
        print(f"{_G}  Journal: {settings.augur_log_path}{_R}")
        print(f"{_C}{'='*58}{_R}\n")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_signal_on_cooldown(self, symbol: str, bet_ts_ms: int) -> bool:
        """Prevents re-executing the same ARIA signal every 60s."""
        last = self._executed_signals.get(symbol, 0)
        return (bet_ts_ms - last) < _EXECUTION_COOLDOWN_MS

    def _mark_executed(self, symbol: str, bet_ts_ms: int) -> None:
        self._executed_signals[symbol] = bet_ts_ms

    def _compute_size(self, coherence: float, resolution_score: float) -> float:
        """
        Position size in USDT.
        Scales with conviction; always ≤ mexc_max_position_usdt.
        """
        base = self.config.base_trade_usd  # $200
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
                    "jupiter_n":      len(self._solana_snapshot.get("jupiter_prices", {})),
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

    async def kingdom_sync_loop(self) -> None:
        """
        Every 60s — read ARIA bets, compute cross-agent resolution,
        execute live when conviction ≥ threshold (live mode only).
        """
        logger.info("kingdom_sync_loop_started", interval_s=60)
        while True:
            try:
                aria = self.kingdom.read_aria_state()

                if not aria.active_bets:
                    _journal_append(_AUGUR_JOURNAL, {
                        "event": "waiting_for_aria_state",
                        "timestamp_ms": int(time.time() * 1000),
                    })
                    logger.info("waiting_for_aria_state")
                else:
                    now_ms = int(time.time() * 1000)

                    for bet_dict in aria.active_bets:
                        try:
                            # Build kingdom dataclass (for field access)
                            aria_bet = AgentBet(**{
                                k: v for k, v in bet_dict.items()
                                if k in AgentBet.__dataclass_fields__
                            })
                            # Build pydantic model (required by CrossAgentBetEngine)
                            pm_bet = PredictionBet.model_validate(bet_dict)

                            self.bet_engine.place_bet(pm_bet)
                            resolution = self.bet_engine.resolve(pm_bet.symbol)

                            log_entry = {
                                "event":              "cross_bet_resolution",
                                "symbol":             aria_bet.symbol,
                                "aria_direction":     aria_bet.direction,
                                "aria_coherence":     aria_bet.coherence,
                                "agreement_type":     resolution.agreement_type,
                                "resolution_score":   round(resolution.resolution_score, 3),
                                "recommended_action": resolution.recommended_action,
                                "timestamp_ms":       now_ms,
                            }
                            _journal_append(_AUGUR_JOURNAL, log_entry)
                            logger.info(
                                "cross_bet_resolved",
                                symbol=aria_bet.symbol,
                                agreement=resolution.agreement_type,
                                score=round(resolution.resolution_score, 3),
                                action=resolution.recommended_action,
                            )

                            # ── Live execution gate ──────────────────────────
                            if not self._is_live:
                                continue
                            if aria_bet.direction == "neutral":
                                continue
                            if aria_bet.coherence < self.config.min_coherence:
                                continue
                            if resolution.resolution_score < _EXECUTION_SCORE_FLOOR:
                                continue
                            if self._is_signal_on_cooldown(aria_bet.symbol, aria_bet.timestamp_ms):
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
                                score=resolution.resolution_score,
                                size_usd=size_usd,
                                tps_mult=tps_mult,
                            )

                            order = await self.router.place_order(
                                symbol=aria_bet.symbol,
                                direction=aria_bet.direction,
                                size_usd=size_usd,
                            )

                            self._mark_executed(aria_bet.symbol, aria_bet.timestamp_ms)

                            _journal_append(_AUGUR_JOURNAL, {
                                "event":            "live_order_placed",
                                "symbol":           aria_bet.symbol,
                                "direction":        aria_bet.direction,
                                "size_usd":         size_usd,
                                "order_id":         order.order_id,
                                "venue":            order.venue,
                                "resolution_score": resolution.resolution_score,
                                "agreement":        resolution.agreement_type,
                                "tps_mult":         tps_mult,
                                "timestamp_ms":     now_ms,
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
        Uses ARIA coherence + Solana conviction multiplier.
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
                placed          = 0

                for market in markets[:50]:    # cap per scan
                    try:
                        yes_price   = float(market.get("yesPrice", market.get("yes_price", 0.5)))
                        market_id   = market.get("marketId") or market.get("id", "")
                        question    = market.get("question", market.get("title", ""))

                        if not market_id or yes_price <= 0 or yes_price >= 1:
                            continue

                        # Simple edge: ARIA coherence shifts our p estimate
                        if aria_coherence and self._regime in ("bull", "risk_on"):
                            p_augur = min(yes_price + 0.12 * tps_mult, 0.95)
                        elif self._regime in ("bear", "risk_off", "liquidation"):
                            p_augur = max(yes_price - 0.12 * tps_mult, 0.05)
                        else:
                            continue   # no regime conviction → skip

                        edge = abs(p_augur - yes_price)
                        if edge < min_edge:
                            continue

                        outcome   = "YES" if p_augur > yes_price else "NO"
                        size_usdt = round(
                            self.mexc.prediction_bankroll * self.mexc.max_bet_pct, 2
                        )

                        entry = {
                            "event":           "prediction_signal",
                            "market_id":       market_id,
                            "question":        question[:80],
                            "yes_price":       yes_price,
                            "p_augur":         round(p_augur, 3),
                            "edge":            round(edge, 3),
                            "outcome":         outcome,
                            "size_usdt":       size_usdt,
                            "regime":          self._regime,
                            "tps_mult":        tps_mult,
                            "mode":            "live" if self._is_live else "paper",
                            "timestamp_ms":    int(time.time() * 1000),
                        }

                        if self._is_live:
                            result = await self.mexc.place_prediction_bet(
                                market_id, outcome, size_usdt
                            )
                            entry["result"] = result
                            entry["event"]  = "prediction_bet_placed"

                        _journal_append(_AUGUR_JOURNAL, entry)
                        placed += 1
                        if placed >= 5:    # max 5 prediction bets per scan
                            break

                    except Exception as e:
                        logger.warning("prediction_item_error", error=str(e))

                logger.info(
                    "mexc_prediction_scan_done",
                    total_markets=len(markets), placed=placed, mode=self.router.mode,
                )

            except Exception as e:
                logger.error("mexc_prediction_loop_error", error=str(e))
            await asyncio.sleep(300)

    async def heartbeat_loop(self) -> None:
        """Every 60s — operational pulse with balance check in live mode."""
        while True:
            try:
                uptime_s = int(time.time() - self._start_time)
                aria     = self.kingdom.read_aria_state()
                mode_str = "LIVE" if self._is_live else "PAPER"

                balance_str = ""
                if self._is_live:
                    try:
                        bal = await self.mexc.get_balance()
                        balance_str = f"  mexc_usdt={bal:.2f}"
                        if bal < 20.0:
                            logger.warning("mexc_low_balance", usdt=bal)
                    except Exception:
                        balance_str = "  mexc_usdt=err"

                print(
                    f"\n\033[1;36m[AUGUR HEARTBEAT]\033[0m"
                    f"  mode={mode_str}"
                    f"  uptime={uptime_s}s"
                    f"  regime={self._regime}"
                    f"  cascade={'ON' if self._cascade_alert.get('active') else 'off'}"
                    f"  aria_bets={len(aria.active_bets)}"
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

        # Connectivity checks (non-blocking — failures are logged, not fatal)
        if self._is_live:
            await asyncio.gather(
                self.mexc.health_check(),
                self.bybit.health_check(),
                return_exceptions=True,
            )

        try:
            await asyncio.gather(
                self.valuechain_loop(),
                self.kingdom_sync_loop(),
                self.mexc_prediction_loop(),
                self.heartbeat_loop(),
            )
        finally:
            await self.journal.stop_writer()


if __name__ == "__main__":
    app = AugurApplication(settings)
    asyncio.run(app.run())
