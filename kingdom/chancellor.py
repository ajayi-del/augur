"""
The Kingdom Chancellor.

The Chancellor is the constitution of the kingdom.
Not a trader. Not a signal generator. A governor.

'In the kingdom of ends, each rational being acts
 as both subject and sovereign — under universal law.'
— Kant, Groundwork of the Metaphysics of Morals

Three powers:
  AUTHORIZE — trade proceeds as sized or boosted
  MODIFY    — trade proceeds at reduced size
  VETO      — trade is blocked

The Chancellor has absolute authority.
No agent can override a VETO.
No agent can override a MODIFY.
The constitution is not optional.

The Chancellor serves the kingdom, not either agent.
ARIA is sovereign over narrative. AUGUR is sovereign over probability.
The Chancellor is sovereign over both.
"""

import structlog
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = structlog.get_logger(__name__)


class Agreement(Enum):
    COMPOUND_STRONG  = "compound_strong"    # both agree, strong conviction
    COMPOUND_WEAK    = "compound_weak"      # both agree, moderate conviction
    CONFLICT         = "conflict"           # agents disagree on direction
    SINGLE_ARIA_STRONG  = "single_aria_strong"
    SINGLE_ARIA_WEAK    = "single_aria_weak"
    SINGLE_AUGUR_STRONG = "single_augur_strong"
    SINGLE_AUGUR_WEAK   = "single_augur_weak"
    NONE             = "none"               # no signal from either agent


@dataclass
class ChancellorDecision:
    action:         str    # AUTHORIZE / MODIFY / VETO
    size_modifier:  float  # multiply base size by this (0.0 = veto)
    reason:         str
    aria_executes:  bool
    augur_executes: bool

    @classmethod
    def veto(cls, reason: str) -> "ChancellorDecision":
        return cls(
            action="VETO",
            size_modifier=0.0,
            reason=reason,
            aria_executes=False,
            augur_executes=False,
        )


class Chancellor:
    """
    The Chancellor adjudicates between ARIA and AUGUR.
    Called before every execution — by either agent.

    When ARIA and AUGUR agree: amplify.
    When they disagree: reduce, ARIA executes, AUGUR stands down.
    When the kingdom is threatened: veto everything.
    """

    CONSTITUTION = {
        "max_kingdom_exposure_pct": 0.60,
        "max_symbol_exposure_pct":  0.15,
        "max_daily_loss_pct":       0.05,
        "compound_size_boost":      1.25,
        "conflict_size_penalty":    0.20,
        "veto_drawdown_threshold":  0.08,
        "veto_extreme_cascade_z":   5.0,
        "emergency_halt_balance":   200.0,
    }

    def adjudicate(
        self,
        aria_direction:      Optional[str],
        aria_coherence:      float,
        augur_direction:     Optional[str],
        augur_conviction:    float,
        aria_drawdown:       float,
        daily_loss_pct:      float,
        cascade_zscore:      float,
        total_exposure_pct:  float,
        symbol_exposure_pct: float,
        balance:             float,
    ) -> ChancellorDecision:
        """
        Adjudicate. The decision is binding.
        Callers must respect it without exception.
        """
        c = self.CONSTITUTION

        # Emergency veto — evaluated before any agreement logic
        if balance < c["emergency_halt_balance"]:
            return self._veto_log("balance_below_floor",
                                  balance=round(balance, 2))
        if daily_loss_pct > c["max_daily_loss_pct"]:
            return self._veto_log("daily_loss_limit_breached",
                                  daily_loss_pct=round(daily_loss_pct, 4))
        if aria_drawdown > c["veto_drawdown_threshold"]:
            return self._veto_log("drawdown_veto_threshold",
                                  aria_drawdown=round(aria_drawdown, 4))
        if cascade_zscore > c["veto_extreme_cascade_z"]:
            return self._veto_log("extreme_cascade_active",
                                  cascade_z=round(cascade_zscore, 2))

        # Treasury gates
        if total_exposure_pct >= c["max_kingdom_exposure_pct"]:
            return self._veto_log("kingdom_overextended",
                                  total_pct=round(total_exposure_pct, 3))
        if symbol_exposure_pct >= c["max_symbol_exposure_pct"]:
            return self._veto_log("symbol_overextended",
                                  symbol_pct=round(symbol_exposure_pct, 3))

        agreement = self._assess_agreement(
            aria_direction, aria_coherence,
            augur_direction, augur_conviction,
        )

        decision = self._decide(agreement, c)

        logger.info(
            "chancellor_decision",
            agreement=agreement.value,
            action=decision.action,
            size_modifier=decision.size_modifier,
            aria_executes=decision.aria_executes,
            augur_executes=decision.augur_executes,
            reason=decision.reason,
        )
        return decision

    def _assess_agreement(
        self,
        aria_dir:   Optional[str],
        aria_coh:   float,
        augur_dir:  Optional[str],
        augur_conv: float,
    ) -> Agreement:
        if aria_dir and augur_dir:
            if aria_dir == augur_dir:
                combined = (aria_coh / 10.0) + augur_conv
                return (Agreement.COMPOUND_STRONG if combined > 1.40
                        else Agreement.COMPOUND_WEAK)
            return Agreement.CONFLICT

        if aria_dir:
            return (Agreement.SINGLE_ARIA_STRONG if aria_coh > 7.0
                    else Agreement.SINGLE_ARIA_WEAK)
        if augur_dir:
            return (Agreement.SINGLE_AUGUR_STRONG if augur_conv > 0.60
                    else Agreement.SINGLE_AUGUR_WEAK)
        return Agreement.NONE

    def _decide(self, agreement: Agreement, c: dict) -> ChancellorDecision:
        table: dict[Agreement, ChancellorDecision] = {
            Agreement.COMPOUND_STRONG: ChancellorDecision(
                action="AUTHORIZE",
                size_modifier=c["compound_size_boost"],  # 1.25x
                reason="compound_strong_agreement",
                aria_executes=True, augur_executes=True,
            ),
            Agreement.COMPOUND_WEAK: ChancellorDecision(
                action="AUTHORIZE",
                size_modifier=1.0,
                reason="compound_weak_agreement",
                aria_executes=True, augur_executes=True,
            ),
            Agreement.CONFLICT: ChancellorDecision(
                action="MODIFY",
                size_modifier=c["conflict_size_penalty"],  # 0.20x — agents disagree
                reason="agent_conflict",
                aria_executes=True, augur_executes=False,
            ),
            Agreement.SINGLE_ARIA_STRONG: ChancellorDecision(
                action="AUTHORIZE",
                size_modifier=0.70,
                reason="single_aria_strong",
                aria_executes=True, augur_executes=False,
            ),
            Agreement.SINGLE_ARIA_WEAK: ChancellorDecision(
                action="MODIFY",
                size_modifier=0.40,
                reason="single_aria_weak",
                aria_executes=True, augur_executes=False,
            ),
            Agreement.SINGLE_AUGUR_STRONG: ChancellorDecision(
                action="AUTHORIZE",
                size_modifier=0.50,
                reason="single_augur_strong",
                aria_executes=False, augur_executes=True,
            ),
            Agreement.SINGLE_AUGUR_WEAK: ChancellorDecision(
                action="MODIFY",
                size_modifier=0.25,
                reason="single_augur_weak",
                aria_executes=False, augur_executes=True,
            ),
            Agreement.NONE: ChancellorDecision.veto("no_signal"),
        }
        return table[agreement]

    def _veto_log(self, reason: str, **ctx) -> ChancellorDecision:
        logger.warning("chancellor_emergency_veto", reason=reason, **ctx)
        return ChancellorDecision.veto(reason)
