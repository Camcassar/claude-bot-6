"""
CumRSI2-Regime — configuration.
Secrets come from Railway environment variables (never bake keys into code).
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ── API / environment ─────────────────────────────────────────────────
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET    = os.getenv("TESTNET", "true").lower() == "true"

# ── Market ────────────────────────────────────────────────────────────
SYMBOL      = os.getenv("SYMBOL", "ETHUSDT")  # Bybit USDT linear perpetual
CATEGORY    = "linear"
TIMEFRAME   = os.getenv("TIMEFRAME", "D")  # daily bars (Bybit kline interval)
CANDLE_LIMIT = 400    # default fetch size; velocity_bot overrides as needed
POLL_SECONDS = 60     # main loop interval

# ── Risk ──────────────────────────────────────────────────────────────
LEVERAGE = int(os.getenv("LEVERAGE", "3"))  # applied at execution; backtest is unlevered
