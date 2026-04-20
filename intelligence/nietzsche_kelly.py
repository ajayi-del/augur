import math
import structlog

logger = structlog.get_logger()

class WillToPower:
    """
    Nietzsche's Will to Power applied to position sizing.
    Uses the Kelly Criterion as the base 'Will', 
    tempered by the 'Threshold of the Abyss'.
    """
    
    def __init__(self, risk_aversion: float = 0.5, max_bankroll_pct: float = 0.10):
        self.risk_aversion = risk_aversion  # Fractional Kelly
        self.max_bankroll_pct = max_bankroll_pct

    def compute_size(self, p: float, odds_decimal: float) -> dict:
        """
        Calculate the Kelly size (f*).
        f* = (p * (b + 1) - 1) / b
        where:
        p = AUGUR Assessment Probability (0-1)
        b = Net Odds (Odds Decimal - 1)
        """
        b = odds_decimal - 1.0
        
        if b <= 0:
            return {"size": 0.0, "reason": "No reward for the risk (Odds <= 1)."}

        # Raw Kelly
        f_star = (p * (b + 1) - 1) / b
        
        if f_star <= 0:
            return {"size": 0.0, "reason": f"Expected value is negative. Edge {f_star:.4f} < 0."}

        # Fractional Kelly (The 'Tempered Will')
        size = f_star * self.risk_aversion
        
        # Threshold of the Abyss (Maximum Drawdown cap)
        final_size = min(size, self.max_bankroll_pct)
        
        logger.info("will_to_power_sizing", 
                    prob=p, 
                    odds=odds_decimal, 
                    raw_kelly=f_star, 
                    tempered_size=final_size)
                    
        return {
            "size": final_size,
            "raw_kelly": f_star,
            "is_overman_move": final_size >= (self.max_bankroll_pct * 0.8),
            "reason": "The Will asserts itself."
        }

class NietzscheanEvaluator:
    def __init__(self, risk_aversion: float = 0.5):
        self.will = WillToPower(risk_aversion=risk_aversion)

    def assess_conviction(self, edge: float, coherence: float) -> float:
        """
        Calculates a conviction multiplier (0.0 - 2.0).
        High edge + High coherence = The Overman.
        """
        base = edge * 2.0  # 0.10 edge -> 0.20
        multiplier = base * (coherence / 0.7)
        return min(multiplier, 2.0)
