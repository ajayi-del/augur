"""
MEXC live execution client — primary venue for AUGUR.

Futures API:    https://contract.mexc.com/api/v1
Spot/Predict:   https://api.mexc.com/api/v3

Futures auth:
  ApiKey       header: api_key
  Request-Time header: timestamp_ms
  Signature    header: HMAC-SHA256(api_key + timestamp_ms + body_or_params, secret)

Hard limits enforced here (not caller's responsibility):
  leverage      ≤ 5×
  notional      ≤ max_position_usdt (default $200)
  prediction    ≤ max_bet_pct of bankroll
"""

import hashlib
import hmac
import json
import time
import aiohttp
import structlog
from dataclasses import dataclass
from typing import Optional

logger = structlog.get_logger(__name__)

_FUTURES_BASE = "https://contract.mexc.com"
_SPOT_BASE    = "https://api.mexc.com"

# ARIA symbol → MEXC futures symbol (underscore format)
_SYMBOL_MAP: dict[str, str] = {
    # Majors
    "BTC-USD":       "BTC_USDT",
    "ETH-USD":       "ETH_USDT",
    "SOL-USD":       "SOL_USDT",
    "BNB-USD":       "BNB_USDT",
    "DOGE-USD":      "DOGE_USDT",
    "AVAX-USD":      "AVAX_USDT",
    # Layer 2 / alts
    "ARB-USD":       "ARB_USDT",
    "OP-USD":        "OP_USDT",
    "SUI-USD":       "SUI_USDT",
    "MNT-USD":       "MNT_USDT",
    "EDGE-USD":      "EDGE_USDT",
    # Meme / culture coins
    "BONK-USD":      "BONK_USDT",
    "WIF-USD":       "WIF_USDT",
    "PEPE-USD":      "PEPE_USDT",
    "1000PEPE-USD":  "1000PEPE_USDT",
    "TRUMP-USD":     "TRUMP_USDT",
    "BASED-USD":     "BASED_USDT",
    "CHILLGUY-USD":  "CHILLGUY_USDT",
    "PIPPIN-USD":    "PIPPIN_USDT",
    "PIEVERSE-USD":  "PIEVERSE_USDT",
    # Commodities (ARIA trades these — keep for kingdom sync)
    "XAUT-USD":      "XAUT_USDT",
}

# MEXC futures side codes
_OPEN_LONG   = 1
_CLOSE_LONG  = 2
_OPEN_SHORT  = 3
_CLOSE_SHORT = 4


@dataclass
class MexcOrderResult:
    order_id:  str
    venue:     str
    symbol:    str
    direction: str
    qty:       float
    price:     float
    status:    str
    raw:       dict


