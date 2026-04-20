"""
Cross-agent feedback loop.

Runs every 120s. Reads kingdom state to see if ARIA closed a trade.
Checks whether AUGUR had a bet in the same direction.
Updates the alignment score in kingdom state — which ARIA reads on the next cycle
to calibrate its confidence in AUGUR's conviction signals.

Trade lifecycle cross-learning:
  ARIA trade closes (win) + AUGUR agreed   → boost alignment for this symbol
  ARIA trade closes (win) + AUGUR disagreed → reduce alignment for this symbol
  ARIA trade closes (loss) + AUGUR agreed  → reduce alignment
  ARIA trade closes (loss) + AUGUR disagreed → boost alignment (AUGUR was right)

Alignment score per symbol stored in kingdom state:
  kingdom["finance"]["agent_alignment"][symbol] = 0.0-1.0
  Default: 0.5 (neutral)
"""

import asyncio
import json
import time
import structlog
from pathlib import Path
from typing import Dict, Optional

logger = structlog.get_logger(__name__)

_ALIGN_DELTA   = 0.05   # how much each outcome shifts alignment
_ALIGN_FLOOR   = 0.10
_ALIGN_CEILING = 0.90
_FEEDBACK_LOG  = Path("logs/cross_agent_feedback.jsonl")


class CrossAgentFeedback:
    """
    Reads ARIA outcome data from kingdom state and updates per-symbol
    AUGUR/ARIA alignment scores. Both bots read these to calibrate conviction.
    """

    def __init__(self, kingdom):
        self._kingdom = kingdom
        self._last_seen_aria_close: Dict[str, int] = {}   # symbol → last observed closed_ms

    # ── Public interface ─────────────────────────────────────────────────────

    async def feedback_loop(self) -> None:
        """Inline loop — designed for _supervise() wrapping. Runs every 120s."""
        logger.info("cross_agent_feedback_started", interval_s=120)
        while True:
            try:
                await self._process_feedback()
            except Exception as e:
                logger.error("cross_agent_feedback_error", error=str(e))
            await asyncio.sleep(120)

    # ── Core logic ───────────────────────────────────────────────────────────

    async def _process_feedback(self) -> None:
        state = self._kingdom.read()
        aria  = state.aria

        # ARIA writes closed positions to open_positions with a "closed_ms" field
        # when a position is resolved. We look for newly-closed positions.
        for pos in aria.open_positions:
            symbol    = pos.get("symbol", "")
            closed_ms = pos.get("closed_ms", 0)
            won       = pos.get("won")   # True/False/None

            if not symbol or not closed_ms or won is None:
                continue

            # Already processed this close
            if self._last_seen_aria_close.get(symbol, 0) >= closed_ms:
                continue

            self._last_seen_aria_close[symbol] = closed_ms

            # Check if AUGUR had an active bet on this symbol in same direction
            augur_direction = None
            now_ms = int(time.time() * 1000)
            for bet in state.augur.active_bets:
                if (bet.get("symbol") == symbol and
                        bet.get("expires_ms", 0) > now_ms - 30 * 60 * 1000):
                    augur_direction = bet.get("direction")
                    break

            aria_direction = pos.get("direction")
            augur_agreed   = (augur_direction == aria_direction) if augur_direction else None

            alignment = self._update_alignment(state, symbol, won, augur_agreed)

            entry = {
                "event":          "cross_agent_feedback",
                "symbol":         symbol,
                "aria_direction": aria_direction,
                "aria_won":       won,
                "augur_direction": augur_direction,
                "augur_agreed":   augur_agreed,
                "new_alignment":  alignment,
                "timestamp_ms":   int(time.time() * 1000),
            }
            self._append_log(entry)
            logger.info("cross_agent_feedback_applied",
                        symbol=symbol, aria_won=won,
                        augur_agreed=augur_agreed,
                        alignment=round(alignment, 3))

    def _update_alignment(
        self,
        state,
        symbol: str,
        aria_won: bool,
        augur_agreed: Optional[bool],
    ) -> float:
        finance = state.finance or {}
        alignments: Dict[str, float] = finance.get("agent_alignment", {})
        current = alignments.get(symbol, 0.50)

        if augur_agreed is None:
            # No AUGUR bet — nudge toward neutral
            new = current + (0.50 - current) * 0.1
        elif aria_won and augur_agreed:
            # Both correct — strengthen alignment
            new = current + _ALIGN_DELTA
        elif aria_won and not augur_agreed:
            # ARIA won, AUGUR was wrong — weaken
            new = current - _ALIGN_DELTA
        elif not aria_won and augur_agreed:
            # Both wrong — weaken
            new = current - _ALIGN_DELTA
        else:
            # ARIA lost, AUGUR disagreed — AUGUR was right, strengthen
            new = current + _ALIGN_DELTA

        new = round(max(_ALIGN_FLOOR, min(_ALIGN_CEILING, new)), 4)
        alignments[symbol] = new

        # Write back to kingdom finance section
        finance["agent_alignment"] = alignments
        self._kingdom.write_finance(finance)
        return new

    def get_alignment(self, symbol: str) -> float:
        """Current AUGUR/ARIA alignment for a symbol. 0.5=neutral, 1.0=perfect."""
        try:
            finance = self._kingdom.read_finance()
            return finance.get("agent_alignment", {}).get(symbol, 0.50)
        except Exception:
            return 0.50

    # ── Persistence ──────────────────────────────────────────────────────────

    def _append_log(self, entry: dict) -> None:
        try:
            _FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_FEEDBACK_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
