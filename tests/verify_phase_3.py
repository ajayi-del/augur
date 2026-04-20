import unittest
from intelligence.kant_news import KantNews
from data.sosovalue_feed import NewsItem, ETFFlowData

class TestKantNews(unittest.TestCase):
    
    def setUp(self):
        self.kant = KantNews()

    def test_1_recency_gate(self):
        """Test 1: Reject news > 4h."""
        old_news = NewsItem(
            id="1", title="Stale News", content_plain="", release_time_ms=0, 
            category=3, hours_old=4.5, direction="bullish"
        )
        is_sig, weight, reason = self.kant.validate_structural_soundness(old_news)
        self.assertFalse(is_sig)
        self.assertIn("STALE", reason)

    def test_2_category_gate(self):
        """Test 2: Reject non-institutional news."""
        retail_news = NewsItem(
            id="2", title="Retail Pump", content_plain="", release_time_ms=0, 
            category=1, hours_old=1.0, direction="bullish"
        )
        is_sig, weight, reason = self.kant.validate_structural_soundness(retail_news)
        self.assertFalse(is_sig)
        self.assertIn("NON_INSTITUTIONAL", reason)

    def test_3_contradiction_logic(self):
        """Test 3: Reject bullish news during strong ETF outflow."""
        flow = ETFFlowData(date="2026-04-19", net_flow_usd=-600_000_000, flow_direction="strong_outflow")
        news = NewsItem(
            id="3", title="Bullish Rumor", content_plain="", release_time_ms=0, 
            category=3, hours_old=1.0, direction="bullish"
        )
        is_sig, weight, reason = self.kant.validate_structural_soundness(news, flow)
        self.assertFalse(is_sig)
        self.assertIn("CONTRADICTION", reason)
        print(f"Contradiction Test Passed: {reason}")

    def test_4_weight_calculation(self):
        """Test 4: Verify institutional weight + freshness decay."""
        # Cat 3 (0.90) + 0h old (1.0 factor) = 0.90
        fresh_news = NewsItem(
            id="4", title="Fresh Bull", content_plain="", release_time_ms=0, 
            category=3, hours_old=0.0, direction="bullish"
        )
        is_sig, weight, reason = self.kant.validate_structural_soundness(fresh_news)
        self.assertTrue(is_sig)
        self.assertAlmostEqual(weight, 0.90)
        
        # Cat 3 (0.90) + 4h old (0.5 factor decay) = 0.45
        aged_news = NewsItem(
            id="5", title="Aged Bull", content_plain="", release_time_ms=0, 
            category=3, hours_old=4.0, direction="bullish"
        )
        is_sig, weight, reason = self.kant.validate_structural_soundness(aged_news)
        self.assertTrue(is_sig)
        self.assertAlmostEqual(weight, 0.45)
        print("Test 4 Passed: Weight calculation and decay")

if __name__ == "__main__":
    unittest.main()
