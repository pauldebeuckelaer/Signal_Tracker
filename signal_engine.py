"""
Multi-Coin Signal Tracker — Signal Engine
===========================================
Two strategy types:

  "full"   (HYPE):  VI + RSI + contrarian flow + BTC gate + OB filter
  "simple" (others): RSI + BTC gate only

All strategies share:
  - 30min candles built from twap.db snapshots
  - RSI calculation (period configurable per coin)
  - BTC 3h risk-off gate
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


# ─────────────────────────────────────────────
# CANDLE CONSTRUCTION
# ─────────────────────────────────────────────

def _build_candles(conn, symbol: str, lookback_hours: int = 30) -> pd.DataFrame:
    """
    Build 30-minute OHLCV candles from snapshots table (whale tracker).
    Includes buy_volume, sell_volume for vol_imbalance calculation.
    Used for HYPE (full strategy) where VI is needed.
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

    candles = candles[candles['tick_count'] >= config.SNAPSHOT_MIN_TICKS]

    return candles.reset_index()


def _build_candles_market(conn, symbol: str, lookback_hours: int = 30) -> pd.DataFrame:
    """
    Build 30-minute OHLC candles from market_snapshots table.
    Used for simple strategy coins (VVV, NEAR, PURR) where VI is not needed.
    market_snapshots has 1-min resolution for ALL coins — always fresh.
    """
    query = """
        SELECT snapshot_time, mark_px
        FROM market_snapshots
        WHERE coin = ?
          AND snapshot_time > datetime('now', ?)
        ORDER BY snapshot_time
    """
    df = pd.read_sql_query(query, conn, params=(symbol, f"-{lookback_hours} hours"))

    if df.empty:
        log.warning(f"No market_snapshot data for {symbol} in last {lookback_hours}h")
        return pd.DataFrame()

    df['snapshot_time'] = pd.to_datetime(df['snapshot_time'], utc=True)
    df = df.set_index('snapshot_time')

    candles = df.resample('30min').agg(
        open=('mark_px', 'first'),
        high=('mark_px', 'max'),
        low=('mark_px', 'min'),
        close=('mark_px', 'last'),
        tick_count=('mark_px', 'count'),
    ).dropna(subset=['close'])

    # At 1-min snapshots, expect ~30 ticks per 30-min candle
    candles = candles[candles['tick_count'] >= config.SNAPSHOT_MIN_TICKS]

    return candles.reset_index().rename(columns={'snapshot_time': 'timestamp'})


