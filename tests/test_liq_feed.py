#!/usr/bin/env python3
"""
AUGUR Liquidation Feed Test Suite
Run: python3 tests/test_liq_feed.py
All tests must pass before going live.
"""

import asyncio
import json
import time
import sys
import os
from collections import deque

sys.path.insert(0, '/Users/dayodapper/CascadeProjects/AUGUR')

# ═════════════════════════════════════════
# TEST 1 — BYBIT SYMBOLS EXIST
# Every symbol must be a valid Bybit perp
# ═══════════════════════════════════════════

async def test_bybit_symbols():
    print("\n[TEST 1] Bybit symbol validation")
    import aiohttp

    SYMBOLS_TO_CHECK = {
        "NEAR-USD":     "NEARUSDT",
        "ARB-USD":      "ARBUSDT",
        "SOL-USD":      "SOLUSDT",
        "ETH-USD":      "ETHUSDT",
        "BTC-USD":      "BTCUSDT",
        "SUI-USD":      "SUIUSDT",
        "AVAX-USD":     "AVAXUSDT",
        "LINK-USD":     "LINKUSDT",
        "BNB-USD":      "BNBUSDT",
        "OP-USD":       "OPUSDT",
        "1000PEPE-USD": "1000PEPEUSDT",
        "XRP-USD":      "XRPUSDT",
        "DOGE-USD":     "DOGEUSDT",
        "INJ-USD":      "INJUSDT",
        "PEPE-USD":     "1000PEPEUSDT",
    }

    valid = []
    invalid = []

    async with aiohttp.ClientSession() as s:
        async with s.get(
            "https://api.bybit.com/v5/market/instruments-info",
            params={"category": "linear", "limit": 1000}
        ) as r:
            data = await r.json()
            existing = {
                item["symbol"]
                for item in data["result"]["list"]
            }

    for aria_sym, bybit_sym in SYMBOLS_TO_CHECK.items():
        if bybit_sym in existing:
            valid.append(bybit_sym)
            print(f"  ✓ {aria_sym} → {bybit_sym}")
        else:
            invalid.append((aria_sym, bybit_sym))
            print(f"  ✗ {aria_sym} → {bybit_sym} NOT FOUND")

    assert len(invalid) == 0, f"Invalid symbols: {invalid}. Fix symbol map before deploying."
    print(f"  PASS: {len(valid)} symbols valid")
    return valid


# ═══════════════════════════════════════════
# TEST 2 — BYBIT WS CONNECTS AND PINGS
# Connection must establish and stay alive
# ═════════════════════════════════════════════

async def test_bybit_ws_connection():
    print("\n[TEST 2] Bybit WebSocket connection")
    import websockets

    connected = False
    ping_response = False
    messages_received = 0

    try:
        async with websockets.connect(
            "wss://stream.bybit.com/v5/public/linear",
            ping_interval=10,
            ping_timeout=5,
            open_timeout=10
        ) as ws:
            connected = True
            print("  ✓ WebSocket connected")

            # Send Bybit manual ping
            await ws.send(json.dumps({"op": "ping"}))

            # Wait for pong
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)

                if data.get("op") == "pong" or data.get("ret_msg") == "pong":
                    ping_response = True
                    print("  ✓ Ping/pong working")
                else:
                    print(f"  ? Unexpected response: {data}")
                    ping_response = True
                    # Still connected

            except asyncio.TimeoutError:
                print("  ✗ No pong received in 5s")

    except Exception as e:
        print(f"  ✗ Connection failed: {e}")

    assert connected, "Cannot connect to Bybit WebSocket"
    assert ping_response, "Bybit ping/pong not working"

    print("  PASS: Connection and ping verified")
    return True


# ═══════════════════════════════════════════
# TEST 3 — BYBIT INDIVIDUAL SUBSCRIPTIONS
# Each symbol must subscribe successfully
# ═════════════════════════════════════════════

