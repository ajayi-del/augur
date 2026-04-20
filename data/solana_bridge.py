"""
Solana on-chain signal bridge — weight 0.30 in AUGUR signal stack.

Free public endpoints, no API key required:
  Solana RPC:   getRecentPerformanceSamples → network TPS (congestion proxy)
  Jupiter v4:   /price?ids=SOL,BTC,ETH,BONK → DEX prices
  Drift API:    /fundingRates?marketIndex=N  → funding rates (already in ValueChain)

Cross-venue divergence:
  If Jupiter SOL price differs from ARIA/SoDEX price by ≥ 0.3%:
  → Actionable arbitrage signal: buy cheaper venue, short expensive.
"""

import time
import aiohttp
import structlog
from typing import Dict, Optional

logger = structlog.get_logger(__name__)

_SOLANA_RPC     = "https://api.mainnet-beta.solana.com"
_COINGECKO_URL  = "https://api.coingecko.com/api/v3/simple/price"

# CoinGecko ID → canonical symbol
_COINGECKO_IDS: Dict[str, str] = {
    "solana":           "SOL",
    "bitcoin":          "BTC",
    "ethereum":         "ETH",
    "dogecoin":         "DOGE",
    "sui":              "SUI",
    "arbitrum":         "ARB",
    "optimism":         "OP",
    "avalanche-2":      "AVAX",
    "bnb":              "BNB",
    "pepe":             "PEPE",
    "bonk":             "BONK",
}

_TPS_CACHE_TTL   = 60    # seconds
_PRICE_CACHE_TTL = 30    # seconds (CoinGecko free tier: 30 req/min)

_HIGH_TPS_THRESHOLD = 3000   # strong network activity above this
_DIV_THRESHOLD_PCT  = 0.30   # minimum divergence to flag arbitrage


class SolanaBridge:
    """
    Reads Solana network state and DEX prices.
    Provides supplementary signal data to AUGUR's probability engine.
    All methods are safe to call concurrently; state is read-only except caches.
    """

    def __init__(self):
        self._tps_cache:        Optional[float]    = None
        self._tps_cached_at:    float              = 0.0
        self._price_cache:      Dict[str, float]   = {}
        self._price_cached_at:  float              = 0.0

    # ── Network TPS ───────────────────────────────────────────────────────────

    async def get_network_tps(self) -> float:
        """
        Average TPS over last 5 performance samples (~2 min window).
        Returns 0.0 on error — callers should treat as unknown, not zero.
        """
        if time.time() - self._tps_cached_at < _TPS_CACHE_TTL and self._tps_cache is not None:
            return self._tps_cache

        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method":  "getRecentPerformanceSamples",
                "params":  [5],
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                async with s.post(_SOLANA_RPC, json=payload) as r:
                    data = await r.json(content_type=None)
                    samples = data.get("result", [])
                    if not samples:
                        return 0.0
                    avg_tps = sum(
                        smpl["numTransactions"] / max(smpl["samplePeriodSecs"], 1)
                        for smpl in samples
                    ) / len(samples)
                    self._tps_cache    = avg_tps
                    self._tps_cached_at = time.time()
                    logger.debug("solana_tps_fetched", tps=round(avg_tps, 0))
                    return avg_tps
        except Exception as e:
            logger.warning("solana_tps_error", error=str(e))
        return 0.0

    # ── CoinGecko market prices ───────────────────────────────────────────────

    async def get_jupiter_prices(self) -> Dict[str, float]:
        """
        {symbol: usd_price} from CoinGecko simple price API.
        Replaces Jupiter (jup.ag DNS fails on GCP Frankfurt).
        Returns empty dict on error — callers must handle gracefully.
        """
        if time.time() - self._price_cached_at < _PRICE_CACHE_TTL and self._price_cache:
            return dict(self._price_cache)

        prices: Dict[str, float] = {}
        ids_str = ",".join(_COINGECKO_IDS.keys())

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(
                    _COINGECKO_URL,
                    params={"ids": ids_str, "vs_currencies": "usd"},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        for cg_id, sym in _COINGECKO_IDS.items():
                            entry = data.get(cg_id, {})
                            p = float(entry.get("usd", 0.0))
                            if p > 0:
                                prices[sym] = p
                    else:
                        logger.warning("coingecko_non_200", status=r.status)
        except Exception as e:
            logger.warning("coingecko_price_error", error=str(e))

        if prices:
            self._price_cache     = prices
            self._price_cached_at = time.time()
            logger.debug("coingecko_prices_fetched", symbols=list(prices.keys()))

        return dict(prices)

    # ── Signal computations ───────────────────────────────────────────────────

    def tps_conviction_multiplier(self, tps: float) -> float:
        """
        Maps TPS to a conviction multiplier (0.80–1.20).
        High TPS means Solana is active → signal confirmation is stronger.
        """
        if tps <= 0:     return 1.0   # unknown — neutral
        if tps >= 4000:  return 1.20
        if tps >= _HIGH_TPS_THRESHOLD: return 1.10
        if tps >= 1000:  return 1.0
        if tps >= 500:   return 0.90
        return 0.80

    def detect_divergence(
        self,
        jupiter_prices: Dict[str, float],
        sodex_prices: Dict[str, float],
    ) -> Dict[str, dict]:
        """
        Compares Jupiter DEX prices vs ARIA/SoDEX prices.
        Returns {symbol: {divergence_pct, direction}} for pairs above threshold.

        direction 'buy_jup'  → Jupiter is cheaper, buy there / short SoDEX
        direction 'buy_sodex' → SoDEX is cheaper, buy there / short Jupiter
        """
        divergences: Dict[str, dict] = {}

        for jup_sym, jup_price in jupiter_prices.items():
            aria_sym   = f"{jup_sym}-USD"
            sodex_price = sodex_prices.get(aria_sym)
            if not sodex_price or sodex_price <= 0 or jup_price <= 0:
                continue

            div_pct = (jup_price - sodex_price) / sodex_price * 100.0

            if abs(div_pct) >= _DIV_THRESHOLD_PCT:
                direction = "buy_jup" if div_pct < 0 else "buy_sodex"
                divergences[jup_sym] = {
                    "divergence_pct": round(div_pct, 4),
                    "direction":      direction,
                    "jup_price":      jup_price,
                    "sodex_price":    sodex_price,
                }
                logger.info(
                    "cross_venue_divergence",
                    symbol=jup_sym,
                    jup=jup_price,
                    sodex=sodex_price,
                    div_pct=round(div_pct, 3),
                    direction=direction,
                )

        return divergences

    async def get_full_snapshot(self) -> dict:
        """
        Convenience: fetch all signals in one call.
        Used in the valuechain_loop update.
        """
        import asyncio
        tps, prices = await asyncio.gather(
            self.get_network_tps(),
            self.get_jupiter_prices(),
            return_exceptions=True,
        )
        if isinstance(tps, Exception):
            tps = 0.0
        if isinstance(prices, Exception):
            prices = {}

        return {
            "tps":                tps,
            "tps_multiplier":     self.tps_conviction_multiplier(tps),
            "jupiter_prices":     prices,
            "is_network_healthy": tps > 500 if tps > 0 else None,
        }
