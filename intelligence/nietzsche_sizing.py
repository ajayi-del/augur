import structlog

logger = structlog.get_logger()

class NietzscheSizing:
    """
    Nietzsche expresses will through force (position sizing).
    Different force calculations for different asset classes.
    """
    
    def calculate_size(self, trade: dict, asset_class: str) -> dict:
        """
        Calculate position size based on asset class.
        """
        if asset_class == "perps":
            return self._size_perps(trade)
        elif asset_class == "prediction":
            return self._size_prediction(trade)
        else:
            return {"size_pct": 0, "reason": "unknown_asset_class"}
    
    def _size_prediction(self, trade: dict) -> dict:
        """
        Kelly criterion for prediction markets.
        f* = (p * b - q) / b
        """
        p = trade["augur_probability"]
        market_prob = trade["market_probability"]
        b = (1.0 / market_prob) - 1
        q = 1.0 - p
        
        # Full Kelly
        if b <= 0:
            return {"size_pct": 0, "reason": "no_positive_odds"}
            
        kelly_full = (p * b - q) / b
        
        # Cap Kelly
        kelly_cap = trade.get("kant_config", {}).get("kelly_cap", 0.05)
        kelly = min(kelly_full, kelly_cap)
        
        # Edge calculation
        edge = p - market_prob
        
        # Will state multiplier (Placeholder for dynamic state)
        will_mult = 1.0 
        
        # Final size
        final_size = kelly * will_mult
        
        # Ensure positive edge
        if edge <= 0:
            final_size = 0
            
        return {
            "size_pct": max(0, final_size),
            "kelly_full": kelly_full,
            "kelly_capped": kelly,
            "will_multiplier": will_mult,
            "edge": edge,
            "reason": f"kelly_{kelly:.2%}_will_{will_mult:.2f}_edge_{edge:.1%}"
        }
    
    def _size_perps(self, trade: dict) -> dict:
        """
        /// @notice Expressions of force must be calibrated to Risk:Reward.
        /// @dev Target Win Rate 40-60% paired with 1:3 RR ratio.
        """
        base_risk_pct = 0.03  # The 3% Risk Rule
        conviction_mult = trade.get("conviction", 0.5)
        
        # RR Target Calibration: 1:3
        # If stop is 2% away, target must be 6%.
        stop_dist = trade.get("stop_dist_pct", 0.02)
        target_dist = stop_dist * 3.0 # Institutional 1:3 target
        
        final_leverage = min(5, trade.get("max_leverage", 3)) # Nietzsche: Rarely exceed 5x
        
        # Exposure Check: The 3-5-7 Rule
        # Verify total exposure < 5% for safety
        total_open_exposure = trade.get("total_exposure_pct", 0.02)
        if total_open_exposure + base_risk_pct > 0.05:
            base_risk_pct *= 0.5 # Scale down force to preserve the Sovereign
        
        final_size = base_risk_pct * conviction_mult
        
        # Calculate Notional vs Margin
        # size_usd here is the budget allocated by FinanceController (already dynamic)
        notional_usd = trade.get("size_usd", 0) * final_leverage
        margin_required = trade.get("size_usd", 0)
        
        return {
            "size_pct": final_size,
            "risk_rule": "3% of Dynamic TSW",
            "rr_ratio": "1:3",
            "target_profit_pct": target_dist,
            "final_leverage": final_leverage,
            "notional_usd": notional_usd,
            "margin_required": margin_required,
            "reason": f"dynamic_risk_{base_risk_pct:.1%}_leverage_{final_leverage}x_notional_${notional_usd:.2f}"
        }
