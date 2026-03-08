"""
Signal Scanner v2.0 — Utilities
=================================
Database helpers, logging, state management.
"""

import sqlite3
import logging
import os
import json
from datetime import datetime, timezone

from config import DB_PATH


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "scanner.log")

os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging():
    logger = logging.getLogger("scanner")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


log = setup_logging()


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

def get_connection():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────
# STATE FILE (positions + scan results)
# ─────────────────────────────────────────────

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
SCANNER_STATUS_FILE = os.path.join(STATE_DIR, "scanner_status.json")
POSITIONS_FILE = os.path.join(STATE_DIR, "positions.json")


def save_status(data):
    data["scan_time"] = datetime.now(timezone.utc).isoformat()
    with open(SCANNER_STATUS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_status():
    if not os.path.exists(SCANNER_STATUS_FILE):
        return None
    with open(SCANNER_STATUS_FILE, "r") as f:
        return json.load(f)


def save_positions(positions):
    """Save position state to disk for crash recovery."""
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
    }
    with open(POSITIONS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_positions():
    """Load position state from disk."""
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE, "r") as f:
            data = json.load(f)
            return data.get("positions", [])
    except (json.JSONDecodeError, KeyError):
        return []