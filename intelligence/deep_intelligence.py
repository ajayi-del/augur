"""
DeepIntelligenceAgent — AUGUR's LLM-powered smart money analysis layer.

Two concurrent loops:
  _deep_cycle_forever   — every 6h, calls DeepSeek with full analysis
  _hot_poll_forever     — every 15min, detects cluster entries without LLM call

Outputs (all atomic writes):
  logs/intelligence_signals.json  — 6h cold signals (confidence boosts + leverage)
  logs/hot_signals.json           — 15min hot signals (cluster/scalper/whale entries)
  logs/augur_calendar.json        — upcoming market events
  logs/smart_wallets.json         — persistent wallet registry with reputation

Philosophical contract:
  ARIA never sees why AUGUR's confidence or leverage changes.
  The split is maintained. Both agents always bet.
"""

import asyncio
import json
import os
import time
import structlog
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import urllib.request

logger = structlog.get_logger(__name__)

# ── Keys / endpoints ──────────────────────────────────────────────────────────
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "sk-d32b8d1d43464b6eb9474c818d00782d")
MOONSHOT_API_KEY  = os.environ.get("MOONSHOT_API_KEY", "sk-fTG21i3sFBfiYPrNnYDjlaHnyrUv59fICSKkKsJ0c0znR924")
_TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "8339456128:AAG3orDeO7AZWDEoKgJc1j2UF6AcquK_2Ic")
_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7102469944")
_DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
_KIMI_URL         = "https://api.moonshot.ai/v1/chat/completions"
_KIMI_MODEL       = "kimi-k2.6"   # Kimi K2.6 (1T params, Apr 2026) — primary analyst for 6h deep cycle
_BYBIT_BASE       = "https://api.bybit.com/v5"
_BYBIT_TICKERS    = f"{_BYBIT_BASE}/market/tickers"
_HL_API           = "https://api.hyperliquid.xyz/info"   # Hyperliquid public API
_NEWS_API         = "https://min-api.cryptocompare.com/data/v2/news/?categories=Cryptocurrency&limit=8&sortOrder=popular"
_TELEGRAM_API     = f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}"

# ── Timing ────────────────────────────────────────────────────────────────────
_DEEP_INTERVAL_S    = 6 * 3600       # full DeepSeek analysis
_HOT_POLL_INTERVAL_S = 15 * 60      # position diff scan

# ── Signal config ─────────────────────────────────────────────────────────────
_MAX_WALLETS          = 20
_MIN_PNL_USD          = 500.0
_MAX_BOOST            = 0.15
_MIN_CONVICTION       = 0.50
_COLD_TTL_S           = 6 * 3600
_HOT_TTL_SCALPER_MS   = 30 * 60 * 1000     # 30 min
_HOT_TTL_CLUSTER_MS   = 2 * 3600 * 1000    # 2 hours
_HOT_TTL_WHALE_MS     = 4 * 3600 * 1000    # 4 hours
_MIN_CLUSTER_SIZE     = 2
_MIN_SCALPER_SIZE_USD = 3_000.0
_MIN_WHALE_SIZE_USD   = 8_000.0
_MIN_WHALE_REP        = 0.60
_POSITION_ADD_THRESH  = 0.30     # 30% increase in size = "new entry" signal

# ── Tracked symbols (Hyperliquid / Bybit base names) ─────────────────────────
_TRACKED_COINS = {
    "SOL", "BTC", "ETH", "BNB", "SUI", "PEPE",
    "OP", "ARB", "DOGE", "AVAX", "APT", "INJ",
}
# Map coin name → AUGUR symbol format
def _coin_to_sym(coin: str) -> str:
    return f"{coin.upper().replace('1K','')}-USD"

_ANALYSIS_SYMBOLS = [
    "SOL-USD", "ETH-USD", "BTC-USD", "ARB-USD",
    "SUI-USD", "AVAX-USD", "BNB-USD", "OP-USD",
    "DOGE-USD", "PEPE-USD",
]
# Bybit futures symbol → base coin
_BYBIT_USDT_SYMBOLS = [f"{c}USDT" for c in _TRACKED_COINS]
_BINANCE_FAPI       = "https://fapi.binance.com/fapi/v1"
_MIN_FLOW_USD       = 5_000.0    # Binance aggregate flow threshold per symbol side

# ── Wallet classification ─────────────────────────────────────────────────────

class WalletType(str, Enum):
    SCALPER = "scalper"   # high freq, short holds — entry = fast signal
    WHALE   = "whale"     # low freq, high PnL — position = directional conviction
    SWING   = "swing"     # medium freq — follow with normal TTL


def _classify(trade_count: int, total_pnl_usd: float) -> WalletType:
    if trade_count > 200:
        return WalletType.SCALPER
    if trade_count < 50 and total_pnl_usd > 20_000:
        return WalletType.WHALE
    return WalletType.SWING


def _recommend_leverage(
    types:       List[WalletType],
    cluster_sz:  int,
    conviction:  float,
    regime:      str,
    aria_agrees: bool = False,
) -> int:
    base = 5
    if conviction > 0.80: base += 4
    elif conviction > 0.65: base += 2
    base += min(cluster_sz, 4)
    if WalletType.SCALPER in types: base += 2
    if "trend" in regime.lower():   base += 1
    computed = int(min(max(base, 5), 15))
    # 8-10x tier requires ARIA to confirm same direction alongside smart money signal.
    # AUGUR-only conviction capped at 7x (no external confirmation).
    if not aria_agrees:
        return min(computed, 7)
    return computed


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class WalletProfile:
    address:            str
    total_pnl_usd:      float
    win_rate:           float
    trade_count:        int
    wallet_type:        str             # WalletType.value
    reputation:         float           # [0, 1] updated by AftermathAnalyzer
    last_seen_ms:       int
    current_positions:  List[dict]      = field(default_factory=list)
    prediction_history: List[dict]      = field(default_factory=list)


