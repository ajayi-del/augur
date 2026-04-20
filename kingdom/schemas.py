from pydantic import BaseModel, Field, validator
from typing import List, Dict, Optional, Literal
from datetime import datetime

class BetSchema(BaseModel):
    agent_id: str
    symbol: str
    direction: Literal["long", "short", "neutral"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_type: str
    coherence: float
    timestamp_ms: int
    expires_ms: int

class AgentStateSchema(BaseModel):
    active_bets: List[BetSchema] = []
    daily_stats: Dict[str, float] = {}
    last_signal_ms: Optional[int] = None

class CrossChainSignalSchema(BaseModel):
    source: str
    target: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    timestamp: float

class FinanceStateSchema(BaseModel):
    peak_equity: float = 150.0
    current_tsw: float = 150.0
    drawdown_pct: float = 0.0
    venue_balances: Dict[str, float] = {}

class KingdomStateSchema(BaseModel):
    version: int = 1
    last_updated: str
    aria: AgentStateSchema = AgentStateSchema()
    augur: AgentStateSchema = AgentStateSchema()
    finance: FinanceStateSchema = FinanceStateSchema()
    valuechain: Dict[str, float] = {}
    solana: Dict[str, float] = {}
    cross_chain_signals: List[CrossChainSignalSchema] = []
    
    @validator("last_updated")
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v)
            return v
        except ValueError:
            raise ValueError("Invalid ISO timestamp")
