"""
Multi-Coin Signal Tracker — Main Entry Point
==============================================
Trades HYPE (full VI+flow strategy) + VVV, NEAR, PURR (RSI dip buyers).

30-second loop with two rhythms:
  - Every 30s: manage positions (per-coin TP/SL/time exit)
  - Every 30min: check all enabled coins for entry signals

Max 4 concurrent positions, $12.50 each.

Usage:
    python3 main_signaltracker.py
    nohup python3 main_signaltracker.py > /dev/null 2>&1 &
"""

import time
from datetime import datetime, timezone

import config
from utils import log, save_positions, load_positions, save_status
from signal_engine import get_signal_for_coin
from storage.database import init_db, record_entry, record_exit
from telegram import alerts
import executor


# ─────────────────────────────────────────────
# COOLDOWN TRACKING
# ─────────────────────────────────────────────
# Tracks when each coin last had a TIME exit.
# Format: { "VVV": datetime, "NEAR": datetime, ... }
_cooldown_until = {}


def _set_cooldown(symbol: str):
    """Set cooldown for a coin after a TIME exit."""
    coin_cfg = config.COIN_CONFIGS.get(symbol, {})
    cooldown_minutes = coin_cfg.get('cooldown_minutes', 0)
    if cooldown_minutes > 0:
        until = datetime.now(timezone.utc) + __import__('datetime').timedelta(minutes=cooldown_minutes)
        _cooldown_until[symbol] = until
        log.info(f"[{symbol}] Cooldown set: no re-entry until {until.strftime('%H:%M UTC')} ({cooldown_minutes}min)")


def _is_on_cooldown(symbol: str) -> bool:
    """Check if a coin is still in cooldown after a TIME exit."""
    if symbol not in _cooldown_until:
        return False
    now = datetime.now(timezone.utc)
    if now < _cooldown_until[symbol]:
        remaining = (_cooldown_until[symbol] - now).total_seconds() / 60
        log.info(f"[{symbol}] On cooldown, {remaining:.0f}min remaining")
        return True
    # Cooldown expired, clean up
    del _cooldown_until[symbol]
    return False


# ─────────────────────────────────────────────
# POSITION MANAGEMENT (TP / SL / TIME EXIT)
# ─────────────────────────────────────────────

