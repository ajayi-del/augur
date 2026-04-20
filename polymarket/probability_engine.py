import math
import time
import structlog
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Optional

logger = structlog.get_logger()


@dataclass
class PolymarketMarket:
    market_id: str
    question: str
    yes_price: float        # current market probability for YES
    hours_to_end: float
    liquidity_usdc: float
    end_date_ms: int


@dataclass
class AugurProbability:
    probability: float
    confidence: float
    lower_bound: float
    upper_bound: float
    signals_breakdown: Dict[str, float]
    n_signals: int = 0
    dominant_signal: str = "none"
    p_cascade: float = 0.5
    p_coherence: float = 0.5
    p_funding: float = 0.5
    p_history: float = 0.5


# ── Weights (must sum to 1.0) ─────────────────────────────────────────────────
# cascade=0.45  coherence=0.30  funding=0.15  history=0.10
WEIGHTS = {"cascade": 0.45, "coherence": 0.30, "funding": 0.15, "history": 0.10}


# ── Signal functions ─────────────────────────────────────────────────────────

def _cascade_signal(cascade_alert: Optional[dict], market_direction: str) -> float:
    """
    ARIA cascade alert → probability. Weight 0.45.
    Active cascades = forced liquidations = downward price pressure.
    """
    if not cascade_alert or not cascade_alert.get("active"):
        return 0.50
    zscore = float(cascade_alert.get("zscore", 0.0))
    if zscore <= 2.0:
        return 0.50
    # Cascade direction is bearish (liquidation = price drop)
    cascade_is_bearish = True
    market_is_short = (market_direction == "short")
    if cascade_is_bearish == market_is_short:
        return 0.50 + 0.15   # aligned with cascade → 0.65
    else:
        return 0.50 - 0.10   # against cascade → 0.40


def _coherence_signal(aria_coherence: Optional[float], market_hours: float) -> float:
    """
    ARIA coherence → probability. Weight 0.30.
    Applies horizon discount: high coherence matters less as expiry approaches.
    """
    if aria_coherence is None:
        return 0.50
    if aria_coherence > 7.0:
        base = 0.71
    elif aria_coherence > 6.0:
        base = 0.65
    elif aria_coherence > 5.0:
        base = 0.58
    else:
        base = 0.50
    discount = math.exp(-0.05 * market_hours)
    return base * discount + 0.50 * (1.0 - discount)


def _funding_signal(funding_rate_pct: Optional[float], market_direction: str) -> float:
    """
    Drift perpetual funding rate → probability. Weight 0.15.
    rate_pct is in percentage terms (e.g. -0.05 = -0.05%).
    """
    if funding_rate_pct is None:
        return 0.50
    if market_direction == "long":
        if funding_rate_pct < -0.05:
            return 0.50 + 0.08   # paid to be long → 0.58
        if funding_rate_pct > 0.10:
            return 0.50 - 0.08   # expensive to long → 0.42
    elif market_direction == "short":
        if funding_rate_pct > 0.10:
            return 0.50 + 0.08   # shorts receive funding → 0.58
        if funding_rate_pct < -0.05:
            return 0.50 - 0.08   # shorts pay funding → 0.42
    return 0.50


def _history_signal(historical_accuracy: Dict[str, float], aria_coherence: Optional[float]) -> float:
    """Look up historical accuracy by coherence bucket."""
    if aria_coherence is not None and historical_accuracy:
        coh_key = f"aria_coherence_{int(aria_coherence)}"
        if coh_key in historical_accuracy:
            return historical_accuracy[coh_key]
    return 0.50


# ── Standalone probability function ─────────────────────────────────────────

def compute_augur_probability(
    market: PolymarketMarket,
    cascade_alert: Optional[dict] = None,
    aria_coherence: Optional[float] = None,
    funding_rate_pct: Optional[float] = None,
    market_direction: str = "long",
    historical_accuracy: Optional[Dict[str, float]] = None,
) -> AugurProbability:
    """
    Synthesizes P(augur) from ValueChain signals.
    Weights: cascade=0.45 | coherence=0.30 | funding=0.15 | history=0.10
    """
    historical_accuracy = historical_accuracy or {}

    p_cascade  = _cascade_signal(cascade_alert, market_direction)
    p_coherence = _coherence_signal(aria_coherence, market.hours_to_end)
    p_funding  = _funding_signal(funding_rate_pct, market_direction)
    p_history  = _history_signal(historical_accuracy, aria_coherence)

    final_p = (
        p_cascade   * WEIGHTS["cascade"]
        + p_coherence * WEIGHTS["coherence"]
        + p_funding   * WEIGHTS["funding"]
        + p_history   * WEIGHTS["history"]
    )

    signals = [p_cascade, p_coherence, p_funding, p_history]
    signal_std = float(np.std(signals))
    confidence = max(0.0, min(1.0, 1.0 - signal_std * 3.0))
    margin = (1.0 - confidence) * 0.15

    breakdown = {
        "cascade":   round(p_cascade, 4),
        "coherence": round(p_coherence, 4),
        "funding":   round(p_funding, 4),
        "history":   round(p_history, 4),
    }

    n_signals = sum(1 for s in signals if abs(s - 0.5) > 0.05)
    deltas = {k: abs(v - 0.5) for k, v in zip(WEIGHTS.keys(), signals)}
    dominant = max(deltas, key=lambda k: deltas[k])

    return AugurProbability(
        probability=round(final_p, 4),
        confidence=round(confidence, 4),
        lower_bound=round(max(0.0, final_p - margin), 4),
        upper_bound=round(min(1.0, final_p + margin), 4),
        signals_breakdown=breakdown,
        n_signals=n_signals,
        dominant_signal=dominant,
        p_cascade=p_cascade,
        p_coherence=p_coherence,
        p_funding=p_funding,
        p_history=p_history,
    )


# ── Class wrapper (used by main.py / MarketScanner) ──────────────────────────

class ProbabilityEngine:
    def __init__(self):
        self.historical_accuracy: Dict[str, float] = {}

    def compute_augur_probability(
        self,
        market_id: str,
        target_asset: str,
        expiry_timestamp: int,
        yes_price: float = 0.50,
        liquidity_usdc: float = 0.0,
        cascade_alert: Optional[dict] = None,
        aria_coherence: Optional[float] = None,
        funding_rate_pct: Optional[float] = None,
        market_direction: str = "long",
    ) -> AugurProbability:
        now = time.time()
        hours_to_end = max(0.0, (expiry_timestamp - now) / 3600.0)

        market = PolymarketMarket(
            market_id=market_id,
            question=f"{target_asset} market",
            yes_price=yes_price,
            hours_to_end=hours_to_end,
            liquidity_usdc=liquidity_usdc,
            end_date_ms=int(expiry_timestamp * 1000),
        )
        return compute_augur_probability(
            market=market,
            cascade_alert=cascade_alert,
            aria_coherence=aria_coherence,
            funding_rate_pct=funding_rate_pct,
            market_direction=market_direction,
            historical_accuracy=self.historical_accuracy,
        )
