"""
Exchange Models - HyperLiquid Trading Bot
==========================================

Type-safe data models for HyperLiquid exchange operations including orders,
positions, balances, and trade execution. Designed for robust error handling
and clean integration with the HyperLiquid SDK.

What's Included
---------------
**Order Management:**
- OrderRequest: Type-safe order creation with validation
- OrderResponse: Unified success/error response wrapper
- OrderResult: Order execution details (fills, status)
- Order: Open/resting orders on the book

**Position & Balance:**
- Position: Open position tracking with PnL
- Balance: Account balance with utilization metrics
- Trade: Completed trade execution records

**Market Data:**
- Candle: OHLCV candle data with technical properties
- Contract: Symbol specifications (tick size, lot size, leverage)

**Enums:**
- OrderType: MARKET, LIMIT
- OrderSide: BUY, SELL
- OrderStatus: NEW, FILLED, REJECTED, CANCELLED, etc.

Key Features
------------
**Validation on Construction:**
    All models validate their inputs on initialization, raising ValueError
    for invalid data. This prevents invalid orders from reaching the exchange.

**HyperLiquid Format Conversion:**
    Models handle conversion between HyperLiquid API formats and clean
    Python objects with properties and helper methods.

**Type Safety:**
    Using enums (OrderSide, OrderType, OrderStatus) instead of strings
    prevents typos and provides IDE autocomplete.

Usage Example
-------------
Create and validate an order:

    from exchange.models import OrderRequest, OrderSide

    # Create order (validates automatically)
    order = OrderRequest(
        symbol="BTC",
        side="buy",
        size=0.1,
        order_type="limit",
        price=45000.0
    )

    # Place order
    response = client.place_order(order)
    if response.success:
        print(f"Order ID: {response.order_result.order_id}")
        print(f"Status: {response.order_result.status.value}")

Work with positions:

    positions = client.get_open_positions()
    for pos in positions:
        print(f"{pos.symbol}: {pos.side} {pos.abs_size}")
        print(f"  PnL: ${pos.unrealized_pnl:,.2f} ({pos.pnl_percent:+.2f}%)")

        # Close if profitable
        if pos.is_profitable():
            close_order = OrderRequest(
                symbol=pos.symbol,
                side=pos.get_close_order_side(),
                order_type="market",
                size=pos.abs_size,
                reduce_only=True
            )

Check account balance:

    balance = client.get_balance()
    print(f"Account value: ${balance.account_value:,.2f}")
    print(f"Available: ${balance.available:,.2f}")
    print(f"Utilization: {balance.get_utilization_percent():.1f}%")

Author: Paul De Beuckelaer
License: MIT
"""

import datetime
import time
import logging
from typing import Optional, Dict, Any, List
from enum import Enum

logger = logging.getLogger(__name__)


def tick_to_decimals(tick_size: float) -> int:
    """
    Convert tick size to number of decimal places for price formatting.

    Analyzes the tick size to determine how many decimal places are needed
    to represent prices accurately. Used for formatting display prices.

    Parameters
    ----------
    tick_size : float
        The minimum price increment (e.g., 0.01, 0.1, 1.0)

    Returns
    -------
    int
        Number of decimal places needed (e.g., 0.01 → 2, 0.1 → 1, 1.0 → 0)

    Examples
    --------
    Calculate decimals for different tick sizes:

        decimals = tick_to_decimals(0.01)  # Returns 2
        decimals = tick_to_decimals(0.1)   # Returns 1
        decimals = tick_to_decimals(1.0)   # Returns 0

    Notes
    -----
    The function strips trailing zeros from the formatted tick size string
    to accurately count significant decimal places.
    """
    tick_size_str = "{0:.8f}".format(tick_size)
    while tick_size_str[-1] == "0":
        tick_size_str = tick_size_str[:-1]

    split_tick = tick_size_str.split(".")

    if len(split_tick) > 1:
        return len(split_tick[1])
    else:
        return 0

class OrderType(Enum):
    """Order execution types"""
    MARKET = "market"
    LIMIT = "limit"