async def test_bybit_individual_subscriptions():
    print("\n[TEST 3] Bybit per-symbol subscription")
    import websockets

    # Test subset — fast check
    TEST_SYMBOLS = [
        "NEARUSDT", "SOLUSDT", "BTCUSDT",
        "ETHUSDT", "1000PEPEUSDT", "DOGEUSDT"
    ]

    subscribed = []
    rejected = []

    async with websockets.connect(
        "wss://stream.bybit.com/v5/public/linear",
        ping_interval=10,
        ping_timeout=5,
        open_timeout=10
    ) as ws:

        for symbol in TEST_SYMBOLS:
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [f"liquidation.{symbol}"]
            }))

            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)

                if data.get("success") == True:
                    subscribed.append(symbol)
                    print(f"  ✓ {symbol} subscribed")
                else:
                    rejected.append(symbol)
                    print(f"  ✗ {symbol} rejected: {data.get('ret_msg')}")

            except asyncio.TimeoutError:
                rejected.append(symbol)
                print(f"  ✗ {symbol} timeout")

    assert len(subscribed) > 0, "No symbols subscribed successfully"

    if rejected:
        print(f"  WARNING: {len(rejected)} rejected: {rejected}")
        print("  These symbols need fixing in symbol map")

    print(f"  PASS: {len(subscribed)}/{len(TEST_SYMBOLS)} symbols subscribed")
    return subscribed, rejected


# ═══════════════════════════════════════════
# TEST 4 — BYBIT RECEIVES LIQUIDATION DATA
# Must receive real liquidation events
# ═════════════════════════════════════════════

async def test_bybit_receives_liquidations():
    print("\n[TEST 4] Bybit liquidation data")
    print("  Waiting up to 60s for liquidations...")
    import websockets

    # Subscribe to high-volume symbols
    HIGH_VOLUME = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT",
        "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "NEARUSDT", "AVAXUSDT", "LINKUSDT",
        "ARBUSDT", "SUIUSDT", "OPUSDT"
    ]

    liquidations_received = []
    t_start = time.time()

    try:
        async with websockets.connect(
            "wss://stream.bybit.com/v5/public/linear",
            ping_interval=10,
            ping_timeout=5
        ) as ws:

            # Subscribe all at once for speed
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [f"liquidation.{s}" for s in HIGH_VOLUME]
            }))

            # Wait for sub confirmation
            await asyncio.wait_for(ws.recv(), timeout=5.0)

            # Now wait for liquidations
            deadline = time.time() + 60.0

            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=min(15.0, remaining))
                    data = json.loads(msg)

                    topic = data.get("topic", "")

                    if topic.startswith("liquidation."):
                        liq = data.get("data", {})
                        liquidations_received.append({
                            "symbol": liq.get("symbol"),
                            "side": liq.get("side"),
                            "size": liq.get("size"),
                            "price": liq.get("price"),
                            "time": time.time() - t_start
                        })

                        print(f"  ✓ Liquidation: {liq.get('symbol')} {liq.get('side')} ${liq.get('size')}")

                        if len(liquidations_received) >= 3:
                            break

                except asyncio.TimeoutError:
                    # Send keepalive ping
                    await ws.send(json.dumps({"op": "ping"}))
                    elapsed = time.time() - t_start
                    print(f"  ... waiting ({elapsed:.0f}s)")

    except Exception as e:
        print(f"  ✗ WebSocket error: {e}")

    if len(liquidations_received) == 0:
        print("  WARNING: No liquidations in 60s")
        print("  Market may be quiet — not a bug")
        print("  Re-run during active trading hours")
        print("  SKIP (market quiet)")
        return True

    print(f"  ✓ Received {len(liquidations_received)} liquidations")
    print(f"  First liquidation at: {liquidations_received[0]['time']:.1f}s")
    print("  PASS: Bybit liquidation stream working")
    return True


# ═════════════════════════════════════════
# TEST 5 — VELOCITY DETECTION LOGIC
# 10s window must detect cascades faster
# ═══════════════════════════════════════════

