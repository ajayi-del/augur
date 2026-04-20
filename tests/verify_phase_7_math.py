import unittest
import time
import math
import numpy as np
from polymarket.probability_engine import ProbabilityEngine, AugurProbability
from data.sosovalue_feed import NewsItem, ETFFlowData
from kingdom.state_sync import AgentBet

class TestPhase7Math(unittest.TestCase):
    
    def setUp(self):
        self.engine = ProbabilityEngine()

    def test_1_exponential_news_decay(self):
        """
        Verify: news_base * e^(-0.15 * h) + 0.5 * (1 - e^(-0.15 * h))
        For bullish news (base 0.75):
        h=0 -> 0.75
        h=2 -> 0.75 * e^-0.3 + 0.5 * (1 - e^-0.3)
               0.75 * 0.7408 + 0.5 * 0.2592 = 0.5556 + 0.1296 = 0.6852
        """
        # Fresh news (0h)
        n0 = NewsItem(id="1", title="Bullish", content_plain="", release_time_ms=0, 
                      category=3, hours_old=0.0, direction="bullish", kant_weight=0.9)
        p0 = self.engine._calculate_news_signal([n0])
        self.assertAlmostEqual(p0, 0.75)
        
        # 2h old news
        n2 = NewsItem(id="2", title="Bullish 2h", content_plain="", release_time_ms=0, 
                      category=3, hours_old=2.0, direction="bullish", kant_weight=0.9)
        p2 = self.engine._calculate_news_signal([n2])
        self.assertAlmostEqual(p2, 0.68519, places=4)
        print(f"News Decay Test: 0h={p0:.4f}, 2h={p2:.4f}")

    def test_2_aria_horizon_discount(self):
        """
        Verify: p_aria * discount + 0.5 * (1 - discount)
        discount = exp(-0.05 * h)
        """
        aria = AgentBet("aria", "BTC-USD", "long", 0.9, "micro", 7.2, 0, 0)
        # Coherence 7.2 maps to: 0.5 + (7.2-4)*0.07 = 0.5 + 0.224 = 0.724
        
        # 0h horizon
        p0 = self.engine._calculate_aria_signal(aria, 0.0)
        self.assertAlmostEqual(p0, 0.724)
        
        # 100h horizon (discount = e^-5 = 0.0067)
        p100 = self.engine._calculate_aria_signal(aria, 100.0)
        # 0.724 * 0.0067 + 0.5 * 0.9933 = 0.00485 + 0.4966 = 0.5014
        self.assertAlmostEqual(p100, 0.5015, places=3)
        print(f"ARIA Horizon Test: 0h={p0:.4f}, 100h={p100:.4f}")

    def test_3_weighted_synthesis_and_confidence(self):
        """
        Verify 40/30/20/10 weight and 1.0 - std*3.0 confidence.
        """
        # aria=0.724 (w=0.4), etf=0.73 (w=0.3), news=0.75 (w=0.2), history=0.5 (w=0.1)
        # final = 0.724*0.4 + 0.73*0.3 + 0.75*0.2 + 0.5*0.1
        # final = 0.2896 + 0.219 + 0.15 + 0.05 = 0.7086
        aria = AgentBet("aria", "BTC-USD", "long", 0.9, "micro", 7.2, 0, 0)
        etf = ETFFlowData("", 0, "strong_inflow")
        news = [NewsItem(
            id="n", title="B", content_plain="", release_time_ms=0, 
            category=3, hours_old=0.0, direction="bullish", kant_weight=0.9
        )]
        
        res = self.engine.compute_augur_probability("m", "BTC", time.time(), aria, etf, news)
        self.assertAlmostEqual(res.probability, 0.7086)
        
        # Confidence logic
        signals = [0.724, 0.73, 0.75, 0.5]
        std = np.std(signals)
        expected_conf = max(0.0, 1.0 - (std * 3.0))
        self.assertAlmostEqual(res.confidence, expected_conf)
        print(f"Synthesis Test: P={res.probability:.4f}, Conf={res.confidence:.4f}")

if __name__ == "__main__":
    unittest.main()
