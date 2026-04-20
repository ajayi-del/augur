"""
BybitFeed — AUGUR's primary real-time market data signal source.

Replaces Jupiter/CoinGecko. Bybit public WebSocket resolves from GCP Frankfurt.
No API key required for market data. Same Bybit credentials used for execution.

Streams:
  tickers.{symbol}       → mark price, open interest, 24h change
  orderbook.25.{symbol}  → bid/ask depth → aggregate ratio
  liquidation.{symbol}   → liquidation events (last 60s count)

Signal outputs wired into augur_signal_loop:
  mark_price   → price momentum
  agg_ratio    → bid_vol/(bid+ask) order imbalance (>0.6 bullish, <0.4 bearish)
  funding_rate → from REST /v5/market/funding/history (cached 5 min)
  liq_count    → liquidation intensity as volatility proxy
"""

import asyncio
import json
import ssl
import time
import certifi
import aiohttp
import structlog
import websockets
from typing import Dict, Optional, Tuple

logger = structlog.get_logger(__name__)

_BYBIT_WS   = "wss://stream.bybit.com/v5/public/linear"
_BYBIT_REST = "https://api.bybit.com"

# ARIA symbol → Bybit linear symbol
_SYMBOL_MAP: Dict[str, str] = {
    "SOL-USD":       "SOLUSDT",
    "BTC-USD":       "BTCUSDT",
    "ETH-USD":       "ETHUSDT",
    "DOGE-USD":      "DOGEUSDT",
    "WIF-USD":       "WIFUSDT",
    "BONK-USD":      "BONKUSDT",
    "TRUMP-USD":     "TRUMPUSDT",
    "PEPE-USD":      "PEPEUSDT",
    "SUI-USD":       "SUIUSDT",
    "ARB-USD":       "ARBUSDT",
    "OP-USD":        "OPUSDT",
    "MNT-USD":       "MNTUSDT",
    "AVAX-USD":      "AVAXUSDT",
    "BNB-USD":       "BNBUSDT",
    "HYPE-USD":      "HYPEUSDT",
    "ENA-USD":       "ENAUSDT",
    "EDGE-USD":      "EDGEUSDT",
    "CHILLGUY-USD":  "CHILLGUYUSDT",
    "PIPPIN-USD":    "PIPPINUSDT",
    "PIEVERSE-USD":  "PIEUSDT",
    # New liquid alts
    "NEAR-USD":      "NEARUSDT",
    "APT-USD":       "APTUSDT",
    "SEI-USD":       "SEIUSDT",
    "INJ-USD":       "INJUSDT",
    "TIA-USD":       "TIAUSDT",
    "JUP-USD":       "JUPUSDT",
    "WLD-USD":       "WLDUSDT",
    "HBAR-USD":      "HBARUSDT",
    "ATOM-USD":      "ATOMUSDT",
}

_FUNDING_CACHE_TTL = 300  # 5 minutes


def aria_to_bybit(aria_symbol: str) -> Optional[str]:
    return _SYMBOL_MAP.get(aria_symbol)


