import structlog
import time
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import List, Optional
from dataclasses import dataclass
from polymarket.probability_engine import ProbabilityEngine, AugurProbability
from polymarket.kelly_sizer import KellySizer

logger = structlog.get_logger()

_POLY_BASE = "https://clob.polymarket.com"

_CRYPTO_KEYWORDS = {
    "BTC", "ETH", "SOL", "BNB", "AVAX", "CRYPTO", "BITCOIN",
    "ETHEREUM", "SOLANA", "FED", "RATE", "INFLATION", "DOGE",
    "ARB", "OP", "PEPE", "DEFI", "NFT", "BLOCKCHAIN", "COINBASE",
    "BINANCE", "INTEREST RATE",
    # TRUMP / TARIFF / RECESSION removed — match political markets, not crypto price markets
}

_MIN_HOURS_TO_EXPIRY = 2.0   # skip markets expiring < 2h from now
_MAX_BETS_PER_SCAN   = 15    # hard cap — quality over quantity


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


def _parse_yes_price(market: dict) -> Optional[float]:
    """Extract YES token price from Polymarket market dict."""
    tokens = market.get("tokens", [])
    for tok in tokens:
        if str(tok.get("outcome", "")).upper() == "YES":
            try:
                return float(tok["price"])
            except (KeyError, ValueError, TypeError):
                pass
    # fallback: take first token price
    if tokens:
        try:
            return float(tokens[0].get("price", 0.50))
        except (ValueError, TypeError):
            pass
    return None


def _parse_expiry_ts(market: dict) -> Optional[int]:
    """Parse end_date_iso → Unix timestamp (seconds)."""
    raw = market.get("end_date_iso") or market.get("endDateIso", "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


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
        min_liquidity: float = 5000.0,   # kept in signature but not used (no field in API)
    ):
        self.engine = prob_engine
        self.sizer = kelly_sizer
        self.min_edge = min_edge
        logger.info("market_scanner_init", min_edge=min_edge, max_bets=_MAX_BETS_PER_SCAN)

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_public_markets(self) -> list:
        """
        Fetches currently liquid markets from /sampling-markets (live order books).
        Falls back to paginated /markets on error. Never crashes.
        """
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(f"{_POLY_BASE}/sampling-markets") as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        markets = (
                            data.get("data", data) if isinstance(data, dict) else data
                        )
                        if isinstance(markets, list):
                            logger.info("polymarket_markets_fetched",
                                        total_raw=len(markets), source="sampling-markets")
                            return markets
                    logger.warning("polymarket_sampling_non200", status=resp.status)
        except Exception as e:
            logger.warning("polymarket_sampling_error", error=str(e))

        # Fallback: paginated /markets
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    f"{_POLY_BASE}/markets",
                    params={"active": "true", "limit": 100},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        page = data.get("data", []) if isinstance(data, dict) else data
                        markets = [
                            m for m in page
                            if not m.get("closed") and not m.get("archived")
                        ]
                        logger.info("polymarket_markets_fetched",
                                    total_raw=len(markets), source="markets-fallback")
                        return markets
        except Exception as e:
            logger.warning("polymarket_fetch_error", error=str(e))

        return []

    def _is_crypto_relevant(self, question: str, tags: list) -> bool:
        q_upper = question.upper()
        if any(kw in q_upper for kw in _CRYPTO_KEYWORDS):
            return True
        tag_str = " ".join(str(t).upper() for t in (tags or []))
        return any(kw in tag_str for kw in {"CRYPTO", "BITCOIN", "ETHEREUM"})

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
        Scans Polymarket public markets for edges. Returns list sorted by edge.
        """
        funding_rates = funding_rates or {}
        now_ts = time.time()
        logger.info("scanning_polymarket", asset=asset, direction=market_direction)

        raw_markets = await self.get_public_markets()
        logger.info("filter_stage", stage="total_fetched", count=len(raw_markets))

        # Filter to crypto-relevant with valid price and sufficient time to expiry
        after_asset = 0
        relevant = []
        for m in raw_markets:
            q = m.get("question", "")
            tags = m.get("tags", [])
            if not self._is_crypto_relevant(q, tags):
                continue
            after_asset += 1
            yes_price = _parse_yes_price(m)
            if yes_price is None or yes_price <= 0 or yes_price >= 1:
                continue
            expiry_ts = _parse_expiry_ts(m)
            if expiry_ts is None:
                continue
            hours_left = (expiry_ts - now_ts) / 3600.0
            if hours_left < _MIN_HOURS_TO_EXPIRY:
                continue
            relevant.append((m, yes_price, expiry_ts))

        logger.info("filter_stage", stage="after_asset_filter",   count=after_asset)
        logger.info("filter_stage", stage="after_horizon_filter", count=len(relevant))

        opportunities: List[BetOpportunity] = []
        funding_rate_pct = funding_rates.get(asset)

        for m, yes_price, expiry_ts in relevant:
            try:
                market_id = m.get("condition_id") or m.get("id", "unknown")
                question  = m.get("question", "")

                prob_res = self.engine.compute_augur_probability(
                    market_id=market_id,
                    target_asset=asset,
                    expiry_timestamp=expiry_ts,
                    yes_price=yes_price,
                    cascade_alert=cascade_alert,
                    aria_coherence=aria_coherence,
                    funding_rate_pct=funding_rate_pct,
                    market_direction=market_direction,
                )

                p_augur  = prob_res.probability
                p_market = yes_price

                # No real signals → p_augur defaults to 0.50 → skip to avoid
                # fake edge from market price deviation alone
                if prob_res.n_signals == 0:
                    continue

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
                            expiry=expiry_ts,
                        )
                        opportunities.append(opp)
                        if len(opportunities) <= 5:
                            logger.info("bet_probability_sample",
                                        question=question[:60], side=side,
                                        p_augur=p_augur, p_market=p_market,
                                        edge=round(edge, 3),
                                        n_signals=prob_res.n_signals,
                                        signals=prob_res.signals_breakdown)

            except Exception as e:
                logger.warning("market_scan_item_error", error=str(e))
                continue

        logger.info("filter_stage", stage="after_edge_filter", count=len(opportunities))

        opportunities.sort(key=lambda o: o.edge, reverse=True)
        opportunities = opportunities[:_MAX_BETS_PER_SCAN]
        return opportunities
