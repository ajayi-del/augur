import asyncio
import json
import os
import time
import structlog
from pathlib import Path
from filelock import FileLock
from dataclasses import dataclass, asdict, field
from typing import Callable, List, Dict, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

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
    active_polymarket_bets: List[Dict] = field(default_factory=list)
    etf_flow_direction: str = "neutral"
    active_news_events: List[Dict] = field(default_factory=list)
    solana_health_score: float = 1.0


@dataclass
class KingdomState:
    aria: AriaState = field(default_factory=AriaState)
    augur: AugurState = field(default_factory=AugurState)
    finance: Dict = field(default_factory=dict)
    # Shared position registry: {"aria": [...], "augur": [...]}
    # Each entry: {symbol, direction, size_usd, venue, opened_ms}
    position_registry: Dict = field(default_factory=dict)
    version: str = "2.0"


class _KingdomFileWatcher:
    """
    Watchdog event handler for kingdom_state.json.
    Notifies asyncio when ARIA writes a new signal — sub-100ms latency.
    Falls back to polling (120s) if watchdog is unavailable.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, callback: Callable) -> None:
        self._loop     = loop
        self._callback = callback
        self._last_ms  = 0.0
        self._debounce = 0.050  # 50ms debounce — prevent duplicate fires on atomic writes

    def on_modified(self, event) -> None:
        if not str(event.src_path).endswith("kingdom_state.json"):
            return
        now = time.time()
        if now - self._last_ms < self._debounce:
            return
        self._last_ms = now
        self._loop.call_soon_threadsafe(self._callback)


if _WATCHDOG_AVAILABLE:
    class _WatchdogHandler(_KingdomFileWatcher, FileSystemEventHandler):
        pass


class KingdomStateSync:
    """
    KINGDOM SYNC — The Sovereign Wire
    Coordinates intelligence between ARIA and AUGUR via kingdom_state.json.
    v2.2: Atomic, thread-safe, watchdog event-driven (sub-100ms latency).
    """

    def __init__(self, state_path: Optional[str] = None):
        path = state_path or _DEFAULT_KINGDOM_PATH
        self.state_path = Path(path).expanduser()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.state_path.with_suffix(".lock")
        self.lock = FileLock(str(self.lock_path), timeout=5)

    # ── Watchdog ─────────────────────────────────────────────────────────────

    def start_watcher(
        self,
        callback: Callable,
        loop: asyncio.AbstractEventLoop,
    ) -> Optional[object]:
        """
        Start a filesystem watcher on kingdom_state.json.
        When ARIA writes, callback fires on the event loop within ~50ms.

        Returns the Observer so the caller can stop it on shutdown.
        Returns None if watchdog is not installed (falls back to polling).
        """
        if not _WATCHDOG_AVAILABLE:
            logger.warning(
                "kingdom_watcher_unavailable",
                reason="watchdog not installed — falling back to 120s polling",
                install="pip install watchdog",
            )
            return None

        handler  = _WatchdogHandler(loop=loop, callback=callback)
        observer = Observer()
        observer.schedule(handler, path=str(self.state_path.parent), recursive=False)
        observer.start()
        logger.info(
            "kingdom_watcher_started",
            path=str(self.state_path),
            debounce_ms=50,
            latency_target_ms=100,
        )
        return observer

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
                    position_registry=data.get("position_registry", {}),
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
        bet_fields = AgentBet.__dataclass_fields__.keys()
        return [
            AgentBet(**{k: v for k, v in b.items() if k in bet_fields})
            for b in aria.active_bets
            if b.get("symbol") == symbol and b.get("expires_ms", 0) > now_ms
        ]

    def read_finance(self) -> Dict:
        return self.read().finance

    # ── Write ───────────────────────────────────────────────────────────────

    def write_position(
        self,
        agent_id: str,
        symbol: str,
        direction: str,
        size_usd: float,
        venue: str,
    ) -> None:
        """Register an open position in the shared registry."""
        try:
            with self.lock:
                current: Dict = {}
                if self.state_path.exists() and self.state_path.stat().st_size > 0:
                    try:
                        with open(self.state_path, "r") as f:
                            current = json.load(f)
                    except json.JSONDecodeError:
                        current = {}

                registry: Dict = current.get("position_registry", {})
                positions: list = registry.get(agent_id, [])

                # Evict entries older than 4 hours
                now_ms = int(time.time() * 1000)
                positions = [p for p in positions if now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000]

                positions.append({
                    "symbol":    symbol,
                    "direction": direction,
                    "size_usd":  size_usd,
                    "venue":     venue,
                    "opened_ms": now_ms,
                })
                registry[agent_id] = positions
                current["position_registry"] = registry
                current["version"] = "2.0"

                tmp = self.state_path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(current, f, indent=2)
                tmp.replace(self.state_path)
                logger.debug("position_registered", agent=agent_id, symbol=symbol)
        except Exception as e:
            logger.error("write_position_error", error=str(e))

    def count_open_positions(self, agent_id: str) -> int:
        """Returns the number of tracked open positions for an agent."""
        try:
            state = self.read()
            registry = state.position_registry
            positions = registry.get(agent_id, [])
            now_ms = int(time.time() * 1000)
            return sum(1 for p in positions if now_ms - p.get("opened_ms", 0) < 4 * 3600 * 1000)
        except Exception:
            return 0

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

    # ── Augur data bus ──────────────────────────────────────────────────────

    def publish_augur_data(self, key: str, data: dict) -> None:
        """
        Write arbitrary AUGUR intelligence to kingdom state.
        ARIA reads this to confirm or deny its own signals.

        Stored under kingdom["augur_data"][key].
        Both agents can read; only AUGUR writes.
        """
        try:
            with self.lock:
                current: Dict = {}
                if self.state_path.exists() and self.state_path.stat().st_size > 0:
                    try:
                        with open(self.state_path, "r") as f:
                            current = json.load(f)
                    except json.JSONDecodeError:
                        current = {}

                augur_data = current.get("augur_data", {})
                augur_data[key] = data
                current["augur_data"] = augur_data
                current["version"]    = "2.0"

                tmp = self.state_path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(current, f, indent=2)
                tmp.replace(self.state_path)
                logger.debug("augur_data_published", key=key)
        except Exception as e:
            logger.error("publish_augur_data_error", key=key, error=str(e))

    def get_augur_data(self, key: str, default=None) -> Optional[Dict]:
        """Read AUGUR intelligence by key. Used by both agents."""
        try:
            with self.lock:
                if not self.state_path.exists():
                    return default
                with open(self.state_path, "r") as f:
                    data = json.load(f)
                return data.get("augur_data", {}).get(key, default)
        except Exception:
            return default

    def get_aria_cascade(self, symbol: str) -> Optional[Dict]:
        """
        Read ARIA's live cascade alert for a symbol.
        Used by BybitCascadeEngine to detect if ARIA already sees the cascade.
        """
        try:
            state = self.read()
            alert = state.aria.cascade_alert
            if not alert.get("active"):
                return None
            # ARIA cascade_alert is global (not per-symbol currently)
            # Return with direction inferred from the alert phase
            phase = alert.get("phase", "none")
            direction = (
                "bearish" if "sell" in phase.lower() else
                "bullish" if "buy" in phase.lower() else
                None
            )
            return {
                "active":    alert.get("active", False),
                "zscore":    alert.get("zscore", 0.0),
                "phase":     phase,
                "direction": direction,
                "symbol":    symbol,
            }
        except Exception:
            return None

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
