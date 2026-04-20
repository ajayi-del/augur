"""
RoutingClient — Bybit only.
"""

import structlog
from execution.bybit_client import BybitClient, OrderResult

logger = structlog.get_logger(__name__)


class RoutingClient:
    def __init__(self, bybit: BybitClient, mode: str = "live"):
        self.bybit = bybit
        self.mode  = mode
        logger.info("routing_client_ready", mode=mode, primary="bybit_linear")

    async def place_order(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        entry: float = 0.0,
        stop: float = 0.0,
        tp1: float = 0.0,
        tp2: float = 0.0,
        tp3: float = 0.0,
        leverage: int = 5,
    ) -> OrderResult:
        result = await self.bybit.place_order(
            symbol=symbol,
            direction=direction,
            size_usd=size_usd,
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            leverage=leverage,
        )
        logger.info("routing_bybit_ok",
                    symbol=symbol, order_id=result.order_id, venue=result.venue)
        return result
