import time
import structlog
from datetime import datetime
from pathlib import Path
from kingdom.state_sync import KingdomStateSync

logger = structlog.get_logger()

class FinanceController:
    """
    The Sovereign Exchequer: Manages Total Sovereign Wealth (TSW).
    Enforces Drawdown limits and Autonomous Budgeting.
    v2.2 Hardened: IO-Efficient Caching and Reality Synchronization.
    """
    
    def __init__(self, settings):
        self.settings = settings
        self.state_path = Path(settings.kingdom_state_path)
        self.max_drawdown = settings.MAX_DRAWDOWN_PCT if hasattr(settings, 'MAX_DRAWDOWN_PCT') else 0.15
        
        # State Sync
        self.sync = KingdomStateSync(settings.kingdom_state_path)
        
        # IO Caching
        self._last_sync_time = 0
        self._cached_finance = None
        self._sync_interval = 15 # Seconds
        
    async def reconcile_with_venues(self, executor) -> float:
        """
        /// @notice Recursive reconciliation of absolute physical wealth.
        /// @dev Polls all active perps venues for live account balances.
        """
        tsw = 0.0
        for venue in executor.perps_router.venues:
            try:
                bal = await venue.get_balance()
                tsw += bal
                logger.debug("venue_balance_detected", venue=venue.name, balance=bal)
            except Exception as e:
                logger.error("venue_balance_fetch_failed", venue=venue.name, error=str(e))
        
        # If no wealth detected, fallback to seed for bootstrap
        if tsw == 0:
            tsw = self.settings.INITIAL_CAPITAL_SEED
            
        return tsw

    def get_finance_reality(self, force_refresh: bool = False) -> dict:
        """
        Synchronize with financial reality across all venues.
        Calculates TSW, Peak Equity, and current Drawdown.
        Uses 15s cache to prevent Disk IO thrashing.
        """
        now = time.time()
        if not force_refresh and self._cached_finance and (now - self._last_sync_time) < self._sync_interval:
            return self._cached_finance

        finance = self.sync.read_finance()
        
        # TSW is now an empirical sum stored in the state, updated by the reconciler
        current_tsw = finance.get("current_tsw", self.settings.INITIAL_CAPITAL_SEED)
            
        peak_equity = max(finance.get("peak_equity", self.settings.INITIAL_CAPITAL_SEED), current_tsw)
        drawdown = 1.0 - (current_tsw / peak_equity) if peak_equity > 0 else 0.0
        
        # Update Finance State
        finance["peak_equity"] = peak_equity
        finance["current_tsw"] = current_tsw
        finance["drawdown_pct"] = drawdown
        finance["last_reconciliation_iso"] = datetime.utcnow().isoformat()
        
        # PERSIST: Synchronize with the One Source of Truth
        self.sync.write_finance(finance)
        
        self._cached_finance = finance
        self._last_sync_time = now
        
        # Tiered Logic Logging
        regime = "GREEN"
        if drawdown >= 0.15: regime = "RED (HALT)"
        elif drawdown >= 0.10: regime = "YELLOW (PANIC)"
        
        logger.info("sovereign_financial_sync", 
                    regime=regime,
                    tsw=f"${current_tsw:.2f}", 
                    drawdown=f"{drawdown:.2%}")
                    
        return finance

    def allocate_budget(self, conviction: float, asset_class: str) -> float:
        """
        Autonomous Budgeting: Reallocates capital based on conviction and asset type.
        v2.4: Adaptive Small-Account Scaling (Ensures Survival Gate compliance).
        """
        finance = self.get_finance_reality()
        tsw = finance["current_tsw"]
        drawdown = finance["drawdown_pct"]
        
        # Ratios: Institutional (70/30) vs Small Account (10/5)
        # For < $500, we must be conservative to stay above the 85% floor.
        if tsw < 500:
            ratios = {"perps": 0.1, "prediction": 0.05}
        else:
            ratios = {"perps": 0.7, "prediction": 0.3}
        
        # DEFENSIVE TIERING
        defensive_mult = 1.0
        if drawdown >= 0.10:
            defensive_mult = 0.5 
            logger.warning("YELLOW_ALERT_RISK_HALVED", drawdown=f"{drawdown:.2%}")
            
        # Scale budget based on conviction (CAPPED AT 1.0) and defense
        conviction_clamped = min(1.0, conviction)
        base_budget = tsw * ratios.get(asset_class, 0.1)
        allocated_size = base_budget * conviction_clamped * defensive_mult
        
        return allocated_size

    def is_halted(self) -> bool:
        """Check if Sovereign Survival Threshold has been breached."""
        finance = self.get_finance_reality()
        return finance["drawdown_pct"] >= self.max_drawdown
