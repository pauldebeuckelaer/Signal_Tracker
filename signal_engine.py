"""
Combined Strategy Bot — Signal Engine
=======================================
Reads twap.db to compute:
  - 30min HYPE candles with vol_imbalance and RSI
  - Capped whale flow per 30min bin
  - BTC 3h price change for risk-off gate

Returns a signal dict the main loop uses for entry/exit decisions.
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from utils import log
import config


def get_connection():
    """Get a read-only connection to twap.db."""
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _build_candles(conn, symbol: str, lookback_hours: int = 30) -> pd.DataFrame:
    """
    Build 30-minute OHLCV candles from snapshots table.
    Includes buy_volume, sell_volume for vol_imbalance calculation.
    """
    query = """
        SELECT timestamp, price, buy_volume, sell_volume, unique_addresses
        FROM snapshots
        WHERE symbol = ?
          AND timestamp > datetime('now', ?)
        ORDER BY timestamp
    """
    df = pd.read_sql_query(query, conn, params=(symbol, f"-{lookback_hours} hours"))

    if df.empty:
        log.warning(f"No snapshot data for {symbol} in last {lookback_hours}h")
        return pd.DataFrame()

    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.set_index('timestamp')

    candles = df.resample('30min').agg(
        open=('price', 'first'),
        high=('price', 'max'),
        low=('price', 'min'),
        close=('price', 'last'),
        buy_volume=('buy_volume', 'sum'),
        sell_volume=('sell_volume', 'sum'),
        avg_unique_addr=('unique_addresses', 'mean'),
        tick_count=('price', 'count'),
    ).dropna(subset=['close'])

    # Drop incomplete candles
    candles = candles[candles['tick_count'] >= config.SNAPSHOT_MIN_TICKS]

    return candles.reset_index()


def _add_indicators(candles: pd.DataFrame) -> pd.DataFrame:
    """Add RSI and vol_imbalance to candle dataframe."""
    d = candles.copy()

    if len(d) < config.RSI_PERIOD + 5:
        log.warning(f"Not enough candles for indicators: {len(d)}")
        return d

    # Vol imbalance
    d['vol_imbalance'] = (d['buy_volume'] - d['sell_volume']) / (d['buy_volume'] + d['sell_volume'] + 1)

    # RSI(14)
    delta = d['close'].diff()
    gain = delta.clip(lower=0).rolling(config.RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(config.RSI_PERIOD).mean()
    rs = gain / loss
    d['rsi'] = 100 - (100 / (1 + rs))

    # ATR for reference (not used in entry, but useful for logging)
    h_l = d['high'] - d['low']
    h_pc = (d['high'] - d['close'].shift(1)).abs()
    l_pc = (d['low'] - d['close'].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    d['atr'] = tr.rolling(14).mean()
    d['atr_pct'] = d['atr'] / d['close'] * 100

    return d


def _build_capped_flow(conn, symbol: str, lookback_hours: int = None) -> pd.DataFrame:
    """
    Calculate capped whale flow from orders table.
    Returns a df with: timestamp, capped_flow, unique_whales
    """
    if lookback_hours is None:
        lookback_hours = config.FLOW_LOOKBACK_HOURS

    query = """
        SELECT first_seen_at, address, side, size
        FROM orders
        WHERE symbol = ?
          AND first_seen_at > datetime('now', ?)
        ORDER BY first_seen_at
    """
    df = pd.read_sql_query(query, conn, params=(symbol, f"-{lookback_hours} hours"))

    if df.empty:
        return pd.DataFrame(columns=['timestamp', 'capped_flow', 'unique_whales'])

    df['first_seen_at'] = pd.to_datetime(df['first_seen_at'], utc=True)
    df['bin'] = df['first_seen_at'].dt.floor('30min')
    df['signed_size'] = df['size'] * df['side'].map({'BUY': 1, 'SELL': -1})

    # Group by bin + address, then cap
    addr_bin = df.groupby(['bin', 'address'])['signed_size'].sum().reset_index()
    addr_bin['capped'] = addr_bin['signed_size'].clip(-config.FLOW_CAP_USD, config.FLOW_CAP_USD)

    # Aggregate per bin
    flow = addr_bin.groupby('bin').agg(
        capped_flow=('capped', 'sum'),
        unique_whales=('address', 'nunique'),
    ).reset_index()

    flow.columns = ['timestamp', 'capped_flow', 'unique_whales']
    return flow


def _get_btc_change(conn) -> float:
    """
    Get BTC 3-hour price change (%) from snapshots.
    Returns the percentage change over the last 6 x 30min bars.
    """
    query = """
        SELECT timestamp, price
        FROM snapshots
        WHERE symbol = 'BTC'
          AND timestamp > datetime('now', '-4 hours')
        ORDER BY timestamp
    """
    df = pd.read_sql_query(query, conn)

    if df.empty or len(df) < 2:
        log.warning("Not enough BTC data for 3h change")
        return 0.0

    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)

    current_price = df['price'].iloc[-1]

    # Find price ~3 hours ago
    target_time = df['timestamp'].iloc[-1] - timedelta(hours=3)
    past_df = df[df['timestamp'] <= target_time]

    if past_df.empty:
        # Use oldest available
        past_price = df['price'].iloc[0]
    else:
        past_price = past_df['price'].iloc[-1]

    if past_price <= 0:
        return 0.0

    return (current_price - past_price) / past_price * 100

def _get_orderbook_flow(conn, symbol: str) -> dict:
    """
    Get net aggressor flow from orderbook_snapshots for the last 30 minutes.
    Returns dict with 'flow', 'count', 'latest_time', 'stale'.
    """
    try:
        query = """
            SELECT snapshot_time, net_aggressor_flow
            FROM orderbook_snapshots
            WHERE coin = ?
              AND snapshot_time > datetime('now', ?)
            ORDER BY snapshot_time DESC
        """
        lookback = f"-{config.OB_FLOW_LOOKBACK_MINUTES} minutes"
        df = pd.read_sql_query(query, conn, params=(symbol, lookback))

        if df.empty:
            return {'flow': 0, 'count': 0, 'latest_time': None, 'stale': True}

        df['snapshot_time'] = pd.to_datetime(df['snapshot_time'], utc=True)
        latest = df['snapshot_time'].max()
        now = datetime.now(timezone.utc)
        stale = (now - latest).total_seconds() / 60 > config.OB_FLOW_STALE_MINUTES

        return {
            'flow': float(df['net_aggressor_flow'].sum()),
            'count': len(df),
            'latest_time': latest.isoformat(),
            'stale': stale,
        }
    except Exception as e:
        log.warning(f"Orderbook flow query error: {e}")
        return {'flow': 0, 'count': 0, 'latest_time': None, 'stale': True}


def get_signal() -> dict:
    """
    Main signal function. Called every candle close (30min).

    Returns:
        dict with keys:
            - symbol: 'HYPE'
            - signal_type: 'CONTRA' | 'BASELINE' | 'NONE'
            - vi: float (vol_imbalance)
            - rsi: float
            - capped_flow: float
            - unique_whales: int
            - btc_3h_change: float
            - price: float
            - entry: bool
            - reason: str
    """
    try:
        conn = get_connection()

        # 1. Build HYPE candles with indicators
        candles = _build_candles(conn, config.SYMBOL, lookback_hours=30)
        if candles.empty or len(candles) < config.RSI_PERIOD + 5:
            conn.close()
            return _no_signal("Not enough candle data")

        candles = _add_indicators(candles)

        # Get latest candle
        latest = candles.iloc[-1]
        vi = latest.get('vol_imbalance', 0)
        rsi = latest.get('rsi', 50)
        price = latest.get('close', 0)

        if pd.isna(rsi) or pd.isna(vi) or price <= 0:
            conn.close()
            return _no_signal("Indicators not ready (NaN)")

        # 2. Get current capped flow
        flow_df = _build_capped_flow(conn, config.SYMBOL)

        if flow_df.empty:
            capped_flow = 0.0
            unique_whales = 0
        else:
            # Get flow for the latest 30min bin
            latest_time = candles['timestamp'].iloc[-1]
            flow_match = flow_df[flow_df['timestamp'] == latest_time]

            if flow_match.empty:
                # Try the most recent flow bin
                capped_flow = flow_df['capped_flow'].iloc[-1]
                unique_whales = int(flow_df['unique_whales'].iloc[-1])
            else:
                capped_flow = flow_match['capped_flow'].iloc[0]
                unique_whales = int(flow_match['unique_whales'].iloc[0])

        # 3. Get BTC 3h change
        btc_3h = _get_btc_change(conn)



        # 4. Evaluate entry conditions
        base_signal = {
            'symbol': config.SYMBOL,
            'vi': round(float(vi), 3),
            'rsi': round(float(rsi), 1),
            'capped_flow': round(float(capped_flow), 0),
            'unique_whales': unique_whales,
            'btc_3h_change': round(btc_3h, 2),
            'price': round(float(price), 4),
            'atr_pct': round(float(latest.get('atr_pct', 0)), 2) if pd.notna(latest.get('atr_pct')) else 0,
        }

        # Check base conditions: VI > 0.3 AND RSI < 40
        if vi <= config.VI_THRESHOLD:
            return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                    'reason': f'VI too low ({vi:.3f} <= {config.VI_THRESHOLD})'}

        if rsi >= config.RSI_THRESHOLD:
            return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                    'reason': f'RSI too high ({rsi:.1f} >= {config.RSI_THRESHOLD})'}
        # ── ORDERBOOK AGGRESSOR FLOW FILTER ──
        if config.OB_FLOW_ENABLED:
            ob = _get_orderbook_flow(conn, config.SYMBOL)
            base_signal['ob_flow'] = round(ob['flow'], 0)
            base_signal['ob_snapshots'] = ob['count']

            if ob['stale']:
                log.warning(f"Orderbook data stale (latest: {ob['latest_time']}), skipping filter")
            elif ob['flow'] < 0:
                conn.close()
                return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                        'reason': f"OB flow negative ({ob['flow']:.0f} over {ob['count']} snapshots)"}

        conn.close()

        # Base conditions met — check flow for signal type
        if capped_flow < 0:
            # CONTRARIAN: flow negative + VI/RSI met → enter regardless of BTC
            return {**base_signal, 'signal_type': 'CONTRA', 'entry': True,
                    'reason': f'CONTRA: VI={vi:.3f} RSI={rsi:.1f} flow={capped_flow:.0f} whales={unique_whales}'}

        else:
            # BASELINE: flow >= 0, need BTC gate
            if btc_3h > config.BTC_3H_CHANGE_MIN:
                return {**base_signal, 'signal_type': 'BASELINE', 'entry': True,
                        'reason': f'BASELINE: VI={vi:.3f} RSI={rsi:.1f} BTC_3h={btc_3h:+.2f}%'}
            else:
                return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                        'reason': f'BTC risk-off (3h={btc_3h:+.2f}% <= {config.BTC_3H_CHANGE_MIN}%)'}

    except Exception as e:
        log.error(f"Signal error: {e}", exc_info=True)
        return _no_signal(f"Error: {e}")


def _no_signal(reason: str) -> dict:
    return {
        'symbol': config.SYMBOL,
        'signal_type': 'NONE',
        'vi': 0, 'rsi': 50, 'capped_flow': 0,
        'unique_whales': 0, 'btc_3h_change': 0,
        'price': 0, 'atr_pct': 0,
        'entry': False,
        'reason': reason,
    }