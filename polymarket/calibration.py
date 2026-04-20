import structlog
from typing import List

logger = structlog.get_logger()

class CalibrationEngine:
    """
    Self-calibration after every 20 resolved bets.
    Aligns internal perceived edge with empirical win rates.
    """

    def __init__(self, initial_min_edge: float = 0.08):
        self.min_edge = initial_min_edge
        self.resolved_bets = 0
        self.performance_history = []

    def calibrate(self, resolved_bets: List[dict]) -> float:
        """
        Adjust MIN_EDGE based on prediction calibration error.
        """
        if len(resolved_bets) < 20:
            return self.min_edge

        # actual_win_rate = count(won) / count(resolved)
        # predicted_win_rate = mean(augur_probability at entry)
        
        wins = sum(1 for b in resolved_bets if b.get("won"))
        total = len(resolved_bets)
        actual_win_rate = wins / total
        
        avg_predicted = sum(b.get("p_augur", 0.5) for b in resolved_bets) / total
        
        error = avg_predicted - actual_win_rate
        
        old_edge = self.min_edge
        
        # Calibration logic
        if error > 0.10:
            # Over-confident: Raise the bar
            self.min_edge += 0.02
        elif error < -0.10:
            # Under-confident: Lower the bar slightly
            self.min_edge -= 0.01
            
        # Clamp MIN_EDGE to [0.05, 0.25]
        self.min_edge = max(0.05, min(0.25, self.min_edge))
        
        if self.min_edge != old_edge:
            logger.warning("calibration_applied", 
                           old_edge=old_edge, 
                           new_edge=self.min_edge,
                           actual_win_rate=actual_win_rate,
                           predicted_win_rate=avg_predicted)
        
        return self.min_edge

    def update_signal_weights(self, weights: dict, performance: dict) -> dict:
        """
        Recommended improvement: Update signal weights based on 
        which individual signals were most accurate.
        (Placeholder for future dynamic weighting logic)
        """
        return weights
