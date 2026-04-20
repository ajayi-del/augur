import unittest
import time
from polymarket.probability_engine import ProbabilityEngine, AugurProbability
from polymarket.kelly_sizer import KellySizer
from data.sosovalue_feed import NewsItem, ETFFlowData
from kingdom.state_sync import AgentBet

class TestPhase4(unittest.TestCase):
    
    def setUp(self):
        self.engine = ProbabilityEngine()
        self.sizer = KellySizer(bankroll=1000.0)

    def test_1_base_rate(self):
        """Test 1: P(augur) with no signals should be 0.50."""
        # Now
        now = time.time()
        res = self.engine.compute_augur_probability(
            market_id="m1", target_asset="BTC", expiry_timestamp=now + 86400
        )
        self.assertAlmostEqual(res.probability, 0.50)
        self.assertEqual(res.confidence, 1.0) # Perfect alignment (all are 0.5)

    def test_2_aria_horizon_discount(self):
        """Test 2: ARIA signal should decay over market horizon."""
        aria_bet = AgentBet(
            agent_id="aria", symbol="BTC-USD", direction="long", 
            confidence=0.8, evidence_type="micro", coherence=7.0, 
            timestamp_ms=0, expires_ms=0
        )
        
        # 1. Short horizon (1 hour)
        p_short = self.engine._calculate_aria_signal(aria_bet, 1.0)
        # 2. Long horizon (100 hours)
        p_long = self.engine._calculate_aria_signal(aria_bet, 100.0)
        
        # p_short should be closer to 0.8 than p_long
        self.assertGreater(p_short, p_long)
        self.assertGreater(p_long, 0.50)
        print(f"ARIA Signal Short (1h): {p_short:.3f} | Long (100h): {p_long:.3f}")

    def test_3_etf_impact(self):
        """Test 3: ETF flow should shift probability."""
        flow = ETFFlowData(date="", net_flow_usd=600_000_000, flow_direction="strong_inflow")
        res = self.engine.compute_augur_probability(
            market_id="m1", target_asset="BTC", expiry_timestamp=time.time() + 86400,
            etf_flow=flow
        )
        # ETF weight is 0.3, Signal is 0.73 (Phase 7 mapping)
        # Expected = 0.73*0.3 + 0.5*0.7 = 0.219 + 0.35 = 0.569
        self.assertAlmostEqual(res.probability, 0.569)

    def test_4_kelly_sizing(self):
        """Test 4: Kelly sizing with various edges."""
        # No edge
        size_0 = self.sizer.calculate_bet_size(p_augur=0.50, p_market=0.50)
        self.assertEqual(size_0, 0.0)
        
        # 10% edge (P_augur=0.6, P_market=0.5)
        # Bankroll 1000, max_cap 5% (50.0)
        # raw_kelly = (0.6-0.5)/(1-0.5) = 0.1/0.5 = 0.2
        # half_kelly = 0.1
        # size = 10% of 1000 = 100.0
        # capped at 50.0
        size_10 = self.sizer.calculate_bet_size(p_augur=0.60, p_market=0.50)
        self.assertEqual(size_10, 50.0)
        
        # Smaller edge (P_augur=0.52, P_market=0.5)
        # raw_kelly = 0.02/0.5 = 0.04
        # half_kelly = 0.02
        # size = 20.0
        size_2 = self.sizer.calculate_bet_size(p_augur=0.52, p_market=0.50)
        self.assertEqual(size_2, 20.0)
        print(f"Kelly Size (10% edge): {size_10} | (2% edge): {size_2}")

if __name__ == "__main__":
    unittest.main()
