import structlog

logger = structlog.get_logger()

class HedgehogClient:
    """
    Hedgehog Markets on Solana.
    """
    
    def __init__(self):
        self.base_url = "https://api.hedgehog.markets"
        
    async def get_markets(self) -> list:
        return [{
            "venue": "hedgehog",
            "market_id": "hh-1",
            "question": "Will SOL reach $300?",
            "probabilities": [0.42, 0.58],
            "volume": 8000,
            "liquidity": 2500
        }]
        
    async def place_order(self, market_id: str, outcome: str, size: float, price: float) -> dict:
        logger.info("hedgehog_bet_placed", market=market_id, outcome=outcome, size=size)
        return {
            "success": True,
            "tx_id": "hh_sol_tx_123",
            "venue": "hedgehog"
        }
