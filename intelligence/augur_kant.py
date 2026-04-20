"""
AUGUR Kant Validation Engine.

'What can I know? What ought I to do? What may I hope?'
— Immanuel Kant, Critique of Pure Reason

AUGUR Kant does not predict outcomes.
It validates the preconditions for an ethical trade.

If preconditions fail: no trade.
If preconditions pass: proceed to Nietzsche.

Structural soundness is not a preference.
The kingdom cannot be built on sand.
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import List

from intelligence.augur_personalities import AugurPersonality, AugurSignal

logger = structlog.get_logger(__name__)


@dataclass
class KantCheck:
    name:   str
    passed: bool
    reason: str = ""


@dataclass
class KantFrame:
    """
    The result of Kant's categorical validation.
    If passed=False, Nietzsche is never consulted.
    The failed_checks list is the explanation.
    """
    passed:       bool
    structure:    str    # what structural regime this trade inhabits
    confidence:   float  # structural confidence [0, 1]
    coherence_min: float # minimum coherence for this structure
    order_type:   str    # market / limit
    size_cap:     float  # max size_usd — Kant constrains Nietzsche
    failed_checks: List[KantCheck] = field(default_factory=list)
    all_checks:    List[KantCheck] = field(default_factory=list)


class AugurKant:
    """
    Kant runs six universal checks then personality-specific checks.
    ALL must pass. One failure is a veto.

    This is not risk management. Risk management is downstream.
    Kant is structural ethics — the trade must be worthy of execution.
    """

    _STRUCTURE_CONFIGS = {
        "cascade_momentum": {"coherence_min": 4.0, "order_type": "market", "size_cap": 400.0},
        "directional":      {"coherence_min": 3.0, "order_type": "limit",  "size_cap": 200.0},
        "arbitrage":        {"coherence_min": 2.0, "order_type": "market", "size_cap": 150.0},
        "narrative":        {"coherence_min": 5.0, "order_type": "limit",  "size_cap": 180.0},
        "scout":            {"coherence_min": 2.0, "order_type": "limit",  "size_cap": 60.0},
        "sentinel_close":   {"coherence_min": 0.0, "order_type": "market", "size_cap": 0.0},
        "idle":             {"coherence_min": 8.0, "order_type": "limit",  "size_cap": 0.0},
    }

    def validate(
        self,
        signal:                   AugurSignal,
        personality:              AugurPersonality,
        bybit_connected:          bool,
        total_exposure_pct:       float,
        symbol_exposure_pct:      float,
        augur_has_position:       bool,
        aria_regime:              str,
        aria_drawdown:            float,
        kingdom_total_positions:  int,
        max_open_trades:          int,
    ) -> KantFrame:

        structure = self._classify_structure(signal, personality)
        cfg       = self._STRUCTURE_CONFIGS.get(structure, self._STRUCTURE_CONFIGS["idle"])
        confidence = self._compute_structural_confidence(signal, personality)

        checks = (
            self._run_universal_checks(
                signal=signal,
                personality=personality,
                bybit_connected=bybit_connected,
                total_exposure_pct=total_exposure_pct,
                symbol_exposure_pct=symbol_exposure_pct,
                augur_has_position=augur_has_position,
                aria_regime=aria_regime,
                aria_drawdown=aria_drawdown,
                kingdom_total_positions=kingdom_total_positions,
                max_open_trades=max_open_trades,
            )
            + self._run_personality_checks(signal, personality)
        )

        failed = [c for c in checks if not c.passed]
        passed = len(failed) == 0

        frame = KantFrame(
            passed=passed,
            structure=structure,
            confidence=confidence,
            coherence_min=cfg["coherence_min"],
            order_type=cfg["order_type"],
            size_cap=cfg["size_cap"],
            failed_checks=failed,
            all_checks=checks,
        )

        logger.info(
            "augur_kant_frame",
            symbol=signal.symbol,
            personality=personality.value,
            structure=structure,
            confidence=round(confidence, 3),
            passed=passed,
            failed=[c.name for c in failed],
        )

        return frame

    def _classify_structure(self, signal: AugurSignal, personality: AugurPersonality) -> str:
        if personality == AugurPersonality.SENTINEL:
            return "sentinel_close"
        if personality == AugurPersonality.ARBITRAGE:
            return "arbitrage"
        if personality == AugurPersonality.SCOUT:
            return "scout"
        if personality == AugurPersonality.ORACLE:
            return "narrative"
        if signal.cascade_zscore > 2.0:
            return "cascade_momentum"
        return "directional"

    def _compute_structural_confidence(
        self, signal: AugurSignal, personality: AugurPersonality
    ) -> float:
        base = min(abs(signal.combined - 0.50) * 2, 1.0)
        # Confirmations from independent signals boost structural confidence
        confirmations = sum([
            signal.price_momentum_pct >  0.2 and signal.direction == "long",
            signal.price_momentum_pct < -0.2 and signal.direction == "short",
            signal.agg_ratio > 0.60           and signal.direction == "long",
            signal.agg_ratio < 0.40           and signal.direction == "short",
            signal.cascade_zscore > 2.0,
        ])
        return round(min(base + confirmations * 0.08, 1.0), 3)

    def _run_universal_checks(
        self,
        signal:                  AugurSignal,
        personality:             AugurPersonality,
        bybit_connected:         bool,
        total_exposure_pct:      float,
        symbol_exposure_pct:     float,
        augur_has_position:      bool,
        aria_regime:             str,
        aria_drawdown:           float,
        kingdom_total_positions: int,
        max_open_trades:         int,
    ) -> List[KantCheck]:
        age_s = (int(time.time() * 1000) - signal.timestamp_ms) / 1000.0

        return [
            # 1. Signal must be fresh — stale signals are structurally unsound
            KantCheck(
                name="signal_fresh",
                passed=age_s < 300,
                reason=f"signal is {age_s:.0f}s old (max 300s)",
            ),
            # 2. Regime consistency — long into a bear/liquidation regime is irrational
            KantCheck(
                name="regime_consistent",
                passed=not (
                    aria_regime in ("risk_off", "bear", "liquidation")
                    and signal.direction == "long"
                    and personality not in (AugurPersonality.HEDGER, AugurPersonality.SENTINEL)
                ),
                reason=f"aria regime={aria_regime} conflicts with long signal",
            ),
            # 3. Capital structure — kingdom cannot be overextended
            KantCheck(
                name="capital_sound",
                passed=(total_exposure_pct < 0.60 and symbol_exposure_pct < 0.15),
                reason=(
                    f"overextended: total={total_exposure_pct:.1%} "
                    f"symbol={symbol_exposure_pct:.1%}"
                ),
            ),
            # 4. Venue health — cannot trade on broken infrastructure
            KantCheck(
                name="venue_healthy",
                passed=bybit_connected,
                reason="primary venue (bybit) disconnected",
            ),
            # 5. No duplicate position — unless personality allows amplification
            KantCheck(
                name="position_clean",
                passed=(
                    not augur_has_position
                    or personality in (AugurPersonality.MOMENTUM, AugurPersonality.HEDGER)
                ),
                reason="duplicate position without compound agreement",
            ),
            # 6. Max trades gate — the kingdom has limits
            KantCheck(
                name="trade_cap",
                passed=kingdom_total_positions < max_open_trades,
                reason=f"max trades reached ({kingdom_total_positions}/{max_open_trades})",
            ),
        ]

    def _run_personality_checks(
        self, signal: AugurSignal, personality: AugurPersonality
    ) -> List[KantCheck]:
        if personality == AugurPersonality.SENTINEL:
            return [KantCheck(
                name="sentinel_no_new_positions",
                passed=False,
                reason="sentinel mode: protection active, no new positions",
            )]

        if personality == AugurPersonality.ARBITRAGE:
            return [KantCheck(
                name="arb_edge_covers_fees",
                passed=signal.edge > 0.005,
                reason=f"edge {signal.edge:.4f} too small to cover fees (min 0.005)",
            )]

        if personality == AugurPersonality.ORACLE:
            return [KantCheck(
                name="narrative_fresh",
                passed=0 < signal.narrative_age_hours < 4.0,
                reason=f"narrative {signal.narrative_age_hours:.1f}h old (must be 0-4h)",
            )]

        if personality == AugurPersonality.MOMENTUM:
            return [KantCheck(
                name="momentum_confirmed",
                passed=(
                    signal.cascade_zscore > 1.0
                    or abs(signal.price_momentum_pct) > 0.3
                ),
                reason="momentum signal weak: z<1.0 and price change <0.3%",
            )]

        return []