class OrderSide(Enum):
    """Order direction types"""
    BUY = "buy"
    SELL = "sell"

    @classmethod
    def from_string(cls, side: str) -> 'OrderSide':
        """
        Create OrderSide from string representation

        Args:
            side: String representation of order side

        Returns:
            OrderSide enum value

        Raises:
            ValueError: If side string is invalid
        """
        side_lower = side.lower()
        if side_lower in ['buy', 'b']:
            return cls.BUY
        elif side_lower in ['sell', 's']:
            return cls.SELL
        else:
            raise ValueError(f"Invalid order side: {side}")

    @property
    def is_buy(self) -> bool:
        """Check if this is a buy order"""
        return self == OrderSide.BUY


class OrderStatus(Enum):
    """Order execution status types"""
    PENDING = "PENDING"
    NEW = "NEW"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class Candle:
    """
    OHLCV candle data structure for HyperLiquid

    Represents a single time period of market data including
    open, high, low, close, volume, and trade count.
    """

    def __init__(self, candle_info: Dict[str, Any], timeframe: str):
        """
        Initialize candle from HyperLiquid API response

        Expected candle_info structure:
        {
            "t": 1640995200000,  # timestamp (ms)
            "T": 1640995259999,  # close timestamp (ms)
            "o": "46000.0",      # open price
            "h": "46100.0",      # high price
            "l": "45900.0",      # low price
            "c": "46050.0",      # close price
            "v": "10.5",         # volume
            "n": 150             # number of trades
        }

        Args:
            candle_info: Raw candle data from API
            timeframe: Time period (e.g., "1h", "4h", "1d")
        """
        self.timestamp = candle_info['t']
        self.open = float(candle_info['o'])
        self.high = float(candle_info['h'])
        self.low = float(candle_info['l'])
        self.close = float(candle_info['c'])
        self.volume = float(candle_info['v'])
        self.trades = candle_info.get('n', 0)
        self.timeframe = timeframe

    @property
    def body_size(self) -> float:
        """Calculate candle body size (absolute difference between open and close)"""
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        """Calculate upper wick size"""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Calculate lower wick size"""
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        """Check if candle is bullish (close > open)"""
        return self.close > self.open

    def __str__(self):
        return f"Candle(O:{self.open} H:{self.high} L:{self.low} C:{self.close} V:{self.volume})"

    def __repr__(self):
        return self.__str__()


class Contract:
    """
    Trading contract specification for HyperLiquid perpetual futures

    Contains all necessary information for order validation and execution
    including precision, tick sizes, and leverage limits.
    """

    def __init__(self, contract_info: Dict[str, Any]):
        """
        Initialize contract from HyperLiquid meta response

        Expected contract_info structure:
        {
            "name": "BTC",
            "szDecimals": 5,
            "maxLeverage": 50,
            "onlyIsolated": false
        }

        Args:
            contract_info: Raw contract data from API
        """
        self.symbol = contract_info['name']
        self.base_asset = contract_info['name']
        self.quote_asset = "USD"  # HyperLiquid uses USD-margined perps

        # Decimals and precision
        self.quantity_decimals = contract_info.get('szDecimals', 5)
        self.price_decimals = self._get_price_decimals(self.symbol)

        # Calculate tick and lot sizes
        self.lot_size = 1 / pow(10, self.quantity_decimals)
        self.tick_size = self._get_tick_size(self.symbol)

        # HyperLiquid specific fields
        self.max_leverage = contract_info.get('maxLeverage', 50)
        self.only_isolated = contract_info.get('onlyIsolated', False)

    def _get_price_decimals(self, symbol: str) -> int:
        """Get price decimals based on HyperLiquid standards"""
        price_decimals_map = {
            'BTC': 0,  # $1 increments
            'ETH': 1,  # $0.1 increments
            'SOL': 2,  # $0.01 increments
            'ATOM': 3,  # $0.001 increments
            'APT': 2,  # $0.01 increments
            'HYPE': 2,  # $0.01 increments
        }
        return price_decimals_map.get(symbol, 2)  # Default to 2 decimals

    def _get_tick_size(self, symbol: str) -> float:
        """Get tick size based on HyperLiquid standards"""
        tick_sizes = {
            'BTC': 1.0,  # $1 increments
            'ETH': 0.1,  # $0.10 increments
            'SOL': 0.01,  # $0.01 increments
            'ATOM': 0.001,  # $0.001 increments
            'APT': 0.01,  # $0.01 increments
            'HYPE': 0.01,  # $0.01 increments
        }
        return tick_sizes.get(symbol, 0.01)  # Default to $0.01

    def round_size(self, size: float) -> float:
        """Round size to contract's step size"""
        return round(size / self.lot_size) * self.lot_size

    def round_price(self, price: float) -> float:
        """Round price to contract's tick size"""
        return round(price / self.tick_size) * self.tick_size

    def validate_size(self, size: float) -> bool:
        """Validate if size meets minimum requirements"""
        return size >= self.lot_size

    def validate_price(self, price: float) -> bool:
        """Validate if price meets minimum requirements"""
        return price > 0

    def __str__(self):
        return f"Contract({self.symbol}, qty_dec:{self.quantity_decimals}, px_dec:{self.price_decimals})"

    def __repr__(self):
        return self.__str__()


