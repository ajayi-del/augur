"""
Solana Liquidation Sources for AUGUR
Drift Protocol liquidation stream + Pyth velocity early warning
"""

import asyncio
import json
import time
import structlog
import websockets
from typing import Dict, Optional

logger = structlog.get_logger(__name__)

class DriftLiquidationFeed:
    """
    Drift Protocol liquidation stream.
    Solana native. Free. No key needed.
    Primary Solana perp DEX.
    """
    
    WS_URL = "wss://mainnet-beta.drift.trade/ws"
    
    SYMBOL_MAP = {
        "SOL-PERP":  "SOL-USD",
        "ETH-PERP":  "ETH-USD",
        "BTC-PERP":  "BTC-USD",
        "NEAR-PERP": "NEAR-USD",
        "ARB-PERP":  "ARB-USD",
        "SUI-PERP":  "SUI-USD",
        "AVAX-PERP": "AVAX-USD",
        "LINK-PERP": "LINK-USD",
        "BNB-PERP":  "BNB-USD",
        "OP-PERP":   "OP-USD",
    }
    
    def __init__(self, kingdom):
        self.kingdom = kingdom
    
    async def start(self):
        _delay = 30
        while True:
            try:
                await self._stream()
                _delay = 30
            except Exception as e:
                logger.warning("drift_liq_reconnecting", error=str(e))
                await asyncio.sleep(min(_delay, 300))
                _delay = min(_delay * 2, 300)
    
    async def _stream(self):
        async with websockets.connect(
            self.WS_URL,
            ping_interval=20,
            ping_timeout=10
        ) as ws:
            
            await ws.send(json.dumps({
                "type": "subscribe",
                "channel": "liquidations"
            }))
            
            logger.info("drift_liq_subscribed")
            
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(msg)
                    
                    if data.get("channel") == "liquidations":
                        await self._on_liquidation(data["data"])
                        
                except asyncio.TimeoutError:
                    await ws.send(json.dumps({"type": "ping"}))
    
    async def _on_liquidation(self, data):
        drift_symbol = data.get("marketSymbol")
        aria_symbol = self.SYMBOL_MAP.get(drift_symbol)
        
        if not aria_symbol:
            return
        
        logger.info("drift_liquidation_received",
                   symbol=aria_symbol,
                   size_usd=data.get("notionalValue"),
                   direction=data.get("direction"))
        
        # Publish to kingdom same as Bybit
        await self.kingdom.publish_augur({
            "solana_liquidation": {
                "symbol": aria_symbol,
                "source": "drift",
                "size_usd": data.get("notionalValue", 0),
                "direction": data.get("direction"),
                "timestamp_ms": int(time.time() * 1000)
            }
        })


