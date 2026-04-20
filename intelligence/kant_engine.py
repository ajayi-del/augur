import structlog

logger = structlog.get_logger()

class KantEngine:
    """
    Kant perceives the structure of reality.
    This works identically for perps AND prediction markets.
    """
    
    STRUCTURES = {
        # === PERPS REGIMES ===
        "cascade_warning": {
            "coherence_min": 4.0,
            "atr_threshold": 0.7,
            "order_type": "market",
            "max_hold_hours": 4,
            "asset_class": "perps"
        },
        "funding_regime": {
            "coherence_min": 3.5,
            "atr_threshold": 0.3,
            "order_type": "limit",
            "max_hold_hours": 24,
            "asset_class": "perps"
        },
        "news_driven": {
            "coherence_min": 4.5,
            "atr_threshold": 0.5,
            "order_type": "market",
            "max_hold_hours": 8,
            "asset_class": "perps"
        },
        
        # === PREDICTION MARKET REGIMES ===
        "information_asymmetry": {
            "min_edge": 0.10,           # Minimum 10% edge required
            "kelly_cap": 0.05,          # Max 5% of bankroll per bet
            "min_confidence": 0.70,     # Kant confidence threshold
            "max_exposure_pct": 0.15,   # Max 15% of bankroll in all bets
            "asset_class": "prediction"
        },
        "event_driven": {
            "min_edge": 0.15,           # Higher edge for event-driven
            "kelly_cap": 0.08,          # Can size larger on events
            "min_confidence": 0.80,
            "max_exposure_pct": 0.20,
            "asset_class": "prediction"
        },
        
        # === UNIVERSAL ===
        "idle": {
            "coherence_min": 5.5,
            "min_edge": 0.20,
            "asset_class": "any"
        }
    }
    
    async def perceive_for_asset(self, signals: dict, asset_class: str) -> dict:
        """
        Determine Kant structure specific to asset class.
        """
        if asset_class == "perps":
            return await self._perceive_perps(signals)
        elif asset_class == "prediction":
            return await self._perceive_prediction(signals)
        else:
            return {"structure": "idle", "confidence": 1.0, "config": self.STRUCTURES["idle"]}
    
    async def _perceive_perps(self, signals: dict) -> dict:
        # Existing logic for perps regimes
        structures = []
        
        cross_chain = signals.get("cross_chain_cascade")
        if cross_chain and cross_chain.get("confidence", 0) > 0.6:
            structures.append(("cascade_warning", cross_chain["confidence"]))
            
        funding = signals.get("funding_extreme")
        if funding:
            structures.append(("funding_regime", funding["extremity"]))
            
        news = signals.get("news_coherence")
        if news and news.get("coherence", 0) > 0.7:
            structures.append(("news_driven", news["coherence"]))
            
        if not structures:
            return {"structure": "idle", "confidence": 1.0, "config": self.STRUCTURES["idle"]}
            
        structures.sort(key=lambda x: x[1], reverse=True)
        dominant, confidence = structures[0]
        return {"structure": dominant, "confidence": confidence, "config": self.STRUCTURES[dominant]}

    async def _perceive_prediction(self, signals: dict) -> dict:
        """
        Kant perception for prediction markets.
        Reality is structured by information asymmetry.
        """
        news = signals.get("news_coherence", {})
        macro = signals.get("macro_sentiment", {})
        flows = signals.get("etf_flows", {})
        
        # Calculate information edge confidence
        confidence = 0.0
        evidence = []
        
        if news.get("coherence", 0) > 0.7:
            confidence += 0.3
            evidence.append("news_coherence")
            
        if macro.get("signal") == "hawkish" and "rates" in signals.get("topic", "").lower():
            confidence += 0.3
            evidence.append("macro_alignment")
            
        if flows.get("direction") == "risk_off" and signals.get("direction") == "bearish":
            confidence += 0.2
            evidence.append("flow_confirmation")
            
        # Select structure based on confidence
        if confidence >= 0.8:
            return {
                "structure": "event_driven",
                "confidence": confidence,
                "evidence": evidence,
                "config": self.STRUCTURES["event_driven"]
            }
        elif confidence >= 0.7:
            return {
                "structure": "information_asymmetry",
                "confidence": confidence,
                "evidence": evidence,
                "config": self.STRUCTURES["information_asymmetry"]
            }
        else:
            return {
                "structure": "idle",
                "confidence": confidence,
                "evidence": evidence,
                "config": self.STRUCTURES["idle"]
            }
