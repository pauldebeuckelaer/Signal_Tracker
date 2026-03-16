"""
Combined Strategy Bot — Main Entry Point
==========================================
VI + RSI dip buyer with contrarian flow priority + BTC risk-off gate.

30-second loop with two rhythms:
  - Every 30s: manage position (TP/SL/time exit)
  - Every 30min: check for new entry signal

Backtested: 53 trades, 73.6% WR, +33.56%, PF 2.65, 7/7 positive weeks.

Usage:
    python3 main_combined.py
    nohup python3 main_combined.py > /dev/null 2>&1 &
"""

import time
from datetime import datetime, timezone

import config
from utils import log, save_positions, load_positions, save_status
from signal_engine import get_signal
from storage.database import init_db, record_entry, record_exit
from telegram import alerts
import executor


# ─────────────────────────────────────────────
# POSITION MANAGEMENT (TP / SL / TIME EXIT)
# ─────────────────────────────────────────────

def check_exit_conditions():
    """
    Check TP, SL, and max hold time for the active position.
    Called every 30 seconds.
    """
    positions = executor.get_positions()
    if not positions:
        return

    client = executor.get_client()

    for pos in list(positions):
        symbol = pos['symbol']
        price = _get_price(client, symbol)

        if not price:
            log.warning(f"Can't get price for {symbol}, skipping exit check")
            continue

        entry_price = pos['entry_price']

        # Update max/min tracking
        if price > pos.get('max_price', 0):
            pos['max_price'] = price
        if price < pos.get('min_price', float('inf')):
            pos['min_price'] = price

        # PnL (long only)
        pnl_pct = (price - entry_price) / entry_price * 100

        # ── TAKE PROFIT ──
        if pnl_pct >= config.TP_PCT:
            executor.close_position(symbol, f"TP hit: {pnl_pct:+.2f}% >= {config.TP_PCT}%")
            continue

        # ── STOP LOSS ──
        if pnl_pct <= -config.SL_PCT:
            executor.close_position(symbol, f"SL hit: {pnl_pct:+.2f}% <= -{config.SL_PCT}%")
            continue

        # ── MAX HOLD TIME ──
        entry_time = datetime.fromisoformat(pos['entry_time'])
        now = datetime.now(timezone.utc)
        hold_minutes = (now - entry_time).total_seconds() / 60
        max_hold_minutes = config.MAX_HOLD_BARS * config.CANDLE_FREQ_MINUTES

        if hold_minutes >= max_hold_minutes:
            executor.close_position(symbol, f"TIME exit: held {hold_minutes:.0f}min (max {max_hold_minutes}min), PnL={pnl_pct:+.2f}%")
            continue

    save_positions(executor.get_positions())


# ─────────────────────────────────────────────
# SIGNAL CHECK & ENTRY
# ─────────────────────────────────────────────

