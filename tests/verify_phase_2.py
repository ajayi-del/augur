import os
import asyncio
import unittest
from data.sosovalue_feed import SoSoValueFeed, ETFFlowData, NewsItem
from core.config import config as settings

class TestSoSoValueFeed(unittest.IsolatedAsyncioTestCase):
    
    def setUp(self):
        self.api_key = settings.sosovalue_api_key
        if not self.api_key:
            self.skipTest("SOSOVALUE_API_KEY not found in .env")
        self.feed = SoSoValueFeed(self.api_key)

    async def test_1_btc_etf_flow_real(self):
        """Test 1: Real BTC ETF flow call."""
        result = await self.feed.get_etf_flow("BTC")
        print(f"\nReal BTC ETF Flow: {result}")
        self.assertIsInstance(result, ETFFlowData)
        self.assertIsInstance(result.net_flow_usd, float)
        self.assertIn(result.flow_direction, ["strong_inflow", "inflow", "neutral", "outflow", "strong_outflow"])

    async def test_2_eth_etf_flow_real(self):
        """Test 2: Real ETH ETF flow call."""
        result = await self.feed.get_etf_flow("ETH")
        print(f"Real ETH ETF Flow: {result}")
        self.assertIsInstance(result, ETFFlowData)
        self.assertIn(result.flow_direction, ["strong_inflow", "inflow", "neutral", "outflow", "strong_outflow"])

    async def test_3_news_real_call(self):
        """Test 3: Real news call for BTC."""
        news = await self.feed.get_news("BTC", categories=[3, 5, 6])
        print(f"Real News Items Count: {len(news)}")
        if news:
            item = news[0]
            print(f"Sample News: {item.title} | Direction: {item.direction}")
            self.assertIsInstance(item, NewsItem)
            self.assertTrue(hasattr(item, 'hours_old'))
            self.assertIn(item.direction, ["bullish", "bearish", "neutral"])

    async def test_4_news_freshness_filter(self):
        """Test 4: Verify freshness filter works."""
        # Test with very strict 0.01 hours (36 seconds)
        fresh_news = await self.feed.get_news("BTC", max_hours_old=0.01)
        # It's likely empty unless news just dropped, which is fine as long as logic holds
        for item in fresh_news:
            self.assertLessEqual(item.hours_old, 0.01)
        print(f"Strict News Filter (0.01h) returned {len(fresh_news)} items")

    def test_5_direction_computation(self):
        """Test 5: Unit test direction classification logic."""
        # Bullish
        bull_title = "BlackRock IBIT records record inflow and surge in adoption"
        self.assertEqual(self.feed._compute_direction(bull_title), "bullish")
        
        # Bearish
        bear_title = "SEC rejects Bitcoin ETF application amid crash fears and ban regulation"
        self.assertEqual(self.feed._compute_direction(bear_title), "bearish")
        
        # Neutral
        neutral_title = "The price of bitcoin is currently sideways"
        self.assertEqual(self.feed._compute_direction(neutral_title), "neutral")
        print("Test 5 Passed: Keyword direction classification")

if __name__ == "__main__":
    unittest.main()