class MexcClient:
    """
    MEXC live execution: futures perps + prediction market bets.
    Instantiate once; reuse across loops.
    """

    def __init__(
        self,
        api_key: str,
        secret: str,
        leverage: int = 5,
        max_position_usdt: float = 200.0,
        prediction_bankroll: float = 50.0,
        prediction_max_bet_pct: float = 0.05,
    ):
        if leverage > 5:
            raise ValueError("MEXC leverage must not exceed 5× (hard rule)")
        self.api_key             = api_key
        self.secret              = secret
        self.leverage            = leverage
        self.max_position_usdt   = max_position_usdt
        self.prediction_bankroll = prediction_bankroll
        self.max_bet_pct         = prediction_max_bet_pct
        logger.info("mexc_client_init", leverage=leverage,
                    max_usdt=max_position_usdt, pred_bankroll=prediction_bankroll)

    # ── Symbol helpers ────────────────────────────────────────────────────────

    def to_mexc_symbol(self, aria_symbol: str) -> str:
        return _SYMBOL_MAP.get(aria_symbol, aria_symbol.replace("-USD", "_USDT"))

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _futures_sign(self, ts_ms: int, payload: str) -> str:
        raw = f"{self.api_key}{ts_ms}{payload}"
        return hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    def _futures_headers(self, ts_ms: int, payload: str) -> dict:
        return {
            "ApiKey":       self.api_key,
            "Request-Time": str(ts_ms),
            "Signature":    self._futures_sign(ts_ms, payload),
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _futures_get(self, path: str, params: dict | None = None) -> dict:
        ts = int(time.time() * 1000)
        param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        headers = self._futures_headers(ts, param_str)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{_FUTURES_BASE}{path}", params=params, headers=headers) as r:
                return await r.json(content_type=None)

    async def _futures_post(self, path: str, body: dict) -> dict:
        ts = int(time.time() * 1000)
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._futures_headers(ts, body_str)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(
                f"{_FUTURES_BASE}{path}", data=body_str, headers=headers
            ) as r:
                text = await r.text()
                if not text.strip():
                    raise RuntimeError(
                        f"MEXC returned empty body — check API key has futures "
                        f"trading permission (HTTP {r.status})"
                    )
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    raise RuntimeError(f"MEXC non-JSON response: {text[:200]}")

    # ── Market data (public) ──────────────────────────────────────────────────

    async def get_ticker_price(self, aria_symbol: str) -> float:
        """Current mid price for a futures symbol. Used to compute qty from notional."""
        mexc_sym = self.to_mexc_symbol(aria_symbol)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get(
                    f"{_FUTURES_BASE}/api/v1/contract/ticker",
                    params={"symbol": mexc_sym},
                ) as r:
                    data = await r.json(content_type=None)
                    ticker = data.get("data", {})
                    if isinstance(ticker, list):
                        ticker = ticker[0] if ticker else {}
                    price = float(ticker.get("lastPrice", 0.0))
                    if price > 0:
                        return price
        except Exception as e:
            logger.warning("mexc_ticker_error", symbol=mexc_sym, error=str(e))
        return 0.0

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Available USDT balance from MEXC futures account."""
        try:
            data = await self._futures_get("/api/v1/private/account/assets")
            assets = data.get("data", [])
            if isinstance(assets, dict):
                assets = [assets]
            for a in assets:
                if a.get("currency", "").upper() == "USDT":
                    return float(a.get("availableBalance", 0.0))
        except Exception as e:
            logger.warning("mexc_balance_error", error=str(e))
        return 0.0

    async def get_open_positions(self) -> list:
        """Current open futures positions."""
        try:
            data = await self._futures_get("/api/v1/private/position/list/all")
            return data.get("data", []) or []
        except Exception as e:
            logger.warning("mexc_positions_error", error=str(e))
            return []

    # ── Futures execution ─────────────────────────────────────────────────────

    async def place_futures_order(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        entry_price: float = 0.0,
        leverage: int | None = None,
    ) -> MexcOrderResult:
        """
        Place a MEXC futures order.

        symbol:      ARIA format (BTC-USD)
        direction:   'long' or 'short'
        size_usd:    notional USDT (capped at max_position_usdt)
        entry_price: 0.0 → fetch live price from ticker
        """
        size_usd = min(size_usd, self.max_position_usdt)
        lev = min(leverage or self.leverage, 5)
        mexc_sym = self.to_mexc_symbol(symbol)
        side = _OPEN_LONG if direction == "long" else _OPEN_SHORT

        price = entry_price if entry_price > 0 else await self.get_ticker_price(symbol)
        if price <= 0:
            raise RuntimeError(f"Cannot get price for {symbol} — aborting order")

        qty = round(size_usd / price, 6)

        body = {
            "symbol":   mexc_sym,
            "side":     side,
            "openType": 2,            # cross margin
            "type":     5,            # market order (fastest fill)
            "vol":      str(qty),
            "leverage": lev,
        }

        try:
            resp = await self._futures_post("/api/v1/private/order/submit", body)
            success = resp.get("success", False)
            order_id = str(resp.get("data", "") or f"MEXC-{int(time.time())}")

            if success:
                logger.info(
                    "mexc_futures_placed",
                    symbol=mexc_sym, direction=direction,
                    size_usd=size_usd, qty=qty, price=price,
                    order_id=order_id,
                )
                return MexcOrderResult(
                    order_id=order_id, venue="mexc_futures",
                    symbol=symbol, direction=direction,
                    qty=qty, price=price, status="placed", raw=resp,
                )

            msg = resp.get("message", str(resp))
            logger.warning("mexc_futures_rejected", symbol=mexc_sym, reason=msg, body=body)
            raise RuntimeError(f"MEXC rejected: {msg}")

        except RuntimeError:
            raise
        except Exception as e:
            logger.error("mexc_futures_error", symbol=symbol, error=str(e))
            raise

    # ── Prediction markets ────────────────────────────────────────────────────

    async def get_prediction_markets(self, category: str = "crypto") -> list:
        """Active MEXC prediction markets."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(
                    f"{_SPOT_BASE}/api/v3/prediction/markets",
                    params={"category": category, "status": "active"},
                    headers={"X-MEXC-APIKEY": self.api_key},
                ) as r:
                    data = await r.json(content_type=None)
                    return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.warning("mexc_prediction_markets_unavailable", error=str(e))
            return []

    async def place_prediction_bet(
        self, market_id: str, outcome: str, size_usdt: float
    ) -> dict:
        """
        Place a MEXC prediction market bet.
        size_usdt is capped at max_bet_pct × prediction_bankroll.
        """
        max_bet = self.prediction_bankroll * self.max_bet_pct
        size_usdt = min(size_usdt, max_bet)

        ts = int(time.time() * 1000)
        body = {
            "marketId":  market_id,
            "outcome":   outcome.upper(),
            "size":      str(round(size_usdt, 2)),
            "timestamp": ts,
        }
        body_str = json.dumps(body, separators=(",", ":"))
        sig = hmac.new(self.secret.encode(), body_str.encode(), hashlib.sha256).hexdigest()
        body["signature"] = sig

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(
                    f"{_SPOT_BASE}/api/v3/prediction/order",
                    data=json.dumps(body),
                    headers={"X-MEXC-APIKEY": self.api_key, "Content-Type": "application/json"},
                ) as r:
                    resp = await r.json(content_type=None)
                    logger.info(
                        "mexc_prediction_placed",
                        market_id=market_id, outcome=outcome, size=size_usdt,
                    )
                    return resp
        except Exception as e:
            logger.warning("mexc_prediction_error", market_id=market_id, error=str(e))
            return {"success": False, "error": str(e)}

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            bal = await self.get_balance()
            logger.info("mexc_health_ok", usdt_balance=round(bal, 2))
            return True
        except Exception as e:
            logger.warning("mexc_health_failed", error=str(e))
            return False