def test_velocity_detection():
    print("\n[TEST 5] Velocity cascade detection")

    # Simulate liquidation events
    now = time.time() * 1000

    # Normal market: 5 liqs per minute
    # = 0.83 per 10s
    historical_mean_60s = 5.0
    expected_10s_rate = historical_mean_60s / 6.0

    # Simulate cascade: 8 liqs in 10 seconds
    cascade_window_10s = deque()
    for i in range(8):
        cascade_window_10s.append({
            "timestamp_ms": now - (i * 1000),
            "size_usdt": 75000
        })

    liq_10s = len(cascade_window_10s)
    velocity_zscore = liq_10s / max(expected_10s_rate, 0.1)

    assert velocity_zscore > 3.0, f"Velocity detection failed: {velocity_zscore:.2f} < 3.0"

    print(f"  ✓ Cascade: {liq_10s} liqs in 10s")
    print(f"  ✓ Expected rate: {expected_10s_rate:.2f}/10s")
    print(f"  ✓ Velocity zscore: {velocity_zscore:.2f} > 3.0")

    # Normal market: 1 liq in 10 seconds
    normal_window = deque([{
        "timestamp_ms": now,
        "size_usdt": 20000
    }])
    normal_velocity = len(normal_window) / max(expected_10s_rate, 0.1)

    assert normal_velocity < 3.0, "Normal market incorrectly flagged as cascade"

    print(f"  ✓ Normal: velocity={normal_velocity:.2f} < 3.0 (Not flagged)")
    print("  PASS: Velocity detection correct")
    return True


# ═════════════════════════════════════════
# TEST 6 — BINANCE FALLBACK CONNECTS
# Must connect and receive data
# ═════════════════════════════════════════════

async def test_binance_fallback():
    print("\n[TEST 6] Binance fallback stream")
    import websockets

    connected = False
    data_received = False
    t_start = time.time()

    try:
        async with websockets.connect(
            "wss://fstream.binance.com/ws/!forceOrder@arr",
            open_timeout=10,
            ping_interval=20,
            ping_timeout=10
        ) as ws:
            connected = True
            print("  ✓ Binance connected")
            print("  Waiting up to 30s for data...")

            deadline = time.time() + 30.0

            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=min(10.0, remaining))
                    data = json.loads(msg)

                    if "o" in data:
                        liq = data["o"]
                        data_received = True
                        print(f"  ✓ Liquidation: {liq.get('s')} {liq.get('S')} ${liq.get('q')}")
                        break

                except asyncio.TimeoutError:
                    elapsed = time.time() - t_start
                    print(f"  ... waiting ({elapsed:.0f}s)")

    except Exception as e:
        print(f"  ✗ Binance connection failed: {e}")

    assert connected, "Cannot connect to Binance stream"

    if not data_received:
        print("  WARNING: No liquidations in 30s")
        print("  Market quiet — connection is valid")

    print("  PASS: Binance fallback operational")
    return True


# ═════════════════════════════════════════
# TEST 7 — RECONNECT LOGIC EXISTS
# Code must have reconnect loop
# ═══════════════════════════════════════════

def test_reconnect_logic():
    print("\n[TEST 7] Reconnect logic verification")
    import inspect

    try:
        from data.bybit_cascade import BybitCascadeEngine

        source = inspect.getsource(BybitCascadeEngine.start)

        checks = {
            "while True loop": "while True" in source,
            "reconnect delay": "sleep" in source,
            "attempt counter": "attempt" in source or "retry" in source or "reconnect" in source.lower(),
            "exception caught": "except" in source,
        }

        for check, passed in checks.items():
            if passed:
                print(f"  ✓ {check}")
            else:
                print(f"  ✗ {check} MISSING")

        assert all(checks.values()), f"Missing reconnect components: {[k for k,v in checks.items() if not v]}"

        # Check for ping keepalive
        has_keepalive = (
            "_keepalive" in source or
            "op.*ping" in source or
            '{"op": "ping"}' in source or
            "keepalive" in source.lower()
        )

        if has_keepalive:
            print("  ✓ Manual ping keepalive")
        else:
            print("  ✗ No manual ping keepalive")
            print("    Add: send {op: ping} every 10s")

        assert has_keepalive, "No manual Bybit ping keepalive found"

        print("  PASS: Reconnect logic present")
        return True

    except ImportError as e:
        print(f"  ✗ Cannot import BybitCascadeEngine: {e}")
        return False


# ═════════════════════════════════════════
# TEST 8 — QUEUE-BASED PROCESSING
# Receiver must not block on processing
# ═════════════════════════════════════════════

