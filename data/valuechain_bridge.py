import math
import time
import structlog
import aiohttp
from typing import Optional, Dict
from kingdom.state_sync import KingdomStateSync

logger = structlog.get_logger()

_DRIFT_BASE = "https://data.api.drift.trade"

# Drift perpetual market indices → symbol name
_DRIFT_MARKETS: Dict[int, str] = {
    0:  "SOL",
    1:  "BTC",
    2:  "ETH",
    3:  "APT",
    4:  "BNB",
    5:  "SUI",
    6:  "1KPEPE",
    7:  "OP",
    8:  "ARB",
    9:  "DOGE",
    10: "MATIC",
    11: "AVAX",
}

_FUNDING_CACHE_TTL = 300  # 5 minutes


class ValueChainBridge:
    """
    Reads ARIA's processed on-chain output via kingdom_state.json.
    No direct RPC calls — ARIA is the ValueChain data source.
    Drift public API (no auth) used for funding rates only.
    """

    def __init__(self, kingdom: KingdomStateSync):
        self._kingdom = kingdom
        self._funding_cache: Dict[str, float] = {}
        self._funding_cached_at: float = 0.0

    # ── ARIA state readers ───────────────────────────────────────────────────

    def get_cascade_signal(self) -> dict:
        """Returns aria.cascade_alert dict from kingdom state."""
        try:
            aria = self._kingdom.read_aria_state()
            alert = aria.cascade_alert or {}
            return {
                "active":  bool(alert.get("active", False)),
                "zscore":  float(alert.get("zscore", 0.0)),
                "phase":   str(alert.get("phase", "none")),
            }
        except Exception as e:
            logger.warning("valuechain_cascade_read_error", error=str(e))
            return {"active": False, "zscore": 0.0, "phase": "none"}

    def get_regime(self) -> str:
        """Returns aria.regime from kingdom state."""
        try:
            aria = self._kingdom.read_aria_state()
            return aria.regime or "unknown"
        except Exception as e:
            logger.warning("valuechain_regime_read_error", error=str(e))
            return "unknown"

    def get_aria_coherence(self, symbol: str) -> Optional[float]:
        """Returns coherence of the highest-confidence active ARIA bet for symbol."""
        try:
            bets = self._kingdom.get_active_aria_bets(symbol)
            if not bets:
                return None
            return max(b.coherence for b in bets)
        except Exception as e:
            logger.warning("valuechain_coherence_read_error", symbol=symbol, error=str(e))
            return None

    # ── Drift public API ─────────────────────────────────────────────────────

    async def get_funding_rates(self) -> Dict[str, float]:
        """
        Fetches funding rates from Drift public data API.
        Returns {symbol: rate_pct} where rate_pct is in percentage (0.05 = 0.05%).
        Cache TTL 5 minutes. Falls back to {} on error. Never crashes.
        """
        if time.time() - self._funding_cached_at < _FUNDING_CACHE_TTL and self._funding_cache:
            return self._funding_cache

        rates: Dict[str, float] = {}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                for idx, symbol in _DRIFT_MARKETS.items():
                    try:
                        async with session.get(
                            f"{_DRIFT_BASE}/fundingRates",
                            params={"marketIndex": idx},
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json(content_type=None)

                            logger.debug("drift_raw_response", symbol=symbol, idx=idx,
                                         type=type(data).__name__,
                                         keys=list(data.keys()) if isinstance(data, dict) else "list",
                                         len=len(data) if isinstance(data, (list, dict)) else "?")

                            # API format varies — try multiple shapes
                            records = (
                                data if isinstance(data, list)
                                else data.get("records")
                                or data.get("fundingRateRecords")
                                or data.get("data", {}).get("fundingRateRecords")
                                or data.get("data")
                                or []
                            )
                            if isinstance(records, dict):
                                records = list(records.values())
                            if not records:
                                continue
                            rate_raw = float(records[0].get("fundingRate", 0.0))
                            # Drift stores fundingRate in 1e-9 (PRICE_PRECISION).
                            # Normalise to percentage: multiply by 100 / 1e9 = 1e-7
                            # If the value is already small (<0.1) assume it's already pct.
                            if abs(rate_raw) > 1:
                                rate_pct = rate_raw * 1e-7
                            else:
                                rate_pct = rate_raw
                            rates[symbol] = rate_pct
                    except Exception:
                        continue

            self._funding_cache = rates
            self._funding_cached_at = time.time()
            logger.info("drift_funding_fetched", n=len(rates),
                        symbols=list(rates.keys())[:5])

        except Exception as e:
            logger.warning("drift_funding_fetch_error", error=str(e))

        return rates