class BybitFeed:
    """
    Bybit public WebSocket market data feed for AUGUR signal stack.
    No auth. Auto-reconnects. Never raises into the caller.
    """

    def __init__(self, symbols: list):
        self._symbols = [s for s in symbols if aria_to_bybit(s)]
        self._running = False

        # Keyed by Bybit symbol (e.g. SOLUSDT)
        self._mark_prices:   Dict[str, float] = {}
        self._prev_prices:   Dict[str, Tuple[float, float]] = {}  # (price, timestamp)
        self._agg_ratios:    Dict[str, float] = {}
        self._funding_cache: Dict[str, float] = {}
        self._funding_at:    float = 0.0
        self._liq_events:    Dict[str, list]  = {}   # list of event timestamps

    # ── Symbol helpers ────────────────────────────────────────────────────────

    def _bybit(self, aria_sym: str) -> Optional[str]:
        return _SYMBOL_MAP.get(aria_sym)

    def _build_topics(self) -> list:
        topics = []
        for aria_sym in self._symbols:
            b = _SYMBOL_MAP.get(aria_sym)
            if not b:
                continue
            topics.append(f"tickers.{b}")
            topics.append(f"orderbook.25.{b}")
            topics.append(f"liquidation.{b}")
        return topics

    # ── Public signal accessors ───────────────────────────────────────────────

    def get_mark_price(self, aria_symbol: str) -> float:
        b = self._bybit(aria_symbol)
        return self._mark_prices.get(b, 0.0) if b else 0.0

    def get_agg_ratio(self, aria_symbol: str) -> float:
        """bid_vol / (bid_vol + ask_vol). >0.6 bullish, <0.4 bearish, 0.5 neutral."""
        b = self._bybit(aria_symbol)
        return self._agg_ratios.get(b, 0.5) if b else 0.5

    def get_price_momentum(self, aria_symbol: str, window_s: float = 30.0) -> float:
        """
        Percentage price change over the last window_s seconds.
        Returns 0.0 if not enough data.
        """
        b = self._bybit(aria_symbol)
        if not b or b not in self._prev_prices:
            return 0.0
        prev_price, prev_ts = self._prev_prices[b]
        curr = self._mark_prices.get(b, 0.0)
        if prev_price <= 0 or curr <= 0:
            return 0.0
        age = time.time() - prev_ts
        if age < 5 or age > window_s * 3:
            return 0.0
        return (curr - prev_price) / prev_price * 100.0

    def get_liquidations_60s(self, aria_symbol: str) -> int:
        """Count of liquidation events in the last 60 seconds."""
        b = self._bybit(aria_symbol)
        if not b:
            return 0
        now = time.time()
        events = self._liq_events.get(b, [])
        recent = [t for t in events if now - t < 60.0]
        self._liq_events[b] = recent
        return len(recent)

    def get_funding_rate(self, aria_symbol: str) -> float:
        """Latest funding rate from cache (filled by _refresh_funding_rates)."""
        b = self._bybit(aria_symbol)
        return self._funding_cache.get(b, 0.0) if b else 0.0

    def is_connected(self) -> bool:
        return self._running

    # ── REST: funding rates ───────────────────────────────────────────────────

    async def _refresh_funding_rates(self) -> None:
        """Fetch latest funding rate for all symbols. 5-min cache."""
        if time.time() - self._funding_at < _FUNDING_CACHE_TTL:
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                for aria_sym in self._symbols[:20]:   # cap to avoid rate limits
                    b = _SYMBOL_MAP.get(aria_sym)
                    if not b:
                        continue
                    try:
                        async with s.get(
                            f"{_BYBIT_REST}/v5/market/funding/history",
                            params={"category": "linear", "symbol": b, "limit": "1"},
                        ) as r:
                            data = await r.json(content_type=None)
                            rows = data.get("result", {}).get("list", [])
                            if rows:
                                self._funding_cache[b] = float(rows[0].get("fundingRate", 0.0))
                    except Exception:
                        pass
            self._funding_at = time.time()
            logger.debug("bybit_funding_refreshed", n=len(self._funding_cache))
        except Exception as e:
            logger.warning("bybit_funding_refresh_error", error=str(e))

    # ── WebSocket stream ──────────────────────────────────────────────────────

    def _process_ticker(self, b_sym: str, data: dict) -> None:
        price = float(data.get("markPrice", 0.0))
        if price <= 0:
            return
        old = self._mark_prices.get(b_sym, 0.0)
        if old > 0 and abs(price - old) / old < 0.50:   # sanity check
            # Snapshot previous price every ~30s for momentum calc
            prev_ts = self._prev_prices.get(b_sym, (0.0, 0.0))[1]
            if time.time() - prev_ts >= 30.0:
                self._prev_prices[b_sym] = (old, time.time())
        self._mark_prices[b_sym] = price

    def _process_orderbook(self, b_sym: str, data: dict) -> None:
        bids = data.get("b", [])
        asks = data.get("a", [])
        bid_vol = sum(float(row[1]) for row in bids if len(row) >= 2)
        ask_vol = sum(float(row[1]) for row in asks if len(row) >= 2)
        total = bid_vol + ask_vol
        if total > 0:
            self._agg_ratios[b_sym] = bid_vol / total

    def _process_liquidation(self, b_sym: str, data: dict) -> None:
        events = self._liq_events.setdefault(b_sym, [])
        events.append(time.time())
        # Keep only last 5 minutes
        now = time.time()
        self._liq_events[b_sym] = [t for t in events if now - t < 300]

    async def _handle_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return

        topic = data.get("topic", "")
        payload = data.get("data", {})
        if not topic or not payload:
            return

        if topic.startswith("tickers."):
            b_sym = topic.split(".", 1)[1]
            self._process_ticker(b_sym, payload)
        elif topic.startswith("orderbook."):
            b_sym = topic.split(".", 2)[2]
            self._process_orderbook(b_sym, payload)
        elif topic.startswith("liquidation."):
            b_sym = topic.split(".", 1)[1]
            self._process_liquidation(b_sym, payload if isinstance(payload, dict) else {})

    async def start(self) -> None:
        """Runs the WebSocket stream inline. Designed for _supervise() wrapping."""
        self._running = True
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        backoff = 1.0

        try:
            while self._running:
                try:
                    logger.info("bybit_feed_connecting", url=_BYBIT_WS,
                                symbols=len(self._symbols))
                    async with websockets.connect(
                        _BYBIT_WS, ssl=ssl_ctx,
                        ping_interval=20, ping_timeout=10,
                    ) as ws:
                        backoff = 1.0
                        topics = self._build_topics()
                        if topics:
                            await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                        logger.info("bybit_feed_subscribed", n_topics=len(topics))

                        # Refresh funding rates on connect
                        asyncio.create_task(self._refresh_funding_rates())

                        async def _keepalive():
                            while self._running:
                                await asyncio.sleep(15)
                                try:
                                    await ws.send(json.dumps({"op": "ping"}))
                                except Exception:
                                    break

                        asyncio.create_task(_keepalive())

                        while self._running:
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                                await self._handle_message(msg)
                            except asyncio.TimeoutError:
                                break  # trigger reconnect
                            except websockets.ConnectionClosed:
                                break

                except (websockets.WebSocketException, OSError, ssl.SSLError) as e:
                    logger.warning("bybit_feed_disconnected", error=str(e),
                                   reconnect_s=round(backoff, 1))

                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
