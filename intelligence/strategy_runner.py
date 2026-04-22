"""
StrategyRunner — AUGUR's 30-second execution heartbeat.

Three strategies evaluated every 30s across 12 symbols:
  PERP_CASCADE   — independent Bybit liquidation cascade signals
  PERP_MOMENTUM  — ARIA cascade + Bybit orderbook confirmed signals
  SMART_MONEY    — DeepSeek hot signal (cluster/scalper/whale entry)

Intelligence layer:
  cold signals (6h) → confidence boost + leverage recommendation
  hot signals (15m) → quick trade trigger + elevated leverage + size multiplier
  Nietzsche selects leverage 5–15× based on wallet conviction.

Max 8 open trades. Min $60 notional. Dynamic sizing with intel multiplier.
"""

import asyncio
import time
import structlog
from dataclasses import dataclass, field
from typing import Callable, Optional

from intelligence.strategies.perp_cascade import PerpCascadeStrategy, StrategySignal
from intelligence.strategies.perp_momentum import PerpMomentumStrategy

logger = structlog.get_logger(__name__)

EVAL_INTERVAL_S = 30

SYMBOLS = [
    "SOL-USD",  "ETH-USD",  "BTC-USD",  "NEAR-USD",
    "ARB-USD",  "SUI-USD",  "AVAX-USD", "BNB-USD",
    "OP-USD",   "DOGE-USD", "INJ-USD",  "PEPE-USD",
]

_MIN_SIZE_USD   = 60.0    # minimum notional per trade
_MAX_OPEN       = 8       # global position cap


