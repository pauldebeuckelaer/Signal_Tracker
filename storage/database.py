"""
Signal Scanner v2.0 — Database
================================
SQLite storage for scan history and trade journal.
"""

import sqlite3
import os
from datetime import datetime, timezone

from utils import log

# ─────────────────────────────────────────────
# DATABASE PATH
# ─────────────────────────────────────────────

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "scanner.db")

os.makedirs(DB_DIR, exist_ok=True)


def get_db():
    """Get a writable connection to scanner.db."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            activity_ratio REAL,
            recent_orders INTEGER,
            recent_whales INTEGER,
            direction TEXT,
            buy_pct REAL,
            price REAL,
            price_chg_pct REAL,
            oi_usd REAL,
            daily_vol REAL,
            funding_8h REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            entry_price REAL NOT NULL,
            exit_price REAL,
            size REAL NOT NULL,
            entry_z REAL,
            entry_addrs INTEGER,
            activity_ratio REAL,
            exit_z REAL,
            exit_reason TEXT,
            pnl_pct REAL,
            pnl_usd REAL,
            max_price REAL,
            min_price REAL,
            hold_minutes REAL
        )
    """)

    # Indexes for common queries
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_time ON scans(scan_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_symbol ON scans(symbol, scan_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")

    conn.commit()
    conn.close()
    log.info(f"Database initialized: {DB_PATH}")


# ─────────────────────────────────────────────
# SCAN HISTORY
# ─────────────────────────────────────────────

def save_scan(results: list):
    """Save a batch of scan results (one per token per scan cycle)."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    for r in results:
        conn.execute("""
            INSERT INTO scans (scan_time, symbol, status, activity_ratio,
                               recent_orders, recent_whales, direction, buy_pct,
                               price, price_chg_pct, oi_usd, daily_vol, funding_8h)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now,
            r["symbol"],
            r["status"],
            r["activity_ratio"],
            r["recent_orders"],
            r["recent_whales"],
            r["direction"],
            r["buy_pct"],
            r["price"],
            r.get("price_chg_pct"),
            r.get("oi_usd"),
            r.get("daily_vol"),
            r.get("funding_8h"),
        ))

    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# TRADE JOURNAL
# ─────────────────────────────────────────────

def record_entry(symbol: str, side: str, entry_price: float, size: float,
                 entry_z: float, entry_addrs: int, activity_ratio: float = 0) -> int:
    """Record a new trade entry. Returns the trade ID."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute("""
        INSERT INTO trades (symbol, side, status, entry_time, entry_price, size,
                            entry_z, entry_addrs, activity_ratio, max_price, min_price)
        VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, side, now, entry_price, size, entry_z, entry_addrs,
          activity_ratio, entry_price, entry_price))

    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def record_exit(symbol: str, exit_price: float, exit_z: float,
                exit_reason: str, max_price: float = None, min_price: float = None):
    """Record a trade exit. Matches on symbol + status='open'."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Find the open trade
    row = conn.execute("""
        SELECT id, entry_price, entry_time, size, side
        FROM trades WHERE symbol = ? AND status = 'open'
        ORDER BY entry_time DESC LIMIT 1
    """, (symbol,)).fetchone()

    if not row:
        log.warning(f"No open trade found for {symbol} to record exit")
        conn.close()
        return

    entry_price = row["entry_price"]
    entry_time = row["entry_time"]
    size = row["size"]
    side = row["side"]

    # Calculate PnL
    if side == "long":
        pnl_pct = (exit_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100
    pnl_usd = pnl_pct / 100 * size * entry_price

    # Calculate hold time
    try:
        entry_dt = datetime.fromisoformat(entry_time)
        exit_dt = datetime.fromisoformat(now)
        hold_minutes = (exit_dt - entry_dt).total_seconds() / 60
    except Exception:
        hold_minutes = 0

    conn.execute("""
        UPDATE trades SET
            status = 'closed',
            exit_time = ?,
            exit_price = ?,
            exit_z = ?,
            exit_reason = ?,
            pnl_pct = ?,
            pnl_usd = ?,
            max_price = COALESCE(?, max_price),
            min_price = COALESCE(?, min_price),
            hold_minutes = ?
        WHERE id = ?
    """, (now, exit_price, exit_z, exit_reason, round(pnl_pct, 4),
          round(pnl_usd, 4), max_price, min_price, round(hold_minutes, 1),
          row["id"]))

    conn.commit()
    conn.close()


def get_open_trades() -> list:
    """Get all currently open trades."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time
    """).fetchall()
    trades = [dict(r) for r in rows]
    conn.close()
    return trades


def get_trade_summary(days: int = 30) -> dict:
    """Quick performance summary for the last N days."""
    conn = get_db()
    rows = conn.execute("""
        SELECT symbol, side, entry_price, exit_price, pnl_pct, pnl_usd,
               exit_reason, hold_minutes, entry_z, entry_addrs, entry_time
        FROM trades
        WHERE status = 'closed'
            AND entry_time > datetime('now', ?)
        ORDER BY entry_time
    """, (f"-{days} days",)).fetchall()

    if not rows:
        conn.close()
        return {"trades": 0}

    trades = [dict(r) for r in rows]
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    summary = {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "total_pnl": sum(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "best_trade": max(pnls),
        "worst_trade": min(pnls),
        "avg_hold_min": sum(t["hold_minutes"] or 0 for t in trades) / len(trades),
        "by_symbol": {},
    }

    # Per-symbol breakdown
    symbols = set(t["symbol"] for t in trades)
    for sym in symbols:
        sym_trades = [t for t in trades if t["symbol"] == sym]
        sym_pnls = [t["pnl_pct"] for t in sym_trades]
        sym_wins = [p for p in sym_pnls if p > 0]
        summary["by_symbol"][sym] = {
            "trades": len(sym_trades),
            "win_rate": len(sym_wins) / len(sym_trades) * 100,
            "total_pnl": sum(sym_pnls),
        }

    conn.close()
    return summary