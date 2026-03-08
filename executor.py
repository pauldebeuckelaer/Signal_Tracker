"""
Signal Scanner v2.0 — Executor
================================
Order placement, position tracking, stop management.
Manages up to MAX_POSITIONS concurrent positions across tokens.
"""

import time
from datetime import datetime, timezone

from utils import log, save_positions, load_positions
from telegram import alerts
from storage.database import record_entry, record_exit
from exchange.hyperliquid_client import HyperLiquidClient
import config


# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────

_client = None
_positions = []   # list of position dicts


def get_client():
    """Lazy-init the exchange client."""
    global _client
    if _client is None:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "exchange"))


        _client = HyperLiquidClient(
            private_key=config.PRIVATE_KEY,
            testnet=config.TESTNET,
            config={
                "balance_override": {
                    "main_account_address": config.MAIN_ACCOUNT,
                }
            },
        )
        log.info(f"Exchange client initialized (testnet={config.TESTNET})")
    return _client


def init():
    """Initialize executor: load saved positions, verify against exchange."""
    global _positions
    saved = load_positions()

    if saved:
        log.info(f"Loaded {len(saved)} saved positions from disk")
        # Verify they still exist on exchange
        _positions = []
        client = get_client()
        live_positions = client.get_open_positions()
        live_symbols = {p.symbol for p in live_positions}

        for pos in saved:
            if pos["symbol"] in live_symbols:
                _positions.append(pos)
                log.info(f"  Confirmed: {pos['symbol']} {pos['side']} @ ${pos['entry_price']:.4f}")
            else:
                log.warning(f"  Stale position removed: {pos['symbol']}")

        save_positions(_positions)
    else:
        _positions = []
        log.info("No saved positions found")


def get_positions():
    return _positions


def has_position(symbol: str) -> bool:
    return any(p["symbol"] == symbol for p in _positions)


def position_count() -> int:
    return len(_positions)


def can_open() -> bool:
    return position_count() < config.MAX_POSITIONS


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────

def open_position(symbol: str, direction: str, signal: dict):
    """
    Open a new position on a token.
    """
    if has_position(symbol):
        log.warning(f"Already have position in {symbol}, skipping")
        return False

    if not can_open():
        log.warning(f"Max positions ({config.MAX_POSITIONS}) reached, skipping {symbol}")
        return False

    client = get_client()
    price = signal["price"]

    if not price or price <= 0:
        log.error(f"Invalid price for {symbol}: {price}")
        return False

    try:
        size = config.FIXED_POSITION_USD / price

        # Get contract for rounding
        contract = client._get_contract(symbol)
        if contract:
            size = contract.round_size(size)

        if size <= 0:
            log.error(f"Size too small for {symbol} at ${price:.4f}")
            return False

        from exchange.models import OrderRequest
        order = OrderRequest(
            symbol=symbol,
            side="buy" if direction == "long" else "sell",
            size=size,
            order_type="limit",
            price=price,
        )

        result = client.place_order(order)

        if result and result.success:
            pos = {
                "symbol": symbol,
                "side": direction,
                "size": size,
                "entry_price": price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "entry_z": signal["cf_z"],
                "entry_addrs": signal["unique_addresses"],
                "max_price": price,
                "min_price": price,
            }
            _positions.append(pos)
            save_positions(_positions)

            record_entry(
                symbol=symbol, side=direction, entry_price=price,
                size=size, entry_z=signal["cf_z"],
                entry_addrs=signal["unique_addresses"],
            )

            log.info(f"ENTRY: {direction.upper()} {size:.4f} {symbol} @ ${price:.4f} "
                     f"(z={signal['cf_z']:+.2f}, addrs={signal['unique_addresses']})")

            alerts.entry_alert(
                symbol=symbol,
                direction=direction,
                price=price,
                z=signal["cf_z"],
                whales=signal["unique_addresses"],
                activity_ratio=0,  # filled in by caller if available
                reason=f"z={signal['cf_z']:+.2f}, {signal['unique_addresses']} addrs",
            )
            return True
        else:
            error = result.error if result else "Unknown"
            log.error(f"Order failed for {symbol}: {error}")
            return False

    except Exception as e:
        log.error(f"Entry error for {symbol}: {e}")
        return False


# ─────────────────────────────────────────────
# EXIT
# ─────────────────────────────────────────────

