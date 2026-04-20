"""
RoutingClient — Bybit primary, MEXC secondary (prediction markets only).

Perp route:
  Bybit linear perps (primary — reliable from GCP Frankfurt)
  → on failure → MEXC futures (secondary — geo-blocked until IP whitelist)

Prediction route:
  MEXC prediction markets only (no fallback — binary risk is isolated)
"""

import structlog
from execution.venues.mexc_client import MexcClient, MexcOrderResult
from execution.bybit_client import BybitClient, OrderResult

logger = structlog.get_logger(__name__)


class RoutingClient:
    """
    Unified execution router: Bybit primary, MEXC fallback.
    Instantiated once in AugurApplication; shared across all loops.
    """

    def __init__(self, mexc: MexcClient, bybit: BybitClient, mode: str = "live"):
        self.mexc  = mexc
        self.bybit = bybit
        self.mode  = mode
        logger.info(
            "routing_client_ready",
            mode=mode, primary="bybit_linear", fallback="mexc_futures",
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
        Route a perp order: Bybit primary, MEXC futures fallback.

        Price fetch: MEXC public ticker (no auth, not geo-blocked) used to
        compute qty before routing to Bybit.
        """
        if entry <= 0:
            entry = await self.mexc.get_ticker_price(symbol)
            if entry > 0:
                logger.debug("routing_price_fetched", symbol=symbol, price=entry)

        # Primary: Bybit linear perps
        try:
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
            logger.info(
                "routing_bybit_ok",
                symbol=symbol, order_id=result.order_id, venue=result.venue,
            )
            return result

        except Exception as e:
            logger.warning(
                "routing_bybit_failed_fallback_mexc",
                symbol=symbol, error=str(e),
            )

        # Fallback: MEXC futures (will fail while geo-blocked; logged for visibility)
        mexc_result = await self.mexc.place_futures_order(
            symbol=symbol,
            direction=direction,
            size_usd=size_usd,
            entry_price=entry,
            leverage=leverage,
        )
        logger.info(
            "mexc_fallback_used",
            symbol=symbol, order_id=mexc_result.order_id, reason="bybit_failed",
        )
        return OrderResult(
            order_id=mexc_result.order_id,
            venue=mexc_result.venue,
            symbol=symbol,
            direction=direction,
            size_usd=size_usd,
            entry=mexc_result.price,
            status=mexc_result.status,
        )

    async def place_prediction_bet(
        self, market_id: str, outcome: str, size_usdt: float
    ) -> dict:
        """
        Place a MEXC prediction market bet.
        No fallback — if MEXC fails, log and skip.
        """
        return await self.mexc.place_prediction_bet(market_id, outcome, size_usdt)
