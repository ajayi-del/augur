import os
import json
import time
import unittest
from pathlib import Path
from kingdom.state_sync import KingdomStateSync, AgentBet, AriaState
from polymarket.probability_engine import ProbabilityEngine
from polymarket.kelly_sizer import KellySizer

# Constants
TEST_KINGDOM_PATH = "/tmp/augur_pathway_kingdom.json"

class TestAriaSignalPathway(unittest.TestCase):
    
    def setUp(self):
        if os.path.exists(TEST_KINGDOM_PATH):
            os.remove(TEST_KINGDOM_PATH)
        self.sync = KingdomStateSync(TEST_KINGDOM_PATH)
        self.engine = ProbabilityEngine()
        self.sizer = KellySizer(bankroll=1000.0)

    def test_aria_to_augur_pathway(self):
        """
        END-TO-END: The Sovereign Link
        Verifies that an ARIA signal correctly influences an AUGUR prediction.
        """
        print("\n--- Begin End-to-End Pathway Test ---")
        
        # 1. Simulate ARIA publishing a signal to the kingdom state
        now_ms = int(time.time() * 1000)
        aria_signal = {
            "agent_id": "aria",
            "symbol": "BTC-USD",
            "direction": "long",
            "confidence": 0.85,
            "evidence_type": "microstructure",
            "coherence": 8.0,
            "timestamp_ms": now_ms,
            "expires_ms": now_ms + 1800000 # 30 mins
        }
        
        with open(TEST_KINGDOM_PATH, "w") as f:
            json.dump({
                "aria": {
                    "active_bets": [aria_signal],
                    "regime": "trending"
                },
                "version": "2.0"
            }, f)
        
        print("Step 1: ARIA signal injected into kingdom_state.json")

        # 2. AUGUR read from the kingdom state
        active_aria_bets = self.sync.get_active_aria_bets("BTC-USD")
        self.assertEqual(len(active_aria_bets), 1)
        self.assertEqual(active_aria_bets[0].direction, "long")
        print(f"Step 2: AUGUR retrieved ARIA signal (Direction: {active_aria_bets[0].direction})")

        # 3. Compute P(augur) influenced by ARIA
        # Assume a 1-hour horizon market (very sensitive to ARIA)
        expiry = time.time() + 3600
        res = self.engine.compute_augur_probability(
            market_id="m_pathway",
            target_asset="BTC",
            expiry_timestamp=expiry,
            aria_bet=active_aria_bets[0]
        )
        
        # ARIA (long) at 1h horizon should give p_aria ~ 0.785
        # Weighted by 40% -> 0.785*0.4 + 0.5*0.6 = 0.314 + 0.3 = 0.614
        self.assertGreater(res.probability, 0.60)
        print(f"Step 3: P(augur) shifted from 0.50 -> {res.probability:.3f} (Coherence: {active_aria_bets[0].coherence})")

        # 4. Calculate Sizing
        # Market price is 0.52 (52 cents)
        size = self.sizer.calculate_bet_size(res.probability, 0.52)
        # raw_kelly = (0.614 - 0.52) / 0.48 = 0.094 / 0.48 = 0.195
        # half_kelly = 0.097 -> ~97.00 capitalized
        # capped at 50.00
        self.assertEqual(size, 50.00)
        print(f"Step 4: Kelly Size determined: ${size:.2f} (Edge: {round(res.probability - 0.52, 3)})")
        
        print("--- Pathway Test Passed: ARIA signal successfully expressed in AUGUR execution ---")

    def test_isolation_from_aria(self):
        """Verifies that no ARIA internal modules are imported."""
        import sys
        aria_modules = [m for m in sys.modules if "ARIA" in m]
        # We expect 0 ARIA modules unless they were accidentally imported
        self.assertEqual(len(aria_modules), 0, f"Isolation failure! ARIA modules found: {aria_modules}")
        print("Test Passed: AUGUR is successfully isolated from ARIA internals.")

if __name__ == "__main__":
    unittest.main()
