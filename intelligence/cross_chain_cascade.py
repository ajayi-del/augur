import time
from pathlib import Path
from typing import Optional
import structlog
from datetime import datetime
import asyncio

from kingdom.state_sync import atomic_load, atomic_save

logger = structlog.get_logger()

class CrossChainCascadeDetector:
    """
    Detects Solana liquidations and alerts ARIA via kingdom_state.json.
    Also reads ARIA's ValueChain cascades for early warning.
    """
    
    def __init__(self, kingdom_state_path: str = "/shared/kingdom_state.json"):
        self.kingdom_path = Path(kingdom_state_path)
        self.solana_zscore = 0.0
        self.valuechain_zscore = 0.0
        self.last_solana_cascade = 0
        self.last_valuechain_cascade = 0
        self.baseline = {"mean": 1000, "std": 300}  # Placeholder for baseline metrics
        
    async def monitor_solana_liquidations(self):
        """
        Monitor Kamino, MarginFi, Jupiter Lend for liquidations.
        Calculate z-score based on liquidation volume vs baseline.
        """
        # Query Solana lending protocols
        kamino_liqs = await self.get_kamino_liquidations()
        marginfi_liqs = await self.get_marginfi_liquidations()
        jupiter_liqs = await self.get_jupiter_liquidations()
        
        total_liqs = kamino_liqs + marginfi_liqs + jupiter_liqs
        
        # Calculate z-score against 30-day baseline
        baseline_mean = self.baseline.get("mean", 0)
        baseline_std = self.baseline.get("std", 1)
        
        if baseline_std == 0:
            baseline_std = 1
            
        zscore = (total_liqs - baseline_mean) / baseline_std
        
        self.solana_zscore = zscore
        
        # CASCADE DETECTED
        if zscore > 2.0:
            await self._write_cross_chain_signal(
                source="solana",
                zscore=zscore,
                total_liqs=total_liqs,
                timestamp=time.time()
            )
            
            # Kant structure shift
            return {
                "structure": "cascade_warning",
                "direction": "bearish",  # Liquidations = forced selling
                "zscore": zscore,
                "confidence": min(zscore / 5.0, 1.0)
            }
            
        return None
        
    async def get_kamino_liquidations(self) -> int:
        # Placeholder integration
        return 0
        
    async def get_marginfi_liquidations(self) -> int:
        # Placeholder integration
        return 0
        
    async def get_jupiter_liquidations(self) -> int:
        # Placeholder integration
        return 0

    async def read_aria_warning(self) -> Optional[dict]:
        """
        Read kingdom_state.json for ARIA's ValueChain cascade warnings.
        Returns early warning signal if ValueChain cascade detected.
        """
        if not self.kingdom_path.exists():
            return None
            
        state = atomic_load(self.kingdom_path, max_age_s=300)
        
        if state and state.get("valuechain", {}).get("zscore", 0) > 2.0:
            vc_state = state["valuechain"]
            age = time.time() - vc_state.get("timestamp", 0)
            
            # Signal decays with age
            if age < 300:  # 5 minutes
                decay = 1.0 - (age / 600)  # Decay over 10 minutes
                
                return {
                    "structure": "cascade_warning",
                    "source": "valuechain",
                    "zscore": vc_state["zscore"] * decay,
                    "direction": vc_state.get("direction", "bearish"),
                    "age_seconds": age,
                    "confidence": 0.8 * decay
                }
                
        return None
        
    async def _write_cross_chain_signal(self, source: str, zscore: float, 
                                        total_liqs: int, timestamp: float):
        """Write cascade detection to shared state"""
        
        solana_state = {
            "zscore": zscore if source == "solana" else self.solana_zscore,
            "total_liquidations": total_liqs,
            "timestamp": timestamp,
        }
        
        signal = {
            "source": source,
            "target": "valuechain" if source == "solana" else "solana",
            "confidence": min(zscore / 5.0, 1.0),
            "timestamp": timestamp
        }
        
        state_updates = {
            "solana": solana_state,
        }
        
        # Pull existing to amend list safely or atomic_save can do it with a dict wrap
        current_state = atomic_load(self.kingdom_path)
        signals = current_state.get("cross_chain_signals", [])
        signals.append(signal)
        # Keep only last 10
        signals = signals[-10:]
        
        state_updates["cross_chain_signals"] = signals
        
        atomic_save(self.kingdom_path, state_updates)
        logger.info("cross_chain_cascade_detected", source=source, zscore=zscore)
