"""
Multi-Coin Signal Tracker — Configuration
===========================================
Per-coin strategies with shared infrastructure.

HYPE:  VI + RSI + contrarian flow + BTC gate + OB filter  (full stack)
VVV:   RSI + BTC gate                                     (simple dip buyer)
NEAR:  RSI + BTC gate                                     (simple dip buyer)
PURR:  RSI + BTC gate                                     (simple dip buyer)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", "/home/pauldb46/Hyperliquid_TWAP_Tracker/data/twap.db")

# ─────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────

SIGNAL_CHECK_SECONDS = 60
POSITION_CHECK_SECONDS = 30
CANDLE_FREQ_MINUTES = 30

# ─────────────────────────────────────────────
# EXECUTION (GLOBAL)
# ─────────────────────────────────────────────

MAX_POSITIONS = 4                     # max 4 concurrent across all coins
FIXED_POSITION_USD = 12.50            # $12.50 per trade

# ─────────────────────────────────────────────
# BTC GATE (shared across all coins)
# ─────────────────────────────────────────────

BTC_SYMBOL = "BTC"
BTC_3H_CHANGE_MIN = -2.0
BTC_LOOKBACK_BARS = 6                 # 6 x 30min = 3 hours

# ─────────────────────────────────────────────
# CANDLE CONSTRUCTION (shared)
# ─────────────────────────────────────────────

CANDLE_LOOKBACK_BARS = 50
SNAPSHOT_MIN_TICKS = 10

# ─────────────────────────────────────────────
# PER-COIN CONFIGURATIONS
# ─────────────────────────────────────────────
# Each coin dict contains all parameters specific to that coin.
# strategy_type:
#   "full"   = VI + RSI + contrarian flow + BTC gate + OB filter (HYPE)
#   "simple" = RSI + BTC gate only (VVV, NEAR, PURR)

COIN_CONFIGS = {
    "HYPE": {
        "symbol": "HYPE",
        "strategy_type": "full",
        "enabled": True,

        # Signal parameters
        "rsi_period": 14,
        "rsi_threshold": 40,          # RSI < 40
        "vi_threshold": 0.3,          # VI > 0.3 (full strategy only)

        # Exit parameters
        "tp_pct": 1.5,
        "sl_pct": 2.0,
        "max_hold_bars": 12,          # 12 x 30min = 6 hours

        # Capped flow (full strategy only)
        "flow_cap_usd": 5000,
        "flow_lookback_hours": 6,

        # Orderbook flow filter (full strategy only)
        "ob_flow_enabled": True,
        "ob_flow_lookback_minutes": 30,
        "ob_flow_stale_minutes": 5,
    },

    "VVV": {
        "symbol": "VVV",
        "strategy_type": "simple",
        "enabled": True,

        "rsi_period": 14,
        "rsi_threshold": 40,          # RSI < 40

        "tp_pct": 3.0,
        "sl_pct": 3.0,
        "max_hold_bars": 6,           # 6 x 30min = 3 hours
    },

    "NEAR": {
        "symbol": "NEAR",
        "strategy_type": "simple",
        "enabled": True,

        "rsi_period": 14,
        "rsi_threshold": 35,          # RSI < 35 (tighter)

        "tp_pct": 1.5,
        "sl_pct": 1.0,
        "max_hold_bars": 6,           # 6 x 30min = 3 hours
    },

    "PURR": {
        "symbol": "PURR",
        "strategy_type": "simple",
        "enabled": True,

        "rsi_period": 14,
        "rsi_threshold": 40,          # RSI < 40

        "tp_pct": 3.0,
        "sl_pct": 3.0,
        "max_hold_bars": 24,          # 24 x 30min = 12 hours
    },
}

# Helper to get list of enabled coins
ENABLED_COINS = [sym for sym, cfg in COIN_CONFIGS.items() if cfg.get("enabled")]

# ─────────────────────────────────────────────
# LEGACY SINGLE-COIN REFERENCES
# ─────────────────────────────────────────────
# Kept for backward compat with executor/database
# that still reference config.SYMBOL, config.TP_PCT etc.
# These default to HYPE but shouldn't be used for
# multi-coin logic — use COIN_CONFIGS[symbol] instead.

SYMBOL = "HYPE"
VI_THRESHOLD = 0.3
RSI_THRESHOLD = 40
RSI_PERIOD = 14
TP_PCT = 1.5
SL_PCT = 2.0
MAX_HOLD_BARS = 12
FLOW_CAP_USD = 5000
FLOW_LOOKBACK_HOURS = 6
OB_FLOW_ENABLED = True
OB_FLOW_LOOKBACK_MINUTES = 30
OB_FLOW_STALE_MINUTES = 5

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