"""
AUGUR Nietzsche — The Will to Power.

'Man must be surpassed. What is great in man is that
he is a bridge and not an end.'
— Friedrich Nietzsche, Thus Spoke Zarathustra

Kant certifies the structure is sound.
Nietzsche determines the magnitude of will.

Four inputs. One output. The will_state.
The will_state is not a risk parameter.
It is the character of the trade.

AGGRESSIVE: the evidence screams. Act with full force.
CONVICTED:  the evidence speaks. Act with full size.
NEUTRAL:    the evidence whispers. Act with caution.
CAUTIOUS:   the evidence murmurs. Act very small.
ABSTAIN:    the evidence is silent. Wait.
"""

import structlog
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from intelligence.augur_personalities import AugurPersonality, AugurSignal, PERSONALITY_SIZE_MULT
from intelligence.augur_kant import KantFrame

logger = structlog.get_logger(__name__)


class WillState(Enum):
    AGGRESSIVE = "aggressive"  # conviction > 0.75, alignment > 0.70, wr > 0.55
    CONVICTED  = "convicted"   # conviction > 0.55, alignment > 0.50
    NEUTRAL    = "neutral"     # conviction > 0.40
    CAUTIOUS   = "cautious"    # conviction > 0.25
    ABSTAIN    = "abstain"     # conviction ≤ 0.25 — no trade


# Will state → (size_mult, preferred_order_type)
_WILL_CONFIG = {
    WillState.AGGRESSIVE: (1.30, "market"),  # speed > price when fully convicted
    WillState.CONVICTED:  (1.00, "limit"),
    WillState.NEUTRAL:    (0.70, "limit"),
    WillState.CAUTIOUS:   (0.40, "limit"),
    WillState.ABSTAIN:    (0.00, None),
}


@dataclass
class NietzscheOutput:
    """
    The will_state is the soul of the trade.
    Everything else is implementation detail.
    """
    conviction:      float
    will_state:      WillState
    size_mult:       float
    order_type:      Optional[str]
    hist_wr:         float
    agent_alignment: float
    edge:            float
    personality:     str


class AugurNietzsche:
    """
    Nietzsche reads four truths simultaneously and produces one will.

    The four inputs:
      1. edge          — what the market shows right now
      2. hist_wr       — what history teaches
      3. agent_alignment — what ARIA believes (the twin's conviction)
      4. kant_frame.confidence — how structurally sound the signal is

    Personality multiplier applies last — it is the character modifier.
    Conviction below the ABSTAIN threshold means no trade.
    The system waits. The system is patient.
    """

    def compute(
        self,
        signal:          AugurSignal,
        kant_frame:      KantFrame,
        personality:     AugurPersonality,
        hist_wr:         float,
        agent_alignment: float,
    ) -> NietzscheOutput:

        edge = max(signal.edge, 0.0)
        personality_mult = PERSONALITY_SIZE_MULT[personality]

        # Core conviction formula
        conviction = round(min((
            edge              * 0.35 +
            hist_wr           * 0.30 +
            agent_alignment   * 0.25 +
            kant_frame.confidence * 0.10
        ) * personality_mult, 0.95), 4)

        # Will state determination
        if conviction > 0.75 and agent_alignment > 0.70 and hist_wr > 0.55:
            will_state = WillState.AGGRESSIVE
        elif conviction > 0.55 and agent_alignment > 0.50:
            will_state = WillState.CONVICTED
        elif conviction > 0.40:
            will_state = WillState.NEUTRAL
        elif conviction > 0.25:
            will_state = WillState.CAUTIOUS
        else:
            will_state = WillState.ABSTAIN

        size_mult, order_type = _WILL_CONFIG[will_state]

        output = NietzscheOutput(
            conviction=conviction,
            will_state=will_state,
            size_mult=size_mult,
            order_type=order_type,
            hist_wr=hist_wr,
            agent_alignment=agent_alignment,
            edge=edge,
            personality=personality.value,
        )

        logger.info(
            "augur_nietzsche_output",
            symbol=signal.symbol,
            conviction=conviction,
            will_state=will_state.value,
            hist_wr=round(hist_wr, 3),
            agent_alignment=round(agent_alignment, 3),
            edge=round(edge, 3),
            size_mult=size_mult,
            personality=personality.value,
        )

        return output
