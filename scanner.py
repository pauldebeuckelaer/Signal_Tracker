"""
Signal Scanner v2.0 — Activity Radar
=================================
Scans validated tokens for TWAP activity spikes.
Compares recent activity vs baseline to find where the party is.
"""

from datetime import datetime, timezone

from config import (
    VALIDATED_TOKENS,
    RECENT_WINDOW_HOURS,
    BASELINE_WINDOW_HOURS,
    BASELINE_OFFSET_HOURS,
    MIN_RECENT_ORDERS,
    ACTIVITY_RATIO_ACTIVE,
    ACTIVITY_RATIO_HOT,
)
from utils import log, get_connection


def scan_token(conn, symbol):
    """
    Scan a single token's TWAP activity.
    Returns a dict with activity metrics.
    """
    # ── Recent activity ──
    row = conn.execute("""
        SELECT 
            COUNT(*) as orders,
            COUNT(DISTINCT address) as whales,
            COALESCE(SUM(CASE WHEN side='BUY' THEN size ELSE 0 END), 0) as buy_size,
            COALESCE(SUM(CASE WHEN side='SELL' THEN size ELSE 0 END), 0) as sell_size,
            COALESCE(SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END), 0) as buy_orders,
            COALESCE(SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END), 0) as sell_orders
        FROM orders
        WHERE symbol = ?
            AND first_seen_at > datetime('now', ?)
    """, (symbol, f"-{RECENT_WINDOW_HOURS} hours")).fetchone()

    recent_orders = row["orders"]
    recent_whales = row["whales"]
    buy_size = row["buy_size"]
    sell_size = row["sell_size"]
    buy_orders = row["buy_orders"]
    sell_orders = row["sell_orders"]

    # ── Baseline activity ──
    brow = conn.execute("""
        SELECT COUNT(*) as orders
        FROM orders
        WHERE symbol = ?
            AND first_seen_at BETWEEN 
                datetime('now', ?)
                AND datetime('now', ?)
    """, (
        symbol,
        f"-{BASELINE_WINDOW_HOURS + BASELINE_OFFSET_HOURS} hours",
        f"-{BASELINE_OFFSET_HOURS} hours"
    )).fetchone()

    baseline_orders = brow["orders"]
    baseline_rate = baseline_orders / BASELINE_WINDOW_HOURS if BASELINE_WINDOW_HOURS > 0 else 0
    expected_orders = baseline_rate * RECENT_WINDOW_HOURS

    # ── Activity ratio ──
    if expected_orders > 0:
        activity_ratio = recent_orders / expected_orders
    elif recent_orders > 0:
        activity_ratio = float(recent_orders) * 10
    else:
        activity_ratio = 0.0

    # ── Direction ──
    net_size = buy_size - sell_size
    if buy_size + sell_size > 0:
        buy_pct = buy_size / (buy_size + sell_size) * 100
    else:
        buy_pct = 50.0

    if buy_orders > sell_orders:
        direction = "BUY"
    elif sell_orders > buy_orders:
        direction = "SELL"
    else:
        direction = "NEUTRAL"

    # ── Market data from latest snapshot ──
    mrow = conn.execute("""
        SELECT mark_px, prev_day_px, open_interest_usd, day_ntl_vlm, funding_8h
        FROM market_snapshots
        WHERE coin = ?
        ORDER BY snapshot_time DESC
        LIMIT 1
    """, (symbol,)).fetchone()

    price = mrow["mark_px"] if mrow else None
    prev_price = mrow["prev_day_px"] if mrow else None
    oi_usd = mrow["open_interest_usd"] if mrow else None
    daily_vol = mrow["day_ntl_vlm"] if mrow else None
    funding = mrow["funding_8h"] if mrow else None

    price_chg_pct = None
    if price and prev_price and prev_price > 0:
        price_chg_pct = (price - prev_price) / prev_price * 100

    # ── Status ──
    if recent_orders < MIN_RECENT_ORDERS:
        status = "DEAD"
    elif activity_ratio >= ACTIVITY_RATIO_HOT:
        status = "HOT"
    elif activity_ratio >= ACTIVITY_RATIO_ACTIVE:
        status = "ACTIVE"
    else:
        status = "QUIET"

    return {
        "symbol": symbol,
        "tier": VALIDATED_TOKENS[symbol]["tier"],
        "status": status,
        "activity_ratio": round(activity_ratio, 2),
        "recent_orders": recent_orders,
        "recent_whales": recent_whales,
        "baseline_rate_hr": round(baseline_rate, 1),
        "direction": direction,
        "buy_orders": buy_orders,
        "sell_orders": sell_orders,
        "buy_size": round(buy_size, 2),
        "sell_size": round(sell_size, 2),
        "net_size": round(net_size, 2),
        "buy_pct": round(buy_pct, 1),
        "price": price,
        "price_chg_pct": round(price_chg_pct, 2) if price_chg_pct is not None else None,
        "oi_usd": round(oi_usd, 0) if oi_usd else None,
        "daily_vol": round(daily_vol, 0) if daily_vol else None,
        "funding_8h": funding,
        "high_wr": VALIDATED_TOKENS[symbol]["high_wr"],
        "high_corr": VALIDATED_TOKENS[symbol]["high_corr"],
    }


