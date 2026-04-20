import structlog
from typing import Optional, Tuple
from data.sosovalue_feed import ETFFlowData

logger = structlog.get_logger()


class NietzscheNewsConviction:
    """
    NIETZSCHE — The Conviction Scorer
    'The will to power must be measured against the will of the tape.'

    score() returns (conviction, direction, driver) for pipeline use.
    """

    def score(
        self,
        kant_weight: float,
        etf_flow: Optional[ETFFlowData],
        direction_from_news: str,
        aria_bet=None,              # Optional AgentBet dataclass
    ) -> Tuple[float, str, str]:
        """
        Returns (conviction: float 0-1, direction: str, driver: str).

        Weights:
          ARIA agreement   0.40
          Kant structural  0.25
          ETF flow         0.20
          Funding align    0.15
        """
        # 1. ARIA agreement (highest weight)
        if aria_bet is not None:
            aria_matches = (aria_bet.direction == direction_from_news
                            or direction_from_news == "neutral")
            aria_score = (1.0 if aria_matches else 0.0) * 0.40
            aria_coherence_bonus = min(aria_bet.coherence / 10.0, 1.0) * 0.10
        else:
            aria_score = 0.5 * 0.40
            aria_coherence_bonus = 0.0

        # 2. Structural weight (Kant)
        kant_score = kant_weight * 0.25

        # 3. ETF flow confirmation
        if etf_flow:
            flow_bullish = etf_flow.flow_direction in ("strong_inflow", "inflow")
            flow_bearish = etf_flow.flow_direction in ("strong_outflow", "outflow")
            if direction_from_news == "bullish" and flow_bullish:
                flow_score = 0.20
            elif direction_from_news == "bearish" and flow_bearish:
                flow_score = 0.20
            else:
                flow_score = 0.10
        else:
            flow_score = 0.10

        # 4. Funding/OI alignment proxy
        # (no live funding data here — use neutral 0.075)
        funding_score = 0.075

        conviction = aria_score + aria_coherence_bonus + kant_score + flow_score + funding_score
        conviction = max(0.0, min(1.0, conviction))

        # Determine dominant driver
        drivers = {
            "aria_agreement": aria_score + aria_coherence_bonus,
            "kant_structural": kant_score,
            "etf_flow": flow_score,
        }
        driver = max(drivers, key=lambda k: drivers[k])

        logger.debug(
            "nietzsche_conviction",
            conviction=round(conviction, 3),
            direction=direction_from_news,
            driver=driver,
        )

        return conviction, direction_from_news, driver

    # ── Legacy interface ─────────────────────────────────────────────────────

    def calculate_conviction(self, trade: dict, structural_weight: float) -> float:
        """Legacy method used by AUGUR main.py news_loop."""
        aria_agreement = trade.get("aria_signal_agreement", 0.5)
        aria_score = aria_agreement * 0.40

        kant_score = structural_weight * 0.20

        flow_confirmation = 1.0 if trade.get("flow_matches_bias") else 0.5
        flow_score = flow_confirmation * 0.15

        position_confirmation = trade.get("positioning_score", 0.5)
        pos_score = position_confirmation * 0.15

        ssi_agreement = 1.0 if trade.get("ssi_matches_bias") else 0.5
        ssi_score = ssi_agreement * 0.10

        conviction = aria_score + kant_score + flow_score + pos_score + ssi_score
        logger.debug("nietzsche_conviction_legacy", conviction=round(conviction, 3))
        return conviction

    def is_willful_action_required(self, conviction: float) -> bool:
        return conviction >= 0.40


# Backward-compat alias
NietzscheNews = NietzscheNewsConviction
