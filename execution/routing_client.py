import structlog
from execution.jupiter_client import JupiterClient, OrderResult
from execution.bybit_client import BybitClient

logger = structlog.get_logger()


class RoutingClient:
    """
    ROUTING CLIENT — tries Jupiter first, falls back to Bybit on any exception.
    """

    def __init__(
        self,
        jupiter: JupiterClient,
        bybit: BybitClient,
        mode: str = "paper",
    ):
        self.jupiter = jupiter
        self.bybit = bybit
        self.mode = mode

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
        kwargs = dict(
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
        try:
            result = await self.jupiter.place_order(**kwargs)
            logger.info("routing_jupiter_ok", symbol=symbol, order_id=result.order_id)
            return result
        except Exception as e:
            logger.warning(
                "routing_jupiter_failed_fallback_bybit",
                symbol=symbol,
                error=str(e),
            )
            result = await self.bybit.place_order(**kwargs)
            logger.info("routing_bybit_fallback_ok", symbol=symbol, order_id=result.order_id)
            return result
