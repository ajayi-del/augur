import structlog
from typing import Optional, Tuple
from data.sosovalue_feed import ETFFlowData

logger = structlog.get_logger()


class NewsCoherenceScorer:
    """
    NEWS COHERENCE SCORER
    Produces AUGUR's signal coherence score on a 0.0–10.0 scale.

    Minimal required signature for the checkpoint pipeline:
        score(kant_weight, nietzsche_conviction, etf_flow, direction)
        → (score, size_multiplier, reason)

    Full signature (legacy AUGUR main.py) additionally accepts
    drift_oi, drift_funding, ssi_direction, calendar_state, kingdom_state.
    All extra args default to neutral values.
    """

    def score(
        self,
        kant_weight: float,
        nietzsche_conviction: float,
        etf_flow: Optional[ETFFlowData] = None,
        direction: str = "long",
        # Optional extras — kept for backward compat with old callers
        drift_oi: Optional[dict] = None,
        drift_funding: float = 0.0,
        ssi_direction: str = "neutral",
        calendar_state: str = "CLEAR",
        kingdom_state: Optional[dict] = None,
    ) -> Tuple[float, float, str]:
        """
        Returns (coherence_score 0-10, size_multiplier, reason_str).
        """
        # 1. Kant information quality (0 → 2.7)
        kant_score = kant_weight * 3.0

        # 2. Nietzsche conviction (0 → 2.5)
        nietz_score = nietzsche_conviction * 2.5

        # 3. ETF flow (-1.0 → 2.0)
        etf_score = 0.5
        if etf_flow:
            mapping = {
                "strong_inflow": 2.0,
                "inflow": 1.5,
                "neutral": 0.5,
                "outflow": -0.5,
                "strong_outflow": -1.0,
            }
            etf_score = mapping.get(etf_flow.flow_direction, 0.5)

        # 4. OI alignment (0 → 1.5)
        oi_score = 0.75
        if drift_oi:
            oi_ratio = drift_oi.get("oi_ratio", 1.0)
            if direction == "long" and oi_ratio < 1.0:
                oi_score = 1.5
            elif direction == "long" and oi_ratio > 1.5:
                oi_score = 0.0

        # 5. Calendar gate (can be negative blocker)
        cal_score = 1.0
        if calendar_state == "CAUTION":
            cal_score = 0.5
        elif calendar_state == "BLOCK":
            cal_score = -10.0

        total = max(kant_score + nietz_score + etf_score + oi_score + cal_score, 0.0)

        if total >= 8.0:   size_mult = 1.5
        elif total >= 6.5: size_mult = 1.2
        elif total >= 5.0: size_mult = 1.0
        elif total >= 4.0: size_mult = 0.7
        else:              size_mult = 0.0

        reason = (
            f"kant={kant_score:.2f} nietz={nietz_score:.2f} "
            f"etf={etf_score:.2f} oi={oi_score:.2f} cal={cal_score:.2f}"
        )
        logger.debug("news_coherence_scored", score=round(total, 2), direction=direction)
        return total, size_mult, reason