@dataclass
class IntelSignal:
    """6-hour cold signal from DeepSeek full analysis."""
    symbol:            str
    direction:         str
    confidence_boost:  float
    conviction:        float
    leverage_rec:      int
    reasoning:         str
    wallet_count:      int
    expires_ms:        int
    generated_ms:      int


@dataclass
class HotSignal:
    """15-minute hot signal from position diff / cluster detection."""
    symbol:            str
    direction:         str
    confidence_boost:  float
    conviction:        float
    leverage_rec:      int
    size_multiplier:   float    # 1.0–2.5× applied to Kelly base
    trigger:           str      # "cluster_entry" | "scalper_entry" | "whale_position"
    wallet_count:      int
    total_size_usd:    float    # combined tracked position size
    reasoning:         str
    expires_ms:        int
    generated_ms:      int


# ── Wallet discovery — Hyperliquid leaderboard ────────────────────────────────

class WalletDiscovery:
    """
    Fetches top traders from Hyperliquid public leaderboard.
    Hyperliquid has the best public API for smart money tracking — no auth needed.
    Falls back to Bybit large-trade whale detection if HL is unavailable.
    """

    async def get_top_wallets(
        self,
        session: aiohttp.ClientSession,
        limit: int = _MAX_WALLETS,
    ) -> List[WalletProfile]:
        wallets = await self._from_hyperliquid(session, limit)
        source = "hl"
        if not wallets:
            wallets = await self._from_binance_flows(session, limit)
            source = "binance"
        if not wallets:
            wallets = await self._from_bybit_whales(session, limit)
            source = "bybit"
        logger.info("wallet_discovery_complete",
                    found=len(wallets), source=source if wallets else "none")
        return wallets[:limit]

    async def _from_hyperliquid(
        self,
        session: aiohttp.ClientSession,
        limit:   int,
    ) -> List[WalletProfile]:
        wallets: List[WalletProfile] = []
        try:
            async with session.post(
                _HL_API,
                json    = {"type": "leaderboard"},
                headers = {"Content-Type": "application/json"},
                timeout = aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return wallets
                data = await resp.json(content_type=None)

            # HL leaderboard format: {"leaderboardRows": [{...}, ...]}
            rows = (
                data.get("leaderboardRows")
                or (data if isinstance(data, list) else [])
            )
            for row in rows[:limit * 2]:
                addr = row.get("ethAddress") or row.get("user", "")
                if not addr or len(addr) < 10:
                    continue
                # pnl is in windowPnlData or allTimePnl
                pnl_raw = 0.0
                for window in (row.get("windowPnlData") or []):
                    if window.get("window") == "allTime":
                        pnl_raw = float(window.get("pnl", 0) or 0)
                        break
                if pnl_raw == 0:
                    pnl_raw = float(row.get("allTimePnl", 0) or 0)

                if pnl_raw < _MIN_PNL_USD:
                    continue

                tc = int(row.get("tradeCount", 0) or 0)
                wallets.append(WalletProfile(
                    address       = addr,
                    total_pnl_usd = round(pnl_raw, 2),
                    win_rate      = float(row.get("winRate", 0.5) or 0.5),
                    trade_count   = tc,
                    wallet_type   = _classify(tc, pnl_raw).value,
                    reputation    = 0.5,
                    last_seen_ms  = int(time.time() * 1000),
                ))
        except Exception as e:
            logger.warning("hl_leaderboard_error", error=str(e))
        return wallets[:limit]

    async def _from_binance_flows(
        self,
        session: aiohttp.ClientSession,
        limit:   int,
    ) -> List[WalletProfile]:
        """
        Primary fallback: detect large directional flows from Binance futures aggTrades.
        Binance aggTrades show taker direction — aggressive buyers/sellers = smart money flow.
        aggTrade.m=False → buy order hit (LONG flow), m=True → sell order hit (SHORT flow).
        Creates synthetic WalletProfiles representing each directional cluster.
        """
        wallets: List[WalletProfile] = []
        now_ms = int(time.time() * 1000)
        try:
            for coin in list(_TRACKED_COINS):
                sym = f"{coin}USDT"
                try:
                    async with session.get(
                        f"{_BINANCE_FAPI}/aggTrades",
                        params={"symbol": sym, "limit": 100},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        trades = await resp.json(content_type=None)

                    buy_flow  = sum(
                        float(t["q"]) * float(t["p"])
                        for t in trades if not t.get("m", True)
                    )
                    sell_flow = sum(
                        float(t["q"]) * float(t["p"])
                        for t in trades if t.get("m", True)
                    )

                    for direction, flow in (("long", buy_flow), ("short", sell_flow)):
                        if flow < _MIN_FLOW_USD:
                            continue
                        addr = f"binance_flow_{coin}_{direction}"
                        wtype = (WalletType.WHALE if flow > 50_000
                                 else WalletType.SCALPER)
                        wallets.append(WalletProfile(
                            address       = addr,
                            total_pnl_usd = round(flow * 0.015, 2),  # proxy P&L estimate
                            win_rate      = 0.55,
                            trade_count   = 300,
                            wallet_type   = wtype.value,
                            reputation    = 0.52,
                            last_seen_ms  = now_ms,
                            current_positions = [{
                                "coin":           coin,
                                "direction":      direction,
                                "size_usd":       round(flow, 2),
                                "unrealized_pnl": 0.0,
                            }],
                        ))
                        logger.debug("binance_flow_detected",
                                     coin=coin, direction=direction,
                                     flow_usd=round(flow, 0))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("binance_flows_error", error=str(e))
        return wallets[:limit]

    async def _from_bybit_whales(
        self,
        session: aiohttp.ClientSession,
        limit:   int,
    ) -> List[WalletProfile]:
        """
        Fallback: detect large recent Bybit trades as synthetic 'whale' profiles.
        Each large trade → synthetic WalletProfile with a synthetic address.
        """
        wallets: List[WalletProfile] = []
        seen: set = set()
        try:
            for coin in list(_TRACKED_COINS)[:8]:
                sym = f"{coin}USDT"
                try:
                    async with session.get(
                        f"{_BYBIT_BASE}/market/recent-trade",
                        params={"category": "linear", "symbol": sym, "limit": 50},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json(content_type=None)
                    trades = data.get("result", {}).get("list", [])
                    for t in trades:
                        size = float(t.get("size", 0) or 0)
                        price = float(t.get("price", 1) or 1)
                        notional = size * price
                        if notional < _MIN_WHALE_SIZE_USD:
                            continue
                        side = t.get("side", "").lower()
                        direction = "long" if side == "buy" else "short"
                        key = f"{sym}_{direction}_{round(notional, -3)}"
                        if key in seen:
                            continue
                        seen.add(key)
                        addr = f"bybit_whale_{coin}_{direction}_{int(notional)}"
                        wallets.append(WalletProfile(
                            address       = addr,
                            total_pnl_usd = notional * 0.02,  # proxy: 2% est. profit
                            win_rate      = 0.55,
                            trade_count   = 500,               # large trader = scalper
                            wallet_type   = WalletType.SCALPER.value,
                            reputation    = 0.45,
                            last_seen_ms  = int(time.time() * 1000),
                            current_positions = [{
                                "coin":           coin,
                                "direction":      direction,
                                "size_usd":       round(notional, 2),
                                "unrealized_pnl": 0.0,
                            }],
                        ))
                except Exception:
                    continue
        except Exception as e:
            logger.debug("bybit_whale_fallback_error", error=str(e))
        return wallets[:limit]


# ── Position tracker — Hyperliquid clearinghouse ──────────────────────────────

class SmartMoneyTracker:
    """
    Fetches current open perp positions for a tracked wallet via Hyperliquid.
    Position dict uses 'coin' key (str) instead of 'market_index' (int).
    """

    async def get_positions(
        self,
        session: aiohttp.ClientSession,
        wallet: WalletProfile,
    ) -> List[dict]:
        # Bybit-synthetic wallets already have positions injected by WalletDiscovery
        if wallet.address.startswith("bybit_whale_"):
            return wallet.current_positions

        positions: List[dict] = []
        try:
            async with session.post(
                _HL_API,
                json    = {"type": "clearinghouseState", "user": wallet.address},
                headers = {"Content-Type": "application/json"},
                timeout = aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return positions
                data = await resp.json(content_type=None)

            for asset_pos in (data.get("assetPositions") or []):
                pos = asset_pos.get("position") or asset_pos
                coin = str(pos.get("coin", "") or "").upper().replace("1K", "")
                if not coin or coin not in _TRACKED_COINS:
                    continue
                szi  = float(pos.get("szi", 0) or 0)
                if szi == 0:
                    continue
                pos_val = float(pos.get("positionValue", 0) or 0)
                upnl    = float(pos.get("unrealizedPnl", 0) or 0)
                positions.append({
                    "coin":           coin,
                    "direction":      "long" if szi > 0 else "short",
                    "size_usd":       round(abs(pos_val), 2),
                    "unrealized_pnl": round(upnl, 2),
                })
        except Exception as e:
            logger.debug("hl_positions_error",
                         wallet=wallet.address[:10], error=str(e))
        return positions


# ── Cluster detector ──────────────────────────────────────────────────────────

class ClusterDetector:
    """
    Diffs position snapshots across wallet polling cycles.
    Emits HotSignals when:
      - 2+ wallets newly entered same direction / symbol (cluster_entry)
      - A known scalper opens a large new position (scalper_entry)
      - A whale with rep > threshold holds a large directional position (whale_position)
    """

    def __init__(self):
        self._prev: Dict[str, List[dict]] = {}   # {wallet_address: positions}

    def detect(
        self,
        wallets:  List[WalletProfile],
        regime:   str,
    ) -> List[HotSignal]:
        hot: List[HotSignal] = []
        now_ms = int(time.time() * 1000)

        # Build new snapshot
        curr: Dict[str, List[dict]] = {
            w.address: w.current_positions for w in wallets
        }

        # 1. Find new entries per wallet (new pos or size grew > 30%)
        new_entries: Dict[str, List[dict]] = {}   # {wallet_addr: [entry, ...]}
        for w in wallets:
            prev_pos = self._prev.get(w.address, [])
            curr_pos = w.current_positions

            prev_map = {
                (p.get("coin", ""), p["direction"]): p
                for p in prev_pos
            }
            for pos in curr_pos:
                key = (pos.get("coin", ""), pos["direction"])
                if key not in prev_map:
                    new_entries.setdefault(w.address, []).append(
                        {**pos, "_type": "new", "_wallet": w}
                    )
                else:
                    prev_sz = prev_map[key].get("size_usd", 0) or 0
                    curr_sz = pos.get("size_usd", 0) or 0
                    if prev_sz > 0 and (curr_sz - prev_sz) / prev_sz > _POSITION_ADD_THRESH:
                        new_entries.setdefault(w.address, []).append(
                            {**pos, "_type": "add", "_wallet": w}
                        )

        # 2. Cluster: aggregate new entries by (coin, direction)
        cluster_map: Dict[Tuple[str, str], List[dict]] = {}
        for addr, entries in new_entries.items():
            for entry in entries:
                key = (entry.get("coin", ""), entry["direction"])
                cluster_map.setdefault(key, []).append(entry)

        for (coin, direction), entries in cluster_map.items():
            if not coin:
                continue
            sym_name = coin
            symbol   = _coin_to_sym(coin)

            # Gather wallet objects
            wallets_in = [e["_wallet"] for e in entries if "_wallet" in e]
            types      = [WalletType(w.wallet_type) for w in wallets_in]
            cluster_sz = len(wallets_in)
            total_size = sum(e.get("size_usd", 0) for e in entries)
            avg_rep    = sum(w.reputation for w in wallets_in) / max(cluster_sz, 1)

            # Cluster signal: 2+ wallets
            if cluster_sz >= _MIN_CLUSTER_SIZE:
                conviction    = min(0.50 + 0.08 * cluster_sz + avg_rep * 0.15, 0.92)
                boost         = min(_MAX_BOOST, 0.04 * cluster_sz + 0.02)
                lev_rec       = _recommend_leverage(types, cluster_sz, conviction, regime)
                size_mult     = min(1.0 + 0.25 * (cluster_sz - 1), 2.5)
                ttl           = _HOT_TTL_CLUSTER_MS
                hot.append(HotSignal(
                    symbol         = symbol,
                    direction      = direction,
                    confidence_boost = round(boost, 3),
                    conviction     = round(conviction, 3),
                    leverage_rec   = lev_rec,
                    size_multiplier = round(size_mult, 2),
                    trigger        = "cluster_entry",
                    wallet_count   = cluster_sz,
                    total_size_usd = round(total_size, 2),
                    reasoning      = (f"{cluster_sz} wallets entered {direction} {sym_name}; "
                                      f"total ${total_size:.0f}, avg_rep={avg_rep:.2f}"),
                    expires_ms     = now_ms + ttl,
                    generated_ms   = now_ms,
                ))
                logger.info("hot_cluster_detected",
                            symbol=symbol, direction=direction,
                            wallets=cluster_sz, total_usd=round(total_size, 0))

            # Scalper entry: single scalper with large position
            for entry in entries:
                w = entry.get("_wallet")
                if w and w.wallet_type == WalletType.SCALPER.value:
                    if entry.get("size_usd", 0) >= _MIN_SCALPER_SIZE_USD and w.reputation >= 0.5:
                        conviction = min(0.55 + w.reputation * 0.25, 0.85)
                        boost      = min(_MAX_BOOST, 0.06)
                        hot.append(HotSignal(
                            symbol          = symbol,
                            direction       = direction,
                            confidence_boost = round(boost, 3),
                            conviction      = round(conviction, 3),
                            leverage_rec    = _recommend_leverage([WalletType.SCALPER], 1, conviction, regime),
                            size_multiplier  = 1.5,
                            trigger         = "scalper_entry",
                            wallet_count    = 1,
                            total_size_usd  = entry.get("size_usd", 0),
                            reasoning       = (f"Scalper {w.address[:8]} entered {direction} "
                                               f"${entry.get('size_usd', 0):.0f} on {sym_name}, "
                                               f"rep={w.reputation:.2f}"),
                            expires_ms      = now_ms + _HOT_TTL_SCALPER_MS,
                            generated_ms    = now_ms,
                        ))

        # 3. Whale positions (not necessarily new — just large held position from high-rep whale)
        for w in wallets:
            if w.wallet_type != WalletType.WHALE.value or w.reputation < _MIN_WHALE_REP:
                continue
            for pos in w.current_positions:
                if pos.get("size_usd", 0) < _MIN_WHALE_SIZE_USD:
                    continue
                coin = pos.get("coin", "")
                if not coin or coin not in _TRACKED_COINS:
                    continue
                sym_name  = coin
                symbol    = _coin_to_sym(coin)
                direction = pos["direction"]
                # Avoid duplicate with cluster signals
                already   = any(
                    s.symbol == symbol and s.direction == direction
                    and s.trigger == "whale_position"
                    for s in hot
                )
                if not already:
                    conviction = min(0.52 + w.reputation * 0.30, 0.85)
                    boost      = min(_MAX_BOOST, 0.05)
                    hot.append(HotSignal(
                        symbol          = symbol,
                        direction       = direction,
                        confidence_boost = round(boost, 3),
                        conviction      = round(conviction, 3),
                        leverage_rec    = _recommend_leverage([WalletType.WHALE], 1, conviction, regime),
                        size_multiplier  = 1.3,
                        trigger         = "whale_position",
                        wallet_count    = 1,
                        total_size_usd  = pos["size_usd"],
                        reasoning       = (f"Whale {w.address[:8]} holds {direction} "
                                           f"${pos['size_usd']:.0f} {sym_name}, rep={w.reputation:.2f}"),
                        expires_ms      = now_ms + _HOT_TTL_WHALE_MS,
                        generated_ms    = now_ms,
                    ))

        # Advance snapshot
        self._prev = curr
        return hot


# ── Aftermath scorer ──────────────────────────────────────────────────────────

class AftermathAnalyzer:

    def analyze(
        self,
        wallets:          List[WalletProfile],
        previous_signals: List[IntelSignal],
        current_prices:   Dict[str, float],
    ) -> Dict[str, float]:
        deltas: Dict[str, float] = {}
        if not previous_signals:
            return deltas
        for signal in previous_signals:
            price_now = current_prices.get(signal.symbol, 0.0)
            if price_now <= 0 or signal.direction == "neutral":
                continue
            for wallet in wallets:
                for pred in wallet.prediction_history:
                    if pred.get("symbol") != signal.symbol:
                        continue
                    if abs(pred.get("generated_ms", 0) - signal.generated_ms) > 300_000:
                        continue
                    price_then = float(pred.get("price_at_prediction", 0.0))
                    if price_then <= 0:
                        continue
                    pct = (price_now - price_then) / price_then
                    if signal.direction == "short":
                        pct = -pct
                    delta = 0.05 if pct > 0.005 else (-0.03 if pct < -0.005 else 0.0)
                    deltas[wallet.address] = deltas.get(wallet.address, 0.0) + delta
        return deltas


# ── Calendar seeder ───────────────────────────────────────────────────────────

class CalendarSeeder:
    def __init__(self, log_path: str):
        self._path = Path(log_path) / "augur_calendar.json"

    def write(self, events: List[dict]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "events":       events,
                "generated_ms": int(time.time() * 1000),
            }, indent=2))
            tmp.replace(self._path)
            logger.info("calendar_seeded", events=len(events))
        except Exception as e:
            logger.warning("calendar_seed_error", error=str(e))

    def read(self) -> List[dict]:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text()).get("events", [])
        except Exception:
            pass
        return []


# ── DeepSeek analyst ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are AUGUR's deep intelligence analyst. You reason like a quantitative philosopher.

Procedural chain-of-thought for every analysis:
OBSERVE: What are smart money wallets doing? What is the consensus position by symbol?
REASON:  Why are they positioned this way? Funding rates, regime, cascade state, market structure.
PREDICT: Which direction will each symbol move over the next 6 hours? Be directionally decisive.
CALIBRATE: Scale conviction by wallet agreement %. Cut conviction if data is sparse or split.
SIGNAL:  Emit actionable signals with confidence_boost [0, 0.15] and leverage_rec [5, 15].

Rules:
- Only emit directional signals where wallet consensus ≥ 60%
- confidence_boost = 0.0, direction = "neutral" when in doubt
- leverage_rec: 5 (weak), 8 (moderate), 12 (strong), 15 (extreme conviction only)
- Calendar events: on-chain protocol upgrades, large token unlocks, Fed/macro events

Output ONLY valid JSON — no markdown, no text outside the JSON:
{
  "observe": "paragraph",
  "reason": "paragraph",
  "predict": "paragraph",
  "calibrate": "paragraph",
  "signals": [
    {
      "symbol": "SOL-USD",
      "direction": "long",
      "confidence_boost": 0.08,
      "conviction": 0.72,
      "leverage_rec": 10,
      "reasoning": "4 of 5 tracked wallets long SOL avg $9k, funding +0.01%, trending regime",
      "wallet_count": 5
    }
  ],
  "calendar_events": [
    {
      "event": "Solana v2.0 mainnet upgrade",
      "symbol": "SOL",
      "expected_ms": 1234567890000,
      "impact": "high",
      "direction_bias": "long"
    }
  ]
}"""


class DeepSeekAnalyst:

    async def analyze(
        self,
        session:       aiohttp.ClientSession,
        wallets:       List[WalletProfile],
        regime:        str,
        cascade_alert: dict,
        funding_rates: Dict[str, float],
    ) -> Tuple[List[IntelSignal], List[dict]]:
        observation = self._build_observation(wallets, regime, cascade_alert, funding_rates)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": (
                f"MARKET OBSERVATION:\n{observation}\n\n"
                f"SYMBOLS: {', '.join(_ANALYSIS_SYMBOLS)}\n\n"
                "Execute full chain-of-thought. Output valid JSON only."
            )},
        ]
        # Primary: Kimi K2 (larger context, stronger reasoning for 6h analysis)
        # Fallback: DeepSeek (reliable, already proven)
        for (url, api_key, model, label) in [
            (_KIMI_URL, MOONSHOT_API_KEY, _KIMI_MODEL, "kimi_k2"),
            (_DEEPSEEK_URL, DEEPSEEK_API_KEY, "deepseek-chat", "deepseek"),
        ]:
            if not api_key:
                continue
            payload = {
                "model":       model,
                "temperature": 0.3,
                "max_tokens":  4096,
                "messages":    messages,
            }
            try:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type":  "application/json"},
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"{label}_api_error",
                                       status=resp.status, body=body[:200])
                        continue
                    result = await resp.json(content_type=None)
                raw = result["choices"][0]["message"]["content"]
                logger.info(f"{label}_response_ok", chars=len(raw))
                return self._parse(raw)
            except asyncio.TimeoutError:
                logger.warning(f"{label}_timeout")
                continue
            except Exception as e:
                logger.warning(f"{label}_analyze_error", error=str(e))
                continue
        logger.error("all_llm_providers_failed")
        return [], []

    def _build_observation(
        self,
        wallets:       List[WalletProfile],
        regime:        str,
        cascade_alert: dict,
        funding_rates: Dict[str, float],
    ) -> str:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        consensus: Dict[str, Dict] = {
            sym: {"long": 0, "short": 0, "size_usd": 0.0, "wallets": []}
            for sym in _ANALYSIS_SYMBOLS
        }
        wallet_lines = []
        for w in sorted(wallets, key=lambda x: x.reputation, reverse=True)[:10]:
            if not w.current_positions:
                continue
            pos_strs = []
            for pos in w.current_positions:
                coin = pos.get("coin", "")
                if not coin or coin not in _TRACKED_COINS:
                    continue
                sym_name = coin
                sym_key  = _coin_to_sym(coin)
                if sym_key in consensus:
                    consensus[sym_key][pos["direction"]] += 1
                    consensus[sym_key]["size_usd"] += pos.get("size_usd", 0)
                    consensus[sym_key]["wallets"].append(w.wallet_type)
                pos_strs.append(
                    f"{coin}:{pos['direction'].upper()}@${pos.get('size_usd', 0):.0f}"
                    f"(upnl={pos.get('unrealized_pnl', 0):+.0f})"
                )
            if pos_strs:
                wallet_lines.append(
                    f"  {w.address[:8]} [{w.wallet_type}, rep={w.reputation:.2f}, "
                    f"pnl=${w.total_pnl_usd:.0f}]: {', '.join(pos_strs)}"
                )
        consensus_lines = []
        for sym, c in consensus.items():
            total = c["long"] + c["short"]
            if total == 0:
                continue
            pct_long = c["long"] / total * 100
            types = ",".join(set(c["wallets"]))
            consensus_lines.append(
                f"  {sym}: {c['long']}L/{c['short']}S "
                f"(${c['size_usd']:.0f}) {pct_long:.0f}% long [{types}]"
            )
        funding_lines = [
            f"  {sym}: {rate:+.4f}%"
            for sym, rate in funding_rates.items() if abs(rate) > 0.001
        ]
        return (
            f"TIME: {now_iso} | REGIME: {regime}\n"
            f"CASCADE: active={cascade_alert.get('active')}, "
            f"zscore={cascade_alert.get('zscore', 0):.2f}\n\n"
            f"SMART MONEY ({len([w for w in wallets if w.current_positions])} active wallets):\n"
            f"{chr(10).join(wallet_lines) or '  (none open)'}\n\n"
            f"SYMBOL CONSENSUS:\n{chr(10).join(consensus_lines) or '  (no data)'}\n\n"
            f"FUNDING RATES:\n{chr(10).join(funding_lines) or '  (near zero)'}\n"
        )

    def _parse(self, raw: str) -> Tuple[List[IntelSignal], List[dict]]:
        signals: List[IntelSignal] = []
        events:  List[dict] = []
        try:
            text = raw.strip()
            if "```" in text:
                for part in text.split("```"):
                    stripped = part.lstrip("json").strip()
                    if stripped.startswith("{"):
                        text = stripped
                        break
            parsed = json.loads(text)
            now_ms = int(time.time() * 1000)
            for s in parsed.get("signals", []):
                sym = s.get("symbol", "")
                if not sym.endswith("-USD"):
                    sym = f"{sym}-USD"
                direction = s.get("direction", "neutral").lower()
                if direction not in ("long", "short", "neutral"):
                    direction = "neutral"
                boost     = float(max(0.0, min(s.get("confidence_boost", 0.0), _MAX_BOOST)))
                conviction = float(max(0.0, min(s.get("conviction", 0.5), 1.0)))
                lev_rec   = int(max(5, min(s.get("leverage_rec", 5), 15)))
                signals.append(IntelSignal(
                    symbol           = sym,
                    direction        = direction,
                    confidence_boost = boost,
                    conviction       = conviction,
                    leverage_rec     = lev_rec,
                    reasoning        = str(s.get("reasoning", ""))[:500],
                    wallet_count     = int(s.get("wallet_count", 0)),
                    expires_ms       = now_ms + _COLD_TTL_S * 1000,
                    generated_ms     = now_ms,
                ))
            for ev in parsed.get("calendar_events", []):
                events.append({
                    "event":          str(ev.get("event", "")),
                    "symbol":         str(ev.get("symbol", "")),
                    "expected_ms":    int(ev.get("expected_ms", now_ms + 86_400_000)),
                    "impact":         str(ev.get("impact", "medium")),
                    "direction_bias": str(ev.get("direction_bias", "neutral")),
                })
        except Exception as e:
            logger.warning("deepseek_parse_error", error=str(e), raw=raw[:300])
        logger.info("deepseek_parsed", signals=len(signals), events=len(events))
        return signals, events


# ── Main agent ────────────────────────────────────────────────────────────────

class DeepIntelligenceAgent:
    """
    AUGUR's smart money intelligence system.

    Two concurrent loops (started by run_forever):
      _deep_cycle_forever  — 6h: wallet discovery + DeepSeek analysis → cold signals
      _hot_poll_forever    — 15min: position diff → cluster/scalper/whale hot signals

    StrategyRunner calls get_signal() and get_hot_signal() every 30s.
    Signals are read from disk — no shared state, no blocking.
    """

    def __init__(self, log_path: str, kingdom, bridge):
        self._log_path      = Path(log_path)
        self._kingdom       = kingdom
        self._bridge        = bridge
        self._signals_path  = self._log_path / "intelligence_signals.json"
        self._hot_path      = self._log_path / "hot_signals.json"
        self._wallets_path  = self._log_path / "smart_wallets.json"
        self._aftermath_path = self._log_path / "aftermath_log.jsonl"

        self._wallets:           List[WalletProfile] = []
        self._previous_signals:  List[IntelSignal]   = []
        self._last_discovery_ms: int                 = 0

        self._discovery  = WalletDiscovery()
        self._tracker    = SmartMoneyTracker()
        self._cluster    = ClusterDetector()
        self._aftermath  = AftermathAnalyzer()
        self._calendar   = CalendarSeeder(str(log_path))
        self._analyst    = DeepSeekAnalyst()
        self._enabled    = bool(DEEPSEEK_API_KEY)

    # ── Public API for StrategyRunner ──────────────────────────────────────

    def get_signal(self, symbol: str) -> Optional[IntelSignal]:
        """Cold signal from last 6h DeepSeek analysis. Reads from disk."""
        try:
            if not self._signals_path.exists():
                return None
            data   = json.loads(self._signals_path.read_text())
            now_ms = int(time.time() * 1000)
            for s in data.get("signals", []):
                if s.get("symbol") == symbol and s.get("expires_ms", 0) > now_ms:
                    return IntelSignal(**{
                        k: s[k] for k in IntelSignal.__dataclass_fields__ if k in s
                    })
        except Exception:
            pass
        return None

    def get_hot_signal(self, symbol: str) -> Optional[HotSignal]:
        """Hot signal from last 15min cluster/scalper scan. Reads from disk."""
        try:
            if not self._hot_path.exists():
                return None
            data   = json.loads(self._hot_path.read_text())
            now_ms = int(time.time() * 1000)
            # Return highest-conviction unexpired signal for this symbol
            best = None
            for s in data.get("signals", []):
                if s.get("symbol") == symbol and s.get("expires_ms", 0) > now_ms:
                    if best is None or s.get("conviction", 0) > best.get("conviction", 0):
                        best = s
            if best:
                return HotSignal(**{
                    k: best[k] for k in HotSignal.__dataclass_fields__ if k in best
                })
        except Exception:
            pass
        return None

    def get_leverage_for_signal(
        self,
        symbol:    str,
        direction: str,
        base_lev:  int = 5,
    ) -> int:
        """
        Returns recommended leverage for a signal direction on symbol.
        Checks hot signals first (fresher), then cold signals.
        Falls back to base_lev if no intel.
        """
        hot = self.get_hot_signal(symbol)
        if hot and hot.direction == direction:
            return hot.leverage_rec

        cold = self.get_signal(symbol)
        if cold and cold.direction == direction:
            return cold.leverage_rec

        return base_lev

    # ── Persistence ────────────────────────────────────────────────────────

    def _load_wallets(self) -> None:
        try:
            if self._wallets_path.exists():
                data = json.loads(self._wallets_path.read_text())
                self._wallets = [WalletProfile(**w) for w in data.get("wallets", [])]
                self._last_discovery_ms = int(data.get("discovery_ms", 0))
                logger.info("wallets_loaded", count=len(self._wallets))
        except Exception as e:
            logger.warning("wallets_load_error", error=str(e))

    def _save_wallets(self) -> None:
        try:
            tmp = self._wallets_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "wallets":      [asdict(w) for w in self._wallets],
                "discovery_ms": self._last_discovery_ms,
                "updated_ms":   int(time.time() * 1000),
            }, indent=2))
            tmp.replace(self._wallets_path)
        except Exception as e:
            logger.warning("wallets_save_error", error=str(e))

    def _write_cold_signals(self, signals: List[IntelSignal]) -> None:
        try:
            self._log_path.mkdir(parents=True, exist_ok=True)
            tmp = self._signals_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "signals":      [asdict(s) for s in signals],
                "generated_ms": int(time.time() * 1000),
                "wallet_count": len(self._wallets),
            }, indent=2))
            tmp.replace(self._signals_path)
            active = [s for s in signals if s.confidence_boost > 0]
            logger.info("cold_signals_written", total=len(signals), active_boosts=len(active))
        except Exception as e:
            logger.error("cold_signals_write_error", error=str(e))

    def _write_hot_signals(self, signals: List[HotSignal]) -> None:
        try:
            self._log_path.mkdir(parents=True, exist_ok=True)
            tmp = self._hot_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "signals":      [asdict(s) for s in signals],
                "generated_ms": int(time.time() * 1000),
            }, indent=2))
            tmp.replace(self._hot_path)
            if signals:
                logger.info("hot_signals_written",
                            count=len(signals),
                            triggers=[s.trigger for s in signals[:5]])
                # Notify Telegram for strong signals only
                strong = [s for s in signals if s.conviction >= 0.70]
                if strong:
                    self._notify_telegram(strong)
        except Exception as e:
            logger.error("hot_signals_write_error", error=str(e))

    def _notify_deep_cycle(
        self,
        signals:         List[IntelSignal],
        calendar_events: List[dict],
        elapsed_s:       float,
    ) -> None:
        """Send Telegram summary after each 6h deep analysis cycle."""
        if not _TELEGRAM_CHAT_ID or not _TELEGRAM_TOKEN:
            return
        active   = [s for s in signals if s.confidence_boost > 0 and s.direction != "neutral"]
        w_active = sum(1 for w in self._wallets if w.current_positions)
        lines    = [
            f"*AUGUR DEEPSEEK CYCLE COMPLETE* ({elapsed_s:.0f}s)",
            f"Wallets tracked: {len(self._wallets)} ({w_active} with open positions)",
        ]
        if active:
            lines.append(f"\n*{len(active)} ACTIVE SIGNALS:*")
            for s in active[:5]:
                arrow = "⬆" if s.direction == "long" else "⬇"
                lines.append(
                    f"{arrow} *{s.symbol}* `{s.direction.upper()}` "
                    f"boost=+{s.confidence_boost:.2f} "
                    f"lev={s.leverage_rec}x conv={s.conviction:.2f}\n"
                    f"  _{s.reasoning[:80]}_"
                )
        else:
            lines.append("No directional signals — market consensus unclear.")
        if calendar_events:
            lines.append(f"\n*{len(calendar_events)} CALENDAR EVENTS:*")
            for ev in calendar_events[:3]:
                lines.append(f"  {ev.get('event','')} [{ev.get('impact','')}]")
        lines.append(f"\nNext deep cycle in 6h. `/intel` for live status.")
        self._send_tg("\n".join(lines))

    def _send_tg(self, text: str) -> None:
        """Fire-and-forget Telegram message. Never throws."""
        if not _TELEGRAM_CHAT_ID or not _TELEGRAM_TOKEN:
            return
        try:
            data = json.dumps({
                "chat_id":    _TELEGRAM_CHAT_ID,
                "text":       text[:4000],
                "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                f"{_TELEGRAM_API}/sendMessage",
                data    = data,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            logger.debug("telegram_send_error", error=str(e))

    def _notify_telegram(self, signals: List[HotSignal]) -> None:
        """Telegram alert when strong hot signals are detected (conviction ≥ 0.70)."""
        lines = ["*AUGUR INTEL ALERT* 🔥"]
        for s in signals[:4]:
            arrow = "⬆" if s.direction == "long" else "⬇"
            lines.append(
                f"{arrow} *{s.symbol}* `{s.direction.upper()}` — {s.trigger}\n"
                f"  conviction={s.conviction:.2f}  boost=+{s.confidence_boost:.2f}"
                f"  lev={s.leverage_rec}x  wallets={s.wallet_count}\n"
                f"  _{s.reasoning[:80]}_"
            )
        self._send_tg("\n\n".join(lines))
        logger.info("telegram_hot_alert_sent", signals=len(signals))

    def _write_kingdom_whisper(self, signals: List[IntelSignal]) -> None:
        """
        Write DeepSeek observations into kingdom_state.json under deepseek_whisper.
        Ambient intelligence — neither ARIA nor AUGUR's internal logic depends on it,
        but both can read it to adjust probability when the errand bird agrees with them.
        """
        try:
            now_ms = int(time.time() * 1000)
            observations = []
            for s in signals:
                if s.direction == "neutral" or s.conviction < _MIN_CONVICTION:
                    continue
                observations.append({
                    "symbol":    s.symbol,
                    "bias":      s.direction,
                    "strength":  round(s.conviction, 3),
                    "boost":     round(s.confidence_boost, 3),
                    "leverage":  s.leverage_rec,
                    "reason":    s.reasoning[:120],
                    "wallets":   s.wallet_count,
                    "expires_ms": now_ms + _COLD_TTL_S * 1000,
                })
            if observations:
                self._kingdom.write_deepseek_whisper(observations)
                logger.info("deepseek_whisper_published", count=len(observations))
        except Exception as e:
            logger.warning("deepseek_whisper_error", error=str(e))

    def _log_aftermath(self, deltas: Dict[str, float]) -> None:
        if not deltas:
            return
        try:
            with open(self._aftermath_path, "a") as f:
                f.write(json.dumps({
                    "timestamp_ms":      int(time.time() * 1000),
                    "reputation_deltas": deltas,
                }) + "\n")
        except Exception:
            pass

    # ── Shared helpers ─────────────────────────────────────────────────────

    async def _fetch_prices(self, session: aiohttp.ClientSession) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        try:
            async with session.get(
                _BYBIT_TICKERS,
                params={"category": "linear"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    for item in (await resp.json()).get("result", {}).get("list", []):
                        sym = item.get("symbol", "")
                        if sym.endswith("USDT"):
                            price = float(item.get("markPrice", 0) or 0)
                            if price > 0:
                                prices[f"{sym[:-4]}-USD"] = price
        except Exception as e:
            logger.debug("price_fetch_error", error=str(e))
        return prices

    async def _track_all_positions(self, session: aiohttp.ClientSession) -> None:
        sem = asyncio.Semaphore(5)

        async def _one(wallet: WalletProfile) -> None:
            async with sem:
                wallet.current_positions = await self._tracker.get_positions(session, wallet)
                wallet.last_seen_ms = int(time.time() * 1000)
                await asyncio.sleep(0.2)

        await asyncio.gather(*[_one(w) for w in self._wallets], return_exceptions=True)
        active = sum(1 for w in self._wallets if w.current_positions)
        logger.info("positions_tracked", total=len(self._wallets), active=active)

    async def _maybe_discover(self, session: aiohttp.ClientSession) -> None:
        week_ms = 7 * 24 * 3600 * 1000
        now_ms  = int(time.time() * 1000)
        if (not self._wallets
                or len(self._wallets) < 5
                or now_ms - self._last_discovery_ms > week_ms):
            discovered = await self._discovery.get_top_wallets(session)
            if discovered:
                known  = {w.address: w for w in self._wallets}
                merged = []
                for w in discovered:
                    if w.address in known:
                        old = known[w.address]
                        w.reputation         = old.reputation
                        w.prediction_history = old.prediction_history
                    merged.append(w)
                self._wallets = sorted(
                    merged, key=lambda x: x.total_pnl_usd, reverse=True
                )[:_MAX_WALLETS]
                self._last_discovery_ms = now_ms
                logger.info("wallets_updated", count=len(self._wallets))

    # ── Deep cycle (6h) ────────────────────────────────────────────────────

    async def _run_deep_cycle(self) -> None:
        logger.info("deep_cycle_start")
        t0 = time.time()
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Aftermath scoring
                if self._previous_signals:
                    prices = await self._fetch_prices(session)
                    deltas = self._aftermath.analyze(self._wallets, self._previous_signals, prices)
                    for w in self._wallets:
                        d = deltas.get(w.address, 0.0)
                        w.reputation = round(max(0.1, min(1.0, w.reputation + d)), 3)
                    self._log_aftermath(deltas)

                # 2. Discovery
                await self._maybe_discover(session)

                # 3. Track positions
                await self._track_all_positions(session)

                # 4. Context
                regime        = self._bridge.get_regime()
                cascade_alert = self._bridge.get_cascade_signal()
                try:
                    funding_rates = await self._bridge.get_funding_rates()
                except Exception:
                    funding_rates = {}

                # 5. DeepSeek analysis
                signals, calendar_events = await self._analyst.analyze(
                    session       = session,
                    wallets       = self._wallets,
                    regime        = regime,
                    cascade_alert = cascade_alert,
                    funding_rates = funding_rates,
                )

                # 6. Persist
                self._save_wallets()
                self._previous_signals = signals
                self._write_cold_signals(signals)
                if calendar_events:
                    self._calendar.write(calendar_events)

                # 7. Write ambient whisper to kingdom — both ARIA and AUGUR can read
                self._write_kingdom_whisper(signals)

                elapsed = round(time.time() - t0, 1)
                self._notify_deep_cycle(signals, calendar_events, elapsed)

            logger.info(
                "deep_cycle_complete",
                elapsed_s = round(time.time() - t0, 1),
                signals   = len(signals),
                events    = len(calendar_events),
                wallets   = len(self._wallets),
            )
        except Exception as e:
            logger.error("deep_cycle_error", error=str(e))

    async def _deep_cycle_forever(self) -> None:
        while True:
            await self._run_deep_cycle()
            logger.info("deep_cycle_sleeping", next_run_h=6)
            await asyncio.sleep(_DEEP_INTERVAL_S)

    # ── Hot poll (15min) ───────────────────────────────────────────────────

    async def _run_hot_poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # If no wallets yet, try discovery first (uses Binance flows)
                if not self._wallets:
                    await self._maybe_discover(session)
                if self._wallets:
                    await self._track_all_positions(session)
                # Always fetch fresh Binance flow wallets for hot poll
                # These are transient — represent the last ~100 trades per symbol
                flow_wallets = await self._discovery._from_binance_flows(session, _MAX_WALLETS)
                if flow_wallets:
                    # Merge: flow wallets are always fresh, no reputation history needed
                    combined = {w.address: w for w in self._wallets}
                    combined.update({w.address: w for w in flow_wallets})
                    poll_wallets = list(combined.values())
                else:
                    poll_wallets = self._wallets

            regime = self._bridge.get_regime()
            hot    = self._cluster.detect(poll_wallets, regime)
            self._write_hot_signals(hot)
            logger.info("hot_poll_complete",
                        wallets=len(poll_wallets), signals=len(hot))
        except Exception as e:
            logger.warning("hot_poll_error", error=str(e))

    async def _hot_poll_forever(self) -> None:
        # Stagger by 2min so it doesn't overlap with deep cycle startup
        await asyncio.sleep(120)
        while True:
            await self._run_hot_poll()
            await asyncio.sleep(_HOT_POLL_INTERVAL_S)

    # ── Entry point ────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        if not self._enabled:
            logger.warning("deep_intelligence_disabled",
                           reason="DEEPSEEK_API_KEY not set")
            return

        logger.info("deep_intelligence_started",
                    deep_interval_h=6, hot_interval_min=15)
        self._log_path.mkdir(parents=True, exist_ok=True)
        self._load_wallets()

        # Run both loops concurrently
        await asyncio.gather(
            self._deep_cycle_forever(),
            self._hot_poll_forever(),
        )