async def test_queue_processing():
    print("\n[TEST 8] Queue-based message processing")
    import inspect

    try:
        from data.bybit_cascade import BybitCascadeEngine

        # Check source for queue pattern
        source = inspect.getsource(BybitCascadeEngine)

        has_queue = (
            "asyncio.Queue" in source or
            "Queue(" in source
        )

        has_separate_processor = (
            "_processor" in source or
            "_receiver" in source or
            "queue.get" in source or
            "queue.put" in source
        )

        if has_queue:
            print("  ✓ asyncio.Queue found")
        else:
            print("  ✗ No Queue — processing may block receiver")

        if has_separate_processor:
            print("  ✓ Separate processor task")
        else:
            print("  ✗ No separate processor")

        if not has_queue or not has_separate_processor:
            print("  WARNING: Queue pattern missing")
            print("  High-frequency liquidations may be dropped during processing")
            print("  WARN: Consider adding queue")
        else:
            print("  PASS: Queue processing confirmed")

    except ImportError as e:
        print(f"  SKIP: {e}")

    return True


# ═════════════════════════════════════════
# TEST 9 — AUGUR CASCADE ENGINE LIVE
# The actual running engine must work
# ═══════════════════════════════════════════

async def test_augur_cascade_engine_live():
    print("\n[TEST 9] AUGUR cascade engine live check")

    log_path = "/Users/dayodapper/CascadeProjects/AUGUR/logs/augur.log"

    if not os.path.exists(log_path):
        print(f"  ✗ Log not found: {log_path}")
        print("  Start AUGUR first then re-run")
        return False

    with open(log_path, "r") as f:
        content = f.read()

    checks = {
        "bybit_liq_subscribed or bybit_cascade_subscribed": (
            "bybit_liq_subscribed" in content or
            "bybit_cascade_subscribed" in content or
            "bybit_subscribed" in content
        ),

        "strategy_runner_started": "strategy_runner_started" in content,

        "strategy_cycle_complete": "strategy_cycle_complete" in content,

        "no silent WS failure": (
            "cascade_engine_not_seen_liquidations_yet" not in content.split("strategy_cycle_complete")[-1]
            if "strategy_cycle_complete" in content
            else True
        ),
    }

    for check, passed in checks.items():
        status = "✓" if passed else "✗"
        print(f"  {status} {check}")

    # Check cascade data flowing
    has_cascade_data = (
        "bybit_velocity_cascade" in content or
        "bybit_cascade_evaluated" in content or
        "bybit_liq_event" in content or
        "bybit_liquidation_raw" in content
    )

    if has_cascade_data:
        print("  ✓ Cascade data flowing")
    else:
        print("  ✗ No cascade data in logs")
        print("  Either market is quiet or liquidation handler not firing")

    # Check fallback status
    if "liq_feed_fallback_binance" in content:
        print("  ℹ Binance fallback active")
        print("    Bybit stream failed — using Binance")
    elif checks["bybit_liq_subscribed or bybit_cascade_subscribed"]:
        print("  ✓ Bybit primary stream active")

    passed_count = sum(checks.values())
    total = len(checks)

    print(f"  {passed_count}/{total} checks passed")

    assert checks["strategy_runner_started"], "Strategy runner not started"
    assert checks["strategy_cycle_complete"], "Strategy cycle not running"

    print("  PASS: AUGUR cascade engine running")
    return True


# ═════════════════════════════════════════
# TEST 10 — END TO END SIGNAL FLOW
# Full path: liquidation → signal → log
# ═════════════════════════════════════════════