class OrderRequest:
    """
    Type-safe order request with built-in validation

    Represents a complete order request with validation logic
    for safe execution on HyperLiquid exchange.
    """

    def __init__(self, symbol: str, side: str, size: float,
                 order_type: str = 'market', price: Optional[float] = None,
                 reduce_only: bool = False):
        """
        Initialize order request with validation

        Args:
            symbol: Trading symbol (e.g., "BTC", "ETH")
            side: Order side ("buy" or "sell")
            size: Order size in base asset
            order_type: Order type ("market" or "limit")
            price: Limit price (required for limit orders)
            reduce_only: Whether this order only reduces position

        Raises:
            ValueError: If validation fails
        """
        # Convert and validate inputs
        self.symbol = self._validate_symbol(symbol)
        self.side = OrderSide.from_string(side)
        self.size = self._validate_size(size)
        self.order_type = OrderType(order_type.lower())
        self.price = price
        self.reduce_only = reduce_only

        # Validate order consistency
        self._validate_order()

    def _validate_symbol(self, symbol: str) -> str:
        """Validate and normalize symbol"""
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol must be a non-empty string")
        return symbol.upper()

    def _validate_size(self, size: float) -> float:
        """Validate order size"""
        if size <= 0:
            raise ValueError("Size must be positive")
        return float(size)

    def _validate_order(self):
        """Validate order consistency"""
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("Price required for limit orders")

        if self.price is not None and self.price <= 0:
            raise ValueError("Price must be positive")

    def to_hyperliquid_params(self, contract: Contract, current_price: float) -> Dict[str, Any]:
        """
        Convert to HyperLiquid API parameters with proper rounding

        Args:
            contract: Contract specification for rounding
            current_price: Current market price for market orders

        Returns:
            Dictionary of API parameters

        Raises:
            ValueError: If size is too small after rounding
        """
        # Round size to contract specifications
        adjusted_size = contract.round_size(self.size)
        if adjusted_size <= 0:
            raise ValueError(f"Size {self.size} too small for {contract.symbol} (min: {contract.lot_size})")

        # Calculate final price
        if self.order_type == OrderType.MARKET:
            # Market orders use aggressive pricing
            if self.reduce_only:
                # Conservative pricing for position closing
                raw_price = current_price * (1.005 if self.side.is_buy else 0.995)
                tif = "Gtc"  # Good till cancelled
            else:
                # Aggressive pricing for position opening
                raw_price = current_price * (1.10 if self.side.is_buy else 0.90)
                tif = "Ioc"  # Immediate or cancel
        else:
            # Limit orders use specified price
            raw_price = self.price
            tif = "Gtc"

        # Round price to contract specifications
        limit_px = contract.round_price(raw_price)

        return {
            'is_buy': self.side.is_buy,
            'size': adjusted_size,
            'limit_px': limit_px,
            'order_type_param': {"limit": {"tif": tif}},
            'reduce_only': self.reduce_only,
            'original_side': self.side.value,
            'tick_size': contract.tick_size,
            'request': self  # Keep reference to original request
        }

    def __str__(self):
        price_str = f" @ ${self.price}" if self.price else ""
        reduce_str = " (reduce)" if self.reduce_only else ""
        return f"OrderRequest({self.order_type.value.upper()} {self.side.value.upper()} {self.size} {self.symbol}{price_str}{reduce_str})"

    def __repr__(self):
        return self.__str__()