def check_for_entry():
    """
    Check the combined signal and enter if conditions are met.
    Called once per 30-min candle close.
    """
    # Already in a position?
    if not executor.can_open():
        log.info("Position open, skipping signal check")
        return

    # Get the signal
    signal = get_signal()

    # Log the signal regardless
    log.info(f"SIGNAL | {signal['signal_type']:>8} | VI={signal['vi']:.3f} RSI={signal['rsi']:.1f} "
             f"flow={signal['capped_flow']:+.0f} whales={signal['unique_whales']} "
             f"BTC_3h={signal['btc_3h_change']:+.2f}% | ${signal['price']:.4f} | {signal['reason']}")

    if not signal['entry']:
        save_status({
            'last_signal': signal,
            'positions': len(executor.get_positions()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        return

    # ── ENTER ──
    log.info(f">> ENTERING: {signal['reason']}")

    # Build signal dict compatible with executor.open_position
    entry_signal = {
        'price': signal['price'],
        'cf_z': signal['capped_flow'],  # repurpose field for flow value
        'unique_addresses': signal['unique_whales'],
    }

    success = executor.open_position(
        symbol=config.SYMBOL,
        direction='long',
        signal=entry_signal,
    )

    if success:
        # Store extra metadata on the position
        positions = executor.get_positions()
        for pos in positions:
            if pos['symbol'] == config.SYMBOL:
                pos['signal_type'] = signal['signal_type']
                pos['entry_vi'] = signal['vi']
                pos['entry_rsi'] = signal['rsi']
                pos['entry_flow'] = signal['capped_flow']
                pos['entry_btc_3h'] = signal['btc_3h_change']
                break
        save_positions(positions)

    save_status({
        'last_signal': signal,
        'last_entry': success,
        'positions': len(executor.get_positions()),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


# ─────────────────────────────────────────────
# CANDLE BOUNDARY DETECTION
# ─────────────────────────────────────────────

def _is_candle_close() -> bool:
    """
    Check if we're at a 30-minute candle boundary (±2 min tolerance).
    Candle closes at :00 and :30.
    """
    now = datetime.now(timezone.utc)
    minute = now.minute
    # Within 2 minutes of :00 or :30
    return minute <= 2 or (30 <= minute <= 32)


_last_candle_check = None

def _should_check_signal() -> bool:
    """Ensure we only check once per candle close."""
    global _last_candle_check

    if not _is_candle_close():
        return False

    now = datetime.now(timezone.utc)
    # Round to current 30-min boundary
    candle_key = now.strftime('%Y-%m-%d %H:') + ('00' if now.minute < 30 else '30')

    if candle_key == _last_candle_check:
        return False  # Already checked this candle

    _last_candle_check = candle_key
    return True


# ─────────────────────────────────────────────
# HEARTBEAT
# ─────────────────────────────────────────────

def log_heartbeat():
    """Log position status."""
    positions = executor.get_positions()
    if not positions:
        return

    client = executor.get_client()
    for pos in positions:
        price = _get_price(client, pos['symbol'])
        if price and pos['entry_price']:
            pnl = (price - pos['entry_price']) / pos['entry_price'] * 100
            entry_time = datetime.fromisoformat(pos['entry_time'])
            hold_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60

            log.info(f"POS | {pos.get('signal_type', '?'):>8} | "
                     f"LONG {pos['size']:.4f} {pos['symbol']} @ ${pos['entry_price']:.4f} "
                     f"→ ${price:.4f} | PnL: {pnl:+.2f}% | Hold: {hold_min:.0f}min")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _get_price(client, symbol: str):
    """Get current price."""
    try:
        mids = client.get_all_mids()
        price = float(mids.get(symbol, 0))
        return price if price > 0 else None
    except Exception as e:
        log.error(f"Price error for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main():
    log.info("=" * 85)
    log.info("Combined Strategy Bot v1.0")
    log.info(f"Strategy: VI>{config.VI_THRESHOLD} + RSI<{config.RSI_THRESHOLD} "
             f"| Contrarian priority + BTC gate")
    log.info(f"Pair: {config.SYMBOL} | Size: ${config.FIXED_POSITION_USD}")
    log.info(f"Exit: TP={config.TP_PCT}% SL={config.SL_PCT}% "
             f"MaxHold={config.MAX_HOLD_BARS} bars ({config.MAX_HOLD_BARS * config.CANDLE_FREQ_MINUTES}min)")
    log.info(f"BTC risk-off gate: 3h change > {config.BTC_3H_CHANGE_MIN}%")
    log.info(f"Candle freq: {config.CANDLE_FREQ_MINUTES}min | "
             f"Position check: {config.POSITION_CHECK_SECONDS}s")
    log.info("=" * 85)

    # Initialize
    init_db()
    executor.init()

    # Run initial signal check
    log.info("Running initial signal check...")
    check_for_entry()

    heartbeat_counter = 0

    while True:
        try:
            heartbeat_counter += 1

            # Every 30s: check exits (TP/SL/time)
            check_exit_conditions()

            # At candle close: check for new entry
            if _should_check_signal():
                log.info("─" * 60)
                log.info(f"CANDLE CLOSE | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
                check_for_entry()

            # Heartbeat every 5 minutes (10 x 30s)
            if heartbeat_counter >= 10:
                log_heartbeat()
                heartbeat_counter = 0

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        time.sleep(config.POSITION_CHECK_SECONDS)


if __name__ == "__main__":
    main()