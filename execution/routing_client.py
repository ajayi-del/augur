"""
RoutingClient — MEXC primary, Bybit fallback.

Perp route:
  MEXC futures → on failure → Bybit linear perps

Prediction route:
  MEXC prediction markets only (no fallback — binary risk is isolated)

Both routes can fire on the same signal independently.
"""

import structlog
from execution.venues.mexc_client import MexcClient, MexcOrderResult
from execution.bybit_client import BybitClient, OrderResult

logger = structlog.get_logger(__name__)


class RoutingClient:
    """
    Unified execution router: MEXC primary, Bybit fallback.
    Instantiated once in AugurApplication; shared across all loops.
    """

    def __init__(self, mexc: MexcClient, bybit: BybitClient, mode: str = "live"):
        self.mexc  = mexc
        self.bybit = bybit
        self.mode  = mode
        logger.info(
            "routing_client_ready",
            mode=mode, primary="mexc_futures", fallback="bybit_linear",
        )

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
        """
        Route a perp order.
        1. Try MEXC futures.
        2. On any failure, fall back to Bybit linear.
        Returns OrderResult regardless of which venue filled.
        """
        try:
            result = await self.mexc.place_futures_order(
                symbol=symbol,
                direction=direction,
                size_usd=size_usd,
                entry_price=entry,
                leverage=leverage,
            )
            logger.info(
                "routing_mexc_ok",
                symbol=symbol, order_id=result.order_id,
            )
            return OrderResult(
                order_id=result.order_id,
                venue=result.venue,
                symbol=symbol,
                direction=direction,
                size_usd=size_usd,
                entry=result.price,
                status=result.status,
            )

        except Exception as e:
            logger.warning(
                "routing_mexc_failed_fallback_bybit",
                symbol=symbol, error=str(e),
            )

        result = await self.bybit.place_order(
            symbol=symbol, direction=direction,
            size_usd=size_usd, entry=entry,
            stop=stop, tp1=tp1, tp2=tp2, tp3=tp3,
            leverage=leverage,
        )
        logger.info(
            "bybit_fallback_used",
            symbol=symbol, order_id=result.order_id, reason="mexc_failed",
        )
        return result

    async def place_prediction_bet(
        self, market_id: str, outcome: str, size_usdt: float
    ) -> dict:
        """
        Place a MEXC prediction market bet.
        No fallback — if MEXC fails, log and skip.
        """
        return await self.mexc.place_prediction_bet(market_id, outcome, size_usdt)