class OrderResult:
    """
    Result of an order placement operation

    Contains all relevant information about the executed order
    including fill details and execution status.
    """

    def __init__(self, order_id: str, symbol: str, side: OrderSide,
                 size: float, price: float, status: OrderStatus,
                 fill_price: Optional[float] = None, fill_size: Optional[float] = None):
        """
        Initialize order result

        Args:
            order_id: Unique order identifier
            symbol: Trading symbol
            side: Order side
            size: Order size
            price: Order price
            status: Current order status
            fill_price: Average fill price (if filled)
            fill_size: Filled size (if filled)
        """
        self.order_id = str(order_id)
        self.symbol = symbol
        self.side = side
        self.size = size
        self.price = price
        self.status = status
        self.fill_price = fill_price
        self.fill_size = fill_size
        self.timestamp = datetime.datetime.now()

    @property
    def is_filled(self) -> bool:
        """Check if order is completely filled"""
        return self.status == OrderStatus.FILLED

    @property
    def is_resting(self) -> bool:
        """Check if order is resting on the book"""
        return self.status in [OrderStatus.NEW, OrderStatus.PENDING]

    @property
    def is_rejected(self) -> bool:
        """Check if order was rejected"""
        return self.status == OrderStatus.REJECTED

    @classmethod
    def from_hyperliquid_response(cls, result: Dict[str, Any], request: OrderRequest) -> 'OrderResult':
        """
        Create OrderResult from HyperLiquid API response

        Args:
            result: Raw API response
            request: Original order request

        Returns:
            OrderResult instance

        Raises:
            ValueError: If response format is invalid
        """
        if not result or result.get('status') != 'ok':
            raise ValueError(f"Invalid API response: {result}")

        response_data = result.get('response', {}).get('data', {})
        statuses = response_data.get('statuses', [])

        if not statuses:
            raise ValueError("No order statuses in response")

        first_status = statuses[0]

        # Handle errors
        if 'error' in first_status:
            raise ValueError(f"Order rejected: {first_status['error']}")

        # Handle immediate fill
        if 'filled' in first_status:
            fill_data = first_status['filled']
            return cls(
                order_id=fill_data.get('oid'),
                symbol=request.symbol,
                side=request.side,
                size=request.size,
                price=request.price or 0,
                status=OrderStatus.FILLED,
                fill_price=float(fill_data.get('avgPx', 0)),
                fill_size=float(fill_data.get('totalSz', 0))
            )

        # Handle resting order
        elif 'resting' in first_status:
            resting_data = first_status['resting']
            return cls(
                order_id=resting_data.get('oid'),
                symbol=request.symbol,
                side=request.side,
                size=request.size,
                price=request.price or 0,
                status=OrderStatus.NEW
            )

        else:
            raise ValueError(f"Unknown order status format: {first_status}")

    def __str__(self):
        fill_info = f" (filled {self.fill_size} @ ${self.fill_price})" if self.is_filled else ""
        return f"OrderResult({self.status.value} {self.side.value.upper()} {self.size} {self.symbol}{fill_info})"

    def __repr__(self):
        return self.__str__()