def scan_all(conn):
    """Scan all validated tokens. Returns list sorted by activity_ratio desc."""
    results = []
    for symbol in VALIDATED_TOKENS:
        try:
            result = scan_token(conn, symbol)
            results.append(result)
        except Exception as e:
            log.error(f"Error scanning {symbol}: {e}")

    results.sort(key=lambda x: x["activity_ratio"], reverse=True)
    return results


def find_opportunities(results):
    """
    Identify tradeable opportunities:
    tokens that are ACTIVE/HOT with a clear direction.
    """
    opportunities = []
    for r in results:
        if r["status"] in ("HOT", "ACTIVE") and r["direction"] != "NEUTRAL":
            opportunities.append(r)
    return opportunities


def log_scan(results, opportunities):
    """Pretty-print the scan results."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    log.info("=" * 85)
    log.info(f"ACTIVITY RADAR | {now}")
    log.info("=" * 85)

    log.info(f"{'TOKEN':<8} {'T':>1} {'STATUS':<8} {'RATIO':>6} {'ORD':>5} "
             f"{'WHL':>4} {'DIR':<7} {'BUY%':>5} {'PRICE':>10} {'24h%':>7}")
    log.info("-" * 85)

    for r in results:
        price_str = f"${r['price']:.4f}" if r['price'] else "N/A"
        chg_str = f"{r['price_chg_pct']:+.2f}%" if r['price_chg_pct'] is not None else "N/A"
        status_icon = {"HOT": ">> ", "ACTIVE": "> ", "QUIET": "  ", "DEAD": "x "}.get(r["status"], "  ")

        log.info(f"{r['symbol']:<8} {r['tier']:>1} {status_icon}{r['status']:<6} {r['activity_ratio']:>6.2f} "
                 f"{r['recent_orders']:>5} {r['recent_whales']:>4} "
                 f"{r['direction']:<7} {r['buy_pct']:>4.0f}% "
                 f"{price_str:>10} {chg_str:>7}")

    if opportunities:
        log.info("")
        log.info(">> OPPORTUNITIES:")
        for opp in opportunities:
            chg = f"{opp['price_chg_pct']:+.2f}%" if opp['price_chg_pct'] is not None else "N/A"
            log.info(f"  -> {opp['symbol']} | {opp['status']} {opp['activity_ratio']:.1f}x | "
                     f"{opp['direction']} ({opp['buy_pct']:.0f}% buy) | "
                     f"WR: {opp['high_wr']}% | 24h: {chg}")
    else:
        log.info("")
        log.info("-- No opportunities right now.")

    log.info("")