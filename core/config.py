import os
from typing import Literal, List
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Mode
    mode: Literal["paper", "live"] = "paper"
    live_mode_confirmed: bool = False

    # Logging
    log_level: str = "INFO"
    augur_log_path: str = Field(
        default="/Users/dayodapper/CascadeProjects/AUGUR/logs/", env="AUGUR_LOG_PATH"
    )

    # Kingdom — shared with ARIA via kingdom_state.json
    kingdom_state_path: str = Field(
        default="/Users/dayodapper/kingdom/kingdom_state.json",
        env="KINGDOM_STATE_PATH",
    )
    kingdom_sync_interval_s: int = 60

    # Polymarket (public API — no key needed for reading)
    polymarket_private_key: str = Field(default="", env="POLYMARKET_PRIVATE_KEY")
    polymarket_api_key: str     = Field(default="", env="POLYMARKET_API_KEY")
    polymarket_mode: str        = Field(default="paper", env="POLYMARKET_MODE")
    polymarket_min_edge: float  = Field(default=0.08,  env="POLYMARKET_MIN_EDGE")
    polymarket_min_liquidity: float = Field(default=5000.0, env="POLYMARKET_MIN_LIQUIDITY")
    polymarket_max_bet_pct: float   = Field(default=0.05,   env="POLYMARKET_MAX_BET_PCT")
    polymarket_bankroll: float      = Field(default=100.0,  env="POLYMARKET_BANKROLL")

    # Drift (no key in paper mode — public data only)
    drift_gateway_url: str  = Field(default="http://localhost:8080", env="DRIFT_GATEWAY_URL")
    drift_keypair_path: str = Field(default="", env="DRIFT_KEYPAIR_PATH")

    # Jupiter / Solana (no key in paper mode)
    solana_rpc_url: str   = "https://api.mainnet-beta.solana.com"
    jupiter_ws_url: str   = "wss://api.jup.ag/v6/ws"
    jupiter_mode: str     = Field(default="paper", env="JUPITER_MODE")

    # MEXC (primary execution venue)
    mexc_api_key: str                 = Field(default="", env="MEXC_API_KEY")
    mexc_secret_key: str              = Field(default="", env="MEXC_SECRET_KEY")
    mexc_futures_leverage: int        = Field(default=5,     env="MEXC_FUTURES_LEVERAGE")
    mexc_max_position_usdt: float     = Field(default=200.0, env="MEXC_MAX_POSITION_USDT")
    mexc_prediction_bankroll: float   = Field(default=50.0,  env="MEXC_PREDICTION_BANKROLL")
    mexc_prediction_max_bet_pct: float = Field(default=0.05, env="MEXC_PREDICTION_MAX_BET_PCT")
    mexc_min_prediction_edge: float   = Field(default=0.08,  env="MEXC_MIN_PREDICTION_EDGE")

    # Bybit (fallback venue)
    bybit_api_key: str    = Field(default="", env="BYBIT_API_KEY")
    bybit_api_secret: str = Field(default="", env="BYBIT_API_SECRET")
    bybit_mode: str       = Field(default="paper", env="BYBIT_MODE")

    # Optional AI keys (disabled until added)
    grok_api_key: str       = Field(default="", env="GROK_API_KEY")
    openrouter_key: str     = Field(default="", env="OPENROUTER_KEY")
    claude_api_key: str     = Field(default="", env="CLAUDE_API_KEY")

    # SoSoValue (disabled — rate limited; replaced by ValueChain bridge)
    sosovalue_api_key: str  = Field(default="", env="SOSOVALUE_API_KEY")

    # Trading logic
    base_trade_usd: float   = 200.0
    max_trade_usd: float    = 400.0
    min_trade_usd: float    = 50.0
    default_leverage: int   = 5
    min_coherence: float    = 5.0
    risk_pct: float         = 0.01
    news_poll_interval_s: int = 300

    # Tracked markets — alts + meme coins, no stocks
    watched_markets: List[str] = [
        "SOL-PERP", "DOGE-PERP", "WIF-PERP", "BONK-PERP",
        "TRUMP-PERP", "PEPE-PERP", "SUI-PERP", "ARB-PERP",
        "OP-PERP", "MNT-PERP", "EDGE-PERP",
        "CHILLGUY-PERP", "PIPPIN-PERP", "PIEVERSE-PERP",
        "AVAX-PERP", "BNB-PERP", "HYPE-PERP", "ENA-PERP",
    ]
    news_assets: List[str] = [
        "SOL", "DOGE", "WIF", "BONK", "TRUMP", "PEPE",
        "SUI", "ARB", "OP", "MNT", "EDGE",
        "CHILLGUY", "PIPPIN", "PIEVERSE",
        "AVAX", "BNB", "HYPE", "ENA",
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


config = Settings()
