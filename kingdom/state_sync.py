import json
import os
import time
import structlog
from pathlib import Path
from filelock import FileLock
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional

logger = structlog.get_logger()

_DEFAULT_KINGDOM_PATH = os.environ.get(
    "KINGDOM_STATE_PATH",
    os.path.expanduser("~/kingdom/kingdom_state.json")
)


@dataclass
class AgentBet:
    agent_id: str
    symbol: str
    direction: str          # long/short/neutral
    confidence: float       # 0.0-1.0
    evidence_type: str      # microstructure/narrative
    coherence: float
    timestamp_ms: int
    expires_ms: int


@dataclass
class AriaState:
    active_bets: List[Dict] = field(default_factory=list)
    open_positions: List[Dict] = field(default_factory=list)
    cascade_alert: Dict = field(default_factory=lambda: {
        "active": False, "phase": "none", "impact_vols": 0.0
    })
    regime: str = "unknown"
    daily_pnl: float = 0.0
    drawdown: float = 0.0


@dataclass
class AugurState:
    active_bets: List[Dict] = field(default_factory=list)
    open_positions: List[Dict] = field(default_factory=list)
    active_polymarket_bets: List[Dict] = field(default_factory=list)
    etf_flow_direction: str = "neutral"
    active_news_events: List[Dict] = field(default_factory=list)
    solana_health_score: float = 1.0


@dataclass
class KingdomState:
    aria: AriaState = field(default_factory=AriaState)
    augur: AugurState = field(default_factory=AugurState)
    finance: Dict = field(default_factory=dict)
    version: str = "2.0"


class KingdomStateSync:
    """
    KINGDOM SYNC — The Sovereign Wire
    Coordinates intelligence between ARIA and AUGUR via kingdom_state.json.
    v2.1: Atomic, thread-safe, with default path from env/config.
    """

    def __init__(self, state_path: Optional[str] = None):
        path = state_path or _DEFAULT_KINGDOM_PATH
        self.state_path = Path(path).expanduser()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.state_path.with_suffix(".lock")
        self.lock = FileLock(str(self.lock_path), timeout=5)

    # ── Read ────────────────────────────────────────────────────────────────

    def read(self) -> KingdomState:
        """Loads and parses the full KingdomState. Never crashes."""
        try:
            with self.lock:
                if not self.state_path.exists() or self.state_path.stat().st_size == 0:
                    return KingdomState()

                with open(self.state_path, "r") as f:
                    data = json.load(f)

                aria_raw = data.get("aria", {})
                augur_raw = data.get("augur", {})

                # Strip unknown fields so dataclass constructors don't blow up
                aria_fields = AriaState.__dataclass_fields__.keys()
                augur_fields = AugurState.__dataclass_fields__.keys()

                return KingdomState(
                    aria=AriaState(**{k: v for k, v in aria_raw.items() if k in aria_fields}),
                    augur=AugurState(**{k: v for k, v in augur_raw.items() if k in augur_fields}),
                    finance=data.get("finance", {}),
                    version=data.get("version", "2.0"),
                )
        except Exception as e:
            logger.error("kingdom_read_error", error=str(e))
            return KingdomState()

    def read_aria_state(self) -> AriaState:
        """Reads ARIA state and filters expired bets."""
        state = self.read()
        aria = state.aria

        now_ms = int(time.time() * 1000)
        valid_bets = [b for b in aria.active_bets if b.get("expires_ms", 0) > now_ms]

        if len(valid_bets) < len(aria.active_bets):
            logger.info("aria_stale_bets_purged",
                        count=len(aria.active_bets) - len(valid_bets))
            aria.active_bets = valid_bets

        return aria

    def get_active_aria_bets(self, symbol: str) -> List[AgentBet]:
        """Returns non-expired ARIA bets for a specific symbol."""
        aria = self.read_aria_state()
        now_ms = int(time.time() * 1000)
        return [
            AgentBet(**b) for b in aria.active_bets
            if b.get("symbol") == symbol and b.get("expires_ms", 0) > now_ms
        ]

    def read_finance(self) -> Dict:
        return self.read().finance

    # ── Write ───────────────────────────────────────────────────────────────

    def write_augur_state(self, augur: AugurState) -> None:
        self._update_section("augur", asdict(augur))

    def write_aria_state(self, aria: AriaState) -> None:
        self._update_section("aria", asdict(aria))

    def write_finance(self, finance: Dict) -> None:
        self._update_section("finance", finance)

    def publish_augur_bet(self, bet: AgentBet) -> None:
        """Adds a bet to AUGUR's active_bets atomically, purging expired ones."""
        try:
            with self.lock:
                state = self.read()
                augur = state.augur

                now_ms = int(time.time() * 1000)
                augur.active_bets = [
                    b for b in augur.active_bets
                    if b.get("expires_ms", 0) > now_ms
                ]
                augur.active_bets.append(asdict(bet))
                self.write_augur_state(augur)
                logger.info("augur_bet_published",
                            symbol=bet.symbol, direction=bet.direction)
        except Exception as e:
            logger.error("publish_augur_bet_error", error=str(e))

    # ── Internal ────────────────────────────────────────────────────────────

    def _update_section(self, key: str, value: Dict) -> None:
        try:
            with self.lock:
                current: Dict = {}
                if self.state_path.exists() and self.state_path.stat().st_size > 0:
                    try:
                        with open(self.state_path, "r") as f:
                            current = json.load(f)
                    except json.JSONDecodeError:
                        current = {}

                current[key] = value
                current["version"] = "2.0"

                tmp = self.state_path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(current, f, indent=2)
                tmp.replace(self.state_path)
                logger.debug("kingdom_section_updated", key=key)
        except Exception as e:
            logger.error(f"kingdom_write_{key}_error", error=str(e))
