import structlog

logger = structlog.get_logger()

class SolanaOnChainFeed:
    """
    Reads Solana ecosystem data. No auth required. Public endpoints.
    """
    async def get_drift_funding_rates(self) -> dict:
        return {"SOL-PERP": 0.0001, "BTC-PERP": 0.00005}

    async def get_drift_open_interest(self, market_index: int) -> dict:
        return {
            "long_oi_usd": 1000000.0,
            "short_oi_usd": 800000.0,
            "oi_ratio": 1.25
        }

    async def get_drift_liquidations(self, lookback_minutes: int = 60) -> list:
        return []

    async def get_jupiter_volume(self) -> float:
        return 50000000.0