def _add_indicators(candles: pd.DataFrame, rsi_period: int = 14) -> pd.DataFrame:
    """Add RSI and vol_imbalance to candle dataframe."""
    d = candles.copy()

    if len(d) < rsi_period + 5:
        log.warning(f"Not enough candles for indicators: {len(d)}")
        return d

    # Vol imbalance (only if volume data available — snapshots table, not market_snapshots)
    if 'buy_volume' in d.columns and 'sell_volume' in d.columns:
        d['vol_imbalance'] = (d['buy_volume'] - d['sell_volume']) / (d['buy_volume'] + d['sell_volume'] + 1)
    else:
        d['vol_imbalance'] = 0.0

    # RSI
    delta = d['close'].diff()
    gain = delta.clip(lower=0).rolling(rsi_period).mean()
    loss = (-delta.clip(upper=0)).rolling(rsi_period).mean()
    rs = gain / loss
    d['rsi'] = 100 - (100 / (1 + rs))

    # ATR for reference
    h_l = d['high'] - d['low']
    h_pc = (d['high'] - d['close'].shift(1)).abs()
    l_pc = (d['low'] - d['close'].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    d['atr'] = tr.rolling(14).mean()
    d['atr_pct'] = d['atr'] / d['close'] * 100

    return d


# ─────────────────────────────────────────────
# FLOW CALCULATIONS (HYPE only)
# ─────────────────────────────────────────────

def _build_capped_flow(conn, symbol: str, coin_cfg: dict) -> pd.DataFrame:
    """
    Calculate capped whale flow from orders table.
    Returns a df with: timestamp, capped_flow, unique_whales
    """
    lookback_hours = coin_cfg.get('flow_lookback_hours', 6)
    flow_cap = coin_cfg.get('flow_cap_usd', 5000)

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

    addr_bin = df.groupby(['bin', 'address'])['signed_size'].sum().reset_index()
    addr_bin['capped'] = addr_bin['signed_size'].clip(-flow_cap, flow_cap)

    flow = addr_bin.groupby('bin').agg(
        capped_flow=('capped', 'sum'),
        unique_whales=('address', 'nunique'),
    ).reset_index()

    flow.columns = ['timestamp', 'capped_flow', 'unique_whales']
    return flow


def _get_orderbook_flow(conn, symbol: str, coin_cfg: dict) -> dict:
    """
    Get net aggressor flow from orderbook_snapshots for the last N minutes.
    """
    lookback = coin_cfg.get('ob_flow_lookback_minutes', 30)
    stale_minutes = coin_cfg.get('ob_flow_stale_minutes', 5)

    try:
        query = """
            SELECT snapshot_time, net_aggressor_flow
            FROM orderbook_snapshots
            WHERE coin = ?
              AND snapshot_time > datetime('now', ?)
            ORDER BY snapshot_time DESC
        """
        df = pd.read_sql_query(query, conn, params=(symbol, f"-{lookback} minutes"))

        if df.empty:
            return {'flow': 0, 'count': 0, 'latest_time': None, 'stale': True}

        df['snapshot_time'] = pd.to_datetime(df['snapshot_time'], utc=True)
        latest = df['snapshot_time'].max()
        now = datetime.now(timezone.utc)
        stale = (now - latest).total_seconds() / 60 > stale_minutes

        return {
            'flow': float(df['net_aggressor_flow'].sum()),
            'count': len(df),
            'latest_time': latest.isoformat(),
            'stale': stale,
        }
    except Exception as e:
        log.warning(f"Orderbook flow query error for {symbol}: {e}")
        return {'flow': 0, 'count': 0, 'latest_time': None, 'stale': True}


# ─────────────────────────────────────────────
# BTC GATE (shared)
# ─────────────────────────────────────────────

def _get_btc_change(conn) -> float:
    """
    Get BTC 3-hour price change (%) from market_snapshots.
    Uses market_snapshots (1-min, always fresh) instead of snapshots.
    """
    query = """
        SELECT snapshot_time, mark_px
        FROM market_snapshots
        WHERE coin = 'BTC'
          AND snapshot_time > datetime('now', '-4 hours')
        ORDER BY snapshot_time
    """
    df = pd.read_sql_query(query, conn)

    if df.empty or len(df) < 2:
        log.warning("Not enough BTC data for 3h change")
        return 0.0

    df['snapshot_time'] = pd.to_datetime(df['snapshot_time'], utc=True)

    current_price = df['mark_px'].iloc[-1]
    target_time = df['snapshot_time'].iloc[-1] - timedelta(hours=3)
    past_df = df[df['snapshot_time'] <= target_time]

    if past_df.empty:
        past_price = df['mark_px'].iloc[0]
    else:
        past_price = past_df['mark_px'].iloc[-1]

    if past_price <= 0:
        return 0.0

    return (current_price - past_price) / past_price * 100


# ─────────────────────────────────────────────
# MAIN SIGNAL FUNCTIONS
# ─────────────────────────────────────────────

def get_signal_for_coin(coin_cfg: dict) -> dict:
    """
    Evaluate signal for a single coin based on its config.

    Routes to:
      - _get_signal_full()   for strategy_type == "full"   (HYPE)
      - _get_signal_simple() for strategy_type == "simple"  (VVV, NEAR, PURR)
    """
    symbol = coin_cfg['symbol']
    strategy = coin_cfg.get('strategy_type', 'simple')

    try:
        conn = get_connection()

        # 1. Build candles — use snapshots for full strategy (has volume data),
        #    market_snapshots for simple strategy (always fresh, all coins)
        if strategy == "full":
            candles = _build_candles(conn, symbol, lookback_hours=30)
        else:
            candles = _build_candles_market(conn, symbol, lookback_hours=30)

        rsi_period = coin_cfg.get('rsi_period', 14)

        if candles.empty or len(candles) < rsi_period + 5:
            conn.close()
            return _no_signal(symbol, "Not enough candle data")

        candles = _add_indicators(candles, rsi_period=rsi_period)

        # Get latest candle
        latest = candles.iloc[-1]
        vi = latest.get('vol_imbalance', 0)
        rsi = latest.get('rsi', 50)
        price = latest.get('close', 0)

        if pd.isna(rsi) or price <= 0:
            conn.close()
            return _no_signal(symbol, "Indicators not ready (NaN)")

        # 2. BTC gate (shared across all strategies)
        btc_3h = _get_btc_change(conn)

        # 3. Build base signal dict
        base_signal = {
            'symbol': symbol,
            'vi': round(float(vi), 3) if pd.notna(vi) else 0,
            'rsi': round(float(rsi), 1),
            'capped_flow': 0,
            'unique_whales': 0,
            'btc_3h_change': round(btc_3h, 2),
            'price': round(float(price), 4),
            'atr_pct': round(float(latest.get('atr_pct', 0)), 2) if pd.notna(latest.get('atr_pct')) else 0,
        }

        # 4. Route to strategy-specific logic
        if strategy == "full":
            result = _evaluate_full(conn, candles, base_signal, coin_cfg)
        else:
            result = _evaluate_simple(base_signal, coin_cfg)

        conn.close()
        return result

    except Exception as e:
        log.error(f"Signal error for {symbol}: {e}", exc_info=True)
        return _no_signal(symbol, f"Error: {e}")


def _evaluate_full(conn, candles, base_signal: dict, coin_cfg: dict) -> dict:
    """
    Full strategy evaluation (HYPE).
    VI > threshold + RSI < threshold + contrarian flow / BTC gate + OB filter.
    """
    symbol = coin_cfg['symbol']
    vi = base_signal['vi']
    rsi = base_signal['rsi']
    vi_threshold = coin_cfg.get('vi_threshold', 0.3)
    rsi_threshold = coin_cfg.get('rsi_threshold', 40)

    # Check VI
    if vi <= vi_threshold:
        return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                'reason': f'VI too low ({vi:.3f} <= {vi_threshold})'}

    # Check RSI
    if rsi >= rsi_threshold:
        return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                'reason': f'RSI too high ({rsi:.1f} >= {rsi_threshold})'}

    # Get capped flow
    flow_df = _build_capped_flow(conn, symbol, coin_cfg)

    if flow_df.empty:
        capped_flow = 0.0
        unique_whales = 0
    else:
        latest_time = candles['timestamp'].iloc[-1]
        flow_match = flow_df[flow_df['timestamp'] == latest_time]

        if flow_match.empty:
            capped_flow = flow_df['capped_flow'].iloc[-1]
            unique_whales = int(flow_df['unique_whales'].iloc[-1])
        else:
            capped_flow = flow_match['capped_flow'].iloc[0]
            unique_whales = int(flow_match['unique_whales'].iloc[0])

    base_signal['capped_flow'] = round(float(capped_flow), 0)
    base_signal['unique_whales'] = unique_whales

    # Orderbook aggressor flow filter
    if coin_cfg.get('ob_flow_enabled', False):
        ob = _get_orderbook_flow(conn, symbol, coin_cfg)
        base_signal['ob_flow'] = round(ob['flow'], 0)
        base_signal['ob_snapshots'] = ob['count']

        if ob['stale']:
            log.warning(f"Orderbook data stale for {symbol} (latest: {ob['latest_time']}), skipping filter")
        elif ob['flow'] < 0:
            return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                    'reason': f"OB flow negative ({ob['flow']:.0f} over {ob['count']} snapshots)"}

    # Flow-based entry decision
    btc_3h = base_signal['btc_3h_change']

    if capped_flow < 0:
        # CONTRARIAN: negative flow + VI/RSI met → enter regardless of BTC
        return {**base_signal, 'signal_type': 'CONTRA', 'entry': True,
                'reason': f'CONTRA: VI={vi:.3f} RSI={rsi:.1f} flow={capped_flow:.0f} whales={unique_whales}'}
    else:
        # BASELINE: positive flow, need BTC gate
        if btc_3h > config.BTC_3H_CHANGE_MIN:
            return {**base_signal, 'signal_type': 'BASELINE', 'entry': True,
                    'reason': f'BASELINE: VI={vi:.3f} RSI={rsi:.1f} BTC_3h={btc_3h:+.2f}%'}
        else:
            return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                    'reason': f'BTC risk-off (3h={btc_3h:+.2f}% <= {config.BTC_3H_CHANGE_MIN}%)'}


