"""
SOSOVALUE FEED — The Sovereign Eyes
v2.2: Correct /openapi/v1 base, rate-limit guard, ValueChain fallback.

If SoSoValue is unavailable (404/429/5xx), falls back to ARIA's kingdom
state (cascade phase + regime) to synthesise a directional proxy.
"""

import aiohttp
import asyncio
import structlog
import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import json
import os

logger = structlog.get_logger()

_KINGDOM_PATH = Path(
    os.environ.get("KINGDOM_STATE_PATH",
                   os.path.expanduser("~/kingdom/kingdom_state.json"))
)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ETFFlowData:
    date: str
    net_flow_usd: float
    flow_direction: str     # strong_inflow | inflow | neutral | outflow | strong_outflow
    largest_fund: str = "unknown"
    largest_amount: float = 0.0
    source: str = "api"     # api | valuechain_fallback


# Legacy alias
ETFFlow = ETFFlowData


@dataclass
class NewsItem:
    id: str
    title: str
    content_plain: str
    release_time_ms: int
    category: int
    tags: List[str] = field(default_factory=list)
    matched_assets: List[str] = field(default_factory=list)
    hours_old: float = 0.0
    direction: str = "neutral"
    kant_weight: float = 0.0


# ── ValueChain fallback ───────────────────────────────────────────────────────

def _valuechain_fallback() -> ETFFlowData:
    """
    Read ARIA's kingdom state and synthesise an ETFFlowData proxy.
    Uses cascade phase + regime as directional signal.
    """
    try:
        if _KINGDOM_PATH.exists():
            with open(_KINGDOM_PATH) as f:
                state = json.load(f)

            aria = state.get("aria", {})
            regime = aria.get("regime", "unknown")
            cascade = aria.get("cascade_alert", {})
            cascade_active = cascade.get("active", False)
            cascade_phase = cascade.get("phase", "none")

            # Map regime → synthetic flow direction
            if regime in ("risk_on", "trending_up"):
                direction = "inflow"
                net = 150_000_000.0
            elif regime in ("risk_off", "trending_down"):
                direction = "outflow"
                net = -150_000_000.0
            elif cascade_active and cascade_phase in ("trigger", "cascade"):
                direction = "outflow"
                net = -250_000_000.0
            else:
                direction = "neutral"
                net = 0.0

            logger.info("sosovalue_valuechain_fallback",
                        regime=regime, direction=direction)
            return ETFFlowData(
                date="",
                net_flow_usd=net,
                flow_direction=direction,
                source="valuechain_fallback",
            )
    except Exception as e:
        logger.debug("valuechain_fallback_read_error", error=str(e))

    return ETFFlowData(date="", net_flow_usd=0.0, flow_direction="neutral",
                       source="valuechain_fallback")


# ── Main feed class ───────────────────────────────────────────────────────────

