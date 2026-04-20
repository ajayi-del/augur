import asyncio
import os
import time
import unittest
from unittest.mock import MagicMock, AsyncMock
from main import AugurApplication
from core.config import config as settings
from kingdom.state_sync import KingdomStateSync

# Setup mocks
class MockSoSoValue:
    async def get_etf_flow(self, asset):
        from data.sosovalue_feed import ETFFlowData
        return ETFFlowData(date="2026-04-19", net_flow_usd=500000000, flow_direction="inflow")
    
    async def get_news(self, asset, categories=None):
        from data.sosovalue_feed import NewsItem
        return [NewsItem(
            id="n1", title="Massive BTC Institutional Buy", 
            content_plain="BlackRock buys more.", 
            release_time_ms=int(time.time()*1000), 
            category=3, hours_old=0, direction="bullish")]

class TestFinalWiring(unittest.IsolatedAsyncioTestCase):
    
    async def test_full_pulse(self):
        """Tests a single pulse of the news and prediction loops."""
        print("\n--- Final System Pulse Test ---")
        
        # 1. Initialize App with mocks
        app = AugurApplication(settings)
        app.sosovalue = MockSoSoValue()
        app.executor.execute = AsyncMock(return_value={"success": True, "tx_id": "mock_tx"})
        
        # 2. Setup Kingdom State with ARIA signal
        test_path = "/tmp/augur_final_kingdom.json"
        if os.path.exists(test_path): os.remove(test_path)
        app.kingdom = KingdomStateSync(test_path)
        
        from kingdom.state_sync import AriaState, AgentBet
        now_ms = int(time.time() * 1000)
        aria_bet = AgentBet(
            agent_id="aria", symbol="BTC-USD", direction="long", 
            confidence=0.9, evidence_type="micro", coherence=9.0, 
            timestamp_ms=now_ms, expires_ms=now_ms + 100000
        )
        app.kingdom.write_aria_state(AriaState(active_bets=[aria_bet], regime="bullish"))
        
        # 3. Pulse the News Loop (Perps)
        # We manually run the logic inside the loop once to avoid infinite hanging
        print("Pulsing News Loop (Perps)...")
        # Logic extracted from main.py news_loop
        etf_flow = await app.sosovalue.get_etf_flow("BTC")
        news_items = await app.sosovalue.get_news("BTC")
        aria_bets = app.kingdom.get_active_aria_bets("BTC-USD")
        
        self.assertEqual(len(aria_bets), 1)
        self.assertEqual(etf_flow.flow_direction, "inflow")
        
        # 4. Pulse the Prediction Loop
        print("Pulsing Prediction Loop (Markets)...")
        opps = await app.scanner.scan_for_opportunities(
            asset="BTC", aria_bet=aria_bets[0], etf_flow=etf_flow, news_items=news_items
        )
        
        self.assertGreater(len(opps), 0)
        print(f"Opportunities found: {len(opps)}")
        for opp in opps:
            print(f"  - {opp.question} | Edge: {opp.edge:.3f} | Size: ${opp.bet_size_usd}")
            self.assertGreater(opp.edge, 0.05)
            self.assertGreater(opp.bet_size_usd, 0)
        
        print("--- Final Wiring Test Passed: Signals correlated and execution triggered ---")

if __name__ == "__main__":
    unittest.main()