class OrderResponse:
    """
    Unified response for order operations with error handling

    Provides a consistent interface for handling both successful
    and failed order operations with detailed error information.
    """

    def __init__(self, success: bool, order_result: Optional[OrderResult] = None,
                 error: Optional[str] = None, retry_attempted: bool = False):
        """
        Initialize order response

        Args:
            success: Whether operation was successful
            order_result: Order result (if successful)
            error: Error message (if failed)
            retry_attempted: Whether retry was attempted
        """
        self.success = success
        self.order_result = order_result
        self.error = error
        self.retry_attempted = retry_attempted
        self.timestamp = datetime.datetime.now()

    @property
    def is_filled(self) -> bool:
        """Check if order was filled"""
        return self.success and self.order_result and self.order_result.is_filled

    @property
    def is_resting(self) -> bool:
        """Check if order is resting"""
        return self.success and self.order_result and self.order_result.is_resting

    @property
    def order_id(self) -> Optional[str]:
        """Get order ID if available"""
        return self.order_result.order_id if self.order_result else None

    @classmethod
    def success_response(cls, order_result: OrderResult, retry_attempted: bool = False) -> 'OrderResponse':
        """Create successful order response"""
        return cls(True, order_result, retry_attempted=retry_attempted)

    @classmethod
    def error_response(cls, error: str, retry_attempted: bool = False) -> 'OrderResponse':
        """Create error response"""
        return cls(False, error=error, retry_attempted=retry_attempted)

    def __bool__(self):
        return self.success

    def __str__(self):
        if self.success:
            return f"OrderResponse(SUCCESS: {self.order_result})"
        else:
            retry_str = " (after retry)" if self.retry_attempted else ""
            return f"OrderResponse(ERROR{retry_str}: {self.error})"

    def __repr__(self):
        return self.__str__()


