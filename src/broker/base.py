"""
Abstract broker interface. All broker implementations must implement this.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    order_id: str
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Optional[float] = None
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: Optional[datetime] = None
    commission: float = 0.0
    notes: str = ""


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


@dataclass
class AccountInfo:
    cash: float
    equity: float
    buying_power: float
    positions: list[Position]
    total_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    total_commission: float = 0.0


class BrokerBase(ABC):
    """Abstract broker interface."""

    @abstractmethod
    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
    ) -> Order:
        """Place an order. Returns Order with status."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """Get order details by ID."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get current positions."""
        ...

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """Get account summary."""
        ...

    @abstractmethod
    def get_position_for_ticker(self, ticker: str) -> Optional[Position]:
        """Get position for a specific ticker."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if broker connection is alive."""
        ...

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to broker."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection."""
        ...
