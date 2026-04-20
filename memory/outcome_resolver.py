"""
AUGUR outcome resolver.

Runs every 60s. Reads AUGUR's open position journal and checks Bybit for closures.
When a position closes, records the outcome and updates hist_wr.

This closes the learning loop: AUGUR's future native bets get calibrated
confidence multipliers based on real trading results.

Position state tracking:
  - open_positions.json: list of AUGUR's active positions
  - When Bybit shows position gone: record win/loss from PnL
"""

import json
import time
import asyncio
import structlog
from pathlib import Path
from typing import Dict, List, Optional

from memory.augur_hist_wr import augur_hist_wr

logger = structlog.get_logger(__name__)

_OPEN_POSITIONS_PATH = Path("logs/augur_open_positions.json")


def _get_session(ts: float) -> str:
    """UTC hour → session name (matching ARIA session_config)."""
    import datetime
    h = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).hour
    if 13 <= h < 16:  return "overlap"
    if 16 <= h < 22:  return "us"
    if 7  <= h < 13:  return "london"
    return "asian"


class OutcomeResolver:
    """
    Reconciles AUGUR's live positions against Bybit's position list.
    Resolves wins/losses and updates hist_wr.
    """

    def __init__(self, bybit_client):
        self._bybit   = bybit_client
        self._tracked: List[Dict] = []
        self._load()

    # ── Public interface ─────────────────────────────────────────────────────

    def register_position(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        entry_price: float,
        venue: str = "bybit",
    ) -> None:
        """Call after a live order is placed."""
        session = _get_session(time.time())
        self._tracked.append({
            "symbol":      symbol,
            "direction":   direction,
            "size_usd":    size_usd,
            "entry_price": entry_price,
            "venue":       venue,
            "session":     session,
            "opened_ms":   int(time.time() * 1000),
            "resolved":    False,
        })
        self._save()
        logger.info("outcome_resolver_position_registered",
                    symbol=symbol, direction=direction, entry=entry_price)

    async def resolve_loop(self) -> None:
        """Inline loop — designed for _supervise() wrapping. Runs every 60s."""
        logger.info("outcome_resolver_started", interval_s=60)
        while True:
            try:
                await self._resolve_all()
            except Exception as e:
                logger.error("outcome_resolver_error", error=str(e))
            await asyncio.sleep(60)

    # ── Resolution logic ─────────────────────────────────────────────────────

    async def _resolve_all(self) -> None:
        unresolved = [p for p in self._tracked if not p.get("resolved")]
        if not unresolved:
            return

        try:
            open_positions = await self._bybit.get_open_positions()
        except Exception as e:
            logger.warning("outcome_resolver_bybit_positions_error", error=str(e))
            return

        open_syms = {p.get("symbol", "") for p in open_positions}

        for pos in unresolved:
            bybit_sym = pos.get("symbol", "").replace("-USD", "USDT")
            if bybit_sym in open_syms:
                continue   # still open

            # Position has closed — try to get final PnL
            pnl = await self._fetch_pnl(bybit_sym)
            won = pnl > 0 if pnl is not None else None

            pos["resolved"]    = True
            pos["resolved_ms"] = int(time.time() * 1000)
            pos["pnl_usd"]     = round(pnl, 4) if pnl is not None else None
            pos["won"]         = won

            if won is not None:
                augur_hist_wr.update(
                    symbol=pos["symbol"],
                    direction=pos["direction"],
                    won=won,
                    session=pos.get("session", "all"),
                )
                logger.info(
                    "outcome_resolved",
                    symbol=pos["symbol"],
                    direction=pos["direction"],
                    won=won,
                    pnl=round(pnl, 4) if pnl else None,
                    session=pos.get("session"),
                    new_wr=augur_hist_wr.get(pos["symbol"], pos["direction"]),
                )

        # Evict very old resolved positions (> 7 days)
        cutoff = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
        self._tracked = [
            p for p in self._tracked
            if not p.get("resolved") or p.get("resolved_ms", 0) > cutoff
        ]
        self._save()

    async def _fetch_pnl(self, bybit_symbol: str) -> Optional[float]:
        """Fetch closed PnL from Bybit trade history (last 2 hours)."""
        try:
            resp = await self._bybit._get(
                "/v5/position/closed-pnl",
                {"category": "linear", "symbol": bybit_symbol, "limit": "5"},
            )
            rows = resp.get("result", {}).get("list", [])
            if rows:
                return float(rows[0].get("closedPnl", 0.0))
        except Exception as e:
            logger.debug("outcome_pnl_fetch_error", symbol=bybit_symbol, error=str(e))
        return None

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if _OPEN_POSITIONS_PATH.exists():
                self._tracked = json.loads(_OPEN_POSITIONS_PATH.read_text())
        except Exception as e:
            logger.warning("outcome_resolver_load_error", error=str(e))
            self._tracked = []

    def _save(self) -> None:
        try:
            _OPEN_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _OPEN_POSITIONS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._tracked, indent=2))
            tmp.replace(_OPEN_POSITIONS_PATH)
        except Exception as e:
            logger.warning("outcome_resolver_save_error", error=str(e))
