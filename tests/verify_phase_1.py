import os
import json
import time
import unittest
import threading
from pathlib import Path
from kingdom.state_sync import KingdomStateSync, AugurState, AgentBet, AriaState

# Constants for testing
TEST_STATE_PATH = "/tmp/augur_verify_kingdom_state.json"

class TestKingdomSync(unittest.TestCase):
    
    def setUp(self):
        # Cleanup before test
        if os.path.exists(TEST_STATE_PATH):
            os.remove(TEST_STATE_PATH)
        if os.path.exists(TEST_STATE_PATH + ".lock"):
            os.remove(TEST_STATE_PATH + ".lock")
        self.sync_client = KingdomStateSync(TEST_STATE_PATH)

    def tearDown(self):
        # Cleanup after test
        if os.path.exists(TEST_STATE_PATH):
            os.remove(TEST_STATE_PATH)
        if os.path.exists(TEST_STATE_PATH + ".lock"):
            os.remove(TEST_STATE_PATH + ".lock")

    def test_1_empty_file_handling(self):
        """Test 1: Delete file and read should not crash."""
        state = self.sync_client.read()
        self.assertEqual(state.version, "2.0")
        self.assertEqual(state.aria.regime, "unknown")
        self.assertEqual(state.augur.active_bets, [])
        print("Test 1 Passed: Empty file handling")

    def test_2_write_and_read_back(self):
        """Test 2: Create AugurState, write, and verify read back."""
        new_augur = AugurState(
            active_polymarket_bets=[{"id": "test_bet", "target": "YES"}],
            etf_flow_direction="strong_inflow",
            solana_health_score=0.95
        )
        self.sync_client.write_augur_state(new_augur)
        
        state = self.sync_client.read()
        self.assertEqual(state.augur.etf_flow_direction, "strong_inflow")
        self.assertEqual(state.augur.solana_health_score, 0.95)
        self.assertEqual(state.augur.active_polymarket_bets[0]["id"], "test_bet")
        print("Test 2 Passed: Write and read back")

    def test_3_aria_bet_extraction(self):
        """Test 3: Verify expired bets are filtered from ARIA state."""
        now_ms = int(time.time() * 1000)
        
        active_bet = {
            "agent_id": "aria",
            "symbol": "BTC-USD",
            "direction": "long",
            "expires_ms": now_ms + 10000 # 10s future
        }
        expired_bet = {
            "agent_id": "aria",
            "symbol": "ETH-USD",
            "direction": "short",
            "expires_ms": now_ms - 10000 # 10s past
        }
        
        # Manually prepare the kingdom state file
        with open(TEST_STATE_PATH, "w") as f:
            json.dump({
                "aria": {
                    "active_bets": [active_bet, expired_bet],
                    "regime": "cascade"
                }
            }, f)
        
        aria_state = self.sync_client.read_aria_state()
        self.assertEqual(len(aria_state.active_bets), 1)
        self.assertEqual(aria_state.active_bets[0]["symbol"], "BTC-USD")
        self.assertEqual(aria_state.regime, "cascade")
        print("Test 3 Passed: ARIA bet extraction (expiry filtering)")

    def test_4_concurrent_write_safety(self):
        """Test 4: Launch 5 threads and ensure JSON integrity."""
        def worker(val):
            for i in range(10):
                augur = AugurState(etf_flow_direction=f"dir_{val}_{i}")
                self.sync_client.write_augur_state(augur)
                time.sleep(0.01)

        threads = []
        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Read back and ensure it is valid JSON
        state = self.sync_client.read()
        self.assertEqual(state.version, "2.0")
        self.assertTrue("dir_" in state.augur.etf_flow_direction)
        print("Test 4 Passed: Concurrent write safety")

    def test_5_aria_bet_for_specific_symbol(self):
        """Test 5: Extraction by symbol filtered by expiry."""
        now_ms = int(time.time() * 1000)
        
        aria_data = {
            "active_bets": [
                {"agent_id": "aria", "symbol": "BTC-USD", "direction": "long", "expires_ms": now_ms + 10000, "confidence": 0.8, "evidence_type": "micro", "coherence": 7.0, "timestamp_ms": now_ms},
                {"agent_id": "aria", "symbol": "ETH-USD", "direction": "short", "expires_ms": now_ms + 10000, "confidence": 0.6, "evidence_type": "micro", "coherence": 5.0, "timestamp_ms": now_ms}
            ]
        }
        
        with open(TEST_STATE_PATH, "w") as f:
            json.dump({"aria": aria_data}, f)
            
        btc_bets = self.sync_client.get_active_aria_bets("BTC-USD")
        self.assertEqual(len(btc_bets), 1)
        self.assertEqual(btc_bets[0].symbol, "BTC-USD")
        self.assertEqual(btc_bets[0].agent_id, "aria")
        print("Test 5 Passed: Symbol specific bet extraction")

if __name__ == "__main__":
    unittest.main()