def check_exit_conditions():
    """
    Check TP, SL, and max hold time for all active positions.
    Uses per-coin exit parameters from COIN_CONFIGS.
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

        # Get per-coin exit parameters
        coin_cfg = config.COIN_CONFIGS.get(symbol, {})
        tp_pct = coin_cfg.get('tp_pct', config.TP_PCT)
        sl_pct = coin_cfg.get('sl_pct', config.SL_PCT)
        max_hold_bars = coin_cfg.get('max_hold_bars', config.MAX_HOLD_BARS)
        max_hold_minutes = max_hold_bars * config.CANDLE_FREQ_MINUTES

        # ── TAKE PROFIT ──
        if pnl_pct >= tp_pct:
            executor.close_position(symbol, f"TP hit: {pnl_pct:+.2f}% >= {tp_pct}%")
            continue

        # ── STOP LOSS ──
        if pnl_pct <= -sl_pct:
            executor.close_position(symbol, f"SL hit: {pnl_pct:+.2f}% <= -{sl_pct}%")
            continue

        # ── MAX HOLD TIME ──
        entry_time = datetime.fromisoformat(pos['entry_time'])
        now = datetime.now(timezone.utc)
        hold_minutes = (now - entry_time).total_seconds() / 60

        if hold_minutes >= max_hold_minutes:
            executor.close_position(symbol,
                f"TIME exit: held {hold_minutes:.0f}min (max {max_hold_minutes}min), PnL={pnl_pct:+.2f}%")
            # Set cooldown to prevent immediate re-entry
            _set_cooldown(symbol)
            continue

    save_positions(executor.get_positions())


# ─────────────────────────────────────────────
# SIGNAL CHECK & ENTRY (MULTI-COIN)
# ─────────────────────────────────────────────

def check_all_coins():
    """
    Check signals for all enabled coins and enter where conditions are met.
    Called once per 30-min candle close.
    """
    for symbol in config.ENABLED_COINS:
        coin_cfg = config.COIN_CONFIGS[symbol]

        # Skip if already in this coin
        if executor.has_position(symbol):
            log.info(f"[{symbol}] Already in position, skipping")
            continue

        # Skip if on cooldown after TIME exit
        if _is_on_cooldown(symbol):
            continue

        # Skip if max positions reached
        if not executor.can_open():
            log.info(f"[{symbol}] Max positions ({config.MAX_POSITIONS}) reached, skipping")
            break  # no point checking remaining coins

        # Get signal for this coin
        signal = get_signal_for_coin(coin_cfg)

        # Log regardless of entry
        _log_signal(signal, coin_cfg)

        if not signal['entry']:
            continue

        # ── ENTER ──
        log.info(f"[{symbol}] >> ENTERING: {signal['reason']}")

        entry_signal = {
            'price': signal['price'],
            'cf_z': signal.get('capped_flow', 0),
            'unique_addresses': signal.get('unique_whales', 0),
        }

        success = executor.open_position(
            symbol=symbol,
            direction='long',
            signal=entry_signal,
        )

        if success:
            # Store extra metadata on the position
            positions = executor.get_positions()
            for pos in positions:
                if pos['symbol'] == symbol:
                    pos['signal_type'] = signal['signal_type']
                    pos['entry_vi'] = signal.get('vi', 0)
                    pos['entry_rsi'] = signal['rsi']
                    pos['entry_flow'] = signal.get('capped_flow', 0)
                    pos['entry_btc_3h'] = signal['btc_3h_change']
                    pos['strategy_type'] = coin_cfg['strategy_type']
                    pos['entry_ob_flow'] = signal.get('ob_flow', 0)
                    pos['entry_ob_trades'] = signal.get('ob_trades', 0)
                    break
            save_positions(positions)

    # Save status after checking all coins
    save_status({
        'coins_checked': config.ENABLED_COINS,
        'positions': len(executor.get_positions()),
        'open_symbols': [p['symbol'] for p in executor.get_positions()],
        'cooldowns': {sym: until.isoformat() for sym, until in _cooldown_until.items()},
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


def _log_signal(signal: dict, coin_cfg: dict):
    """Log signal details in a consistent format."""
    symbol = signal['symbol']
    strategy = coin_cfg['strategy_type']

    if strategy == "full":
        log.info(f"[{symbol}] {signal['signal_type']:>8} | "
                 f"VI={signal['vi']:.3f} RSI={signal['rsi']:.1f} "
                 f"flow={signal.get('capped_flow', 0):+.0f} "
                 f"whales={signal.get('unique_whales', 0)} "
                 f"BTC_3h={signal['btc_3h_change']:+.2f}% | "
                 f"${signal['price']:.4f} | {signal['reason']}")
    else:
        ob_str = f" OB={signal.get('ob_flow', 'n/a')}/{signal.get('ob_trades', 'n/a')}t" if 'ob_flow' in signal else ""
        log.info(f"[{symbol}] {signal['signal_type']:>8} | "
                 f"RSI={signal['rsi']:.1f} "
                 f"BTC_3h={signal['btc_3h_change']:+.2f}%{ob_str} | "
                 f"${signal['price']:.4f} | {signal['reason']}")


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
    return minute <= 2 or (30 <= minute <= 32)


_last_candle_check = None

def _should_check_signal() -> bool:
    """Ensure we only check once per candle close."""
    global _last_candle_check

    if not _is_candle_close():
        return False

    now = datetime.now(timezone.utc)
    candle_key = now.strftime('%Y-%m-%d %H:') + ('00' if now.minute < 30 else '30')

    if candle_key == _last_candle_check:
        return False

    _last_candle_check = candle_key
    return True


# ─────────────────────────────────────────────
# HEARTBEAT (LOG + TELEGRAM)
# ─────────────────────────────────────────────

TELEGRAM_HEARTBEAT_SECONDS = 2 * 60 * 60  # 2 hours
_last_telegram_heartbeat = None

def log_heartbeat():
    """Log all open positions to file."""
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

            coin_cfg = config.COIN_CONFIGS.get(pos['symbol'], {})
            tp = coin_cfg.get('tp_pct', '?')
            sl = coin_cfg.get('sl_pct', '?')

            log.info(f"POS [{pos['symbol']}] | {pos.get('signal_type', '?'):>8} | "
                     f"LONG {pos['size']:.4f} @ ${pos['entry_price']:.4f} "
                     f"→ ${price:.4f} | PnL: {pnl:+.2f}% | Hold: {hold_min:.0f}min "
                     f"| TP={tp}% SL={sl}%")


def telegram_heartbeat():
    """
    Send position update via Telegram every 2 hours.
    Only sends if there are open positions.
    """
    global _last_telegram_heartbeat

    positions = executor.get_positions()
    if not positions:
        _last_telegram_heartbeat = None  # reset so next open triggers immediately
        return

    now = datetime.now(timezone.utc)

    # Check if 2 hours have passed since last Telegram heartbeat
    if _last_telegram_heartbeat is not None:
        elapsed = (now - _last_telegram_heartbeat).total_seconds()
        if elapsed < TELEGRAM_HEARTBEAT_SECONDS:
            return

    _last_telegram_heartbeat = now

    # Build the message
    client = executor.get_client()
    lines = [f"📊 Position Update ({len(positions)} open)"]

    for pos in positions:
        price = _get_price(client, pos['symbol'])
        if not price or not pos['entry_price']:
            lines.append(f"\n{pos['symbol']}: price unavailable")
            continue

        pnl = (price - pos['entry_price']) / pos['entry_price'] * 100
        entry_time = datetime.fromisoformat(pos['entry_time'])
        hold_min = (now - entry_time).total_seconds() / 60

        coin_cfg = config.COIN_CONFIGS.get(pos['symbol'], {})
        tp = coin_cfg.get('tp_pct', '?')
        sl = coin_cfg.get('sl_pct', '?')
        max_hold = coin_cfg.get('max_hold_bars', 0) * config.CANDLE_FREQ_MINUTES

        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"\n{emoji} {pos['symbol']} | {pos.get('signal_type', '?')}"
            f"\n   ${pos['entry_price']:.4f} → ${price:.4f} ({pnl:+.2f}%)"
            f"\n   Hold: {hold_min:.0f}/{max_hold}min | TP={tp}% SL={sl}%"
        )

    alerts.send("\n".join(lines))


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
    log.info("Multi-Coin Signal Tracker v2.1")
    log.info(f"Coins: {', '.join(config.ENABLED_COINS)}")
    log.info(f"Max positions: {config.MAX_POSITIONS} | Size: ${config.FIXED_POSITION_USD} each")
    log.info(f"BTC risk-off gate: 3h change > {config.BTC_3H_CHANGE_MIN}%")
    log.info(f"Candle freq: {config.CANDLE_FREQ_MINUTES}min | "
             f"Position check: {config.POSITION_CHECK_SECONDS}s")
    log.info("-" * 85)
    for sym in config.ENABLED_COINS:
        cfg = config.COIN_CONFIGS[sym]
        strategy = cfg['strategy_type']
        cooldown = cfg.get('cooldown_minutes', 0)
        if strategy == "full":
            log.info(f"  {sym:6s} | {strategy:6s} | VI>{cfg['vi_threshold']} RSI<{cfg['rsi_threshold']} "
                     f"| TP={cfg['tp_pct']}% SL={cfg['sl_pct']}% "
                     f"Hold={cfg['max_hold_bars']}bars ({cfg['max_hold_bars'] * config.CANDLE_FREQ_MINUTES}min) "
                     f"| OB={cfg.get('ob_flow_enabled', False)} | CD={cooldown}min")
        else:
            ob_info = ""
            if cfg.get('ob_flow_enabled', False):
                ob_info = (f" | OB: flow>={cfg.get('ob_min_net_flow', 0)} "
                           f"trades>={cfg.get('ob_min_trades', 0)}")
            log.info(f"  {sym:6s} | {strategy:6s} | RSI<{cfg['rsi_threshold']} "
                     f"| TP={cfg['tp_pct']}% SL={cfg['sl_pct']}% "
                     f"Hold={cfg['max_hold_bars']}bars ({cfg['max_hold_bars'] * config.CANDLE_FREQ_MINUTES}min)"
                     f"{ob_info} | CD={cooldown}min")
    log.info("=" * 85)

    # Initialize
    init_db()
    executor.init()

    # Run initial signal check
    log.info("Running initial signal check for all coins...")
    check_all_coins()

    heartbeat_counter = 0

    while True:
        try:
            heartbeat_counter += 1

            # Every 30s: check exits (TP/SL/time) for all open positions
            check_exit_conditions()

            # At candle close: check all coins for new entries
            if _should_check_signal():
                log.info("─" * 60)
                log.info(f"CANDLE CLOSE | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
                check_all_coins()

            # Heartbeat every 5 minutes (10 x 30s) — log only
            if heartbeat_counter >= 10:
                log_heartbeat()
                heartbeat_counter = 0

            # Telegram heartbeat every 2 hours (if positions open)
            telegram_heartbeat()

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        time.sleep(config.POSITION_CHECK_SECONDS)


if __name__ == "__main__":
    main()