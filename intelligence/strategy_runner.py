"""
StrategyRunner — AUGUR's 30-second execution heartbeat.

Two strategies evaluated every 30s across 12 symbols:
  PERP_CASCADE  — independent Bybit liquidation cascade signals
  PERP_MOMENTUM — ARIA cascade + Bybit orderbook confirmed signals

Every signal evaluation logs one line so you can see AUGUR thinking.
strategy_cycle_complete fires every 30s without exception.
Never crashes. Always continues.
"""

import asyncio
import time
import structlog
from typing import Callable

from intelligence.strategies.perp_cascade import PerpCascadeStrategy, StrategySignal
from intelligence.strategies.perp_momentum import PerpMomentumStrategy

logger = structlog.get_logger(__name__)

EVAL_INTERVAL_S = 30

# Symbols must be in "SOL-USD" format — matching BybitCascadeEngine and BybitFeed
SYMBOLS = [
    "SOL-USD",  "ETH-USD",  "BTC-USD",  "NEAR-USD",
    "ARB-USD",  "SUI-USD",  "AVAX-USD", "BNB-USD",
    "OP-USD",   "DOGE-USD", "INJ-USD",  "PEPE-USD",
]

_MIN_SIZE_USD = 10.0


class StrategyRunner:
    """
    Evaluates all AUGUR strategies every 30 seconds.
    Routes signals through Chancellor. Executes on Bybit.
    Logs one line per strategy evaluation — AUGUR thinking is always visible.
    """

    def __init__(
        self,
        bybit_feed,
        kingdom,
        chancellor,
        router,
        get_balance:    Callable[[], float],   # lambda: self._cached_balance
        get_daily_loss: Callable[[], float],   # lambda: self._daily_loss_pct
    ):
        self.bybit_feed   = bybit_feed
        self.kingdom      = kingdom
        self.chancellor   = chancellor
        self.router       = router
        self.get_balance  = get_balance
        self.get_daily_loss = get_daily_loss

        self.cascade_strategy  = PerpCascadeStrategy()
        self.momentum_strategy = PerpMomentumStrategy(bybit_feed)

    async def run_forever(self) -> None:
        logger.info(
            "strategy_runner_started",
            strategies = ["PERP_CASCADE", "PERP_MOMENTUM"],
            symbols    = len(SYMBOLS),
            interval_s = EVAL_INTERVAL_S,
        )
        while True:
            try:
                await self._evaluate_all()
            except Exception as e:
                logger.warning("strategy_runner_error", error=str(e))
            await asyncio.sleep(EVAL_INTERVAL_S)

    async def _evaluate_all(self) -> None:
        now_ms        = int(time.time() * 1000)
        cycle_start   = time.time()
        signals_found = 0
        trades_placed = 0

        aria_state    = self.kingdom.read_aria_state()
        aria_drawdown = getattr(aria_state, "drawdown", 0.0) or 0.0
        balance       = self.get_balance()
        daily_loss    = self.get_daily_loss()

        for symbol in SYMBOLS:

            # ── PERP_CASCADE ───────────────────────────────────────────────────
            try:
                cascade_data = self.kingdom.get_augur_data(f"bybit_cascade.{symbol}")
                if cascade_data:
                    signal = self.cascade_strategy.evaluate(symbol, cascade_data)
                    if signal:
                        signals_found += 1
                        placed = await self._route_and_execute(
                            signal, aria_state, aria_drawdown, daily_loss, balance,
                        )
                        if placed:
                            trades_placed += 1
                else:
                    logger.info("perp_cascade_no_data",
                                symbol=symbol, note="cascade_engine_not_seen_liquidations_yet")
            except Exception as e:
                logger.warning("strategy_eval_error",
                               symbol=symbol, strategy="PERP_CASCADE", error=str(e))

            # ── PERP_MOMENTUM ──────────────────────────────────────────────────
            try:
                aria_bets      = self.kingdom.get_active_aria_bets(symbol)
                bybit_cascade  = self.kingdom.get_augur_data(f"bybit_cascade.{symbol}")
                signal = self.momentum_strategy.evaluate(symbol, aria_bets, bybit_cascade)
                if signal:
                    signals_found += 1
                    placed = await self._route_and_execute(
                        signal, aria_state, aria_drawdown, daily_loss, balance,
                    )
                    if placed:
                        trades_placed += 1
            except Exception as e:
                logger.warning("strategy_eval_error",
                               symbol=symbol, strategy="PERP_MOMENTUM", error=str(e))

        elapsed_ms = (time.time() - cycle_start) * 1000
        logger.info(
            "strategy_cycle_complete",
            symbols_evaluated = len(SYMBOLS),
            signals_found     = signals_found,
            trades_placed     = trades_placed,
            elapsed_ms        = round(elapsed_ms, 1),
        )

    async def _route_and_execute(
        self,
        signal:       StrategySignal,
        aria_state,
        aria_drawdown: float,
        daily_loss:    float,
        balance:       float,
    ) -> bool:
        """Route signal through Chancellor and execute if approved. Returns True if order placed."""
        symbol = signal.symbol

        # ARIA context for Chancellor agreement classification
        aria_bets = self.kingdom.get_active_aria_bets(symbol)
        aria_bet  = max(aria_bets, key=lambda b: b.coherence) if aria_bets else None
        aria_dir  = aria_bet.direction if aria_bet else None
        aria_coh  = aria_bet.coherence if aria_bet else 0.0

        total_exp, sym_exp = self._get_exposure_pcts(symbol, balance)

        decision = self.chancellor.adjudicate(
            aria_direction       = aria_dir,
            aria_coherence       = aria_coh,
            augur_direction      = signal.direction,
            augur_conviction     = signal.confidence,
            aria_drawdown        = aria_drawdown,
            daily_loss_pct       = daily_loss,
            cascade_zscore       = signal.cascade_zscore,
            total_exposure_pct   = total_exp,
            symbol_exposure_pct  = sym_exp,
            balance              = balance,
        )

        # ONE log line per strategy evaluation — always fires whether approved or not
        logger.info(
            "strategy_evaluated",
            symbol        = symbol,
            strategy      = signal.strategy,
            direction     = signal.direction,
            confidence    = round(signal.confidence, 3),
            chancellor    = decision.action,
            size_modifier = round(decision.size_modifier, 2),
            reason        = decision.reason,
        )

        if not decision.augur_executes:
            return False

        if balance < _MIN_SIZE_USD:
            logger.info("strategy_insufficient_balance",
                        symbol=symbol, balance=round(balance, 2), min_required=_MIN_SIZE_USD)
            return False

        # Kelly-fractional sizing: edge × balance × fraction × chancellor modifier
        # Floor at $10 so AUGUR scales from minimal balance upward
        kelly_base = signal.edge * balance * signal.size_fraction
        final_size = kelly_base * decision.size_modifier
        final_size = round(max(final_size, _MIN_SIZE_USD), 2)

        # TP from mark price — CASCADE 1.5%, MOMENTUM 2.0%
        mark = self.bybit_feed.get_mark_price(symbol)
        tp_pct = 0.015 if signal.strategy == "PERP_CASCADE" else 0.020
        if mark > 0:
            tp1 = round(mark * (1 + tp_pct) if signal.direction == "long"
                        else mark * (1 - tp_pct), 6)
        else:
            tp1 = 0.0

        try:
            result = await self.router.place_order(
                symbol    = symbol,
                direction = signal.direction,
                size_usd  = final_size,
                leverage  = 5,
                tp1       = tp1,
            )
            logger.info(
                "strategy_trade_placed",
                symbol        = symbol,
                strategy      = signal.strategy,
                direction     = signal.direction,
                size_usd      = round(final_size, 2),
                tp1           = round(tp1, 4) if tp1 else None,
                order_id      = result.order_id,
                venue         = result.venue,
                chancellor    = decision.action,
                size_modifier = round(decision.size_modifier, 2),
            )
            self.kingdom.write_position(
                agent_id  = "augur",
                symbol    = symbol,
                direction = signal.direction,
                size_usd  = final_size,
                venue     = result.venue,
            )
            return True
        except Exception as e:
            logger.error("strategy_execution_error",
                         symbol=symbol, strategy=signal.strategy, error=str(e))
            return False

    def _get_exposure_pcts(self, symbol: str, balance: float):
        """Compute total and per-symbol exposure as fractions of balance."""
        try:
            state      = self.kingdom.read()
            registry   = state.position_registry
            now_ms     = int(time.time() * 1000)
            all_pos    = [
                p for positions in registry.values()
                for p in positions
                if now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000
            ]
            total_usd  = sum(p.get("size_usd", 0) for p in all_pos)
            symbol_usd = sum(p.get("size_usd", 0) for p in all_pos
                             if p.get("symbol") == symbol)
            bal = max(balance, 1.0)
            return (total_usd / bal, symbol_usd / bal)
        except Exception:
            return (0.0, 0.0)