class Balance:
    """
    Account balance from HyperLiquid marginSummary.

    Provides account value, position tracking, and utilization metrics for
    USD-margined perpetual futures trading on HyperLiquid.

    Attributes
    ----------
    account_value : float
        Total account value in USD including unrealized PnL
    total_notional_position : float
        Total notional value of all open positions (signed)
    total_raw_usd : float
        Raw USD balance without PnL adjustments
    total : float
        Alias for account_value (for compatibility)
    available : float
        Estimated available capital (account_value - abs(position_value))
    """

    def __init__(self, margin_summary: Dict[str, Any]):
        """
        Initialize balance from HyperLiquid user_state marginSummary.

        Parameters
        ----------
        margin_summary : dict
            HyperLiquid marginSummary structure from user_state API call.
            Expected keys:
            - 'accountValue': str (total account value in USD)
            - 'totalNtlPos': str (total notional position value)
            - 'totalRawUsd': str (raw USD balance)

            Empty dict creates zero balance (used for error handling).

        Examples
        --------
        From API response:

            user_state = client.info.user_state(address)
            margin_summary = user_state.get('marginSummary', {})
            balance = Balance(margin_summary)

        Error handling (zero balance):

            balance = Balance({})
            print(balance.account_value)  # 0.0
        """
        # Parse HyperLiquid marginSummary
        self.account_value = float(margin_summary.get('accountValue', 0))
        self.total_notional_position = float(margin_summary.get('totalNtlPos', 0))
        self.total_raw_usd = float(margin_summary.get('totalRawUsd', 0))

        # Compatibility aliases
        self.total = self.account_value

        # Estimate available capital
        # Note: This is simplified - actual available depends on margin requirements
        self.available = max(0.0, self.account_value - abs(self.total_notional_position))

    def get_utilization_percent(self) -> float:
        """
        Get percentage of account value currently in positions.

        Returns
        -------
        float
            Utilization percentage (0-100+). Can exceed 100% when leveraged.
            Returns 0.0 if account_value is zero.

        Examples
        --------
        Check position utilization:

            balance = client.get_balance()
            util = balance.get_utilization_percent()
            print(f"Capital utilization: {util:.1f}%")
        """
        if self.account_value <= 0:
            return 0.0
        return (abs(self.total_notional_position) / self.account_value) * 100

    def can_afford(self, amount: float) -> bool:
        """
        Check if available capital can cover a trade of given notional value.

        Parameters
        ----------
        amount : float
            Notional trade value in USD (price * quantity)

        Returns
        -------
        bool
            True if available >= amount, False otherwise

        Examples
        --------
        Before placing order:

            order_value = price * quantity
            if balance.can_afford(order_value):
                client.place_order(order_request)
            else:
                print("Insufficient funds")
        """
        return self.available >= amount

    def get_max_trade_size(self, price: float, max_utilization: float = 0.95) -> float:
        """
        Calculate maximum trade size given price and utilization limit.

        Parameters
        ----------
        price : float
            Asset price in USD
        max_utilization : float, default=0.95
            Maximum percentage of available capital to use (0.0-1.0)

        Returns
        -------
        float
            Maximum quantity that can be traded, or 0.0 if price <= 0

        Examples
        --------
        Calculate max position size:

            max_qty = balance.get_max_trade_size(
                price=45000.0,
                max_utilization=0.90  # Use 90% of available
            )
            print(f"Max BTC size: {max_qty:.4f}")
        """
        if price <= 0:
            return 0.0
        max_spend = self.available * max_utilization
        return max_spend / price

    def validate_order_size(self, request: 'OrderRequest', current_price: float) -> bool:
        """
        Validate if account can afford the given order.

        Parameters
        ----------
        request : OrderRequest
            Order request to validate
        current_price : float
            Current market price for the asset

        Returns
        -------
        bool
            True if order can be afforded or is reduce_only, False otherwise

        Notes
        -----
        Reduce-only orders always return True since they don't require
        additional capital (they close existing positions).

        Examples
        --------
        Validate before order placement:

            order = OrderRequest(symbol="BTC", side="buy", size=0.5, ...)
            if balance.validate_order_size(order, current_price):
                client.place_order(order)
        """
        if request.reduce_only:
            return True  # Reduce-only orders don't require additional capital

        order_value = request.size * current_price
        return self.can_afford(order_value)

    def __float__(self):
        """Convert Balance to float (returns account_value)"""
        return float(self.account_value)

    def __format__(self, format_spec):
        """Support f-string formatting"""
        return format(float(self.account_value), format_spec)

    def __str__(self):
        return (f"Balance(USD: ${self.account_value:,.2f} total, "
                f"${self.available:,.2f} available, "
                f"${self.total_notional_position:,.2f} in positions, "
                f"{self.get_utilization_percent():.1f}% utilized)")

    def __repr__(self):
        return self.__str__()


class Trade:
    """
    Trade execution record from HyperLiquid

    Represents a completed trade with all relevant execution details
    including fees, PnL, and position impact.
    """

    def __init__(self, trade_info: Dict[str, Any]):
        """
        Initialize trade from HyperLiquid trade data

        Expected trade_info structure:
        {
            "coin": "BTC",
            "px": "45050.0",
            "sz": "0.1",
            "side": "B",
            "time": 1640995200000,
            "startPosition": "0.0",
            "dir": "Open Long",
            "closedPnl": "0.0",
            "hash": "0x...",
            "oid": 123456789,
            "crossed": true,
            "fee": "2.25"
        }

        Args:
            trade_info: Raw trade data from API
        """
        self.symbol = trade_info['coin']
        self.price = float(trade_info['px'])
        self.quantity = float(trade_info['sz'])
        self.side = OrderSide.BUY if trade_info['side'] == "B" else OrderSide.SELL
        self.timestamp = trade_info['time']
        self.order_id = trade_info.get('oid')
        self.fee = float(trade_info.get('fee', 0))
        self.direction = trade_info.get('dir', '')
        self.closed_pnl = float(trade_info.get('closedPnl', 0))
        self.start_position = float(trade_info.get('startPosition', 0))
        self.hash = trade_info.get('hash', '')
        self.crossed = trade_info.get('crossed', False)

    @property
    def notional_value(self) -> float:
        """Calculate notional value of the trade"""
        return self.price * self.quantity

    @property
    def is_opening(self) -> bool:
        """Check if this trade opens a new position"""
        return "Open" in self.direction

    @property
    def is_closing(self) -> bool:
        """Check if this trade closes a position"""
        return "Close" in self.direction

    def __str__(self):
        return f"Trade({self.symbol}: {self.side.value.upper()} {self.quantity:.6f} @ ${self.price:.2f})"

    def __repr__(self):
        return self.__str__()

