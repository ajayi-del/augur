"""
AUGUR historical win rate tracker.

Tracks win/loss per (symbol, direction, session) tuple.
Minimum 10 trades before hist_wr replaces the 0.50 neutral default.
Persists to logs/augur_hist_wr.json.

Used by augur_signal_loop to calibrate native bet confidence:
  conf = base_signal_strength * hist_wr_multiplier
"""

import json
import time
import structlog
from pathlib import Path
from typing import Dict, Tuple

logger = structlog.get_logger(__name__)

_PERSIST_PATH  = Path("logs/augur_hist_wr.json")
_MIN_SAMPLES   = 10    # minimum trades before using real win rate


def _key(symbol: str, direction: str, session: str = "all") -> str:
    return f"{symbol}:{direction}:{session}"


class AugurHistWR:
    """
    Win-rate calibration store for AUGUR.
    Thread-safe for async reads; writes are atomic via full-file replace.
    """

    def __init__(self):
        self._data: Dict[str, Dict] = {}   # key → {"wins": int, "total": int}
        self._load()

    # ── Public interface ─────────────────────────────────────────────────────

    def get(self, symbol: str, direction: str, session: str = "all") -> float:
        """
        Returns calibrated win rate for (symbol, direction, session).
        Returns 0.50 (neutral) when sample count < MIN_SAMPLES.
        """
        entry = self._data.get(_key(symbol, direction, session))
        if not entry or entry.get("total", 0) < _MIN_SAMPLES:
            return 0.50
        return entry["wins"] / entry["total"]

    def update(self, symbol: str, direction: str, won: bool, session: str = "all") -> None:
        """Record an outcome. Saves immediately."""
        for k in [_key(symbol, direction, session), _key(symbol, direction, "all")]:
            entry = self._data.setdefault(k, {"wins": 0, "total": 0, "last_updated": 0})
            entry["total"] += 1
            if won:
                entry["wins"] += 1
            entry["last_updated"] = int(time.time())
        self._save()
        logger.debug("hist_wr_updated", symbol=symbol, direction=direction,
                     won=won, session=session,
                     new_wr=round(self.get(symbol, direction, session), 3))

    def confidence_multiplier(self, symbol: str, direction: str, session: str = "all") -> float:
        """
        Convert win rate to a confidence multiplier for AUGUR native bets.
        WR 0.70 → 1.20×, WR 0.60 → 1.10×, WR 0.50 → 1.0×, WR 0.40 → 0.90×
        """
        wr = self.get(symbol, direction, session)
        base = 1.0 + (wr - 0.50) * 2.0   # linear: WR=0.70 → +0.4, WR=0.30 → -0.4
        return round(max(0.60, min(1.40, base)), 3)

    def summary(self) -> Dict:
        """Top 10 best-tracked symbols by sample count."""
        return {
            k: {
                "wr":    round(v["wins"] / v["total"], 3) if v["total"] else 0.5,
                "n":     v["total"],
                "wins":  v["wins"],
            }
            for k, v in sorted(self._data.items(),
                                key=lambda x: x[1].get("total", 0), reverse=True)[:10]
        }

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if _PERSIST_PATH.exists():
                self._data = json.loads(_PERSIST_PATH.read_text())
                logger.info("augur_hist_wr_loaded",
                            keys=len(self._data),
                            path=str(_PERSIST_PATH))
        except Exception as e:
            logger.warning("augur_hist_wr_load_error", error=str(e))
            self._data = {}

    def _save(self) -> None:
        try:
            _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _PERSIST_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(_PERSIST_PATH)
        except Exception as e:
            logger.warning("augur_hist_wr_save_error", error=str(e))


# Module-level singleton
augur_hist_wr = AugurHistWR()
