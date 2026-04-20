import os
import json
import time
import pytest
import threading
from pathlib import Path
from kingdom.state_sync import KingdomStateSync, AugurState, AgentBet, AriaState

# Constants for testing
TEST_STATE_PATH = "/tmp/augur_test_kingdom_state.json"

@pytest.fixture
def sync_client():
    # Cleanup before test
    if os.path.exists(TEST_STATE_PATH):
        os.remove(TEST_STATE_PATH)
    if os.path.exists(TEST_STATE_PATH + ".lock"):
        os.remove(TEST_STATE_PATH + ".lock")
    
    client = KingdomStateSync(TEST_STATE_PATH)
    yield client
    
    # Cleanup after test
    if os.path.exists(TEST_STATE_PATH):
        os.remove(TEST_STATE_PATH)
    if os.path.exists(TEST_STATE_PATH + ".lock"):
        os.remove(TEST_STATE_PATH + ".lock")

def test_empty_file_handling(sync_client):
    """Test 1: Delete file and read should not crash."""
    state = sync_client.read()
    assert state.version == "2.0"
    assert state.aria.regime == "unknown"
    assert state.augur.active_bets == []

def test_write_and_read_back(sync_client):
    """Test 2: Create AugurState, write, and verify read back."""
    new_augur = AugurState(
        active_polymarket_bets=[{"id": "test_bet", "target": "YES"}],
        etf_flow_direction="strong_inflow",
        solana_health_score=0.95
    )
    sync_client.write_augur_state(new_augur)
    
    state = sync_client.read()
    assert state.augur.etf_flow_direction == "strong_inflow"
    assert state.augur.solana_health_score == 0.95
    assert state.augur.active_polymarket_bets[0]["id"] == "test_bet"

def test_aria_bet_extraction(sync_client):
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
    
    aria_state = sync_client.read_aria_state()
    assert len(aria_state.active_bets) == 1
    assert aria_state.active_bets[0]["symbol"] == "BTC-USD"
    assert aria_state.regime == "cascade"

def test_concurrent_write_safety(sync_client):
    """Test 4: Launch 5 threads and ensure JSON integrity."""
    def worker(val):
        for i in range(10):
            augur = AugurState(etf_flow_direction=f"dir_{val}_{i}")
            sync_client.write_augur_state(augur)
            time.sleep(0.01)

    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Read back and ensure it is valid JSON
    state = sync_client.read()
    assert state.version == "2.0"
    assert "dir_" in state.augur.etf_flow_direction

def test_aria_bet_for_specific_symbol(sync_client):
    """Test 5: Extraction by symbol filtered by expiry."""
    now_ms = int(time.time() * 1000)
    
    aria_data = {
        "active_bets": [
            {"agent_id": "aria", "symbol": "BTC-USD", "direction": "long", "expires_ms": now_ms + 1000, "confidence": 0.8, "evidence_type": "micro", "coherence": 7.0, "timestamp_ms": now_ms},
            {"agent_id": "aria", "symbol": "ETH-USD", "direction": "short", "expires_ms": now_ms + 1000, "confidence": 0.6, "evidence_type": "micro", "coherence": 5.0, "timestamp_ms": now_ms}
        ]
    }
    
    with open(TEST_STATE_PATH, "w") as f:
        json.dump({"aria": aria_data}, f)
        
    btc_bets = sync_client.get_active_aria_bets("BTC-USD")
    assert len(btc_bets) == 1
    assert btc_bets[0].symbol == "BTC-USD"
    assert btc_bets[0].agent_id == "aria"

if __name__ == "__main__":
    # If run directly, run pytest
    import sys
    pytest.main([__file__])