def _evaluate_simple(base_signal: dict, coin_cfg: dict) -> dict:
    """
    Simple strategy evaluation (VVV, NEAR, PURR).
    RSI < threshold + BTC gate only.
    """
    symbol = coin_cfg['symbol']
    rsi = base_signal['rsi']
    rsi_threshold = coin_cfg.get('rsi_threshold', 40)
    btc_3h = base_signal['btc_3h_change']

    # Check RSI
    if rsi >= rsi_threshold:
        return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                'reason': f'RSI too high ({rsi:.1f} >= {rsi_threshold})'}

    # Check BTC gate
    if btc_3h <= config.BTC_3H_CHANGE_MIN:
        return {**base_signal, 'signal_type': 'NONE', 'entry': False,
                'reason': f'BTC risk-off (3h={btc_3h:+.2f}% <= {config.BTC_3H_CHANGE_MIN}%)'}

    # All conditions met
    return {**base_signal, 'signal_type': 'DIP', 'entry': True,
            'reason': f'DIP: RSI={rsi:.1f} BTC_3h={btc_3h:+.2f}%'}


# ─────────────────────────────────────────────
# LEGACY WRAPPER (backward compatibility)
# ─────────────────────────────────────────────

def get_signal() -> dict:
    """
    Legacy single-coin signal function.
    Calls get_signal_for_coin with HYPE config.
    """
    return get_signal_for_coin(config.COIN_CONFIGS["HYPE"])


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _no_signal(symbol: str, reason: str) -> dict:
    return {
        'symbol': symbol,
        'signal_type': 'NONE',
        'vi': 0, 'rsi': 50, 'capped_flow': 0,
        'unique_whales': 0, 'btc_3h_change': 0,
        'price': 0, 'atr_pct': 0,
        'entry': False,
        'reason': reason,
    }