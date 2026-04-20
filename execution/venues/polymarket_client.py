import os
import aiohttp
import structlog
from dataclasses import dataclass
from typing import List, Optional, Dict
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, BuyArgs, OrderType
except ImportError:
    ClobClient = None
    OrderArgs = BuyArgs = OrderType = None

logger = structlog.get_logger()

@dataclass
class PolymarketMarket:
    market_id: str
    question: str
    yes_price: float
    liquidity: float
    active: bool
    end_date_ms: int

    @classmethod
    def from_api(cls, m: dict):
        return cls(
            market_id=m.get("condition_id", m.get("id")),
            question=m.get("question", ""),
            yes_price=float(m.get("best_bid", 0.5)),
            liquidity=float(m.get("liquidity", 0.0)),
            active=m.get("active", True),
            end_date_ms=int(m.get("expiration", 0))
        )

@dataclass
class BetResult:
    order_id: str
    market_id: str
    outcome: str
    size_usdc: float
    price: float
    status: str

@dataclass
class ActiveBet:
    market_id: str
    outcome: str
    size: float
    entry_price: float

class PolymarketClient:
    """
    Polymarket CLOB client.
    Deepest liquidity, most markets.
    'The collective mind is always behind the individual will.'
    """
    
    def __init__(
        self,
        private_key: str = None,
        chain_id: int = 137  # Polygon
    ):
        self.private_key = private_key or os.getenv("POLYMARKET_API_KEY")
        if ClobClient and self.private_key:
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.private_key,
                chain_id=chain_id
            )
        else:
            self.client = None
            logger.warning("clob_client_initialization_failed", reason="Missing key or library")

    async def get_markets(
        self,
        status: str = "active",
        min_liquidity_usdc: float = 1000
    ) -> List[PolymarketMarket]:
        if not self.client:
            return []
            
        # Real logic would use self.client.get_sampling_markets()
        # For now, we wrap the clob call
        try:
            markets = await self.client.get_markets()
            return [
                PolymarketMarket.from_api(m)
                for m in markets
                if m.get("active", True)
                and float(m.get("liquidity", 0)) >= min_liquidity_usdc
            ]
        except Exception as e:
            logger.error("polymarket_get_markets_failed", error=str(e))
            return []
    
    async def place_bet(
        self,
        market_id: str,
        outcome: str,  # "YES" or "NO"
        size_usdc: float,
        price: float  # Expected price (0-1)
    ) -> BetResult:
        if not self.client:
            raise Exception("CLOB client not initialized")
            
        # Convert to CLOB order
        order = await self.client.create_order(
            OrderArgs(
                token_id=market_id,
                price=price,
                size=size_usdc,
                side="BUY" if outcome == "YES" else "SELL"
            )
        )
        
        result = await self.client.post_order(
            order,
            OrderType.GTC  # Good till cancelled
        )
        
        return BetResult(
            order_id=result["orderID"],
            market_id=market_id,
            outcome=outcome,
            size_usdc=size_usdc,
            price=price,
            status="open"
        )
    
    async def get_positions(self) -> List[ActiveBet]:
        if not self.client:
            return []
        positions = await self.client.get_positions()
        return [
            ActiveBet(p["id"], p["side"], p["size"], p["price"])
            for p in positions
        ]
    
    async def get_balance(self) -> float:
        if not self.client:
            return 0.0
        balance = await self.client.get_balance()
        return float(balance.get("usdc", 0.0))
