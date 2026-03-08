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


def entry_alert(symbol: str, direction: str, price: float, z: float,
                whales: int, activity_ratio: float, reason: str = ""):
    """Send trade entry notification."""
    msg = (
        f"🟢 ENTRY: {direction.upper()} {symbol}\n"
        f"Price: ${price:.4f}\n"
        f"Z-score: {z:+.2f}\n"
        f"Whales: {whales} | Activity: {activity_ratio:.1f}x\n"
    )
    if reason:
        msg += f"Reason: {reason}\n"
    send(msg)


def exit_alert(symbol: str, direction: str, entry_price: float,
               exit_price: float, pnl_pct: float, reason: str = ""):
    """Send trade exit notification."""
    emoji = "🟢" if pnl_pct >= 0 else "🔴"
    msg = (
        f"{emoji} EXIT: {direction.upper()} {symbol}\n"
        f"Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f}\n"
        f"PnL: {pnl_pct:+.2f}%\n"
        f"Reason: {reason}\n"
    )
    send(msg)


def opportunity_alert(symbol: str, status: str, activity_ratio: float,
                      direction: str, buy_pct: float):
    """Send activity spike notification (no trade taken)."""
    msg = (
        f"👀 ACTIVITY: {symbol} is {status}\n"
        f"Ratio: {activity_ratio:.1f}x | {direction} ({buy_pct:.0f}% buy)\n"
    )
    send(msg)