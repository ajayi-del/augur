"""
PERP_CASCADE — Independent Bybit liquidation cascade strategy.

Bybit is 10x larger than SoDEX.
When Bybit liquidations cascade: SoDEX follows within 200–800ms.
AUGUR exploits this propagation lag.

Entry: Bybit liquidation zscore >= 2.5, clear direction, not exhaustion.
Venue: Bybit futures (via routing_client).
Direction: same as cascade (bullish→long, bearish→short).
Size: Kelly × 0.50 — single-agent, no ARIA confirmation required.

AUGUR acts on its own intelligence here.
When ARIA confirms later: Chancellor upgrades to compound at 1.25×.
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import Optional

logger = structlog.get_logger(__name__)

MIN_ZSCORE    = 2.5
MIN_NOTIONAL  = 500_000   # $500k liquidated in 60s — institutional, not noise
SIZE_FRACTION = 0.50      # 50% Kelly when trading without ARIA confirmation


@dataclass
class StrategySignal:
    """Unified signal format for all AUGUR strategies."""
    symbol:         str
    direction:      str    # "long" | "short"
    strategy:       str    # "PERP_CASCADE" | "PERP_MOMENTUM"
    cascade_zscore: float
    edge:           float  # Kelly fraction approximation
    size_fraction:  float
    confidence:     float  # [0, 1]
    timestamp_ms:   int
    expires_ms:     int
    metadata:       dict = field(default_factory=dict)


class PerpCascadeStrategy:
    """
    AUGUR's primary independent strategy.

    Reads Bybit cascade state from kingdom (published by BybitCascadeEngine).
    Gates signal on statistical significance and directional clarity.
    Returns StrategySignal when cascade is real and tradeable.
    """

    def evaluate(self, symbol: str, cascade: dict) -> Optional[StrategySignal]:
        """
        Evaluate cascade data for a single symbol.
        cascade: dict from kingdom.get_augur_data("bybit_cascade.{symbol}")
        Returns StrategySignal or None. Always logs one line.
        """
        now_ms        = int(time.time() * 1000)
        zscore        = float(cascade.get("zscore", 0.0))
        notional      = float(cascade.get("notional_usd", 0.0))
        direction_raw = cascade.get("direction", "mixed")
        phase         = cascade.get("phase", "quiet")

        # Skip if BybitCascadeEngine already fired an independent trade — avoid double execution
        if cascade.get("independent_lead") and cascade.get("expires_ms", 0) > now_ms:
            logger.info("perp_cascade_engine_lead_active",
                        symbol=symbol, zscore=round(zscore, 2),
                        note="cascade_engine_already_handling")
            return None

        if zscore < MIN_ZSCORE:
            logger.info("perp_cascade_below_threshold",
                        symbol=symbol, zscore=round(zscore, 2), required=MIN_ZSCORE)
            return None

        if notional < MIN_NOTIONAL:
            logger.info("perp_cascade_low_notional",
                        symbol=symbol, notional_usd=round(notional, 0), required=MIN_NOTIONAL)
            return None

        if direction_raw == "mixed":
            logger.info("perp_cascade_mixed_direction", symbol=symbol, zscore=round(zscore, 2))
            return None

        if phase == "exhaustion":
            logger.info("perp_cascade_exhaustion_skip",
                        symbol=symbol, note="exhaustion_phase_reversal_only")
            return None

        direction  = "long" if direction_raw == "bullish" else "short"
        edge       = round(
            min(0.08 + (zscore - MIN_ZSCORE) * 0.02 + (0.03 if phase == "expansion" else 0.0), 0.17),
            4,
        )
        confidence = round(min(0.45 + min(zscore / 10.0, 0.20), 0.90), 3)

        signal = StrategySignal(
            symbol         = symbol,
            direction      = direction,
            strategy       = "PERP_CASCADE",
            cascade_zscore = zscore,
            edge           = edge,
            size_fraction  = SIZE_FRACTION,
            confidence     = confidence,
            timestamp_ms   = now_ms,
            expires_ms     = now_ms + 120_000,   # 2-min expiry — fast cascade
            metadata       = {
                "bybit_liq_60s":  cascade.get("liq_60s", 0),
                "bybit_notional": notional,
                "bybit_phase":    phase,
                "cascade_score":  cascade.get("cascade_score", 0.0),
            },
        )

        logger.info(
            "perp_cascade_signal_ready",
            symbol     = symbol,
            direction  = direction,
            zscore     = round(zscore, 2),
            phase      = phase,
            notional   = round(notional, 0),
            confidence = confidence,
            edge       = edge,
        )
        return signal
