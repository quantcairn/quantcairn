"""
Paper trading broker: simulates order execution with realistic fills.

Features:
- Realistic bid/ask spread simulation
- Slippage on market orders
- Commission calculation
- Position tracking
- Full P&L calculation
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from .base import (
    BrokerBase, Order, OrderSide, OrderType, OrderStatus,
    Position, AccountInfo,
)

logger = logging.getLogger(__name__)


class PaperBroker(BrokerBase):
    """
    Simulated broker for paper trading.

    Simulates:
    - Market orders at ask (buy) / bid (sell) prices
    - Limit orders at specified price or better
    - Commission: $0.005/share (typical for US stocks)
    - Slippage: 0.05% on market orders
    """

    def __init__(self, initial_cash: float = 10000.0, commission_per_share: float = 0.005):
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._commission_per_share = commission_per_share
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._connected = False
        self._current_prices: dict[str, float] = {}

    # ---- BrokerBase Implementation ----

    def connect(self) -> bool:
        self._connected = True
        logger.info("Paper broker connected")
        return True

    def disconnect(self) -> None:
        self._connected = False
        logger.info("Paper broker disconnected")

    def is_connected(self) -> bool:
        return self._connected

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        current_bid: float = 0,
        current_ask: float = 0,
    ) -> Order:
        """Place a simulated order with realistic fill prices."""
        order_id = f"paper-{uuid.uuid4().hex[:8]}"

        # Guard: require at least one valid price to avoid free trades
        if current_bid <= 0 and current_ask <= 0:
            return Order(
                order_id=order_id, ticker=ticker, side=side,
                order_type=order_type, quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Missing bid/ask prices",
            )

        # Determine fill price
        slippage = 0.0005  # 0.05% slippage
        if order_type == OrderType.MARKET:
            if side == OrderSide.BUY:
                fill_price = current_ask * (1 + slippage) if current_ask > 0 else current_bid * (1 + slippage)
            else:
                fill_price = current_bid * (1 - slippage) if current_bid > 0 else current_ask * (1 - slippage)
        else:  # LIMIT
            ref_price = current_bid if side == OrderSide.SELL else current_ask
            if limit_price is None:
                fill_price = ref_price
            elif side == OrderSide.BUY and limit_price >= ref_price:
                fill_price = min(limit_price, ref_price * (1 + slippage))  # Fill at best available
            elif side == OrderSide.SELL and limit_price <= ref_price:
                fill_price = max(limit_price, ref_price * (1 - slippage))
            else:
                # Limit order not fillable → return pending
                order = Order(
                    order_id=order_id, ticker=ticker, side=side,
                    order_type=order_type, quantity=quantity,
                    limit_price=limit_price, status=OrderStatus.PENDING,
                )
                self._orders[order_id] = order
                return order

        commission = quantity * self._commission_per_share

        trade_value = fill_price * quantity
        if side == OrderSide.BUY:
            total_cost = trade_value + commission
            if total_cost > self._cash:
                order = Order(
                    order_id=order_id, ticker=ticker, side=side,
                    order_type=order_type, quantity=quantity,
                    status=OrderStatus.REJECTED,
                    notes="Insufficient cash",
                )
                self._orders[order_id] = order
                return order
            self._cash -= (trade_value + commission)
        else:
            pos = self._positions.get(ticker)
            if pos is None or pos.quantity < quantity:
                order = Order(
                    order_id=order_id, ticker=ticker, side=side,
                    order_type=order_type, quantity=quantity,
                    status=OrderStatus.REJECTED,
                    notes="Insufficient position",
                )
                self._orders[order_id] = order
                return order
            self._cash += (trade_value - commission)

        # Update position
        if ticker not in self._positions:
            self._positions[ticker] = Position(
                ticker=ticker, quantity=0, avg_entry_price=0,
                current_price=fill_price, market_value=0,
                unrealized_pnl=0, unrealized_pnl_pct=0,
            )

        pos = self._positions[ticker]
        if side == OrderSide.BUY:
            total_cost = (pos.avg_entry_price * pos.quantity) + trade_value
            pos.quantity += quantity
            pos.avg_entry_price = total_cost / pos.quantity if pos.quantity > 0 else 0
        else:
            pos.quantity -= quantity
            if pos.quantity == 0:
                pos.avg_entry_price = 0

        # Update position market value
        pos.current_price = fill_price
        pos.market_value = pos.quantity * pos.current_price
        if pos.quantity > 0:
            pos.unrealized_pnl = pos.market_value - (pos.avg_entry_price * pos.quantity)
            pos.unrealized_pnl_pct = (pos.unrealized_pnl / (pos.avg_entry_price * pos.quantity)) * 100
        else:
            pos.unrealized_pnl = 0
            pos.unrealized_pnl_pct = 0

        # Store current price
        self._current_prices[ticker] = fill_price

        order = Order(
            order_id=order_id, ticker=ticker, side=side,
            order_type=order_type, quantity=quantity,
            filled_quantity=quantity, avg_fill_price=fill_price,
            status=OrderStatus.FILLED, filled_at=datetime.now(),
            commission=commission,
        )
        self._orders[order_id] = order

        logger.info(
            f"[PAPER] {side.value} {quantity} {ticker} @ ${fill_price:.2f} "
            f"(commission: ${commission:.2f}, cash: ${self._cash:.2f})"
        )
        return order

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            order = self._orders[order_id]
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED
                return True
        return False

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_position_for_ticker(self, ticker: str) -> Optional[Position]:
        return self._positions.get(ticker)

    def seed_position(self, ticker: str, quantity: int, avg_price: float) -> Optional[Position]:
        """Seed an initial position (for simulation/testing).

        This creates a position without going through the order book —
        useful for simulating 'I already hold N shares at price P'.
        The cash is deducted as if a buy order was filled.
        """
        cost = avg_price * quantity
        if cost > self._cash:
            logger.warning(
                "[PAPER] Refused to seed position: cost $%.2f exceeds cash $%.2f",
                cost,
                self._cash,
            )
            return None
        self._cash -= cost

        pos = Position(
            ticker=ticker,
            quantity=quantity,
            avg_entry_price=avg_price,
            current_price=avg_price,
            market_value=avg_price * quantity,
            unrealized_pnl=0.0,
            unrealized_pnl_pct=0.0,
        )
        self._positions[ticker] = pos
        self._current_prices[ticker] = avg_price

        logger.info(
            f"[PAPER] Seeded position: {quantity} {ticker} @ ${avg_price:.2f} "
            f"(cost=${cost:,.2f}, cash=${self._cash:,.2f})"
        )
        return pos

    def get_account(self) -> AccountInfo:
        equity = self._cash + sum(p.market_value for p in self._positions.values())
        return AccountInfo(
            cash=round(self._cash, 2),
            equity=round(equity, 2),
            buying_power=round(self._cash * 2, 2),  # 2x margin
            positions=self.get_positions(),
        )

    def update_price(self, ticker: str, price: float) -> None:
        """Update current market price for P&L calculation."""
        self._current_prices[ticker] = price
        if ticker in self._positions:
            pos = self._positions[ticker]
            pos.current_price = price
            pos.market_value = pos.quantity * price
            if pos.quantity > 0:
                cost_basis = pos.avg_entry_price * pos.quantity
                pos.unrealized_pnl = pos.market_value - cost_basis
                pos.unrealized_pnl_pct = (pos.unrealized_pnl / cost_basis * 100) if cost_basis else 0
