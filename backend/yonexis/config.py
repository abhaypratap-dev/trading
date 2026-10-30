"""Settings from environment (.env in docker compose)."""
from __future__ import annotations

import os
from pathlib import Path


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


class Settings:
    DATA_DIR = Path(os.getenv("YONEXIS_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'yonexis.db'}")
    REDIS_URL = os.getenv("REDIS_URL", "")

    # LLM (any OpenAI-compatible server: Ollama, vLLM, LM Studio, Azure OpenAI, ...)
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
    LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder:7b")
    LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))

    # Live loop
    LIVE_CONNECTOR = os.getenv("LIVE_CONNECTOR", "simulated")     # simulated | yahoo | kite | upstox
    LIVE_INTERVAL = os.getenv("LIVE_INTERVAL", "5m")
    WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS,LT,AXISBANK,KOTAKBANK,BHARTIARTL,ITC,TATAMOTORS,HINDUNILVR,SUNPHARMA,BAJFINANCE,ADANIENT,TATAPOWER,MARUTI,HCLTECH,WIPRO").split(",") if s.strip()]
    LIVE_REPLAY_SPEED = float(os.getenv("LIVE_REPLAY_SPEED", "2"))  # seconds per candle when replaying simulated data

    # Trading
    TRADING_MODE = os.getenv("TRADING_MODE", "paper")              # paper | live
    BROKER = os.getenv("BROKER", "paper")                          # paper | upstox | kite
    KITE_API_KEY = os.getenv("KITE_API_KEY", "")
    KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
    UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "")
    UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
    UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/api/upstox/callback")
    UPSTOX_FEED_MODE = os.getenv("UPSTOX_FEED_MODE", "full")

    # Paper trading (demo money). Real orders stay locked unless BOTH TRADING_MODE=live and the unlock phrase are set.
    PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "1000000"))
    PAPER_SLIPPAGE_BPS = float(os.getenv("PAPER_SLIPPAGE_BPS", "3"))
    PAPER_LEVERAGE = float(os.getenv("PAPER_LEVERAGE", "1"))
    FUT_MARGIN_PCT = float(os.getenv("FUT_MARGIN_PCT", "15"))      # paper margin blocked for a futures position (% of value)
    FUT_LOTS_PER_TRADE = int(os.getenv("FUT_LOTS_PER_TRADE", "1"))
    LIVE_ORDERS_UNLOCK = os.getenv("LIVE_ORDERS_UNLOCK", "")
    UNLOCK_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"

    @property
    def live_orders_allowed(self) -> bool:
        return self.TRADING_MODE == "live" and self.LIVE_ORDERS_UNLOCK == self.UNLOCK_PHRASE

    # Risk
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "10000"))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE", "100000"))
    MAX_ORDERS_PER_SEC = int(os.getenv("MAX_ORDERS_PER_SEC", "5"))
    NO_ENTRY_AFTER = os.getenv("NO_ENTRY_AFTER", "15:00")
    MIN_SIGNAL_SCORE = float(os.getenv("MIN_SIGNAL_SCORE", "0.0"))

    # Sandbox
    SANDBOX_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "300"))
    SANDBOX_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", "2048"))

    ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
    ENABLE_LIVE_LOOP = _b("ENABLE_LIVE_LOOP", True)

    @property
    def models_dir(self) -> Path:
        return self.DATA_DIR / "models"

    @property
    def uploads_dir(self) -> Path:
        return self.DATA_DIR / "uploads"

    @property
    def strategies_dir(self) -> Path:
        return self.DATA_DIR / "strategies"


settings = Settings()
for _d in (settings.DATA_DIR, settings.models_dir, settings.uploads_dir, settings.strategies_dir):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:          # read-only places (the sandbox) never need these folders
        pass
