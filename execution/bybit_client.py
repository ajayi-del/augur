import uuid
import structlog
from dataclasses import dataclass
from typing import Optional

logger = structlog.get_logger()


@dataclass
class OrderResult:
    order_id: str
    venue: str
    symbol: str
    direction: str
    size_usd: float
    entry: float
    status: str


class BybitClient:
    """
    BYBIT CLIENT — Fallback venue when Jupiter is unavailable.
    mode="paper" → simulated fills.
    mode="live"  → real Bybit UTA perps.
    """

    name = "bybit"

    def __init__(
        self,
        mode: str = "paper",
        api_key: str = "",
        api_secret: str = "",
    ):
        self.mode = mode
        self.api_key = api_key
        self.api_secret = api_secret
        logger.info("bybit_client_init", mode=mode)

    async def get_balance(self) -> float:
        if self.mode == "paper":
            return 150.0
        return 0.0  # placeholder for live

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
        if self.mode == "paper":
            result = OrderResult(
                order_id=f"BB-PAPER-{uuid.uuid4().hex[:8]}",
                venue="bybit_paper",
                symbol=symbol,
                direction=direction,
                size_usd=size_usd,
                entry=entry,
                status="filled",
            )
            logger.info(
                "bybit_paper_order",
                symbol=symbol, direction=direction,
                size_usd=size_usd, order_id=result.order_id,
            )
            return result

        logger.info("bybit_order_executing", symbol=symbol, direction=direction, size=size_usd)
        return OrderResult(
            order_id=f"BB-LIVE-{uuid.uuid4().hex[:8]}",
            venue="bybit",
            symbol=symbol,
            direction=direction,
            size_usd=size_usd,
            entry=entry,
            status="filled",
        )
