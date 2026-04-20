import structlog
from intelligence.kant_evaluator import KantEvaluator
from intelligence.nietzsche_kelly import NietzscheanEvaluator
from execution.prediction_market_client import PredictionMarketExecutionClient

logger = structlog.get_logger()

# Philosophical implementation of the Prediction Agent
class PolymarketAgent:
    """
    AUGUR Prediction Agent: A synthesis of Kantian Reason and Nietzschean Will.
    Evaluates external markets (Polymarket/Drift) for information edge.
    """
    
    def __init__(self, mode: str = "paper"):
        self.kant = KantEvaluator()
        self.nietzsche = NietzscheanEvaluator()
        self.executor = PredictionMarketExecutionClient(mode=mode)
        
    async def evaluate_market(self, market_id: str, topic: str, augur_probability: float, news_coherence: float) -> dict:
        """
        Evaluate a prediction market for mispricing using philosophical gating.
        """
        # 1. Fetch market truth
        market_data = await self.executor.get_market_odds(market_id)
        market_prob = market_data["probability"]
        odds = market_data["odds_decimal"]
        
        # 2. Kantian Categorical Imperative (Data Validation)
        signal = {
            "source": "augur_intelligence",
            "coherence": news_coherence,
            "probability": augur_probability,
            "topic": topic
        }
        kantian_res = self.kant.imperative.validate(signal)
        
        if not kantian_res[0]:
            logger.warning("kantian_rejection", market=topic, reason=kantian_res[1])
            return {"action": "pass", "reason": f"Kantian rejection: {kantian_res[1]}"}

        # 3. Calculate Edge (Mispricing)
        edge = augur_probability - market_prob
        
        # 4. Nietzschean Will to Power (Sizing)
        # We only act if there is an edge that justifies the Will.
        if abs(edge) < 0.10:
            return {"action": "pass", "reason": f"Insufficient edge ({edge:.2f}). No mispricing to exploit."}

        # Determine direction: Yes if we are more bullish than market, No if more bearish.
        direction = "YES" if augur_probability > market_prob else "NO"
        
        # If we are betting NO, the odds and probability must be adjusted for the math
        effective_p = augur_probability if direction == "YES" else (1.0 - augur_probability)
        effective_market_p = market_prob if direction == "YES" else (1.0 - market_prob)
        effective_odds = 1.0 / effective_market_p
        
        sizing = self.nietzsche.will.compute_size(effective_p, effective_odds)
        
        if sizing["size"] <= 0:
            return {"action": "pass", "reason": f"Nietzschean hesitation: {sizing['reason']}"}

        return {
            "action": "bet",
            "market_id": market_id,
            "topic": topic,
            "direction": direction,
            "augur_prob": augur_probability,
            "market_prob": market_prob,
            "edge": edge,
            "bet_size_pct": sizing["size"],
            "is_overman_move": sizing["is_overman_move"]
        }
