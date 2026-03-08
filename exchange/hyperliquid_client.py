"""
HyperLiquid Exchange Client
============================

Trading client integrating the official HyperLiquid SDK with L2 order book
streaming capabilities.

What It Does
------------
- Execute orders (market/limit) with validation
- Query account balance and open positions
- Stream real-time L2 order book data via WebSocket
- Fetch historical candle data
- Comprehensive error handling and logging

HyperLiquid Account Quirk
--------------------------
Due to HyperLiquid's API architecture, the trading wallet (derived from
private_key) cannot directly query its own balance. Instead, balance and
position queries must target the main account address where trades are
actually executed.

Configuration requires:
- private_key: Trading wallet that signs and submits orders
- balance_override['main_account_address']: Main account to query for balance/positions

Without the main_account_address, balance and position queries will fail.

Core Features
-------------
**Dual Info Client Architecture**:
    Maintains two Info instances to prevent WebSocket conflicts:
    - `self.info`: REST API calls (WebSocket disabled)
    - `self.l2_info`: L2 streaming only (WebSocket enabled)

**L2 Order Book Streaming**:
    Subscribes to real-time L2 data via WebSocket and forwards updates
    to a connected imbalance manager for order flow analysis.

Usage Example
-------------
    from hyperliquid_client import HyperLiquidClient

    # Initialize (reads from config loaded via ConfigLoader)
    client = HyperLiquidClient(
        private_key=config['private_key'],  # Trading wallet
        testnet=config['testnet'],
        config={
            'balance_override': {
                'main_account_address': config['balance_override']['main_account_address']
            }
        }
    )

    # Trading operations
    balance = client.get_balance()  # Queries main account
    response = client.place_order(order_request)  # Signs with trading wallet
    positions = client.get_open_positions()  # Queries main account

    # L2 data streaming
    client.connect_to_imbalance_manager(imbalance_mgr, ["BTC", "ETH"])
    stats = client.get_l2_stats()
    client.stop_websocket_streams()

Dependencies
------------
- hyperliquid-python-sdk: Official SDK
- eth_account: Ethereum wallet management
- exchange.models: OrderRequest, OrderResponse, Candle, Contract

Author: Paul De Beuckelaer
License: MIT
"""

import logging
import time
import threading
import json
from typing import Dict, Any, Optional, List, Callable
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account import Account
from exchange.models import (
    OrderRequest, OrderResponse, OrderResult, OrderStatus,
    OrderType, OrderSide, Position, Balance, Contract,
    Candle, Order, Trade
)

logger = logging.getLogger(__name__)