def close_position(symbol: str, reason: str = ""):
    """Close an existing position."""
    global _positions

    pos = next((p for p in _positions if p["symbol"] == symbol), None)
    if not pos:
        log.warning(f"No position to close for {symbol}")
        return False

    client = get_client()

    try:
        # Get current price for PnL calc
        current_price = _get_price(client, symbol)

        from exchange.models import OrderRequest
        order = OrderRequest(
            symbol=symbol,
            side="sell" if pos["side"] == "long" else "buy",
            size=pos["size"],
            order_type="market",
            reduce_only=True,
        )

        result = client.place_order(order)

        if result and result.success:
            # Calculate PnL
            if current_price and pos["entry_price"]:
                if pos["side"] == "long":
                    pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                else:
                    pnl_pct = (pos["entry_price"] - current_price) / pos["entry_price"] * 100
            else:
                pnl_pct = 0.0

            log.info(f"EXIT: {pos['side'].upper()} {pos['size']:.4f} {symbol} | "
                     f"Entry ${pos['entry_price']:.4f} → ${current_price:.4f} | "
                     f"PnL: {pnl_pct:+.2f}% | {reason}")

            record_exit(
                symbol=symbol, exit_price=current_price or 0,
                exit_z=0, exit_reason=reason,
                max_price=pos.get("max_price"),
                min_price=pos.get("min_price"),
            )

            alerts.exit_alert(
                symbol=symbol,
                direction=pos["side"],
                entry_price=pos["entry_price"],
                exit_price=current_price or 0,
                pnl_pct=pnl_pct,
                reason=reason,
            )

            _positions = [p for p in _positions if p["symbol"] != symbol]
            save_positions(_positions)
            return True
        else:
            error = result.error if result else "Unknown"
            log.error(f"Exit failed for {symbol}: {error}")
            return False

    except Exception as e:
        log.error(f"Exit error for {symbol}: {e}")
        return False


# ─────────────────────────────────────────────
# STOP MANAGEMENT
# ─────────────────────────────────────────────

def check_stops():
    """
    Check all positions for stop conditions.
    Called every minute.
    """
    if not _positions:
        return

    client = get_client()

    for pos in list(_positions):
        symbol = pos["symbol"]
        current_price = _get_price(client, symbol)

        if not current_price:
            log.warning(f"Can't get price for {symbol}, skipping stop check")
            continue

        entry_price = pos["entry_price"]

        # Update max/min price tracking
        if current_price > pos.get("max_price", 0):
            pos["max_price"] = current_price
        if current_price < pos.get("min_price", float("inf")):
            pos["min_price"] = current_price

        # PnL
        if pos["side"] == "long":
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100

        # Fixed stop
        if pos["side"] == "long":
            fixed_stop = entry_price * (1 - config.FIXED_STOP_PCT / 100)
            if current_price <= fixed_stop:
                close_position(symbol, f"Fixed stop ${fixed_stop:.4f} (entry ${entry_price:.4f} -{config.FIXED_STOP_PCT}%)")
                continue
        else:
            fixed_stop = entry_price * (1 + config.FIXED_STOP_PCT / 100)
            if current_price >= fixed_stop:
                close_position(symbol, f"Fixed stop ${fixed_stop:.4f} (entry ${entry_price:.4f} +{config.FIXED_STOP_PCT}%)")
                continue

        # Trailing stop (only activates after minimum profit)
        if pnl_pct >= config.TRAILING_ACTIVATE_PCT:
            max_price = pos.get("max_price", current_price)
            if pos["side"] == "long":
                trail_stop = max_price * (1 - config.TRAILING_STOP_PCT / 100)
                if current_price <= trail_stop:
                    close_position(symbol, f"Trail stop ${trail_stop:.4f} (max ${max_price:.4f} -{config.TRAILING_STOP_PCT}%)")
                    continue
            else:
                min_price = pos.get("min_price", current_price)
                trail_stop = min_price * (1 + config.TRAILING_STOP_PCT / 100)
                if current_price >= trail_stop:
                    close_position(symbol, f"Trail stop ${trail_stop:.4f} (min ${min_price:.4f} +{config.TRAILING_STOP_PCT}%)")
                    continue

    save_positions(_positions)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _get_price(client, symbol: str):
    """Get current price for a symbol."""
    try:
        mids = client.get_all_mids()
        price = float(mids.get(symbol, 0))
        return price if price > 0 else None
    except Exception as e:
        log.error(f"Price error for {symbol}: {e}")
        return None