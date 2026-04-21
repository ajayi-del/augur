"""
HobbesGates — legacy risk gate stub.

Not used by main.py (Chancellor + strategy_runner handle all risk gates).
Kept as a stub to avoid import errors from old test files.
FinanceController removed — AUGUR balance is always Bybit USDT, never SoDEX.
"""
import structlog
from pathlib import Path

logger = structlog.get_logger()


class HobbesGates:
    """Stub — risk validation handled by Chancellor in production."""

    def __init__(self, settings=None):
        self.settings = settings

    def validate(self, trade: dict, asset_class: str) -> tuple:
        return True, "hobbes_stub_passthrough"

    def is_halted(self) -> bool:
        return False
