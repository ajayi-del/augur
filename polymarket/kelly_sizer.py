import structlog
from dataclasses import dataclass
from typing import Optional

logger = structlog.get_logger()


# ── Standalone function (used by checkpoints) ────────────────────────────────

def kelly_bet_size(
    augur_prob,             # AugurProbability dataclass
    market_prob: float,     # Current market YES price
    bankroll_usdc: float,
    fractional_kelly: float = 0.5,
    max_cap_pct: float = 0.05,
    min_bet_usd: float = 2.0,
) -> float:
    """
    Compute Half-Kelly bet size in USDC.
    Returns 0.0 if no edge or below min.
    """
    p = augur_prob.probability
    q = market_prob

    if p <= q:
        return 0.0

    edge = p - q
    denom = 1.0 - q
    if denom <= 0:
        return 0.0

    raw_kelly = edge / denom
    sizing_pct = raw_kelly * fractional_kelly
    sizing_pct = min(sizing_pct, max_cap_pct)

    bet = bankroll_usdc * sizing_pct

    # Confidence discount
    bet *= augur_prob.confidence

    if bet < min_bet_usd:
        return 0.0

    return round(bet, 2)


# ── Class wrapper (used by MarketScanner) ────────────────────────────────────

class KellySizer:
    """KELLY SIZER — sovereign bankroll preservation via Half-Kelly."""

    def __init__(
        self,
        bankroll: float,
        max_cap_pct: float = 0.05,
        fractional_kelly: float = 0.5,
    ):
        self.bankroll = bankroll
        self.max_cap_pct = max_cap_pct
        self.fractional_kelly = fractional_kelly
        self.min_bet_usd = 2.0

    def calculate_bet_size(self, p_augur: float, p_market: float) -> float:
        if p_augur <= p_market:
            return 0.0

        edge = p_augur - p_market
        denom = 1.0 - p_market
        if denom <= 0:
            return 0.0

        raw_kelly = edge / denom
        sizing_pct = min(raw_kelly * self.fractional_kelly, self.max_cap_pct)

        bet = self.bankroll * sizing_pct
        if bet < self.min_bet_usd:
            return 0.0

        return round(bet, 2)

    def update_bankroll(self, new_balance: float) -> None:
        self.bankroll = new_balance
        logger.info("bankroll_updated", balance=new_balance)