class Position:
    """
    Open position from HyperLiquid assetPositions.

    Represents a single perpetual futures position with size, entry price,
    and unrealized PnL tracking.

    Attributes
    ----------
    symbol : str
        Trading symbol (e.g., "BTC", "ETH")
    size : float
        Position size (signed: positive=long, negative=short)
    entry_price : float
        Average entry price in USD
    unrealized_pnl : float
        Unrealized profit/loss in USD
    """

    def __init__(self, position_info: Dict[str, Any]):
        """
        Initialize position from HyperLiquid assetPositions data.

        Parameters
        ----------
        position_info : dict
            HyperLiquid position structure from user_state API call.
            Expected keys:
            - 'coin': str (symbol)
            - 'szi': str (signed size)
            - 'entryPx': str (entry price)
            - 'unrealizedPnl': str (unrealized PnL)

        Examples
        --------
        From API response:

            user_state = client.info.user_state(address)
            for asset_pos in user_state.get('assetPositions', []):
                if 'position' in asset_pos:
                    pos = Position(asset_pos['position'])
                    print(pos)
        """
        self.symbol = position_info.get('coin', '')
        self.size = float(position_info.get('szi', 0))
        self.entry_price = float(position_info.get('entryPx', 0))
        self.unrealized_pnl = float(position_info.get('unrealizedPnl', 0))

    @property
    def is_long(self) -> bool:
        """Check if this is a long position (size > 0)"""
        return self.size > 0

    @property
    def is_short(self) -> bool:
        """Check if this is a short position (size < 0)"""
        return self.size < 0

    @property
    def side(self) -> str:
        """Get position side as string ('LONG', 'SHORT', or 'FLAT')"""
        if self.is_long:
            return 'LONG'
        elif self.is_short:
            return 'SHORT'
        else:
            return 'FLAT'

    @property
    def abs_size(self) -> float:
        """Get absolute position size (always positive)"""
        return abs(self.size)

    @property
    def notional_value(self) -> float:
        """Calculate notional value of position (size * entry_price)"""
        return abs(self.size) * self.entry_price

    @property
    def pnl_percent(self) -> float:
        """
        Calculate unrealized PnL as percentage of notional value.

        Returns
        -------
        float
            PnL percentage, or 0.0 if notional_value is zero
        """
        if self.notional_value <= 0:
            return 0.0
        return (self.unrealized_pnl / self.notional_value) * 100

    def get_close_order_side(self) -> str:
        """
        Get the order side needed to close this position.

        Returns
        -------
        str
            'sell' for long positions, 'buy' for short positions

        Examples
        --------
        Create closing order:

            position = client.get_open_positions()[0]
            close_order = OrderRequest(
                symbol=position.symbol,
                side=position.get_close_order_side(),
                order_type="market",
                size=position.abs_size,
                reduce_only=True
            )
        """
        return 'sell' if self.is_long else 'buy'

    def is_profitable(self) -> bool:
        """Check if position has positive unrealized PnL"""
        return self.unrealized_pnl > 0

    def __str__(self):
        side_str = "LONG" if self.is_long else "SHORT"
        pnl_str = f"+${self.unrealized_pnl:,.2f}" if self.unrealized_pnl >= 0 else f"-${abs(self.unrealized_pnl):,.2f}"
        return (f"Position({self.symbol}: {side_str} {self.abs_size:.4f} @ ${self.entry_price:.2f}, "
                f"PnL: {pnl_str} ({self.pnl_percent:+.2f}%))")

    def __repr__(self):
        return self.__str__()


