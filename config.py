"""
Signal Scanner v2.0 — Configuration
=====================================
Token categories, strategy parameters, execution settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

#DB_PATH = os.getenv("DB_PATH", "/home/pauldb46/Hyperliquid_TWAP_Tracker/data/twap.db")
DB_PATH = os.getenv("DB_PATH", r"C:/Users/paul_/PycharmProjects/Hyperliquid_TWAP_Analyzer/data/twap.db")
# ─────────────────────────────────────────────
# SCAN SETTINGS
# ─────────────────────────────────────────────

SCAN_INTERVAL_SECONDS = 300          # activity scan every 5 minutes
POSITION_CHECK_SECONDS = 60          # position management every 1 minute

RECENT_WINDOW_HOURS = 1              # "current" activity window
BASELINE_WINDOW_HOURS = 24           # compare against this
BASELINE_OFFSET_HOURS = 1            # skip recent window in baseline

# ─────────────────────────────────────────────
# ACTIVITY THRESHOLDS
# ─────────────────────────────────────────────

MIN_RECENT_ORDERS = 2                # minimum orders to not be "DEAD"
ACTIVITY_RATIO_ACTIVE = 3.0          # ACTIVE threshold
ACTIVITY_RATIO_HOT = 5.0             # HOT threshold

# ─────────────────────────────────────────────
# EXECUTION SETTINGS
# ─────────────────────────────────────────────

MAX_POSITIONS = 2                    # max concurrent positions
FIXED_POSITION_USD = 12.50           # per position (same as TrailBot)

# ─────────────────────────────────────────────
# SIGNAL SETTINGS (momentum strategy)
# ─────────────────────────────────────────────

# Entry
ENTRY_ZSCORE = 1.0                   # minimum z-score for entry
ENTRY_ZSCORE_MAX = 2.5               # reject extreme z (probably noise)
MIN_UNIQUE_ADDRESSES = 3             # breadth filter — the key fix

# Exit
EXIT_ZSCORE = 0.0                    # exit when z drops below this
HARD_REVERSAL_Z = -1.0               # immediate exit on hard reversal

# Stops
FIXED_STOP_PCT = 2.5                 # fixed stop loss %
TRAILING_STOP_PCT = 1.5              # trailing stop from max price %
TRAILING_ACTIVATE_PCT = 1.0          # only activate trail after +X% profit

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
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "signal_scanner")
TELEGRAM_ADMIN_USER_ID = os.getenv("TELEGRAM_ADMIN_USER_ID", "")

# ─────────────────────────────────────────────
# VALIDATED TOKENS — TIER 1 (Crypto Perps)
# ─────────────────────────────────────────────
# Momentum strategy: follow whale TWAP consensus
# Validated via multi-token correlation backtest (March 6, 2026)
# Criteria: high_corr > low_corr AND high_wr > low_wr
# All long-only based on structural token dynamics

VALIDATED_TOKENS = {
    "ZRO": {
        "tier": 1,
        "direction": "long_only",
        "high_corr": 0.278,
        "high_wr": 69.7,
        "high_buy_ret": 1.173,
        "notes": "Best signal. 6x return improvement in high activity.",
    },
    "HYPE": {
        "tier": 1,
        "direction": "long_only",
        "high_corr": 0.243,
        "high_wr": 63.0,
        "high_buy_ret": 0.257,
        "notes": "Original token. Proven signal, vault buybacks support longs.",
    },
    "XPL": {
        "tier": 2,
        "direction": "long_only",
        "high_corr": 0.273,
        "high_wr": 61.8,
        "high_buy_ret": 0.677,
        "notes": "Signal only exists with activity. Negative corr when quiet.",
    },
    "PAXG": {
        "tier": 2,
        "direction": "long_only",
        "high_corr": 0.105,
        "high_wr": 61.8,
        "high_buy_ret": 0.101,
        "notes": "Gold proxy. Flight to safety plays.",
    },
    "BTC": {
        "tier": 2,
        "direction": "long_only",
        "high_corr": 0.104,
        "high_wr": 55.6,
        "high_buy_ret": 0.104,
        "notes": "Signal exists but weak. Driven by macro more than HL whales.",
    },
    "XRP": {
        "tier": 2,
        "direction": "long_only",
        "high_corr": 0.112,
        "high_wr": 51.5,
        "high_buy_ret": 0.063,
        "notes": "Moderate signal in high activity regime.",
    },
    "AAVE": {
        "tier": 2,
        "direction": "long_only",
        "high_corr": 0.130,
        "high_wr": 44.2,
        "high_buy_ret": 0.046,
        "notes": "Good correlation but low win rate. Use with caution.",
    },
}