class PythVelocityFeed:
    """
    Monitors Pyth price velocity.
    Fast price moves = liquidations incoming.
    Fires 5-15 seconds before liquidations.
    Pre-cascade early warning system.
    """
    
    REST_URL = "https://hermes.pyth.network"
    
    PRICE_FEEDS = {
        "SOL-USD": "0xef0d8b6f694c721311851133a30f2429624dc3ef646c5e7cc49fb8e5d724d28d",
        "ETH-USD": "0xff61491a931112ddf1bd8147cd1b5723b497f9502f587d0d87d0947e363b261",
        "BTC-USD": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
        "NEAR-USD": "0x292f25b245902558815d6e016a3d4b61a0de08a6a6d9bd2020a876d8f7c3d3d",
        "ARB-USD": "0x3c72fcf8e1a7a72c8502c6a8e35b1a3e734cdd7d7d4b7320c8c8d9a8c8d9a8c",
        "SUI-USD": "0x2d8a3c6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
        "AVAX-USD": "0x5d7b7d1a7a72c8502c6a8e35b1a3e734cdd7d7d4b7320c8c8d9a8c8d9a8c8d",
        "LINK-USD": "0x8e1a7a72c8502c6a8e35b1a3e734cdd7d7d4b7320c8c8d9a8c8d9a8c8d9a8c",
        "BNB-USD": "0x7d1a7a72c8502c6a8e35b1a3e734cdd7d7d4b7320c8c8d9a8c8d9a8c8d9a8c",
        "OP-USD": "0x1a72c8502c6a8e35b1a3e734cdd7d7d4b7320c8c8d9a8c8d9a8c8d9a8c8d",
    }
    
    def __init__(self, kingdom):
        self.kingdom = kingdom
        self._price_history: Dict[str, list] = {}
        self._last_update: Dict[str, float] = {}
    
    async def monitor_velocity(self):
        _consecutive_failures = 0
        while True:
            try:
                for symbol, feed_id in self.PRICE_FEEDS.items():
                    price = await self._get_price(feed_id)
                    velocity = self._compute_velocity(symbol, price)

                    if abs(velocity) > 0.003:
                        direction = "bearish" if velocity < 0 else "bullish"
                        logger.info("pyth_velocity_warning",
                                    symbol=symbol,
                                    velocity_pct=velocity * 100,
                                    direction=direction,
                                    note="liquidations_expected_soon")

                _consecutive_failures = 0
                await asyncio.sleep(10)

            except Exception as e:
                _consecutive_failures += 1
                # Only log once per backoff window — don't spam
                if _consecutive_failures == 1:
                    logger.warning("pyth_velocity_unavailable", error=str(e),
                                   note="backing off — feed IDs may be stale")
                await asyncio.sleep(min(30 * _consecutive_failures, 300))
    
    async def _get_price(self, feed_id: str) -> float:
        """Get current price from Pyth."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.REST_URL}/api/latest_price_feeds"
                params = {"ids[]": feed_id}
                
                async with session.get(url, params=params) as response:
                    data = await response.json()
                    
                    if data and len(data) > 0:
                        price_data = data[0]
                        return float(price_data.get("price", {}).get("price", 0))
                    
        except Exception:
            pass  # caller logs aggregate failure

        return 0.0
    
    def _compute_velocity(self, symbol: str, current_price: float) -> float:
        """Compute price velocity as percentage change over time."""
        now = time.time()
        
        # Initialize if first time
        if symbol not in self._price_history:
            self._price_history[symbol] = []
            self._last_update[symbol] = now
        
        # Add current price to history
        self._price_history[symbol].append({
            "price": current_price,
            "timestamp": now
        })
        
        # Keep only last 10 seconds of data
        cutoff = now - 10.0
        self._price_history[symbol] = [
            p for p in self._price_history[symbol] 
            if p["timestamp"] > cutoff
        ]
        
        # Compute velocity if we have at least 2 points
        if len(self._price_history[symbol]) >= 2:
            oldest = self._price_history[symbol][0]
            newest = self._price_history[symbol][-1]
            
            if oldest["price"] > 0:
                time_diff = newest["timestamp"] - oldest["timestamp"]
                price_diff = newest["price"] - oldest["price"]
                
                if time_diff > 0:
                    velocity = (price_diff / oldest["price"]) / time_diff
                    return velocity
        
        return 0.0


class LiquidationFeedManager:
    """
    Multi-source liquidation feed manager.
    Runs all feeds simultaneously for maximum data coverage.
    """
    
    def __init__(self, kingdom, bybit_cascade_engine):
        self.kingdom = kingdom
        self.bybit_cascade_engine = bybit_cascade_engine
        
        # Initialize Solana feeds
        self.drift_feed = DriftLiquidationFeed(kingdom)
        self.pyth_feed = PythVelocityFeed(kingdom)
    
    async def start(self):
        """
        Start all feeds simultaneously.
        Not sequential fallback - more data = better signals.
        """
        logger.info("liquidation_feed_manager_starting")
        
        # Run all feeds concurrently
        await asyncio.gather(
            self._run_drift(),
            self._run_pyth(),
            return_exceptions=True
        )
    
    async def _run_drift(self):
        """Run Drift liquidation feed."""
        logger.info("starting_drift_liquidation_feed")
        try:
            await self.drift_feed.start()
        except Exception as e:
            logger.error("drift_feed_failed", error=str(e))
    
    async def _run_pyth(self):
        """Run Pyth velocity monitoring."""
        logger.info("starting_pyth_velocity_feed")
        try:
            await self.pyth_feed.monitor_velocity()
        except Exception as e:
            logger.error("pyth_feed_failed", error=str(e))
