"""
Bybit V5 perpetuals client — fallback when MEXC execution fails.

Auth: HMAC-SHA256
  sign_str = f"{timestamp}{api_key}{recv_window}{payload}"
  X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN, X-BAPI-RECV-WINDOW
"""

import hashlib
import hmac
import json
import time
import uuid
import aiohttp
import structlog
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

logger = structlog.get_logger(__name__)

_BASE_URL    = "https://api.bybit.com"
_RECV_WINDOW = "5000"

_SYMBOL_MAP: dict[str, str] = {
    # Majors
    "BTC-USD":       "BTCUSDT",
    "ETH-USD":       "ETHUSDT",
    "SOL-USD":       "SOLUSDT",
    "BNB-USD":       "BNBUSDT",
    "DOGE-USD":      "DOGEUSDT",
    "AVAX-USD":      "AVAXUSDT",
    # Layer 2 / alts
    "ARB-USD":       "ARBUSDT",
    "OP-USD":        "OPUSDT",
    "SUI-USD":       "SUIUSDT",
    "MNT-USD":       "MNTUSDT",
    "EDGE-USD":      "EDGEUSDT",
    # Meme / culture coins
    "BONK-USD":      "BONKUSDT",
    "WIF-USD":       "WIFUSDT",
    "PEPE-USD":      "PEPEUSDT",
    "1000PEPE-USD":  "1000PEPEUSDT",
    "TRUMP-USD":     "TRUMPUSDT",
    "CHILLGUY-USD":  "CHILLGUYUSDT",
    "PIPPIN-USD":    "PIPPINUSDT",
    "PIEVERSE-USD":  "PIEUSDT",
    "XAUT-USD":      "XAUTUSDT",
}


@dataclass
class OrderResult:
    order_id:  str
    venue:     str
    symbol:    str
    direction: str
    size_usd:  float
    entry:     float
    status:    str


class BybitClient:
    """
    Bybit V5 linear perps — MEXC fallback.
    mode='live'  → real signed API calls.
    mode='paper' → simulated fills (no network).
    """

    name = "bybit"

    def __init__(self, mode: str = "paper", api_key: str = "", api_secret: str = ""):
        self.mode       = mode
        self.api_key    = api_key
        self.api_secret = api_secret
        logger.info("bybit_client_init", mode=mode, has_key=bool(api_key))

    def _to_bybit_symbol(self, aria_symbol: str) -> str:
        return _SYMBOL_MAP.get(aria_symbol, aria_symbol.replace("-USD", "USDT"))

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, payload: str) -> str:
        raw = f"{timestamp}{self.api_key}{_RECV_WINDOW}{payload}"
        return hmac.new(self.api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    def _auth_headers(self, timestamp: str, payload: str) -> dict:
        return {
            "X-BAPI-API-KEY":     self.api_key,
            "X-BAPI-TIMESTAMP":   timestamp,
            "X-BAPI-SIGN":        self._sign(timestamp, payload),
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "Content-Type":       "application/json",
        }

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _post(self, path: str, body: dict) -> dict:
        ts = str(int(time.time() * 1000))
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._auth_headers(ts, body_str)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(f"{_BASE_URL}{path}", data=body_str, headers=headers) as r:
                return await r.json(content_type=None)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        ts = str(int(time.time() * 1000))
        param_str = urlencode(sorted((params or {}).items()))
        headers = self._auth_headers(ts, param_str)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{_BASE_URL}{path}", params=params, headers=headers) as r:
                return await r.json(content_type=None)

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        if self.mode == "paper":
            return 150.0
        try:
            resp = await self._get(
                "/v5/account/wallet-balance", {"accountType": "UNIFIED"}
            )
            coins = (
                resp.get("result", {}).get("list", [{}])[0].get("coin", [])
            )
            for c in coins:
                if c.get("coin") == "USDT":
                    return float(c.get("availableToWithdraw", 0.0))
        except Exception as e:
            logger.warning("bybit_balance_error", error=str(e))
        return 0.0

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _set_leverage(self, bybit_symbol: str, leverage: int) -> None:
        try:
            await self._post("/v5/position/set-leverage", {
                "category":     "linear",
                "symbol":       bybit_symbol,
                "buyLeverage":  str(leverage),
                "sellLeverage": str(leverage),
            })
        except Exception as e:
            logger.warning("bybit_leverage_error", symbol=bybit_symbol, error=str(e))

    async def place_order(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        entry: float = 0.0,
        stop: float = 0.0,
        tp1: float = 0.0,
        tp2: float = 0.0,
        tp3: float = 0.0,
        leverage: int = 5,
    ) -> OrderResult:
        leverage = min(leverage, 5)

        if self.mode == "paper":
            oid = f"BB-PAPER-{uuid.uuid4().hex[:8]}"
            logger.info(
                "bybit_paper_order",
                symbol=symbol, direction=direction, size_usd=size_usd,
            )
            return OrderResult(
                order_id=oid, venue="bybit_paper", symbol=symbol,
                direction=direction, size_usd=size_usd, entry=entry, status="filled",
            )

        bybit_sym = self._to_bybit_symbol(symbol)
        side      = "Buy" if direction == "long" else "Sell"

        if entry > 0:
            raw_qty = size_usd / entry
            # Bybit qty precision varies by price tier (step size from instrument info)
            # Heuristic: integer for sub-$1 and sub-$10 assets; decimals for majors
            if entry < 1:
                qty = int(raw_qty)          # OP, PEPE etc — integer contracts
            elif entry < 10:
                qty = round(raw_qty, 1)
            elif entry < 100:
                qty = round(raw_qty, 2)
            else:
                qty = round(raw_qty, 3)
        else:
            qty = 1

        await self._set_leverage(bybit_sym, leverage)

        body: dict = {
            "category":    "linear",
            "symbol":      bybit_sym,
            "side":        side,
            "orderType":   "Market",
            "qty":         str(qty),
            "timeInForce": "IOC",
        }
        if stop > 0:
            body["stopLoss"] = str(round(stop, 4))
        if tp1 > 0:
            body["takeProfit"] = str(round(tp1, 4))

        try:
            resp = await self._post("/v5/order/create", body)
            ret_code = resp.get("retCode", -1)
            if ret_code == 0:
                oid = resp.get("result", {}).get("orderId", f"BB-{int(time.time())}")
                logger.info(
                    "bybit_order_placed",
                    symbol=bybit_sym, side=side, qty=qty, order_id=oid,
                )
                return OrderResult(
                    order_id=oid, venue="bybit", symbol=symbol,
                    direction=direction, size_usd=size_usd, entry=entry, status="placed",
                )
            msg = resp.get("retMsg", str(resp))
            logger.warning("bybit_order_rejected", symbol=bybit_sym, reason=msg)
            raise RuntimeError(f"Bybit rejected: {msg}")

        except RuntimeError:
            raise
        except Exception as e:
            logger.error("bybit_order_error", symbol=symbol, error=str(e))
            raise

    async def health_check(self) -> bool:
        try:
            bal = await self.get_balance()
            logger.info("bybit_health_ok", usdt_balance=round(bal, 2))
            return True
        except Exception as e:
            logger.warning("bybit_health_failed", error=str(e))
            return False