class Order:
    """
    Open order from HyperLiquid open_orders API.

    Represents a resting order on the order book with details about
    symbol, side, size, price, and order properties.

    Attributes
    ----------
    order_id : str
        Unique order identifier
    symbol : str
        Trading symbol (e.g., "BTC", "ETH")
    side : OrderSide
        Order side (BUY or SELL)
    size : float
        Order size in base asset
    limit_price : float
        Limit price in USD
    order_type : str
        Order type string from API
    reduce_only : bool
        Whether this is a reduce-only order
    timestamp : int
        Order creation timestamp in milliseconds
    """

    def __init__(self, order_info: Dict[str, Any]):
        """
        Initialize order from HyperLiquid open_orders data.

        Parameters
        ----------
        order_info : dict
            HyperLiquid order structure from open_orders API call.
            Expected keys:
            - 'oid': int or str (order ID)
            - 'coin': str (symbol)
            - 'side': str ('B' for buy, 'A' for sell)
            - 'sz': str (size)
            - 'limitPx': str (limit price)
            - 'orderType': str (order type)
            - 'reduceOnly': bool
            - 'timestamp': int (milliseconds)

        Examples
        --------
        From API response:

            orders = client.info.open_orders(address)
            for order_data in orders:
                order = Order(order_data)
                print(order)
        """
        self.order_id = str(order_info.get('oid', ''))
        self.symbol = order_info.get('coin', '')

        # Parse side ('B' = Buy, 'A' = Ask/Sell)
        side_str = order_info.get('side', 'B')
        self.side = OrderSide.BUY if side_str == 'B' else OrderSide.SELL

        self.size = float(order_info.get('sz', 0))
        self.limit_price = float(order_info.get('limitPx', 0))
        self.order_type = order_info.get('orderType', '')
        self.reduce_only = order_info.get('reduceOnly', False)
        self.timestamp = order_info.get('timestamp', 0)

        # Store original data for any additional fields
        self._raw_data = order_info

    @property
    def is_buy(self) -> bool:
        """Check if this is a buy order"""
        return self.side == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        """Check if this is a sell order"""
        return self.side == OrderSide.SELL

    @property
    def notional_value(self) -> float:
        """Calculate notional value (size * limit_price)"""
        return self.size * self.limit_price

    def get_age_seconds(self) -> float:
        """
        Get order age in seconds since creation.

        Returns
        -------
        float
            Seconds since order was created, or 0.0 if timestamp invalid
        """
        if self.timestamp <= 0:
            return 0.0
        current_time_ms = int(time.time() * 1000)
        age_ms = current_time_ms - self.timestamp
        return age_ms / 1000.0

    def matches_symbol(self, symbol: str) -> bool:
        """Check if order is for given symbol"""
        return self.symbol.upper() == symbol.upper()

    def matches_side(self, side: str) -> bool:
        """
        Check if order matches given side.

        Parameters
        ----------
        side : str
            'buy', 'sell', 'b', 's', 'BUY', 'SELL'

        Returns
        -------
        bool
            True if order side matches
        """
        try:
            target_side = OrderSide.from_string(side)
            return self.side == target_side
        except ValueError:
            return False

    def to_cancel_params(self) -> Dict[str, Any]:
        """
        Get parameters needed to cancel this order.

        Returns
        -------
        dict
            Dictionary with 'coin' and 'oid' for cancellation

        Examples
        --------
        Cancel specific order:

            order = client.get_open_orders()[0]
            cancel_params = order.to_cancel_params()
            # Use with exchange.cancel(coin, oid)
        """
        return {
            'coin': self.symbol,
            'oid': int(self.order_id) if self.order_id.isdigit() else self.order_id
        }

    def __str__(self):
        side_str = "BUY" if self.is_buy else "SELL"
        reduce_str = " (reduce)" if self.reduce_only else ""
        return (f"Order({self.order_id}: {side_str} {self.size:.4f} {self.symbol} "
                f"@ ${self.limit_price:.2f}{reduce_str})")

    def __repr__(self):
        return self.__str__()