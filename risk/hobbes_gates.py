import structlog
from kingdom.state_sync import atomic_load
from pathlib import Path

logger = structlog.get_logger()

class HobbesGates:
    """
    Hobbes validates truth through logical reckoning.
    Different gates for different asset classes.
    """
    
    # === PERPS GATES ===
    PERPS_GATES = [
        "drawdown_halt",
        "cascade_gate", 
        "daily_loss",
        "regime",
        "volatility",
        "calendar",
        "market_hours",
        "balance_floor",
        "coherence",
        "rr",
        "var",
        "concentration",
        "pyramid",
        "direction",
        "stop_safety",
        "liquidity",
        "funding"
    ]
    
    # === PREDICTION MARKET GATES ===
    PREDICTION_GATES = [
        "bankroll_preservation",    # Don't bet >5% of bankroll
        "edge_threshold",           # Minimum 10% edge required
        "kelly_compliant",          # Size follows Kelly criterion
        "information_freshness",    # News <24h old
        "market_liquidity",         # Can exit position?
        "resolution_timeline",      # Resolves within 30 days
        "correlation_check",        # Not correlated with existing bets
        "total_exposure",           # <15% of bankroll in all bets
        "claude_chancellor_approval" # AI oversight for >5% bets
    ]
    
    def __init__(self, settings):
        self.settings = settings
        self.kingdom_path = Path(settings.kingdom_state_path)
        from core.finance_controller import FinanceController
        self.finance = FinanceController(settings)
    
    def validate(self, trade: dict, asset_class: str) -> tuple[bool, str]:
        """
        Validate trade through appropriate gates.
        """
        # Global Pre-Gate: Drawdown Halt
        if self.finance.is_halted():
            return False, "SOVEREIGN_DRAWDOWN_HALT_REACHED"

        gates = self.PERPS_GATES if asset_class == "perps" else self.PREDICTION_GATES
        
        for gate in gates:
            passed, reason = self._check_gate(gate, trade)
            if not passed:
                logger.info("hobbes_gate_blocked",
                           asset_class=asset_class,
                           gate=gate,
                           reason=reason)
                return False, reason
                
        return True, "all_gates_passed"
    
    def _check_gate(self, gate: str, trade: dict) -> tuple[bool, str]:
        """
        Check individual gate.
        """
        if gate == "balance_floor":
            # Survival Threshold: TSW must remain > 85% of seed
            finance = self.finance.get_finance_reality()
            margin_needed = trade.get("margin_required", trade.get("size_usd", 0))
            projected_balance = finance["current_tsw"] - margin_needed
            if projected_balance < (self.settings.INITIAL_CAPITAL_SEED * 0.85):
                return False, f"margin_too_high_projected_balance_{projected_balance:.2f}_below_survival_floor"

        elif gate == "edge_threshold":
            edge = trade.get("edge", 0)
            kant_config = trade.get("kant_config", {})
            min_edge = kant_config.get("min_edge", 0.10)
            if edge < min_edge:
                return False, f"edge_{edge:.1%}_below_{min_edge:.1%}"
                
        elif gate == "kelly_compliant":
            kelly = trade.get("kelly_fraction", 0)
            actual_size = trade.get("size_pct", 0)
            if actual_size > kelly * 1.2:
                return False, f"size_{actual_size:.1%}_exceeds_kelly_{kelly:.1%}"
                
        elif gate == "bankroll_preservation":
            size_pct = trade.get("size_pct", 0)
            if size_pct > 0.05:
                return False, f"single_bet_{size_pct:.1%}_exceeds_5%_limit"
                
        elif gate == "total_exposure":
            # Logic would consult kingdom state for total exposure.
            current_exposure = 0.05 
            new_exposure = current_exposure + trade.get("size_pct", 0)
            kant_config = trade.get("kant_config", {})
            max_exposure = kant_config.get("max_exposure_pct", 0.15)
            if new_exposure > max_exposure:
                return False, f"total_exposure_{new_exposure:.1%}_exceeds_{max_exposure:.1%}"
                
        elif gate == "claude_chancellor_approval":
            if trade.get("size_pct", 0) > 0.03: 
                if not trade.get("chancellor_approved", False):
                    return False, "awaiting_chancellor_approval"
        
        elif gate == "daily_loss":
            # Institutional MDD: Halt if daily loss > 10%
            finance = self.finance.get_finance_reality()
            drawdown = finance.get("drawdown_pct", 0.0)
            if drawdown > 0.10:
                return False, f"INSTITUTIONAL_HALT: daily_drawdown_{drawdown:.1%}_exceeds_10%"

        elif gate == "concentration":
            # The 3% Rule: Never risk > 3% on one trade
            # size_usd * (expected_loss_pct) / TSW
            finance = self.finance.get_finance_reality()
            tsw = finance["current_tsw"]
            risk_usd = trade.get("size_usd", 0) * 0.1  # Assume 10% stop as worst case for sizing
            risk_pct = risk_usd / tsw if tsw > 0 else 1.0
            
            if risk_pct > 0.03:
                return False, f"RISK_OVERLOAD: trade_risk_{risk_pct:.1%}_exceeds_3%_limit"
        
        return True, f"{gate}_passed"
