import structlog
import time
import asyncio
import aiohttp
from typing import List, Optional
from dataclasses import dataclass
from polymarket.probability_engine import ProbabilityEngine, AugurProbability
from polymarket.kelly_sizer import KellySizer

logger = structlog.get_logger()

_POLY_BASE = "https://clob.polymarket.com"

_CRYPTO_KEYWORDS = {
    "BTC", "ETH", "SOL", "BNB", "AVAX", "CRYPTO", "BITCOIN",
    "ETHEREUM", "SOLANA", "FED", "RATE", "INFLATION", "DOGE",
    "ARB", "OP", "PEPE", "DEFI", "NFT", "BLOCKCHAIN",
}


@dataclass
class BetOpportunity:
    market_id: str
    question: str
    p_augur: float
    p_market: float
    edge: float
    bet_size_usd: float
    side: str             # BUY_YES or BUY_NO
    expiry: int


class MarketScanner:
    """
    Scans Polymarket public API for narrative arbitrage opportunities.
    Uses ValueChain bridge signals. No CLOB client or API key required.
    """

    def __init__(
        self,
        clob_client,          # unused in paper mode, kept for interface compat
        prob_engine: ProbabilityEngine,
        kelly_sizer: KellySizer,
        min_edge: float = 0.08,
        min_liquidity: float = 5000.0,
    ):
        self.engine = prob_engine
        self.sizer = kelly_sizer
        self.min_edge = min_edge
        self.min_liquidity = min_liquidity

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_public_markets(self) -> list:
        """Fetches active markets from Polymarket public CLOB API. Never crashes."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(
                    f"{_POLY_BASE}/markets",
                    params={"active": "true", "limit": 100},
                ) as resp:
                    if resp.status != 200:
                        logger.warning("polymarket_api_non200", status=resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    markets = data.get("data", []) if isinstance(data, dict) else data
                    logger.info("polymarket_markets_fetched", count=len(markets))
                    return markets
        except Exception as e:
            logger.warning("polymarket_fetch_error", error=str(e))
            return []

    def _is_crypto_relevant(self, question: str) -> bool:
        q_upper = question.upper()
        return any(kw in q_upper for kw in _CRYPTO_KEYWORDS)

    # ── Scanner ───────────────────────────────────────────────────────────────

    async def scan_for_opportunities(
        self,
        asset: str = "BTC",
        cascade_alert: Optional[dict] = None,
        aria_coherence: Optional[float] = None,
        funding_rates: Optional[dict] = None,
        market_direction: str = "long",
    ) -> List[BetOpportunity]:
        """
        Scans Polymarket public markets for edges.
        Returns BetOpportunity list sorted by edge descending.
        """
        funding_rates = funding_rates or {}
        logger.info("scanning_polymarket", asset=asset)

        raw_markets = await self.get_public_markets()

        # Filter to crypto-relevant and liquid markets
        relevant = [
            m for m in raw_markets
            if self._is_crypto_relevant(m.get("question", ""))
            and float(m.get("liquidity", 0)) >= self.min_liquidity
        ]
        logger.info("polymarket_relevant_markets", count=len(relevant),
                    min_liquidity=self.min_liquidity)

        opportunities: List[BetOpportunity] = []

        for m in relevant:
            try:
                market_id   = m.get("condition_id") or m.get("id", "unknown")
                question    = m.get("question", "")
                yes_price   = float(m.get("outcomePrices", ["0.5"])[0]
                                    if isinstance(m.get("outcomePrices"), list)
                                    else m.get("yes_price", m.get("outcomePrices", 0.50)))
                liquidity   = float(m.get("liquidity", 0))
                end_date_ts = int(m.get("endDateIso", 0) or 0)
                if end_date_ts == 0:
                    # try other field names
                    end_date_ts = int(m.get("end_date_iso", m.get("expiry", 0)) or 0)
                if end_date_ts == 0:
                    continue  # no expiry — skip

                # Pick funding rate for this asset if available
                funding_rate_pct = funding_rates.get(asset)

                prob_res = self.engine.compute_augur_probability(
                    market_id=market_id,
                    target_asset=asset,
                    expiry_timestamp=end_date_ts,
                    yes_price=yes_price,
                    liquidity_usdc=liquidity,
                    cascade_alert=cascade_alert,
                    aria_coherence=aria_coherence,
                    funding_rate_pct=funding_rate_pct,
                    market_direction=market_direction,
                )

                p_augur  = prob_res.probability
                p_market = yes_price

                edge_yes = p_augur - p_market
                edge_no  = p_market - p_augur

                edge = 0.0
                side = ""
                actual_p_market = 0.0

                if edge_yes >= self.min_edge:
                    edge = edge_yes
                    side = "BUY_YES"
                    actual_p_market = p_market
                elif edge_no >= self.min_edge:
                    edge = edge_no
                    side = "BUY_NO"
                    actual_p_market = 1.0 - p_market

                if edge > 0:
                    p_for_sizing = p_augur if side == "BUY_YES" else (1.0 - p_augur)
                    size = self.sizer.calculate_bet_size(p_for_sizing, actual_p_market)
                    if size > 0:
                        opp = BetOpportunity(
                            market_id=market_id,
                            question=question,
                            p_augur=p_augur,
                            p_market=p_market,
                            edge=edge,
                            bet_size_usd=size,
                            side=side,
                            expiry=end_date_ts,
                        )
                        opportunities.append(opp)
                        logger.info("opportunity_found",
                                    question=question[:60], side=side,
                                    edge=round(edge, 3), size=size)

            except Exception as e:
                logger.warning("market_scan_item_error", error=str(e))
                continue

        opportunities.sort(key=lambda o: o.edge, reverse=True)
        return opportunities
