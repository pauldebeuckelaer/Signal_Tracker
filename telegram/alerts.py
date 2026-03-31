"""
Signal Scanner v2.0 — Telegram Alerts
=======================================
Shared Telegram notification module.
"""

import asyncio
import sys
import os
from pathlib import Path

# Add parent dir to path so we can import from root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import log
import config


async def _send(message: str):
    """Internal: send message via Telethon."""
    try:
        if not config.TELEGRAM_ENABLED or not config.TELEGRAM_API_ID:
            return

        from telethon import TelegramClient

        session_path = str(Path(__file__).parent.parent / config.TELEGRAM_SESSION_NAME)

        client = TelegramClient(
            session_path,
            int(config.TELEGRAM_API_ID),
            config.TELEGRAM_API_HASH,
        )
        await client.start(phone=config.TELEGRAM_PHONE)

        if config.TELEGRAM_ADMIN_USER_ID:
            await client.send_message(int(config.TELEGRAM_ADMIN_USER_ID), message)

        await client.disconnect()

    except Exception as e:
        log.error(f"Telegram error: {e}")


def send(message: str):
    """Send a Telegram message (sync wrapper)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_send(message))
        else:
            asyncio.run(_send(message))
    except RuntimeError:
        asyncio.run(_send(message))


def entry_alert(symbol: str, direction: str, price: float, signal: dict = None,
                reason: str = ""):
    """Send trade entry notification with full signal context."""
    coin_cfg = config.COIN_CONFIGS.get(symbol, {})
    strategy = coin_cfg.get('strategy_type', 'simple')

    msg = f"🟢 ENTRY: {direction.upper()} {symbol}\n"
    msg += f"Price: ${price:.4f}\n"

    if strategy == "full" and signal:
        msg += (f"VI: {signal.get('vi', 0):.3f} | RSI: {signal.get('rsi', 0):.1f}\n"
                f"Flow: {signal.get('capped_flow', 0):+.0f} | Whales: {signal.get('unique_whales', 0)}\n"
                f"BTC 3h: {signal.get('btc_3h_change', 0):+.2f}%\n")
    elif signal:
        rsi_1h = signal.get('rsi_1h', None)
        rsi_4h = signal.get('rsi_4h', None)
        msg += (f"RSI: 30m={signal.get('rsi', 0):.1f} "
                f"1h={f'{rsi_1h:.1f}' if rsi_1h else 'n/a'} "
                f"4h={f'{rsi_4h:.1f}' if rsi_4h else 'n/a'}\n"
                f"BTC 3h: {signal.get('btc_3h_change', 0):+.2f}%\n")

    if reason:
        msg += f"Signal: {reason}\n"

    tp = coin_cfg.get('tp_pct', '?')
    sl = coin_cfg.get('sl_pct', '?')
    hold = coin_cfg.get('max_hold_bars', 0) * config.CANDLE_FREQ_MINUTES
    msg += f"TP={tp}% | SL={sl}% | Max hold={hold}min"

    send(msg)



def exit_alert(symbol: str, direction: str, entry_price: float,
               exit_price: float, pnl_pct: float, pnl_usd: float = 0,
               hold_minutes: float = 0, reason: str = ""):
    """Send trade exit notification."""
    emoji = "🟢" if pnl_pct >= 0 else "🔴"
    msg = (
        f"{emoji} EXIT: {direction.upper()} {symbol}\n"
        f"${entry_price:.4f} → ${exit_price:.4f}\n"
        f"PnL: {pnl_pct:+.2f}%"
    )
    if pnl_usd:
        msg += f" (${pnl_usd:+.3f})"
    msg += "\n"
    if hold_minutes:
        msg += f"Hold: {hold_minutes:.0f}min\n"
    msg += f"Reason: {reason}"
    send(msg)


def opportunity_alert(symbol: str, status: str, activity_ratio: float,
                      direction: str, buy_pct: float):
    """Send activity spike notification (no trade taken)."""
    msg = (
        f"👀 ACTIVITY: {symbol} is {status}\n"
        f"Ratio: {activity_ratio:.1f}x | {direction} ({buy_pct:.0f}% buy)\n"
    )
    send(msg)