class StrategyRunner:
    """
    Evaluates all AUGUR strategies every 30 seconds.
    Routes signals through Chancellor. Executes on Bybit.
    """

    def __init__(
        self,
        bybit_feed,
        kingdom,
        chancellor,
        router,
        get_balance:    Callable[[], float],
        get_daily_loss: Callable[[], float],
        intel_agent = None,
    ):
        self.bybit_feed    = bybit_feed
        self.kingdom       = kingdom
        self.chancellor    = chancellor
        self.router        = router
        self.get_balance   = get_balance
        self.get_daily_loss = get_daily_loss
        self.intel_agent   = intel_agent

        self.cascade_strategy  = PerpCascadeStrategy()
        self.momentum_strategy = PerpMomentumStrategy(bybit_feed)

    async def run_forever(self) -> None:
        logger.info(
            "strategy_runner_started",
            strategies = ["PERP_CASCADE", "PERP_MOMENTUM", "SMART_MONEY"],
            symbols    = len(SYMBOLS),
            interval_s = EVAL_INTERVAL_S,
            min_usd    = _MIN_SIZE_USD,
            max_open   = _MAX_OPEN,
        )
        while True:
            try:
                await self._evaluate_all()
            except Exception as e:
                logger.warning("strategy_runner_error", error=str(e))
            await asyncio.sleep(EVAL_INTERVAL_S)

    async def _evaluate_all(self) -> None:
        cycle_start   = time.time()
        signals_found = 0
        trades_placed = 0

        aria_state    = self.kingdom.read_aria_state()
        aria_drawdown = getattr(aria_state, "drawdown", 0.0) or 0.0
        balance       = self.get_balance()
        daily_loss    = self.get_daily_loss()

        # Global position cap — bail early if full
        open_count = self.kingdom.count_open_positions("augur")
        if open_count >= _MAX_OPEN:
            logger.info("strategy_max_positions_reached",
                        open=open_count, cap=_MAX_OPEN)
            return

        for symbol in SYMBOLS:
            if open_count >= _MAX_OPEN:
                break

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
                            open_count += 1
                else:
                    logger.info("perp_cascade_no_data", symbol=symbol)
            except Exception as e:
                logger.warning("strategy_eval_error",
                               symbol=symbol, strategy="PERP_CASCADE", error=str(e))

            if open_count >= _MAX_OPEN:
                break

            # ── PERP_MOMENTUM ──────────────────────────────────────────────────
            try:
                aria_bets     = self.kingdom.get_active_aria_bets(symbol)
                bybit_cascade = self.kingdom.get_augur_data(f"bybit_cascade.{symbol}")
                signal = self.momentum_strategy.evaluate(symbol, aria_bets, bybit_cascade)
                if signal:
                    signals_found += 1
                    placed = await self._route_and_execute(
                        signal, aria_state, aria_drawdown, daily_loss, balance,
                    )
                    if placed:
                        trades_placed += 1
                        open_count += 1
            except Exception as e:
                logger.warning("strategy_eval_error",
                               symbol=symbol, strategy="PERP_MOMENTUM", error=str(e))

            if open_count >= _MAX_OPEN:
                break

            # ── SMART_MONEY — hot signal from DeepSeek cluster/scalper detection ──
            try:
                placed = await self._evaluate_hot_signal(
                    symbol, aria_state, aria_drawdown, daily_loss, balance,
                )
                if placed:
                    trades_placed += 1
                    signals_found += 1
                    open_count += 1
            except Exception as e:
                logger.warning("strategy_eval_error",
                               symbol=symbol, strategy="SMART_MONEY", error=str(e))

        elapsed_ms = (time.time() - cycle_start) * 1000
        logger.info(
            "strategy_cycle_complete",
            symbols_evaluated = len(SYMBOLS),
            signals_found     = signals_found,
            trades_placed     = trades_placed,
            open_positions    = open_count,
            elapsed_ms        = round(elapsed_ms, 1),
        )

    async def _evaluate_hot_signal(
        self,
        symbol:        str,
        aria_state,
        aria_drawdown: float,
        daily_loss:    float,
        balance:       float,
    ) -> bool:
        """
        Fire a SMART_MONEY trade when DeepSeek hot signal exists and:
          - conviction >= 0.65 (strong enough to act without cascade confirmation)
          - direction matches ARIA active bet OR regime OR cascade direction
          - no existing AUGUR position on this symbol
        """
        if self.intel_agent is None:
            return False

        hot = self.intel_agent.get_hot_signal(symbol)
        if hot is None or hot.direction == "neutral":
            return False
        if hot.conviction < 0.65:
            return False

        # Don't double-trade if AUGUR already has a position on this symbol
        if self._has_open_position(symbol):
            return False

        now_ms = int(time.time() * 1000)

        # Build a synthetic StrategySignal from the hot signal
        cascade_zscore = 0.0
        try:
            cd = self.kingdom.get_augur_data(f"bybit_cascade.{symbol}")
            if cd:
                cascade_zscore = float(cd.get("zscore", 0.0))
        except Exception:
            pass

        # Confidence base from hot signal conviction, boosted by cold signal
        confidence = round(min(hot.conviction * 0.80 + hot.confidence_boost, 0.85), 3)
        cold = self.intel_agent.get_signal(symbol)
        if cold and cold.direction == hot.direction and cold.conviction >= 0.50:
            confidence = round(min(confidence + cold.confidence_boost * 0.5, 0.85), 3)

        signal = StrategySignal(
            symbol         = symbol,
            direction      = hot.direction,
            strategy       = "SMART_MONEY",
            cascade_zscore = cascade_zscore,
            edge           = round(min(hot.conviction * 0.12, 0.15), 4),
            size_fraction  = min(0.50 * hot.size_multiplier, 1.0),
            confidence     = confidence,
            timestamp_ms   = now_ms,
            expires_ms     = hot.expires_ms,
            metadata       = {
                "trigger":       hot.trigger,
                "wallet_count":  hot.wallet_count,
                "total_size_usd": hot.total_size_usd,
                "leverage_rec":  hot.leverage_rec,
                "reasoning":     hot.reasoning[:100],
            },
        )

        logger.info(
            "smart_money_signal_ready",
            symbol       = symbol,
            direction    = hot.direction,
            trigger      = hot.trigger,
            conviction   = hot.conviction,
            wallet_count = hot.wallet_count,
            total_usd    = hot.total_size_usd,
            leverage_rec = hot.leverage_rec,
            confidence   = confidence,
        )

        return await self._route_and_execute(
            signal, aria_state, aria_drawdown, daily_loss, balance,
            leverage_override = hot.leverage_rec,
        )

    async def _route_and_execute(
        self,
        signal:           StrategySignal,
        aria_state,
        aria_drawdown:    float,
        daily_loss:       float,
        balance:          float,
        leverage_override: Optional[int] = None,
    ) -> bool:
        symbol = signal.symbol

        aria_bets = self.kingdom.get_active_aria_bets(symbol)
        aria_bet  = max(aria_bets, key=lambda b: b.coherence) if aria_bets else None
        aria_dir  = aria_bet.direction if aria_bet else None
        aria_coh  = aria_bet.coherence if aria_bet else 0.0

        total_exp, sym_exp = self._get_exposure_pcts(symbol, balance)

        # ── Intelligence boost (cold signal, 6h) ──────────────────────────────
        # Hot signal boost already baked into SMART_MONEY signal confidence above.
        # For CASCADE and MOMENTUM, apply cold boost here.
        intel_size_mult = 1.0
        base_leverage   = 5

        if self.intel_agent is not None and signal.strategy != "SMART_MONEY":
            cold = self.intel_agent.get_signal(symbol)
            if (cold is not None and
                    cold.direction == signal.direction and
                    cold.conviction >= 0.50 and
                    cold.confidence_boost > 0.0):
                signal.confidence = round(
                    min(signal.confidence + cold.confidence_boost, 0.85), 3
                )
                base_leverage = cold.leverage_rec
                logger.info(
                    "cold_intel_boost",
                    symbol     = symbol,
                    boost      = cold.confidence_boost,
                    confidence = signal.confidence,
                    leverage   = base_leverage,
                    wallets    = cold.wallet_count,
                )

            # Hot signal on same direction upgrades leverage + size even for non-SMART_MONEY
            hot = self.intel_agent.get_hot_signal(symbol)
            if (hot is not None and
                    hot.direction == signal.direction and
                    hot.conviction >= 0.55):
                base_leverage   = max(base_leverage, hot.leverage_rec)
                intel_size_mult = hot.size_multiplier
                logger.info(
                    "hot_intel_size_leverage_upgrade",
                    symbol      = symbol,
                    leverage    = base_leverage,
                    size_mult   = intel_size_mult,
                    trigger     = hot.trigger,
                    wallet_count = hot.wallet_count,
                )

        # Nietzsche leverage selection: use override (SMART_MONEY) or computed above
        final_leverage = int(leverage_override if leverage_override else base_leverage)
        final_leverage = max(5, min(final_leverage, 15))

        # Leverage cap: 8-10x ONLY when ARIA agrees with a SMART_MONEY signal.
        # AUGUR-only trades (no ARIA confirmation or non-SMART_MONEY) capped at 7x.
        aria_agrees_with_smart_money = (
            signal.strategy == "SMART_MONEY"
            and aria_dir is not None
            and aria_dir == signal.direction
        )
        if not aria_agrees_with_smart_money:
            final_leverage = min(final_leverage, 7)

        # Institutional signal: SMART_MONEY always has it; CASCADE/MOMENTUM check hot signal
        has_institutional = (
            signal.strategy == "SMART_MONEY"
            or (
                self.intel_agent is not None
                and (lambda h: h is not None
                               and h.direction == signal.direction
                               and h.conviction >= 0.65)(
                    self.intel_agent.get_hot_signal(symbol)
                )
            )
        )

        decision = self.chancellor.adjudicate(
            aria_direction            = aria_dir,
            aria_coherence            = aria_coh,
            augur_direction           = signal.direction,
            augur_conviction          = signal.confidence * 10.0,  # normalised 0-10 to match ARIA coherence scale
            aria_drawdown             = daily_loss,   # AUGUR tracks own P&L, not ARIA balance
            daily_loss_pct            = daily_loss,
            cascade_zscore            = signal.cascade_zscore,
            total_exposure_pct        = total_exp,
            symbol_exposure_pct       = sym_exp,
            balance                   = balance,
            has_institutional_signal  = has_institutional,
        )

        logger.info(
            "strategy_evaluated",
            symbol        = symbol,
            strategy      = signal.strategy,
            direction     = signal.direction,
            confidence    = round(signal.confidence, 3),
            leverage      = final_leverage,
            chancellor    = decision.action,
            size_modifier = round(decision.size_modifier, 2),
            reason        = decision.reason,
        )

        if not decision.augur_executes:
            return False

        effective_notional = balance * final_leverage
        if effective_notional < _MIN_SIZE_USD:
            logger.info("strategy_insufficient_balance",
                        symbol=symbol, balance=round(balance, 2),
                        leverage=final_leverage, effective=round(effective_notional, 2),
                        min_required=_MIN_SIZE_USD)
            return False

        # Dynamic Kelly sizing with intel multiplier
        kelly_base = signal.edge * balance * signal.size_fraction
        raw_size   = kelly_base * decision.size_modifier * intel_size_mult
        final_size = round(max(raw_size, _MIN_SIZE_USD), 2)

        # TP/SL percentages by strategy
        tp_pcts = {
            "PERP_CASCADE":  0.015,    # 1.5%
            "PERP_MOMENTUM": 0.020,    # 2.0%
            "SMART_MONEY":   0.012,    # 1.2% — scalper signals need tighter TP
        }
        sl_pct  = 0.010    # 1.0% SL — tight enough to protect, loose enough vs noise
        tp_pct  = tp_pcts.get(signal.strategy, 0.015)
        mark    = self.bybit_feed.get_mark_price(symbol)
        if mark > 0:
            is_long = signal.direction == "long"
            tp1 = round(mark * (1 + tp_pct) if is_long else mark * (1 - tp_pct), 6)
            sl1 = round(mark * (1 - sl_pct) if is_long else mark * (1 + sl_pct), 6)
        else:
            tp1 = 0.0
            sl1 = 0.0

        try:
            result = await self.router.place_order(
                symbol    = symbol,
                direction = signal.direction,
                size_usd  = final_size,
                leverage  = final_leverage,
                entry     = mark if mark > 0 else 0.0,  # needed for correct qty calc
                tp1       = tp1,
                stop      = sl1,
            )
            logger.info(
                "strategy_trade_placed",
                symbol        = symbol,
                strategy      = signal.strategy,
                direction     = signal.direction,
                size_usd      = round(final_size, 2),
                leverage      = final_leverage,
                entry         = round(mark, 4) if mark else None,
                tp1           = round(tp1, 4) if tp1 else None,
                sl1           = round(sl1, 4) if sl1 else None,
                order_id      = result.order_id,
                venue         = result.venue,
                chancellor    = decision.action,
                intel_mult    = round(intel_size_mult, 2),
                aria_agrees   = aria_agrees_with_smart_money,
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

    def _has_open_position(self, symbol: str) -> bool:
        try:
            state  = self.kingdom.read()
            now_ms = int(time.time() * 1000)
            for p in state.position_registry.get("augur", []):
                if (p.get("symbol") == symbol and
                        now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000):
                    return True
        except Exception:
            pass
        return False

    def _get_exposure_pcts(self, symbol: str, balance: float):
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
