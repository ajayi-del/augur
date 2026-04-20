"""
PERP_MOMENTUM — Follow ARIA cascade on Bybit with cross-venue confirmation.

ARIA detects cascade aftermath on SoDEX.
AUGUR enters the same direction on Bybit after confirming via orderbook.

AUGUR does not copy ARIA blindly.
The Bybit orderbook confirmation gate filters false positives.
Both venues seeing the same pressure = maximum kingdom conviction.

Entry: ARIA active bet with coherence >= 6.0
       + Bybit cascade context active (confirms market is moving)
       + Bybit orderbook agg_ratio confirms direction
Venue: Bybit futures (via routing_client).
Size: Kelly × 0.60 — slightly larger because two data sources agree.
"""

import time
import structlog
from typing import Optional, List

from intelligence.strategies.perp_cascade import StrategySignal

logger = structlog.get_logger(__name__)

MIN_ARIA_COHERENCE = 6.0
MIN_AGG_CONFIRM    = 0.40   # agg_ratio < 0.40 = sellers dominate (short confirmed)
                             # agg_ratio > 0.60 = buyers dominate (long confirmed)
SIZE_FRACTION      = 0.60


class PerpMomentumStrategy:
    """
    Cross-venue momentum strategy.

    Three-gate filter:
      1. ARIA has active bet for this symbol with coherence >= 6.0
      2. Bybit cascade is active (zscore > 1.5) — market is moving
      3. Bybit orderbook agg_ratio confirms ARIA's direction

    When all three agree: both venues see the same truth.
    Chancellor sees compound signal. Kingdom earns maximum conviction.
    """

    def __init__(self, bybit_feed):
        self.bybit_feed = bybit_feed

    def evaluate(
        self,
        symbol:          str,
        aria_bets:       List,   # from kingdom.get_active_aria_bets(symbol)
        bybit_cascade:   Optional[dict],  # from kingdom.get_augur_data(f"bybit_cascade.{symbol}")
    ) -> Optional[StrategySignal]:
        """
        Evaluate ARIA bet + Bybit cascade + orderbook for momentum signal.
        Returns StrategySignal or None. Always logs one line.
        """
        now_ms = int(time.time() * 1000)

        # Gate 1: ARIA must have an active bet for this symbol
        if not aria_bets:
            logger.info("perp_momentum_no_aria_bet", symbol=symbol)
            return None

        best_bet = max(aria_bets, key=lambda b: b.coherence)

        if best_bet.coherence < MIN_ARIA_COHERENCE:
            logger.info("perp_momentum_weak_aria",
                        symbol=symbol,
                        coherence=round(best_bet.coherence, 2),
                        required=MIN_ARIA_COHERENCE)
            return None

        direction = best_bet.direction
        if direction not in ("long", "short"):
            return None

        # Gate 2: Bybit cascade must be active — confirms market structure is moving
        cascade_zscore = 0.0
        if not bybit_cascade or not bybit_cascade.get("active"):
            logger.info("perp_momentum_no_bybit_cascade",
                        symbol=symbol,
                        note="bybit_cascade_not_active_for_this_symbol")
            return None
        cascade_zscore = float(bybit_cascade.get("zscore", 0.0))

        # Gate 3: Bybit orderbook must confirm ARIA's direction
        agg_ratio = self.bybit_feed.get_agg_ratio(symbol)

        confirmed = (
            (direction == "short" and agg_ratio < MIN_AGG_CONFIRM) or
            (direction == "long"  and agg_ratio > (1.0 - MIN_AGG_CONFIRM))
        )

        if not confirmed:
            logger.info("perp_momentum_bybit_no_confirm",
                        symbol     = symbol,
                        direction  = direction,
                        agg_ratio  = round(agg_ratio, 3),
                        note       = "bybit_orderbook_does_not_confirm_aria")
            return None

        # All three gates pass — signal ready
        coherence_score = min(best_bet.coherence / 10.0, 0.40)
        if direction == "short":
            imbalance_score = min(max((MIN_AGG_CONFIRM - agg_ratio) * 2.0, 0.0), 0.25)
        else:
            imbalance_score = min(max((agg_ratio - 0.60) * 2.0, 0.0), 0.25)

        confidence = round(min(0.35 + coherence_score + imbalance_score, 0.85), 3)
        edge       = round(max(0.10 + (best_bet.coherence - MIN_ARIA_COHERENCE) * 0.01, 0.10), 4)

        signal = StrategySignal(
            symbol         = symbol,
            direction      = direction,
            strategy       = "PERP_MOMENTUM",
            cascade_zscore = cascade_zscore,
            edge           = edge,
            size_fraction  = SIZE_FRACTION,
            confidence     = confidence,
            timestamp_ms   = now_ms,
            expires_ms     = now_ms + 300_000,   # 5-min expiry — momentum persists longer
            metadata       = {
                "aria_coherence":        best_bet.coherence,
                "bybit_agg_ratio":       round(agg_ratio, 3),
                "bybit_cascade_zscore":  cascade_zscore,
                "cross_venue_confirmed": True,
            },
        )

        logger.info(
            "perp_momentum_signal_ready",
            symbol              = symbol,
            direction           = direction,
            aria_coherence      = round(best_bet.coherence, 2),
            bybit_agg_ratio     = round(agg_ratio, 3),
            cascade_zscore      = round(cascade_zscore, 2),
            confidence          = confidence,
            cross_venue_confirmed = True,
        )
        return signal
