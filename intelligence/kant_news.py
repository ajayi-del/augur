import structlog
from typing import List, Optional, Tuple
from data.sosovalue_feed import NewsItem, ETFFlowData

logger = structlog.get_logger()


class KantNewsFilter:
    """
    KANT — The Structural Validator
    Epistemic gatekeeper that enforces deterministic logic over narrative flows.
    'Categories are the conditions of the possibility of experience.'

    evaluate() returns (is_significant, weight, reason, direction) for pipeline use.
    """

    INSTITUTIONAL_CATEGORIES = [3, 5, 6]
    MAX_HOURS_OLD = 4.0

    def evaluate(
        self,
        news_item: NewsItem,
        etf_flow: Optional[ETFFlowData] = None,
    ) -> Tuple[bool, float, str, str]:
        """
        Pipeline-facing method.
        Returns (is_significant, weight, reason, direction).
        """
        is_sig, weight, reason = self.validate_structural_soundness(news_item, etf_flow)
        return is_sig, weight, reason, news_item.direction

    def validate_structural_soundness(
        self,
        news_item: NewsItem,
        etf_flow: Optional[ETFFlowData] = None,
    ) -> Tuple[bool, float, str]:
        """
        Returns (is_significant, weight, reason).
        """
        # 1. Recency gate
        if news_item.hours_old > self.MAX_HOURS_OLD:
            return False, 0.0, f"STALE: {news_item.hours_old:.1f}h (max {self.MAX_HOURS_OLD}h)"

        # 2. Category gate (must be institutional)
        if news_item.category not in self.INSTITUTIONAL_CATEGORIES:
            return False, 0.0, f"NON_INSTITUTIONAL: category={news_item.category}"

        # 3. Kant paralogism check — directional contradiction with macro flow
        if (etf_flow and etf_flow.flow_direction == "strong_outflow"
                and news_item.direction == "bullish"):
            return False, 0.0, "CONTRADICTION: bullish_news + strong_outflow"

        if (etf_flow and etf_flow.flow_direction == "strong_inflow"
                and news_item.direction == "bearish"):
            return False, 0.0, "CONTRADICTION: bearish_news + strong_inflow"

        # 4. Weight assignment by institutional strength
        if news_item.category == 3:
            weight = 0.90    # ETF / institutional flows
        elif news_item.category == 6:
            weight = 0.85    # Macro / regulatory
        else:
            weight = 0.70    # Policy / other

        # 5. Freshness decay: linear 1.0 → 0.5 over MAX_HOURS_OLD
        freshness = 1.0 - (news_item.hours_old / self.MAX_HOURS_OLD) * 0.5
        final_weight = weight * freshness

        # Store on item for downstream consumers
        news_item.kant_weight = final_weight

        return True, final_weight, "SOUND"

    def filter_batch(
        self,
        news_items: List[NewsItem],
        etf_flow: Optional[ETFFlowData] = None,
    ) -> List[NewsItem]:
        """Applies validation to a batch; returns only significant items."""
        valid: List[NewsItem] = []
        for item in news_items:
            is_sig, weight, reason = self.validate_structural_soundness(item, etf_flow)
            if is_sig:
                valid.append(item)
                logger.info("kant_validated", title=item.title[:40], weight=round(weight, 2))
            else:
                logger.debug("kant_rejected", title=item.title[:40], reason=reason)
        return valid


# Backward-compat alias
KantNews = KantNewsFilter
