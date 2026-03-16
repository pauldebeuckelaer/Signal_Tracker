"""
Combined Strategy Bot — Configuration
=======================================
VI + RSI dip buyer with contrarian whale flow filter + BTC risk-off gate.

Strategy logic:
  1. VI > 0.3 AND RSI(14) < 40 → potential entry
  2. If capped_flow < 0 → ENTER (contrarian, highest conviction)
  3. If capped_flow >= 0 AND BTC 3h change > -2% → ENTER (baseline + BTC gate)
  4. If capped_flow >= 0 AND BTC 3h change <= -2% → SKIP
  5. Exit: TP=1.5%, SL=2.0%, MaxHold=12 bars (6 hours)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

# GCP path:
DB_PATH = os.getenv("DB_PATH", "/home/pauldb46/Hyperliquid_TWAP_Tracker/data/twap.db")
# Local path:
#DB_PATH = os.getenv("DB_PATH", r"C:/Users/paul_/PycharmProjects/Hyperliquid_TWAP_Analyzer/data/twap.db")

# ─────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────

SIGNAL_CHECK_SECONDS = 60             # check for new signal every 60s
POSITION_CHECK_SECONDS = 30           # manage positions every 30s
CANDLE_FREQ_MINUTES = 30              # 30-minute candles

# ─────────────────────────────────────────────
# TRADING PAIR
# ─────────────────────────────────────────────

SYMBOL = "HYPE"
BTC_SYMBOL = "BTC"

# ─────────────────────────────────────────────
# EXECUTION
# ─────────────────────────────────────────────

MAX_POSITIONS = 1                     # one position at a time
FIXED_POSITION_USD = 12.50            # $12.50 per trade

# ─────────────────────────────────────────────
# SIGNAL PARAMETERS (validated via backtest)
# ─────────────────────────────────────────────

# Entry conditions
VI_THRESHOLD = 0.3                    # vol_imbalance > 0.3
RSI_THRESHOLD = 40                    # RSI(14) < 40
RSI_PERIOD = 14                       # RSI lookback

# Contrarian flow (priority entry)
# When flow < 0 AND VI+RSI met → enter regardless of BTC
# No additional threshold needed — any negative flow qualifies

# BTC risk-off gate (baseline entry only)
BTC_3H_CHANGE_MIN = -2.0             # BTC 3h change must be > -2%
BTC_LOOKBACK_BARS = 6                # 6 x 30min = 3 hours

# ─────────────────────────────────────────────
# EXIT PARAMETERS (validated via TP/SL sweep)
# ─────────────────────────────────────────────

TP_PCT = 1.5                          # take profit at +1.5%
SL_PCT = 2.0                          # stop loss at -2.0%
MAX_HOLD_BARS = 12                    # max hold = 12 x 30min = 6 hours

# ─────────────────────────────────────────────
# CAPPED FLOW CALCULATION
# ─────────────────────────────────────────────

FLOW_CAP_USD = 5000                   # cap per address per bin
FLOW_LOOKBACK_HOURS = 6               # hours of order data for flow calc

# ─────────────────────────────────────────────
# CANDLE CONSTRUCTION
# ─────────────────────────────────────────────

CANDLE_LOOKBACK_BARS = 50             # need ~50 bars for RSI(14) warmup
SNAPSHOT_MIN_TICKS = 10               # minimum ticks per candle (50% of 30min ≈ 15)

# ─────────────────────────────────────────────
# EXCHANGE CREDENTIALS (from .env)
# ─────────────────────────────────────────────

PRIVATE_KEY = os.getenv("HYPERLIQUID_PRIVATE_KEY", "")
MAIN_ACCOUNT = os.getenv("HYPERLIQUID_MAIN_ACCOUNT", "")
TESTNET = os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true"

# ─────────────────────────────────────────────
# TELEGRAM (from .env)
# ─────────────────────────────────────────────

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "combined_bot")
TELEGRAM_ADMIN_USER_ID = os.getenv("TELEGRAM_ADMIN_USER_ID", "")