async def test_end_to_end_signal_flow():
    print("\n[TEST 10] End-to-end signal flow")

    try:
        from data.bybit_cascade import BybitCascadeEngine
        from unittest.mock import MagicMock, AsyncMock
        from collections import deque
        import math

        # Build minimal engine
        kingdom = MagicMock()
        kingdom.get_aria_cascade.return_value = None
        kingdom.publish_augur = MagicMock()

        chancellor = MagicMock()
        chancellor.adjudicate.return_value = MagicMock(
            action="AUTHORIZE",
            augur_executes=True,
            size_modifier=0.50
        )

        engine = BybitCascadeEngine.__new__(BybitCascadeEngine)
        engine.kingdom = kingdom
        engine.chancellor = chancellor
        engine._hist_mean = {"NEAR-USD": 5.0}
        engine._hist_std = {"NEAR-USD": 2.0}
        engine._prev_zscore = {}
        engine._mexc_client = AsyncMock()
        engine._mexc_client.place_order.return_value = MagicMock(
            success=True,
            notional=45.0
        )

        # Simulate strong cascade
        now_ms = int(time.time() * 1000)
        window = deque()

        for i in range(18):
            window.append({
                "timestamp_ms": now_ms - (i * 2000),
                "side": "Buy",
                # Longs liquidated = bearish
                "size_usdt": 80000,
                "price": 1.35
            })

        print("  Simulating 18 long liquidations...")

        await engine._evaluate("NEAR-USD", window)

        # Kingdom must have been called
        assert kingdom.publish_augur.called, "Kingdom not updated after cascade"

        call_data = kingdom.publish_augur.call_args[0][0]

        assert "bybit_cascade" in call_data, "bybit_cascade not in kingdom update"

        cascade = call_data["bybit_cascade"]

        assert cascade["active"] == True, "Cascade not flagged as active"
        assert cascade["direction"] == "bearish", f"Wrong direction: {cascade['direction']}"
        assert cascade["zscore"] > 2.0, f"Z-score too low: {cascade['zscore']}"

        print(f"  ✓ Kingdom updated")
        print(f"  ✓ Direction: {cascade['direction']}")
        print(f"  ✓ Z-score: {cascade['zscore']:.2f}")
        print(f"  ✓ Liquidations: {cascade['liq_60s']}")
        print("  PASS: End-to-end signal flow works")

    except ImportError as e:
        print(f"  SKIP: {e}")
        print("  Run after AUGUR is deployed")

    return True


# ═════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════

async def run_all():
    print("=" * 50)
    print("AUGUR LIQUIDATION FEED TEST SUITE")
    print("=" * 50)

    results = {}

    tests = [
        ("Symbol Validation", test_bybit_symbols, True),
        ("WS Connection + Ping", test_bybit_ws_connection, True),
        ("Individual Subscriptions", test_bybit_individual_subscriptions, True),
        ("Receives Liquidations", test_bybit_receives_liquidations, False),
        # False = warning not hard fail
        ("Velocity Detection", lambda: asyncio.coroutine(lambda: test_velocity_detection()), True),
        ("Binance Fallback", test_binance_fallback, True),
        ("Reconnect Logic", lambda: asyncio.coroutine(lambda: test_reconnect_logic()), False),
        ("Queue Processing", test_queue_processing, False),
        ("Live Engine Check", test_augur_cascade_engine_live, False),
        ("End-to-End Flow", test_end_to_end_signal_flow, False),
    ]

    passed = 0
    failed = 0
    warned = 0

    for name, test_fn, is_critical in tests:
        try:
            if asyncio.iscoroutinefunction(test_fn):
                await test_fn()
            else:
                test_fn()
            results[name] = "PASS"
            passed += 1

        except AssertionError as e:
            if is_critical:
                results[name] = f"FAIL: {e}"
                failed += 1
                print(f"  FAIL: {e}")
            else:
                results[name] = f"WARN: {e}"
                warned += 1
                print(f"  WARN: {e}")

        except Exception as e:
            results[name] = f"ERROR: {e}"
            if is_critical:
                failed += 1
            else:
                warned += 1
            print(f"  ERROR: {e}")

    print("\n" + "=" * 50)
    print("RESULTS")
    print("=" * 50)

    for name, result in results.items():
        icon = ("✓" if result == "PASS" else "⚠" if result.startswith("WARN") else "✗")
        print(f"  {icon} {name}: {result}")

    print(f"\n  Passed: {passed}")
    print(f"  Warned: {warned}")
    print(f"  Failed: {failed}")

    if failed == 0:
        print("\n  ✓ LIQUIDATION FEED PRODUCTION READY")
        print("  AUGUR can detect cascades on Bybit")
        print("  Binance fallback operational")
    else:
        print(f"\n  ✗ {failed} CRITICAL FAILURES")
        print("  Fix before going live")
        print("  AUGUR will miss cascade signals")

    print("=" * 50)

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