class SoSoValueFeed:

    # Corrected base URL discovered from 404 response path
    BASE_URL = "https://openapi.sosovalue.com/openapi/v1"

    CURRENCY_ID_MAP = {
        "BTC": "1673723677362319866",
        "ETH": "1673723677362319867",
        "SOL": "1673723677362319868",
    }

    BULLISH_KEYWORDS = [
        "buy", "inflow", "long", "approval", "bullish", "increase",
        "rise", "record", "surge", "adoption", "growth", "positive",
        "launch", "partnership", "upgrade", "accumulate",
    ]
    BEARISH_KEYWORDS = [
        "sell", "outflow", "short", "rejection", "bearish", "decrease",
        "fall", "hack", "ban", "regulation", "crash", "negative",
        "liquidate", "probe", "fine", "delay",
    ]

    # Rate limit guard: minimum seconds between API calls
    _MIN_INTERVAL_S = 5.0
    _last_call_ts: float = 0.0

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"x-soso-api-key": self.api_key}

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _strip_html(self, text: str) -> str:
        return re.sub(r"<[^>]*>", "", text)

    def _classify_flow(self, net_flow: float) -> str:
        if net_flow >= 500_000_000:   return "strong_inflow"
        if net_flow >= 100_000_000:   return "inflow"
        if net_flow <= -500_000_000:  return "strong_outflow"
        if net_flow <= -100_000_000:  return "outflow"
        return "neutral"

    def _compute_direction(self, text: str) -> str:
        text = text.lower()
        bull = sum(1 for k in self.BULLISH_KEYWORDS if k in text)
        bear = sum(1 for k in self.BEARISH_KEYWORDS if k in text)
        if bull > bear: return "bullish"
        if bear > bull: return "bearish"
        return "neutral"

    async def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """
        Rate-limited GET with ValueChain-friendly error handling.
        Returns None on 429/5xx so callers can fall back.
        """
        # Soft rate limit: don't hammer the API
        elapsed = time.time() - SoSoValueFeed._last_call_ts
        if elapsed < self._MIN_INTERVAL_S:
            await asyncio.sleep(self._MIN_INTERVAL_S - elapsed)
        SoSoValueFeed._last_call_ts = time.time()

        url = self.BASE_URL + endpoint
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                headers=self.headers, connector=connector,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(url, params=params) as response:
                    if response.status == 429:
                        logger.warning("sosovalue_rate_limited",
                                       endpoint=endpoint, retry_after="60s")
                        return None
                    if response.status == 404:
                        logger.debug("sosovalue_404", endpoint=endpoint)
                        return None
                    if response.status != 200:
                        logger.warning("sosovalue_api_error",
                                       status=response.status, endpoint=endpoint)
                        return None
                    return await response.json()
        except Exception as e:
            logger.warning("sosovalue_request_failed", error=str(e), endpoint=endpoint)
            return None

    # ── Public API ───────────────────────────────────────────────────────────

    async def get_etf_flow(self, asset: str = "BTC") -> ETFFlowData:
        """Fetch ETF flow; falls back to ValueChain on any error."""
        data = await self._get(f"/etf/{asset.lower()}/daily-inflow")

        if data is None:
            return _valuechain_fallback()

        items = data.get("data", [])
        item = items[0] if isinstance(items, list) and items else {}
        if not item:
            return _valuechain_fallback()

        net_flow = float(item.get("netInflow", 0.0))
        return ETFFlowData(
            date=item.get("date", ""),
            net_flow_usd=net_flow,
            flow_direction=self._classify_flow(net_flow),
            largest_fund=item.get("maxInflowFund", "unknown"),
            largest_amount=float(item.get("maxInflowFundAmount", 0.0)),
            source="api",
        )

    async def get_btc_etf_flow(self) -> ETFFlowData:
        """Convenience alias — fetches BTC ETF flow."""
        return await self.get_etf_flow("BTC")

    async def get_news(
        self,
        asset: str,
        categories: Optional[List[int]] = None,
        max_hours_old: float = 24.0,
    ) -> List[NewsItem]:
        """Fetch news; returns empty list (not an error) on rate limit."""
        if categories is None:
            categories = [3, 5, 6]

        currency_id = self.CURRENCY_ID_MAP.get(asset)
        if not currency_id:
            return []

        params = {
            "currencyId": currency_id,
            "pageNum": 1,
            "pageSize": 20,
            "categoryList": ",".join(map(str, categories)),
        }
        data = await self._get("/news/featured/currency", params=params)

        if data is None:
            return []

        raw_items = data.get("data", {}).get("list", [])
        now_ms = int(time.time() * 1000)
        result: List[NewsItem] = []

        for item in raw_items:
            release_ms = item.get("releaseTime", 0)
            hours_old = (now_ms - release_ms) / (3600 * 1000)
            if hours_old > max_hours_old:
                continue

            title = item.get("title", "")
            content = self._strip_html(item.get("content", ""))
            direction = self._compute_direction(title + " " + content)

            result.append(NewsItem(
                id=str(item.get("id")),
                title=title,
                content_plain=content,
                release_time_ms=release_ms,
                category=int(item.get("category", 0)),
                tags=item.get("tags", []),
                matched_assets=[asset],
                hours_old=hours_old,
                direction=direction,
            ))

        return result
