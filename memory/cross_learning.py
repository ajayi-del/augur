"""
Cross-Learning Engine.

The twins teach each other.

When ARIA closes a trade:
  Was AUGUR right about it?
  The truth flows to AUGUR's alignment score.

When AUGUR closes a position:
  Was ARIA also in this trade?
  The truth flows to AUGUR's hist_wr per (symbol, direction).

No human intervention. No manual calibration.
The kingdom gets smarter by existing.

In 30 days: hist_wr calibrated.
In 90 days: both agents know each other.
In 180 days: the system no one designed — it emerged.
"""

import json
import time
import structlog
from pathlib import Path
from typing import Optional

from memory.augur_hist_wr import augur_hist_wr

logger = structlog.get_logger(__name__)

_LOG_PATH      = Path("logs/cross_learning.jsonl")
_ALIGN_DELTA   = 0.05
_ALIGN_FLOOR   = 0.10
_ALIGN_CEILING = 0.90


class CrossLearningEngine:
    """
    Bi-directional learning between ARIA and AUGUR.

    The alignment score in kingdom finance encodes:
      > 0.5 → AUGUR tends to agree with ARIA when ARIA is right
      < 0.5 → AUGUR tends to disagree with ARIA when ARIA is right
      = 0.5 → no meaningful relationship established yet

    The hist_wr per (symbol, direction) encodes:
      > 0.5 → AUGUR wins more than it loses on this symbol/direction
      < 0.5 → AUGUR loses more than it wins
      = 0.5 → insufficient data (< 10 trades)
    """

    def __init__(self, kingdom):
        self._kingdom = kingdom

    def on_aria_trade_closed(
        self,
        symbol:          str,
        aria_direction:  str,
        aria_won:        bool,
        augur_direction: Optional[str],
        personality:     Optional[str] = None,
    ) -> None:
        """
        Called when ARIA closes a trade.
        Determines if AUGUR agreed and updates alignment accordingly.

        Both right → +0.05 (alignment grows)
        ARIA right, AUGUR wrong → -0.05 (AUGUR misread)
        Both wrong → -0.05 (agreement doesn't help if both wrong)
        ARIA wrong, AUGUR right → +0.05 (AUGUR had better read)
        No AUGUR bet → no delta (can't learn from absence)
        """
        augur_agreed = (augur_direction == aria_direction) if augur_direction else None

        if augur_agreed is None:
            delta = 0.0
        elif aria_won and augur_agreed:
            delta = +_ALIGN_DELTA
        elif aria_won and not augur_agreed:
            delta = -_ALIGN_DELTA
        elif not aria_won and augur_agreed:
            delta = -_ALIGN_DELTA
        else:  # ARIA wrong, AUGUR disagreed → AUGUR was right
            delta = +_ALIGN_DELTA

        new_alignment = self._update_alignment(symbol, delta) if delta != 0.0 else 0.50

        entry = {
            "event":           "cross_learning_aria_closed",
            "symbol":          symbol,
            "aria_direction":  aria_direction,
            "aria_won":        aria_won,
            "augur_direction": augur_direction,
            "augur_agreed":    augur_agreed,
            "alignment_delta": delta,
            "new_alignment":   new_alignment,
            "personality":     personality,
            "timestamp_ms":    int(time.time() * 1000),
        }
        self._log(entry)
        logger.info(
            "cross_learning_fired",
            symbol=symbol,
            aria_won=aria_won,
            augur_agreed=augur_agreed,
            alignment_delta=delta,
            new_alignment=new_alignment,
        )

    def on_augur_bet_resolved(
        self,
        symbol:      str,
        direction:   str,
        personality: str,
        augur_won:   bool,
        session:     str = "all",
    ) -> None:
        """
        Called when AUGUR closes a perp position.
        Updates hist_wr so future bets are calibrated by reality.
        """
        base_symbol = symbol.replace("-USD", "").replace("-PERP", "")
        augur_hist_wr.update(base_symbol, direction, augur_won, session)

        entry = {
            "event":        "augur_bet_resolved",
            "symbol":       symbol,
            "direction":    direction,
            "personality":  personality,
            "correct":      augur_won,
            "timestamp_ms": int(time.time() * 1000),
        }
        self._log(entry)
        logger.info(
            "augur_bet_resolved",
            symbol=symbol,
            direction=direction,
            personality=personality,
            correct=augur_won,
        )

    def get_alignment(self, symbol: str) -> float:
        """Current AUGUR/ARIA alignment for a symbol. 0.5=neutral."""
        try:
            finance = self._kingdom.read_finance()
            return finance.get("agent_alignment", {}).get(symbol, 0.50)
        except Exception:
            return 0.50

    def _update_alignment(self, symbol: str, delta: float) -> float:
        try:
            finance    = self._kingdom.read_finance()
            alignments = finance.get("agent_alignment", {})
            current    = alignments.get(symbol, 0.50)
            new        = round(max(_ALIGN_FLOOR, min(_ALIGN_CEILING, current + delta)), 4)
            alignments[symbol]         = new
            finance["agent_alignment"] = alignments
            self._kingdom.write_finance(finance)
            return new
        except Exception as e:
            logger.error("cross_learning_alignment_error", error=str(e))
            return 0.50

    def _log(self, entry: dict) -> None:
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
