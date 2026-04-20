import structlog

logger = structlog.get_logger()

class CategoricalImperative:
    """
    Kant's Categorical Imperative applied to market data.
    'Act only according to that maxim whereby you can at the same time 
     will that it should become a universal law.'
    
    In trading: Only accept information that, if universalized, 
    would maintain the structural integrity of a rational market.
    """
    
    def __init__(self, min_source_coherence: float = 0.7):
        self.min_coherence = min_source_coherence

    def validate(self, signal: dict) -> tuple[bool, str]:
        """
        Validate a signal against the Categorical Imperative.
        Checks for source reliability, data sanity, and structural coherence.
        """
        # 1. Source Reliability
        source = signal.get("source", "unknown")
        coherence = signal.get("coherence", 0.0)
        
        if coherence < self.min_coherence:
            return False, f"Source '{source}' fails the Categorical Imperative. Coherence {coherence} is too low."
            
        # 2. Universal Sanity
        # If the implied probability is outside the bounds of reason (0 or 1), it's a hallucination.
        prob = signal.get("probability", 0.5)
        if prob <= 0.0 or prob >= 1.0:
            return False, "Data is structurally flawed. Probability must be (0, 1)."
            
        # 3. Conflict Check
        # Does this signal conflict with established structural realities?
        if signal.get("conflict_flag", False):
            return False, "Signal contains internal contradictions. Categorical failure."

        return True, "Signal is architecturally sound."

class KantEvaluator:
    def __init__(self):
        self.imperative = CategoricalImperative()

    def evaluate_reality(self, signals: list[dict]) -> dict:
        """
        Evaluate a list of signals to find the 'Universal Maxim' (The strongest valid signal).
        """
        valid_signals = []
        for s in signals:
            is_valid, reason = self.imperative.validate(s)
            if is_valid:
                valid_signals.append(s)
            else:
                logger.warning("kantian_rejection", source=s.get("source"), reason=reason)

        if not valid_signals:
            return {"status": "void", "reason": "No signals survived the Categorical Imperative."}

        # Select signal with highest coherence (Pure Reason)
        valid_signals.sort(key=lambda x: x["coherence"], reverse=True)
        return {"status": "categorical", "dominant_signal": valid_signals[0]}
