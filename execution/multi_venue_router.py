import asyncio
import structlog
from execution.venues.polymarket_client import PolymarketClient
from execution.venues.augur_turbo_client import AugurTurboClient
from execution.venues.hedgehog_client import HedgehogClient

logger = structlog.get_logger()

class NoHealthyVenuesError(Exception): pass
class AllVenuesFailedError(Exception): pass

class MultiVenueRouter:
    """
    Routes prediction market bets across multiple venues.
    Handles failover, cross-pricing, and best execution.
    """
    
    def __init__(self):
        self.venues = [
            PolymarketClient(),
            AugurTurboClient(),
            HedgehogClient()
        ]
        self.venue_health = {v.__class__.__name__: True for v in self.venues}
        
    async def get_unified_market(self, topic: str, outcome: str) -> dict:
        """
        Get aggregated market data across all healthy venues.
        """
        probabilities = []
        
        for venue in self.venues:
            v_name = venue.__class__.__name__
            if not self.venue_health.get(v_name): continue
                
            try:
                # In a real app, we would search/match the specific market
                # This mock returns generic data for the topic
                markets = await venue.get_markets(topic)
                for market in markets:
                    prob = market.get("probabilities", [0.5])[0] 
                    probabilities.append({
                        "venue": v_name,
                        "probability": prob,
                        "liquidity": market.get("liquidity", 100),
                        "market_id": market.get("market_id")
                    })
            except Exception as e:
                logger.warning("venue_unhealthy", venue=v_name, error=str(e))
                self.venue_health[v_name] = False
                
        if not probabilities:
            raise NoHealthyVenuesError(f"No venues available for {topic}")
            
        # Calculate aggregated probability (volume-weighted)
        total_weight = sum(p["liquidity"] for p in probabilities)
        agg_prob = sum(p["probability"] * p["liquidity"] for p in probabilities) / total_weight
        
        # Find best venue (highest edge vs aggregated - representing best entry)
        best_venue = max(probabilities, key=lambda p: abs(p["probability"] - agg_prob))
        
        return {
            "topic": topic,
            "outcome": outcome,
            "aggregated_probability": agg_prob,
            "venue_probabilities": probabilities,
            "best_venue": best_venue["venue"],
            "best_probability": best_venue["probability"],
            "market_id": best_venue["market_id"]
        }
        
    async def execute_bet(self, bet: dict) -> dict:
        """
        Execute bet on best available venue with automatic failover.
        """
        v_name = bet.get("venue")
        venue = next((v for v in self.venues if v.__class__.__name__ == v_name), self.venues[0])
        
        try:
            result = await venue.place_order(
                bet["market_id"],
                bet["direction"],
                bet["size_usdc"],
                bet.get("price", 0.5)
            )
            if result["success"]: return result
        except Exception as e:
            logger.warning("venue_execution_failed", venue=v_name, error=str(e))
            self.venue_health[v_name] = False
            
        # Failover logic (Try others)
        for v in self.venues:
            if v.__class__.__name__ == v_name or not self.venue_health.get(v.__class__.__name__):
                continue
            try:
                result = await v.place_order(bet["market_id"], bet["direction"], bet["size_usdc"], 0.5)
                if result["success"]: return result
            except: continue
                
        raise AllVenuesFailedError("All venues failed execution.")
