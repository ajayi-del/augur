import time
import structlog
from typing import List, Dict, Optional, Literal, Any
from pydantic import BaseModel
from memory.trade_journal import TradeJournal

logger = structlog.get_logger()

# Constants
EVIDENCE_INDEPENDENCE = {
    ("microstructure", "narrative"): 1.00,
    ("narrative", "microstructure"): 1.00,
    ("microstructure", "microstructure"): 0.35,
    ("narrative", "narrative"): 0.40,
}

ARIA_BET_EXPIRY_MS  = 30 * 60 * 1000
AUGUR_BET_EXPIRY_MS = 6 * 60 * 60 * 1000
MIN_MARKET_SCORE_TO_BONUS = 4.0

class AgentBet(BaseModel):
    agent_id: str
    symbol: str
    direction: str
    confidence: float
    evidence_type: str
    coherence: float
    timestamp_ms: int
    expires_ms: int

class MarketResolution(BaseModel):
    symbol: str
    market_direction: str
    market_confidence: float
    agreement_type: str
    aria_bet: Optional[AgentBet] = None
    augur_bet: Optional[AgentBet] = None
    resolution_score: float
    size_multiplier: float
    recommended_action: str
    independence_factor: float
    reasoning_tree: Dict[str, Any] = {}

class CrossAgentBetEngine:
    """
    Cross-Agent Prediction Market Engine.
    Computes deterministic consensus between ARIA and AUGUR bets.
    v2.1: Integrated Self-Learning Calibration with TradeJournal.
    """
    def __init__(self, journal: Optional[TradeJournal] = None):
        # Keyed by symbol then agent_id
        self._bets: Dict[str, Dict[str, AgentBet]] = {}
        self.journal = journal
        self._agreement_bonus_mult: float = 1.0
        
    def place_bet(self, bet: AgentBet) -> None:
        """Place a bet from an agent into the internal state with validation."""
        self._evict_expired()
        
        if bet.symbol not in self._bets:
            self._bets[bet.symbol] = {}
        
        self._bets[bet.symbol][bet.agent_id] = bet
        logger.info("bet_placed", agent=bet.agent_id, symbol=bet.symbol, direction=bet.direction, confidence=bet.confidence)

    def calibrate_all(self) -> None:
        """
        Calibrate agreement bonus for ALL symbols based on historical performance.
        Self-Learning Loop: The Consensus is calibrated by Reality.
        """
        if not self.journal:
            return

        symbols = set([e.get("symbol") for e in self.journal.get_closed()])
        if not symbols:
            return

        total_bonus_adj = 0.0
        for symbol in symbols:
            # Analyze performance for consensus trades on this symbol
            analysis = self.journal.get_historical_analysis("all", symbol)
            
            # If win rate is high (>60%), increase the agreement bonus for the system
            if analysis["win_rate"] > 0.60 and analysis["sample_size"] >= 5:
                # Strong empirical evidence of consensus alpha
                total_bonus_adj += 0.05
            elif analysis["win_rate"] < 0.40 and analysis["sample_size"] >= 5:
                # Evidence that consensus is failing on this symbol
                total_bonus_adj -= 0.05

        # Clamp bonus multiplier between 0.5 and 1.5
        self._agreement_bonus_mult = max(0.5, min(1.5, 1.0 + total_bonus_adj))
        logger.info("system_calibrated", 
                    bonus_mult=f"{self._agreement_bonus_mult:.2f}", 
                    symbols_analyzed=len(symbols))
        
    def _evict_expired(self) -> None:
        now_ms = int(time.time() * 1000)
        
        for symbol in list(self._bets.keys()):
            for agent_id in list(self._bets[symbol].keys()):
                bet = self._bets[symbol][agent_id]
                if now_ms >= bet.expires_ms:
                    logger.info("evicting_expired_bet", symbol=symbol, agent=agent_id)
                    del self._bets[symbol][agent_id]
                    
            if not self._bets[symbol]:
                del self._bets[symbol]
                
    def resolve(self, symbol: str, now_ms: Optional[int] = None) -> MarketResolution:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
            
        self._evict_expired()
        
        symbol_bets = self._bets.get(symbol, {})
        aria_bet = symbol_bets.get("aria")
        augur_bet = symbol_bets.get("augur")
        
        # Case: silence
        if aria_bet is None and augur_bet is None:
            return MarketResolution(
                symbol=symbol,
                market_direction="neutral",
                market_confidence=0.0,
                agreement_type="silence",
                aria_bet=None,
                augur_bet=None,
                resolution_score=0.0,
                size_multiplier=0.0,
                recommended_action="wait",
                independence_factor=0.0
            )
            
        # Case: single agent
        if aria_bet is None:
            score = augur_bet.confidence * 5.0
            return self._build_resolution(symbol, augur_bet, None, score, "single_augur")
            
        if augur_bet is None:
            score = aria_bet.confidence * 5.0
            return self._build_resolution(symbol, None, aria_bet, score, "single_aria")
            
        # Case: both have bets
        if aria_bet.direction != augur_bet.direction:
            return MarketResolution(
                symbol=symbol,
                market_direction="contested",
                market_confidence=0.0,
                agreement_type="disagreement",
                aria_bet=aria_bet,
                augur_bet=augur_bet,
                resolution_score=0.0,
                size_multiplier=0.0,
                recommended_action="cancel",
                independence_factor=0.0
            )
            
        # Agreement
        indep = EVIDENCE_INDEPENDENCE.get(
            (aria_bet.evidence_type, augur_bet.evidence_type),
            0.5
        )
        
        base = (aria_bet.confidence * 5.0 + augur_bet.confidence * 5.0) / 2.0
        
        # Apply the calibrated agreement bonus
        if base >= MIN_MARKET_SCORE_TO_BONUS:
            bonus = min(aria_bet.confidence, augur_bet.confidence) * indep * 2.0 * self._agreement_bonus_mult
        else:
            bonus = 0.0
            
        score = min(base + bonus, 10.0)
        agreement_type = "strong_agreement" if score >= 7.0 else "weak_agreement"
        
        tree = {
            "root": "prediction_market_consensus",
            "sub_claims": [
                {
                    "source": "aria",
                    "direction": aria_bet.direction,
                    "confidence": aria_bet.confidence,
                    "evidence": aria_bet.evidence_type
                },
                {
                    "source": "augur",
                    "direction": augur_bet.direction,
                    "confidence": augur_bet.confidence,
                    "evidence": augur_bet.evidence_type
                }
            ],
            "logic": {
                "base_score": base,
                "independence_factor": indep,
                "calibration_bonus": bonus,
                "final_score": score,
                "agreement": agreement_type
            }
        }
        
        return self._build_resolution(symbol, augur_bet, aria_bet, score, agreement_type, indep, tree)

    def _build_resolution(self, symbol, augur_bet, aria_bet, score, agg_type, indep=0.0, tree=None):
        direction = augur_bet.direction if augur_bet else aria_bet.direction
        if not tree:
            tree = {
                "source": "single_agent",
                "direction": direction,
                "score": score,
                "type": agg_type
            }
            
        return MarketResolution(
            symbol=symbol,
            market_direction=direction,
            market_confidence=score / 10.0,
            agreement_type=agg_type,
            aria_bet=aria_bet,
            augur_bet=augur_bet,
            resolution_score=score,
            size_multiplier=self._score_to_mult(score),
            recommended_action="compound" if aria_bet and augur_bet else "proceed",
            independence_factor=indep,
            reasoning_tree=tree
        )

    def _score_to_mult(self, score: float) -> float:
        if score >= 8.0: return 1.5
        if score >= 6.5: return 1.2
        if score >= 5.0: return 1.0
        if score >= 3.5: return 0.7
        return 0.0

    def get_active_bets(self, symbol: str) -> Dict[str, AgentBet]:
        self._evict_expired()
        return self._bets.get(symbol, {}).copy()

    def get_all_resolutions(self) -> dict:
        self._evict_expired()
        resolutions = {}
        for symbol in self._bets.keys():
            resolutions[symbol] = self.resolve(symbol)
        return resolutions
