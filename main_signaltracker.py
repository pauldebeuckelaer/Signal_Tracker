"""
Signal Scanner v2.0 — Entry Point
===================================
Multi-token execution engine.

1-minute loop with two rhythms:
- Every 1 min: manage positions (stops, signal exits)
- Every 5 min: scan for new opportunities

Usage:
    python3 main.py
    nohup python3 main.py > /dev/null 2>&1 &
"""

import time
from datetime import datetime, timezone

import config
from utils import log, save_status, get_connection
from scanner import scan_all, find_opportunities, log_scan
from strategies import get_signal, should_enter, should_exit
from storage.database import init_db, save_scan
import executor


def run_position_management():
    """
    Every 1 minute: check stops and signal exits for open positions.
    """
    positions = executor.get_positions()
    if not positions:
        return

    # Check stop losses first
    executor.check_stops()

    # Check signal exits for remaining positions
    for pos in list(executor.get_positions()):
        signal = get_signal(pos["symbol"])

        exit_eval = should_exit(signal, pos)
        if exit_eval["exit"]:
            executor.close_position(pos["symbol"], exit_eval["reason"])


def run_opportunity_scan():
    """
    Every 5 minutes: scan all tokens, find opportunities, enter if valid.
    """
    try:
        conn = get_connection()
        results = scan_all(conn)
        opportunities = find_opportunities(results)
        log_scan(results, opportunities)
        save_scan(results)
        save_status({
            "tokens": results,
            "opportunities": opportunities,
            "has_opportunities": len(opportunities) > 0,
            "positions": len(executor.get_positions()),
        })
        conn.close()
    except Exception as e:
        log.error(f"Scan error: {e}")
        return

    # No room for new positions
    if not executor.can_open():
        return

    # Evaluate opportunities
    for opp in opportunities:
        symbol = opp["symbol"]

        # Already in this token
        if executor.has_position(symbol):
            continue

        # Check if we still have room
        if not executor.can_open():
            break

        # Get fresh signal
        signal = get_signal(symbol)
        token_config = config.VALIDATED_TOKENS.get(symbol, {})
        entry_eval = should_enter(signal, token_config)

        if entry_eval["enter"]:
            log.info(f">> TAKING OPPORTUNITY: {symbol} | {entry_eval['reason']}")
            executor.open_position(symbol, signal["direction"], signal)
        else:
            log.info(f"-- Skipping {symbol}: {entry_eval['reason']}")


def main():
    log.info("=" * 85)
    log.info("Signal Scanner v2.0 — Multi-Token Execution Engine")
    log.info(f"Tokens: {', '.join(config.VALIDATED_TOKENS.keys())}")
    log.info(f"Max positions: {config.MAX_POSITIONS} | "
             f"Position size: ${config.FIXED_POSITION_USD}")
    log.info(f"Entry: z>{config.ENTRY_ZSCORE} & addrs>={config.MIN_UNIQUE_ADDRESSES} | "
             f"Exit: z<{config.EXIT_ZSCORE}")
    log.info(f"Stops: fixed={config.FIXED_STOP_PCT}% | "
             f"trail={config.TRAILING_STOP_PCT}% (activate at +{config.TRAILING_ACTIVATE_PCT}%)")
    log.info(f"Scan: {config.SCAN_INTERVAL_SECONDS}s | "
             f"Position check: {config.POSITION_CHECK_SECONDS}s")
    log.info("=" * 85)

    # Initialize database
    init_db()

    # Initialize executor (load saved positions, verify on exchange)
    executor.init()

    scan_counter = 0
    scans_per_cycle = config.SCAN_INTERVAL_SECONDS // config.POSITION_CHECK_SECONDS

    # Initial scan on startup
    log.info("Running initial scan...")
    run_opportunity_scan()

    while True:
        try:
            scan_counter += 1

            # Every minute: manage positions
            run_position_management()

            # Every 5 minutes: scan for opportunities
            if scan_counter >= scans_per_cycle:
                run_opportunity_scan()
                scan_counter = 0

            # Log heartbeat
            positions = executor.get_positions()
            if positions:
                for pos in positions:
                    client = executor.get_client()
                    price = executor._get_price(client, pos["symbol"])
                    if price and pos["entry_price"]:
                        pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100
                        log.info(f"Position: {pos['side'].upper()} {pos['size']:.4f} "
                                 f"{pos['symbol']} @ ${pos['entry_price']:.4f} "
                                 f"(PnL: {pnl:+.2f}%)")

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        time.sleep(config.POSITION_CHECK_SECONDS)


if __name__ == "__main__":
    main()