class HyperLiquidClient:
    """
    Trading client for HyperLiquid exchange with integrated L2 order book streaming.
    """

    def __init__(self, private_key: str, testnet: bool = False, config: Dict = None):
        self.config = config or {}
        self.balance_override = self.config.get('balance_override', {})
        self.testnet = testnet

        # Create wallet
        if private_key.startswith('0x'):
            private_key = private_key[2:]
        self.wallet = Account.from_key(private_key)
        self._address = self.wallet.address

        # Initialize SDK for trading
        main_account = self.balance_override.get('main_account_address')
        self.exchange = Exchange(
            wallet=self.wallet,
            base_url="https://api.hyperliquid-testnet.xyz" if testnet else None,
        )

        # Initialize Info client for market data (no WebSocket initially)
        self.info = Info(
            base_url="https://api.hyperliquid-testnet.xyz" if testnet else None,
            skip_ws=True
        )

        # L2 WEBSOCKET STATE
        self.l2_ws_active = False
        self.l2_ws_thread = None
        self.l2_info = None  # Separate Info client for WebSocket
        self.imbalance_manager = None
        self.subscribed_symbols = set()

        # L2 Statistics
        self.l2_stats = {
            'messages_received': 0,
            'orderbook_updates': 0,
            'last_update': None,
            'errors': 0,
            'active': False
        }

        # === DEBUG: Log all addresses on init ===
        logger.info("=" * 70)
        logger.info("HYPERLIQUID CLIENT INITIALIZATION - DEBUG INFO")
        logger.info("=" * 70)
        logger.info(f"WALLET ADDRESS (from private key):  {self._address}")
        logger.info(f"MAIN ACCOUNT ADDRESS (from config): {main_account or 'NOT SET!'}")
        logger.info(f"TESTNET MODE:                       {self.testnet}")
        logger.info(f"API BASE URL:                       {'https://api.hyperliquid-testnet.xyz' if testnet else 'https://api.hyperliquid.xyz'}")

        # Check if addresses match
        if main_account:
            if main_account.lower() == self._address.lower():
                logger.info("ADDRESS CHECK: Wallet and main account are THE SAME")
            else:
                logger.warning("ADDRESS CHECK: Wallet and main account are DIFFERENT!")
                logger.warning(f"  -> Orders will be signed by:    {self._address}")
                logger.warning(f"  -> Balance queries will check:  {main_account}")
        else:
            logger.warning("ADDRESS CHECK: No main_account_address configured!")
            logger.warning("  -> Balance and position queries may fail")
        logger.info("=" * 70)

    # CORE TRADING METHODS

    def get_balance(self) -> Balance:
        """Get account balance with debug logging."""
        try:
            main_account_address = self.balance_override.get('main_account_address')

            # === DEBUG ===
            logger.info("-" * 50)
            logger.info("GET_BALANCE DEBUG")
            logger.info(f"Querying address: {main_account_address or 'NONE - WILL FAIL'}")

            if not main_account_address:
                raise ValueError("Main account address required")

            # Make the API call
            logger.info(f"Calling info.user_state({main_account_address})...")
            main_account_info = self.info.user_state(main_account_address)

            # === DEBUG: Log raw response ===
            logger.info(f"RAW API RESPONSE:")
            logger.info(json.dumps(main_account_info, indent=2, default=str))

            margin_summary = main_account_info.get('marginSummary', {})
            logger.info(f"MARGIN SUMMARY: {json.dumps(margin_summary, indent=2)}")

            balance = Balance(margin_summary)
            logger.info(f"PARSED BALANCE: account_value=${balance.account_value:,.2f}, available=${balance.available:,.2f}")
            logger.info("-" * 50)

            return balance

        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return Balance({})  # Return zero balance on error

    def place_order(self, order_request: OrderRequest) -> OrderResponse:
        """Place order with comprehensive debug logging."""
        try:
            # === DEBUG: Log order attempt ===
            logger.info("=" * 70)
            logger.info("PLACE_ORDER DEBUG")
            logger.info("=" * 70)
            logger.info(f"Order Request: {order_request}")
            logger.info(f"Signing wallet address: {self._address}")
            logger.info(f"Exchange wallet address: {self.exchange.wallet.address if hasattr(self.exchange, 'wallet') else 'N/A'}")

            # Check Exchange object configuration
            if hasattr(self.exchange, 'account_address'):
                logger.info(f"Exchange account_address: {self.exchange.account_address}")
            if hasattr(self.exchange, 'vault_address'):
                logger.info(f"Exchange vault_address: {self.exchange.vault_address}")

            # Get contract info
            contract = self._get_contract(order_request.symbol)
            if not contract:
                logger.error(f"FAILED: Unknown symbol: {order_request.symbol}")
                return OrderResponse.error_response(f"Unknown symbol: {order_request.symbol}")

            logger.info(f"Contract found: {order_request.symbol}, tick_size={contract.tick_size}, lot_size={contract.lot_size}")

            # Get current price
            current_price = self._get_current_market_price(order_request.symbol)
            if not current_price:
                logger.error(f"FAILED: Could not get price for {order_request.symbol}")
                return OrderResponse.error_response(f"Could not get price for {order_request.symbol}")

            logger.info(f"Current market price: ${current_price}")

            # Convert to API parameters
            api_params = order_request.to_hyperliquid_params(contract, current_price)

            logger.info(f"API PARAMETERS:")
            logger.info(f"  symbol:     {order_request.symbol}")
            logger.info(f"  is_buy:     {api_params['is_buy']}")
            logger.info(f"  size:       {api_params['size']}")
            logger.info(f"  limit_px:   {api_params['limit_px']}")
            logger.info(f"  order_type: {api_params['order_type_param']}")

            # Execute order
            logger.info("Calling exchange.order()...")
            result = self.exchange.order(
                order_request.symbol,
                api_params['is_buy'],
                api_params['size'],
                api_params['limit_px'],
                api_params['order_type_param']
            )

            # === DEBUG: Log raw response ===
            logger.info(f"RAW SDK RESPONSE:")
            logger.info(json.dumps(result, indent=2, default=str))

            # Check for errors in response
            if isinstance(result, dict):
                if result.get('status') == 'err':
                    error_msg = result.get('response', 'Unknown error')
                    logger.error(f"ORDER FAILED: {error_msg}")
                    return OrderResponse.error_response(error_msg)

                if 'response' in result and isinstance(result['response'], dict):
                    response_data = result['response']
                    if 'data' in response_data and 'statuses' in response_data['data']:
                        statuses = response_data['data']['statuses']
                        for status in statuses:
                            if 'error' in status:
                                logger.error(f"ORDER ERROR in status: {status['error']}")

            logger.info("=" * 70)

            if result:
                order_result = OrderResult.from_hyperliquid_response(result, order_request)
                logger.info(f"Order placed successfully: {order_result}")
                return OrderResponse.success_response(order_result)
            else:
                logger.error("API call returned None/empty")
                return OrderResponse.error_response("API call failed")

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return OrderResponse.error_response(str(e))

    def cancel_all_orders(self, symbol: str = None) -> bool:
        """Cancel all open orders for one symbol or all symbols."""
        try:
            if symbol:
                result = self.exchange.cancel(symbol, None)
                logger.info("Cancelled all orders for symbol: %s", symbol)
            else:
                # Get all open orders and cancel by symbol
                open_orders = self.get_open_orders()
                symbols_with_orders = set(order.get('coin') for order in open_orders if order.get('coin'))

                for coin in symbols_with_orders:
                    self.exchange.cancel(coin, None)
                    logger.debug("Cancelled orders for symbol: %s", coin)

                logger.info("Cancelled all orders for %d symbols", len(symbols_with_orders))

            return True
        except Exception as e:
            logger.error("Failed to cancel orders: %s", e)
            return False

    def get_open_positions(self) -> List[Position]:
        """Get all non-zero open positions with debug logging."""
        try:
            main_account_address = self.balance_override.get('main_account_address')

            # === DEBUG ===
            logger.info("-" * 50)
            logger.info("GET_OPEN_POSITIONS DEBUG")
            logger.info(f"Querying address: {main_account_address or 'NONE'}")

            if not main_account_address:
                logger.warning("No main account address configured")
                return []

            account_info = self.info.user_state(main_account_address)

            # === DEBUG: Log raw response ===
            logger.info(f"RAW POSITIONS DATA:")
            raw_positions = account_info.get('assetPositions', [])
            logger.info(json.dumps(raw_positions, indent=2, default=str))

            positions = []
            for pos_data in raw_positions:
                if isinstance(pos_data, dict) and 'position' in pos_data:
                    position_info = pos_data['position']
                    size = float(position_info.get('szi', 0))

                    if abs(size) > 1e-8:  # Only non-zero positions
                        positions.append(Position(position_info))

            logger.info(f"Found {len(positions)} open positions")
            logger.info("-" * 50)
            return positions

        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def get_open_orders(self) -> List[Order]:
        """Get all open orders for the account with debug logging."""
        try:
            main_account_address = self.balance_override.get('main_account_address')
            account_address = main_account_address if main_account_address else self._address

            # === DEBUG ===
            logger.info("-" * 50)
            logger.info("GET_OPEN_ORDERS DEBUG")
            logger.info(f"Querying address: {account_address}")

            orders_data = self.info.open_orders(account_address)

            # === DEBUG ===
            logger.info(f"RAW ORDERS DATA:")
            logger.info(json.dumps(orders_data, indent=2, default=str))

            if not orders_data:
                logger.debug("No open orders found")
                return []

            orders = [Order(order_info) for order_info in orders_data]
            logger.info(f"Found {len(orders)} open orders")
            logger.info("-" * 50)
            return orders

        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    # === NEW DEBUG METHOD ===
    def debug_account_state(self) -> Dict:
        """
        Comprehensive debug method to check account state and diagnose issues.
        Call this when you can't place orders to see what's happening.

        Returns
        -------
        Dict with all debug information
        """
        debug_info = {
            'wallet_address': self._address,
            'main_account_address': self.balance_override.get('main_account_address'),
            'testnet': self.testnet,
            'api_responses': {}
        }

        logger.info("=" * 70)
        logger.info("COMPREHENSIVE ACCOUNT DEBUG")
        logger.info("=" * 70)

        # 1. Check wallet address state
        logger.info(f"\n1. WALLET ADDRESS: {self._address}")
        try:
            wallet_state = self.info.user_state(self._address)
            debug_info['api_responses']['wallet_state'] = wallet_state
            logger.info(f"   Wallet state response:")
            logger.info(json.dumps(wallet_state, indent=4, default=str))
        except Exception as e:
            logger.error(f"   ERROR querying wallet state: {e}")
            debug_info['api_responses']['wallet_state_error'] = str(e)

        # 2. Check main account address state (if different)
        main_addr = self.balance_override.get('main_account_address')
        if main_addr and main_addr.lower() != self._address.lower():
            logger.info(f"\n2. MAIN ACCOUNT ADDRESS: {main_addr}")
            try:
                main_state = self.info.user_state(main_addr)
                debug_info['api_responses']['main_account_state'] = main_state
                logger.info(f"   Main account state response:")
                logger.info(json.dumps(main_state, indent=4, default=str))
            except Exception as e:
                logger.error(f"   ERROR querying main account state: {e}")
                debug_info['api_responses']['main_account_state_error'] = str(e)

        # 3. Check if wallet is an authorized agent
        logger.info(f"\n3. CHECKING AGENT/API WALLET STATUS")
        # The exchange object might have info about vault/agent setup
        if hasattr(self.exchange, 'account_address'):
            logger.info(f"   Exchange.account_address: {self.exchange.account_address}")
            debug_info['exchange_account_address'] = self.exchange.account_address
        if hasattr(self.exchange, 'vault_address'):
            logger.info(f"   Exchange.vault_address: {self.exchange.vault_address}")
            debug_info['exchange_vault_address'] = self.exchange.vault_address

        # 4. Try to get clearinghouse state for more details
        logger.info(f"\n4. CLEARINGHOUSE STATE")
        for addr_name, addr in [('wallet', self._address), ('main_account', main_addr)]:
            if addr:
                try:
                    # Try clearinghouse state endpoint
                    ch_state = self.info.user_state(addr)
                    withdrawable = ch_state.get('withdrawable', 'N/A')
                    margin_summary = ch_state.get('marginSummary', {})
                    account_value = margin_summary.get('accountValue', 'N/A')

                    logger.info(f"   {addr_name} ({addr}):")
                    logger.info(f"      Account Value: {account_value}")
                    logger.info(f"      Withdrawable: {withdrawable}")

                    # Check cross margin summary
                    if 'crossMarginSummary' in ch_state:
                        cross = ch_state['crossMarginSummary']
                        logger.info(f"      Cross Margin Summary: {json.dumps(cross, indent=8)}")

                except Exception as e:
                    logger.error(f"   ERROR getting clearinghouse state for {addr_name}: {e}")

        # 5. Check meta info (exchange is working)
        logger.info(f"\n5. EXCHANGE META (verify API connectivity)")
        try:
            meta = self.info.meta()
            logger.info(f"   Connected to exchange, {len(meta.get('universe', []))} contracts available")
            debug_info['meta_ok'] = True
        except Exception as e:
            logger.error(f"   ERROR getting meta: {e}")
            debug_info['meta_ok'] = False
            debug_info['meta_error'] = str(e)

        logger.info("=" * 70)
        logger.info("END DEBUG")
        logger.info("=" * 70)

        return debug_info

    # MARKET DATA METHODS

    def get_market_data(self, symbol: str) -> Dict[str, Any]:
        """Get L2 order book snapshot for a symbol."""
        try:
            data = self.info.l2_snapshot(symbol)
            return data if data else {}
        except Exception as e:
            logger.error(f"Failed to get market data for {symbol}: {e}")
            return {}

    def get_all_mids(self) -> Dict[str, float]:
        """Get mid prices for all available trading symbols."""
        try:
            mids = self.info.all_mids()
            return mids if mids else {}
        except Exception as e:
            logger.error(f"Failed to get all mids: {e}")
            return {}

    def get_historical_candles(self, symbol: str, timeframe: str = "1h",
                               start_time: int = None, end_time: int = None) -> List[Candle]:
        """Get historical OHLCV candle data."""
        try:
            # Convert timeframe
            interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
            interval = interval_map.get(timeframe.lower(), "1h")

            # Calculate time range if not provided
            if end_time is None:
                end_time = int(time.time() * 1000)
            if start_time is None:
                interval_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
                minutes_back = interval_minutes.get(interval, 60) * 100
                start_time = end_time - (minutes_back * 60 * 1000)

            # Get raw candles
            raw_candles = self.info.candles_snapshot(symbol, interval, start_time, end_time)

            # Convert to Candle objects
            candles = [Candle(candle_data, timeframe) for candle_data in raw_candles or []]
            logger.debug(f"Retrieved {len(candles)} candles for {symbol} ({timeframe})")
            return candles

        except Exception as e:
            logger.error(f"Failed to get candles for {symbol}: {e}")
            return []

    # L2 WEBSOCKET INTEGRATION

    def connect_to_imbalance_manager(self, imbalance_manager, symbols: List[str]) -> bool:
        """Connect L2 order book data feed to an imbalance manager via WebSocket."""
        try:
            logger.info(f"L2: Connecting imbalance manager for symbols: {symbols}")

            self.imbalance_manager = imbalance_manager
            self.subscribed_symbols = set(symbols)

            if self._start_l2_websocket():
                logger.info("L2: WebSocket connection established")
                return True
            else:
                logger.error("L2: WebSocket connection failed")
                return False

        except Exception as e:
            logger.error(f"L2: Failed to connect imbalance manager: {e}")
            return False

    def _start_l2_websocket(self) -> bool:
        """Start L2 WebSocket connection"""
        try:
            # Create separate Info client for WebSocket
            self.l2_info = Info(
                base_url="https://api.hyperliquid-testnet.xyz" if self.testnet else None,
                skip_ws=False
            )

            # Wait for WebSocket to be ready
            time.sleep(2)

            if not hasattr(self.l2_info, 'ws_manager') or not self.l2_info.ws_manager:
                logger.warning("L2: WebSocket manager not available")
                return False

            # Subscribe to L2 data for each symbol
            subscription_count = 0
            for symbol in self.subscribed_symbols:
                try:
                    callback = self._create_l2_callback(symbol)
                    subscription_id = self.l2_info.ws_manager.subscribe(
                        {"type": "l2Book", "coin": symbol},
                        callback
                    )
                    if subscription_id:
                        subscription_count += 1
                        logger.debug(f"L2: Subscribed to {symbol}")
                except Exception as e:
                    logger.warning(f"L2: Failed to subscribe to {symbol}: {e}")

            if subscription_count > 0:
                self.l2_ws_active = True
                self.l2_stats['active'] = True
                logger.info(f"L2: WebSocket active with {subscription_count} subscriptions")
                return True

            return False

        except Exception as e:
            logger.error(f"L2: WebSocket start failed: {e}")
            return False

    def _create_l2_callback(self, symbol: str) -> Callable:
        """Create callback function for L2 WebSocket data"""

        def callback(message):
            try:
                self.l2_stats['messages_received'] += 1

                if isinstance(message, dict) and 'data' in message:
                    data = message['data']

                    if 'levels' in data and 'coin' in data:
                        converted_data = self._convert_l2_data(data)

                        if self.imbalance_manager and converted_data:
                            self.imbalance_manager.update_order_book(symbol, converted_data)
                            self.l2_stats['orderbook_updates'] += 1
                            self.l2_stats['last_update'] = time.time()

            except Exception as e:
                logger.debug(f"L2: Callback error for {symbol}: {e}")
                self.l2_stats['errors'] += 1

        return callback

    def _convert_l2_data(self, hyperliquid_data: Dict) -> Dict:
        """Convert HyperLiquid L2 format to imbalance manager format"""
        try:
            levels = hyperliquid_data.get('levels', [])
            if len(levels) < 2:
                return {}

            bids = levels[0] or []
            asks = levels[1] or []

            return {
                'levels': [asks, bids],  # asks first, then bids
                'symbol': hyperliquid_data.get('coin', ''),
                'timestamp': hyperliquid_data.get('time', int(time.time() * 1000))
            }

        except Exception as e:
            logger.error(f"L2: Data conversion error: {e}")
            return {}

    def stop_websocket_streams(self):
        """Stop L2 WebSocket order book streams."""
        try:
            logger.info("L2: Stopping WebSocket streams...")

            self.l2_ws_active = False
            self.l2_stats['active'] = False

            # Stop WebSocket subscriptions
            if hasattr(self, 'l2_info') and hasattr(self.l2_info, 'ws_manager'):
                try:
                    for symbol in self.subscribed_symbols:
                        try:
                            if hasattr(self.l2_info.ws_manager, 'unsubscribe'):
                                self.l2_info.ws_manager.unsubscribe({"type": "l2Book", "coin": symbol})
                        except:
                            pass
                except:
                    pass

            logger.info("L2: WebSocket streams stopped")

        except Exception as e:
            logger.error(f"L2: Error stopping streams: {e}")

    def get_l2_stats(self) -> Dict:
        """Get L2 WebSocket feed statistics and health metrics."""
        return {
            'active': self.l2_stats['active'],
            'subscribed_symbols': list(self.subscribed_symbols),
            'messages_received': self.l2_stats['messages_received'],
            'orderbook_updates': self.l2_stats['orderbook_updates'],
            'last_update': self.l2_stats['last_update'],
            'errors': self.l2_stats['errors']
        }

    # HELPER METHODS

    def _get_contract(self, symbol: str) -> Optional[Contract]:
        """Get contract specification for a symbol."""
        try:
            meta = self.info.meta()
            for contract_data in meta.get('universe', []):
                if contract_data.get('name') == symbol:
                    return Contract(contract_data)
            return None
        except Exception as e:
            logger.error(f"Failed to get contract for {symbol}: {e}")
            return None

    def _get_current_market_price(self, symbol: str) -> Optional[float]:
        """Get current market price with automatic fallback."""
        try:
            # Try fast mid price lookup first
            mids = self.get_all_mids()
            if symbol in mids:
                price = float(mids[symbol])
                if price > 0:
                    return price

            # Fallback to orderbook mid calculation
            book = self.get_market_data(symbol)
            levels = book.get('levels', [])
            if len(levels) >= 2 and levels[0] and levels[1]:
                bid = float(levels[0][0][0]) if levels[0] and levels[0][0] else 0.0
                ask = float(levels[1][0][0]) if levels[1] and levels[1][0] else 0.0

                if bid > 0 and ask > 0:
                    return (bid + ask) / 2

            return None

        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None