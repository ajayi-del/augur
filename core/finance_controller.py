"""
FinanceController — legacy stub.

AUGUR balance comes from Bybit V5 account only (bybit_client.get_balance()).
This class is not used by main.py. Retained as a stub to prevent import errors.
SoDEX / PHANTOM capital tracking removed — AUGUR executes on Bybit, never SoDEX.
"""
import structlog

logger = structlog.get_logger()


class FinanceController:
    """Stub — AUGUR balance is Bybit USDT only."""

    def __init__(self, settings=None):
        self.settings = settings

    async def reconcile_with_venues(self, executor=None) -> float:
        return 0.0

    def get_finance_reality(self, force_refresh: bool = False) -> dict:
        return {"current_tsw": 0.0, "peak_equity": 0.0, "drawdown_pct": 0.0}

    def allocate_budget(self, conviction: float, asset_class: str) -> float:
        return 0.0

    def is_halted(self) -> bool:
        return False
