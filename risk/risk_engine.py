import structlog
from dataclasses import dataclass
from typing import Optional

logger = structlog.get_logger()


@dataclass
class RiskResult:
    approved: bool
    gate: int           # 0 = passed all gates
    reason: str


class RiskEngine:
    """
    RISK ENGINE — 8 sovereign gates.
    validate() is the primary entry point used by checkpoints.
    validate_trade() is the legacy interface used by AUGUR main.py.
    """

    def __init__(self, settings=None):
        self.settings = settings
        self._max_drawdown = getattr(settings, "MAX_DRAWDOWN_PCT", 0.15) if settings else 0.15

    # ── Primary interface (used by checkpoints) ──────────────────────────────

    def validate(
        self,
        symbol: str,
        direction: str,
        coherence_score: float,
        size_mult: float,
        trade_type: str,
        kingdom_state=None,
        edge: float = 0.0,
        confidence: float = 1.0,
    ) -> RiskResult:
        """
        Returns RiskResult(approved, gate, reason).
        Gate 0 = all clear.
        Gate 1 = kingdom conflict (ARIA bet contradicts direction).
        Gate 2 = drawdown halt.
        Gate 3 = coherence too low.
        Gate 4 = insufficient edge (for polymarket bets).
        """
        # Gate 1 — Kingdom conflict: ARIA has an active bet in the opposite direction
        if kingdom_state is not None:
            aria = getattr(kingdom_state, "aria", None)
            if aria is not None:
                active_bets = getattr(aria, "active_bets", [])
                for bet in active_bets:
                    bet_sym = bet.get("symbol", "")
                    bet_dir = bet.get("direction", "")
                    # Normalise symbol comparison (BTC-USD vs BTC-PERP)
                    asset_self = symbol.split("-")[0]
                    asset_bet = bet_sym.split("-")[0]
                    if asset_self == asset_bet and bet_dir != direction and bet_dir != "neutral":
                        return RiskResult(
                            approved=False,
                            gate=1,
                            reason=(
                                f"kingdom_conflict: ARIA has {bet_dir} on {bet_sym} "
                                f"but requested direction={direction}"
                            ),
                        )

            # Gate 2 — Drawdown halt from kingdom
            drawdown = getattr(aria, "drawdown", 0.0) if aria else 0.0
            if drawdown >= self._max_drawdown:
                return RiskResult(
                    approved=False,
                    gate=2,
                    reason=f"drawdown_halt: drawdown={drawdown:.2%} >= max={self._max_drawdown:.2%}",
                )

        # Gate 3 — Coherence floor
        min_coherence = getattr(self.settings, "min_coherence", 5.0) if self.settings else 5.0
        if coherence_score < min_coherence * 0.5:   # use 50% of threshold as hard floor
            return RiskResult(
                approved=False,
                gate=3,
                reason=f"coherence_too_low: {coherence_score:.2f} < {min_coherence * 0.5:.2f}",
            )

        # Gate 4 — Edge gate for prediction market bets
        if trade_type in ("polymarket_bet", "prediction") and edge < 0.0:
            return RiskResult(
                approved=False,
                gate=4,
                reason=f"negative_edge: {edge:.3f}",
            )

        return RiskResult(approved=True, gate=0, reason="sovereign_gates_passed")

    # ── Legacy interface (used by AUGUR main.py) ─────────────────────────────

    def validate_trade(self, trade: dict, context: dict) -> tuple:
        """Returns (passed: bool, reason: str)."""
        if not trade.get("passes_kant_temporal", True):
            return False, "gate_1_temporal_fail"
        if not trade.get("passes_kant_category", True):
            return False, "gate_2_category_fail"
        if not trade.get("passes_kant_parity", True):
            return False, "gate_3_contradiction_fail"
        if trade.get("liquidity_usd", 99999) < 5000:
            return False, "gate_4_liquidity_fail"
        if trade.get("conviction_score", 1.0) < 0.40:
            return False, "gate_5_conviction_fail"
        if trade.get("size_pct_of_bankroll", 0.0) > 0.05:
            return False, "gate_6_exposure_fail"
        if trade.get("coherence_score", 1.0) < 0.50:
            return False, "gate_7_coherence_fail"

        finance = context.get("finance", {})
        if finance.get("drawdown_pct", 0.0) >= self._max_drawdown:
            return False, "gate_8_drawdown_halt"

        return True, "sovereign_gates_passed"
