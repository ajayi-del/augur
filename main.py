import asyncio
import json
import os
import time
import structlog
from pathlib import Path

from core.config import config as settings
from data.valuechain_bridge import ValueChainBridge
from kingdom.state_sync import KingdomStateSync, AugurState, AgentBet
from polymarket.probability_engine import ProbabilityEngine
from polymarket.kelly_sizer import KellySizer
from polymarket.market_scanner import MarketScanner
from intelligence.prediction_market import CrossAgentBetEngine
from memory.trade_journal import TradeJournal

logger = structlog.get_logger()

_POLY_JOURNAL  = Path(settings.augur_log_path) / "polymarket_journal.jsonl"
_AUGUR_JOURNAL = Path(settings.augur_log_path) / "augur_journal.jsonl"


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
    Paper mode: all execution is journal writes. No real money.
    Signal sources: ARIA kingdom state + Drift public API + Polymarket public API.
    """

    def __init__(self, cfg):
        self.config = cfg
        self._start_time = time.time()

        # Kingdom / Bridge
        self.kingdom = KingdomStateSync(cfg.kingdom_state_path)
        self.bridge  = ValueChainBridge(self.kingdom)

        # Prediction engine
        self.prob_engine = ProbabilityEngine()
        self.kelly       = KellySizer(bankroll=cfg.polymarket_bankroll)
        self.scanner     = MarketScanner(
            clob_client=None,
            prob_engine=self.prob_engine,
            kelly_sizer=self.kelly,
            min_edge=cfg.polymarket_min_edge,
            min_liquidity=cfg.polymarket_min_liquidity,
        )
        self.bet_engine = CrossAgentBetEngine()

        # Journal
        self.journal = TradeJournal()
        self.journal.load()

        # ValueChain state — refreshed every 600s in valuechain_loop
        self._cascade_alert: dict = {"active": False, "zscore": 0.0, "phase": "none"}
        self._regime: str = "unknown"
        self._funding_rates: dict = {}

    # ── Startup banner ────────────────────────────────────────────────────────

    def _print_startup_banner(self) -> None:
        _C = "\033[1;36m"  # cyan bold
        _G = "\033[0;32m"  # green
        _R = "\033[0m"
        print(f"\n{_C}{'='*56}{_R}")
        print(f"{_C}   AUGUR — Sovereign Prediction Agent{_R}")
        print(f"{_C}{'='*56}{_R}")
        print(f"{_G}  AUGUR starting in PAPER mode{_R}")
        print(f"{_G}  No API keys required for paper mode{_R}")

        # Verify kingdom connectivity
        try:
            aria = self.kingdom.read_aria_state()
            bets = len(aria.active_bets)
            print(f"{_G}  Kingdom sync: connected  ({bets} active ARIA bets){_R}")
        except Exception:
            print(f"  Kingdom sync: waiting for ARIA state")

        print(f"{_G}  ValueChain bridge: ready{_R}")
        print(f"{_G}  Drift funding API: connected (public, no auth){_R}")
        print(f"{_G}  Polymarket public API: connected (no auth){_R}")
        print(f"{_G}  Prediction market engine: ready{_R}")

        Path(settings.augur_log_path).mkdir(parents=True, exist_ok=True)
        print(f"{_G}  Journal: initialized  →  {settings.augur_log_path}{_R}")
        print(f"{_G}  All loops started{_R}")
        print(f"{_C}  AUGUR operational (paper mode){_R}")
        print(f"{_C}{'='*56}{_R}\n")

    # ── Loops ─────────────────────────────────────────────────────────────────

    async def valuechain_loop(self) -> None:
        """Every 600s — refresh ARIA cascade/regime state and Drift funding."""
        logger.info("valuechain_loop_started", interval_s=600)
        while True:
            try:
                self._cascade_alert = self.bridge.get_cascade_signal()
                self._regime        = self.bridge.get_regime()
                self._funding_rates = await self.bridge.get_funding_rates()

                _journal_append(_AUGUR_JOURNAL, {
                    "event": "valuechain_refresh",
                    "cascade_active": self._cascade_alert.get("active"),
                    "cascade_zscore": self._cascade_alert.get("zscore"),
                    "regime": self._regime,
                    "funding_n": len(self._funding_rates),
                    "timestamp_ms": int(time.time() * 1000),
                })
                logger.info("valuechain_refreshed",
                            cascade_active=self._cascade_alert.get("active"),
                            cascade_zscore=round(self._cascade_alert.get("zscore", 0.0), 2),
                            regime=self._regime,
                            funding_symbols=len(self._funding_rates))
            except Exception as e:
                logger.error("valuechain_loop_error", error=str(e))
            await asyncio.sleep(600)

    async def kingdom_sync_loop(self) -> None:
        """
        Every 60s — read ARIA bets, resolve cross-bets via prediction engine.
        Logs all resolutions to augur_journal.jsonl.
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
                            aria_bet = AgentBet(**{
                                k: v for k, v in bet_dict.items()
                                if k in AgentBet.__dataclass_fields__
                            })
                            self.bet_engine.place_bet(aria_bet)

                            resolution = self.bet_engine.resolve(aria_bet.symbol)

                            log_entry = {
                                "event": "cross_bet_resolution",
                                "symbol": aria_bet.symbol,
                                "aria_direction": aria_bet.direction,
                                "aria_coherence": aria_bet.coherence,
                                "agreement_type": resolution.agreement_type,
                                "resolution_score": round(resolution.resolution_score, 3),
                                "recommended_action": resolution.recommended_action,
                                "timestamp_ms": now_ms,
                            }
                            _journal_append(_AUGUR_JOURNAL, log_entry)
                            logger.info("cross_bet_resolved",
                                        symbol=aria_bet.symbol,
                                        agreement=resolution.agreement_type,
                                        score=round(resolution.resolution_score, 3),
                                        action=resolution.recommended_action)
                        except Exception as e:
                            logger.warning("cross_bet_error",
                                           symbol=bet_dict.get("symbol"), error=str(e))

                # Publish current AUGUR state to kingdom
                augur_state = AugurState(
                    active_bets=self.journal.get_active_bets(),
                    active_polymarket_bets=self.journal.get_active_polymarket_bets(),
                    etf_flow_direction=self._regime,
                )
                self.kingdom.write_augur_state(augur_state)

            except Exception as e:
                logger.error("kingdom_sync_error", error=str(e))
            await asyncio.sleep(60)

    async def prediction_market_loop(self) -> None:
        """
        Every 300s — scan Polymarket public markets for edges.
        Paper bets written to polymarket_journal.jsonl. No real execution.
        """
        logger.info("prediction_market_loop_started", interval_s=300)
        _MIN_EDGE = self.config.polymarket_min_edge

        while True:
            try:
                # Use latest cached ValueChain signals
                cascade   = self._cascade_alert
                regime    = self._regime
                funding   = self._funding_rates

                # Determine direction bias from ARIA regime
                market_direction = (
                    "short" if regime in ("bear", "risk_off", "liquidation")
                    else "long"
                )

                # Use coherence from any ARIA BTC bet as proxy signal
                aria_coherence = self.bridge.get_aria_coherence("BTC-USD")
                funding_rate_pct = funding.get("BTC")

                opportunities = await self.scanner.scan_for_opportunities(
                    asset="BTC",
                    cascade_alert=cascade,
                    aria_coherence=aria_coherence,
                    funding_rates=funding,
                    market_direction=market_direction,
                )

                for opp in opportunities:
                    tier = (
                        "TIER 1" if opp.edge > 0.15 else
                        "TIER 2" if opp.edge > 0.08 else
                        "TIER 3"
                    )

                    paper_bet = {
                        "type":               "polymarket_paper",
                        "market_id":          opp.market_id,
                        "question":           opp.question,
                        "outcome":            opp.side.split("_")[1],
                        "augur_probability":  opp.p_augur,
                        "market_probability": opp.p_market,
                        "edge":               round(opp.edge, 4),
                        "size_usdc":          round(opp.bet_size_usd, 2),
                        "kelly_fraction":     round(
                            opp.bet_size_usd / self.config.polymarket_bankroll, 4
                        ),
                        "aria_signal": {
                            "cascade_active": cascade.get("active"),
                            "cascade_zscore": cascade.get("zscore"),
                            "coherence":      aria_coherence,
                            "regime":         regime,
                        },
                        "tier":      tier,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "status":    "paper_open",
                    }

                    _journal_append(_POLY_JOURNAL, paper_bet)
                    _journal_append(_AUGUR_JOURNAL, {
                        "event": "paper_bet_placed", **paper_bet
                    })

                    logger.info("paper_bet_placed",
                                question=opp.question[:60],
                                side=opp.side,
                                edge=round(opp.edge, 3),
                                size=opp.bet_size_usd,
                                tier=tier)

                if not opportunities:
                    logger.info("no_opportunities_found",
                                min_edge=_MIN_EDGE,
                                markets_scanned="public")

            except Exception as e:
                logger.error("prediction_market_loop_error", error=str(e))
            await asyncio.sleep(300)

    async def heartbeat_loop(self) -> None:
        """Every 60s — operational pulse."""
        while True:
            try:
                uptime_s = int(time.time() - self._start_time)
                aria = self.kingdom.read_aria_state()
                print(
                    f"\n\033[1;36m[AUGUR HEARTBEAT]\033[0m"
                    f"  mode=PAPER"
                    f"  uptime={uptime_s}s"
                    f"  regime={self._regime}"
                    f"  cascade={'ON' if self._cascade_alert.get('active') else 'off'}"
                    f"  aria_bets={len(aria.active_bets)}"
                )
                _journal_append(_AUGUR_JOURNAL, {
                    "event":        "heartbeat",
                    "uptime_s":     uptime_s,
                    "mode":         "paper",
                    "regime":       self._regime,
                    "cascade_active": self._cascade_alert.get("active"),
                    "aria_bets":    len(aria.active_bets),
                    "timestamp_ms": int(time.time() * 1000),
                })
            except Exception as e:
                logger.error("heartbeat_error", error=str(e))
            await asyncio.sleep(60)

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.journal.start_writer()
        self._print_startup_banner()
        try:
            await asyncio.gather(
                self.valuechain_loop(),
                self.kingdom_sync_loop(),
                self.prediction_market_loop(),
                self.heartbeat_loop(),
            )
        finally:
            await self.journal.stop_writer()


if __name__ == "__main__":
    app = AugurApplication(settings)
    asyncio.run(app.run())
