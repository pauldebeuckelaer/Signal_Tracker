"""
Signal Scanner v2.0 — Strategies
==================================
Signal generation and entry/exit logic.

Strategy: Momentum (Tier 1 crypto perps)
- Enter when capped flow z-score spikes WITH whale breadth
- Exit on z-score decay, hard reversal, or stops
"""

from datetime import datetime, timezone
from utils import log, get_connection

import config


def get_signal(symbol: str) -> dict:
    """
    Calculate the capped-flow z-score signal for a token.
    Uses rolling 24-bin lookback (same as TrailBot v3.0).

    Returns dict with: cf_z, direction, cf_raw, unique_addresses, bin_count, price
    """
    conn = get_connection()

    try:
        # Get recent TWAP orders in 30-min bins (rolling 12h window = 24 bins)
        rows = conn.execute("""
            SELECT 
                strftime('%Y-%m-%d %H:', first_seen_at) || 
                    CASE WHEN CAST(strftime('%M', first_seen_at) AS INTEGER) < 30 
                         THEN '00' ELSE '30' END as bin,
                address,
                side,
                CASE 
                    WHEN size * (SELECT mark_px FROM market_snapshots 
                                 WHERE coin = ? ORDER BY snapshot_time DESC LIMIT 1) > ?
                    THEN ?
                    ELSE size * (SELECT mark_px FROM market_snapshots 
                                 WHERE coin = ? ORDER BY snapshot_time DESC LIMIT 1)
                END as capped_usd
            FROM orders
            WHERE symbol = ?
                AND first_seen_at > datetime('now', '-12 hours')
            ORDER BY first_seen_at
        """, (symbol, 5000, 5000, symbol, symbol)).fetchall()

        if not rows:
            return _empty_signal(symbol)

        # Aggregate by bin
        bins = {}
        all_addresses = set()
        for row in rows:
            b = row["bin"]
            if b not in bins:
                bins[b] = {"buy": 0.0, "sell": 0.0, "addrs": set()}
            if row["side"] == "BUY":
                bins[b]["buy"] += row["capped_usd"]
            else:
                bins[b]["sell"] += row["capped_usd"]
            bins[b]["addrs"].add(row["address"])
            all_addresses.add(row["address"])

        # Calculate capped flow per bin
        sorted_bins = sorted(bins.keys())
        cf_values = []
        for b in sorted_bins:
            cf = bins[b]["buy"] - bins[b]["sell"]
            cf_values.append(cf)

        if len(cf_values) < 2:
            return _empty_signal(symbol)

        # Current bin = last one
        current_cf = cf_values[-1]
        current_bin = sorted_bins[-1]
        current_addrs = len(bins[current_bin]["addrs"])

        # Z-score: (current - mean) / std of lookback
        lookback = cf_values[:-1]  # everything except current
        if not lookback:
            return _empty_signal(symbol)

        mean_cf = sum(lookback) / len(lookback)
        variance = sum((x - mean_cf) ** 2 for x in lookback) / len(lookback)
        std_cf = variance ** 0.5

        if std_cf < 1e-8:
            cf_z = 0.0
        else:
            cf_z = (current_cf - mean_cf) / std_cf

        # Direction
        if cf_z >= config.ENTRY_ZSCORE:
            direction = "long"
        elif cf_z <= -config.ENTRY_ZSCORE:
            direction = "short"
        else:
            direction = "neutral"

        # Get current price
        price_row = conn.execute("""
            SELECT mark_px FROM market_snapshots 
            WHERE coin = ? ORDER BY snapshot_time DESC LIMIT 1
        """, (symbol,)).fetchone()
        price = price_row["mark_px"] if price_row else None

        # Count unique addresses in the CURRENT bin only (breadth)
        return {
            "symbol": symbol,
            "cf_z": round(cf_z, 2),
            "cf_raw": round(current_cf, 2),
            "direction": direction,
            "unique_addresses": current_addrs,
            "total_addresses_12h": len(all_addresses),
            "bin_count": len(cf_values),
            "price": price,
        }

    except Exception as e:
        log.error(f"Signal error for {symbol}: {e}")
        return _empty_signal(symbol)
    finally:
        conn.close()


def _empty_signal(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "cf_z": 0.0,
        "cf_raw": 0.0,
        "direction": "neutral",
        "unique_addresses": 0,
        "total_addresses_12h": 0,
        "bin_count": 0,
        "price": None,
    }


def should_enter(signal: dict, token_config: dict) -> dict:
    """
    Evaluate whether to enter a position.

    Returns: {"enter": bool, "reason": str}
    """
    z = signal["cf_z"]
    addrs = signal["unique_addresses"]
    direction = signal["direction"]
    token_dir = token_config.get("direction", "long_only")

    # No price = no trade
    if signal["price"] is None:
        return {"enter": False, "reason": "no price data"}

    # Not enough bins (cold start)
    if signal["bin_count"] < 6:
        return {"enter": False, "reason": f"cold start ({signal['bin_count']} bins)"}

    # Direction filter
    if token_dir == "long_only" and direction != "long":
        return {"enter": False, "reason": f"not long (z={z:+.2f})"}

    # Z-score too low
    if z < config.ENTRY_ZSCORE:
        return {"enter": False, "reason": f"z too low ({z:+.2f})"}

    # Z-score too high (noise spike)
    if z > config.ENTRY_ZSCORE_MAX:
        return {"enter": False, "reason": f"z too high ({z:+.2f}), likely noise"}

    # BREADTH FILTER — the key improvement over TrailBot
    if addrs < config.MIN_UNIQUE_ADDRESSES:
        return {"enter": False, "reason": f"low breadth ({addrs} addr, need {config.MIN_UNIQUE_ADDRESSES})"}

    return {
        "enter": True,
        "reason": f"z={z:+.2f}, {addrs} addrs",
    }


def should_exit(signal: dict, position: dict) -> dict:
    """
    Evaluate whether to exit a position based on signal.
    Stop losses are handled separately in executor.

    Returns: {"exit": bool, "reason": str}
    """
    z = signal["cf_z"]

    # Hard reversal
    if z <= config.HARD_REVERSAL_Z:
        return {"exit": True, "reason": f"hard reversal (z={z:+.2f})"}

    # Z-threshold decay
    if z < config.EXIT_ZSCORE:
        return {"exit": True, "reason": f"z-threshold (z={z:+.2f} < {config.EXIT_ZSCORE})"}

    return {"exit": False, "reason": ""}