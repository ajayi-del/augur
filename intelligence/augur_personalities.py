"""
AUGUR Personality System.

Six personalities. Six philosophical stances.
Each expresses a different relationship with risk, time, and conviction.

ARIA has personalities that trade what IS.
AUGUR has personalities that trade what WILL BE.
But both use the same Kantian structure:
  Is the trade structurally sound?
  Is the will strong enough to act?

The personality determines character — not outcome.
Kant determines preconditions. Nietzsche determines will.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class AugurPersonality(Enum):
    ORACLE    = "oracle"     # Sees what others cannot yet see. Trades the future.
    SCOUT     = "scout"      # Finds what others overlook. First and careful.
    ARBITRAGE = "arbitrage"  # Sees the same truth priced differently. Harvests the gap.
    MOMENTUM  = "momentum"   # Rides the wave confirmed on two shores.
    SENTINEL  = "sentinel"   # The kingdom is under threat. Protect. Do not trade.
    HEDGER    = "hedger"     # My twin carries too much. I carry the other side.


@dataclass
class AugurSignal:
    """
    Raw signal state before personality/kant/nietzsche processing.
    Carries everything both philosophers need to evaluate.
    One signal object flows through the entire pipeline.
    """
    symbol:              str
    direction:           str    # long / short
    combined:            float  # weighted signal composite [0, 1]
    confidence:          float  # raw confidence before hist_wr calibration
    coherence:           float  # signal coherence score [0, 10]
    tps:                 float  # Solana TPS at signal time
    price_momentum_pct:  float  # 30s Bybit mark price change %
    agg_ratio:           float  # orderbook bid/(bid+ask) — 0=all sellers, 1=all buyers
    funding_rate:        float  # Bybit/Drift latest funding rate
    cascade_zscore:      float  # ARIA cascade z-score from kingdom
    timestamp_ms:        int
    narrative_age_hours: float  = 0.0   # 0 = no narrative; >0 = hours since event
    edge:                float  = 0.0   # abs(combined - 0.50) * 2 — probability edge


# All symbols ARIA tracks. Used by SCOUT personality detection.
_ARIA_UNIVERSE = {
    "SOL", "AVAX", "BNB", "SUI", "ARB", "OP", "MNT", "HYPE", "ENA",
    "NEAR", "APT", "INJ", "SEI", "TIA", "HBAR", "ATOM", "JUP", "WLD",
    "DOGE", "WIF", "BONK", "TRUMP", "PEPE", "CHILLGUY", "PIPPIN",
    "PIEVERSE", "EDGE",
}

# Size multipliers applied by Nietzsche after conviction calculation
PERSONALITY_SIZE_MULT = {
    AugurPersonality.ORACLE:    0.85,
    AugurPersonality.SCOUT:     0.40,
    AugurPersonality.ARBITRAGE: 1.20,
    AugurPersonality.MOMENTUM:  1.10,
    AugurPersonality.SENTINEL:  0.0,   # blocks all new positions
    AugurPersonality.HEDGER:    0.60,
}


def assign_personality(
    signal:                  AugurSignal,
    aria_drawdown:           float,
    calendar_block_active:   bool,
    bybit_divergence_pct:    float,
    bybit_funding_diff:      float,
    aria_max_position_usd:   float,
) -> AugurPersonality:
    """
    Priority-ordered personality assignment.

    Priority:
      1. SENTINEL  — survival: drawdown breach or calendar block
      2. ARBITRAGE — math: cross-venue divergence or funding differential
      3. MOMENTUM  — cascade: confirmed on ARIA (SoDEX) and AUGUR (Bybit)
      4. HEDGER    — protection: twin overexposed on one asset
      5. ORACLE    — narrative: fresh time-sensitive event signal
      6. SCOUT     — discovery: symbol outside ARIA universe
      7. MOMENTUM  — default: directional price action
    """
    # 1. SENTINEL: survival overrides everything
    if aria_drawdown > 0.03:
        logger.info("augur_personality_assigned",
                    personality="sentinel", reason="aria_drawdown_breach",
                    symbol=signal.symbol, aria_drawdown=round(aria_drawdown, 4))
        return AugurPersonality.SENTINEL

    if calendar_block_active:
        logger.info("augur_personality_assigned",
                    personality="sentinel", reason="calendar_block_active",
                    symbol=signal.symbol)
        return AugurPersonality.SENTINEL

    # 2. ARBITRAGE: pure mathematics
    if bybit_divergence_pct > 0.0025:
        logger.info("augur_personality_assigned",
                    personality="arbitrage", reason="cross_venue_divergence",
                    symbol=signal.symbol,
                    divergence_pct=round(bybit_divergence_pct, 5))
        return AugurPersonality.ARBITRAGE

    if bybit_funding_diff > 0.0005:
        logger.info("augur_personality_assigned",
                    personality="arbitrage", reason="funding_differential",
                    symbol=signal.symbol,
                    funding_diff=round(bybit_funding_diff, 5))
        return AugurPersonality.ARBITRAGE

    # 3. MOMENTUM: cascade confirmed on both venues
    if signal.cascade_zscore > 2.0 and signal.agg_ratio < 0.35:
        logger.info("augur_personality_assigned",
                    personality="momentum",
                    reason="cascade_confirmed_both_venues",
                    symbol=signal.symbol,
                    zscore=round(signal.cascade_zscore, 2),
                    agg_ratio=round(signal.agg_ratio, 3),
                    size_mult=1.10)
        return AugurPersonality.MOMENTUM

    # 4. HEDGER: twin overexposed
    if aria_max_position_usd > 150.0:
        logger.info("augur_personality_assigned",
                    personality="hedger", reason="aria_position_oversized",
                    symbol=signal.symbol,
                    aria_exposure=aria_max_position_usd)
        return AugurPersonality.HEDGER

    # 5. ORACLE: narrative signal within 4h window
    if 0 < signal.narrative_age_hours < 4.0:
        logger.info("augur_personality_assigned",
                    personality="oracle", reason="fresh_narrative_signal",
                    symbol=signal.symbol,
                    narrative_age_h=round(signal.narrative_age_hours, 2))
        return AugurPersonality.ORACLE

    # 6. SCOUT: unknown territory
    base_symbol = signal.symbol.replace("-USD", "").replace("-PERP", "")
    if base_symbol not in _ARIA_UNIVERSE:
        logger.info("augur_personality_assigned",
                    personality="scout", reason="symbol_outside_aria_universe",
                    symbol=signal.symbol)
        return AugurPersonality.SCOUT

    # 7. Default: MOMENTUM
    logger.info("augur_personality_assigned",
                personality="momentum", reason="default_directional",
                symbol=signal.symbol, size_mult=1.0)
    return AugurPersonality.MOMENTUM
