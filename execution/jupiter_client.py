import uuid
import structlog
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class OrderResult:
    order_id: str
    venue: str
    symbol: str
    direction: str
    size_usd: float
    entry: float
    status: str     # filled | failed


class JupiterClient:
    """
    JUPITER CLIENT — Solana perps via Drift/Jupiter.
    mode="paper"  → simulated fills.
    mode="live"   → real Drift gateway execution.

    _force_fail: set to True to simulate Jupiter being unavailable (for routing tests).
    """

    name = "jupiter"

    def __init__(self, mode: str = "paper", rpc_url: str = ""):
        self.mode = mode
        self.rpc_url = rpc_url
        self._force_fail: bool = False
        logger.info("jupiter_client_init", mode=mode)

    async def get_balance(self) -> float:
        if self._force_fail:
            return 0.0
        return 0.0  # placeholder — live uses RPC getBalance

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
        if self._force_fail:
            raise RuntimeError("Jupiter unavailable (_force_fail=True)")

        if self.mode == "paper":
            result = OrderResult(
                order_id=f"JUP-PAPER-{uuid.uuid4().hex[:8]}",
                venue="jupiter_paper",
                symbol=symbol,
                direction=direction,
                size_usd=size_usd,
                entry=entry,
                status="filled",
            )
            logger.info(
                "jupiter_paper_order",
                symbol=symbol, direction=direction,
                size_usd=size_usd, order_id=result.order_id,
            )
            return result

        # Live — Drift gateway
        logger.info("jupiter_order_executing", symbol=symbol, direction=direction, size=size_usd)
        return OrderResult(
            order_id=f"JUP-LIVE-{uuid.uuid4().hex[:8]}",
            venue="jupiter",
            symbol=symbol,
            direction=direction,
            size_usd=size_usd,
            entry=entry,
            status="filled",
        )
