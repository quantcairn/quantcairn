"""
Paper trading broker: simulates order execution with realistic fills.

Features:
- Realistic bid/ask spread simulation
- Slippage on market orders
- Commission calculation
- Position tracking
- Full P&L calculation
- Trade history
"""
import copy
import logging
import os
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import (
    BrokerBase, Order, OrderSide, OrderType, OrderStatus,
    Position, AccountInfo,
)
from .paper_portfolio_state import (
    PaperPortfolioState,
    PaperPortfolioStateError,
    PaperPortfolioStateStore,
    new_writer_run_id,
)

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Record of a single executed trade."""
    ticker: str
    side: OrderSide
    quantity: int
    price: float
    slippage: float
    commission: float
    executed_at: str
    notes: str = ""


class PaperBroker(BrokerBase):
    """
    Simulated broker for paper trading.

    Simulates:
    - Market orders at ask (buy) / bid (sell) prices
    - Limit orders at specified price or better
    - Commission: $0.005/share (typical for US stocks) or per-trade model
    - Slippage: configurable percentage on market orders
    """

    def __init__(
        self,
        initial_cash: float = 10000.0,
        commission_per_share: float = 0.005,
        slippage_pct: float = 0.05,
        commission_per_trade: float = 0.0,
        portfolio_state_path: str | Path | None = None,
        persist_portfolio_state: bool = False,
        account_id: str = "paper-default",
        writer_port: int | None = None,
        writer_mode: str = "paper",
        writer_run_id: str | None = None,
    ):
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._commission_per_share = commission_per_share
        self._slippage_pct = slippage_pct  # max slippage as percentage (e.g. 0.05 = 0.05%)
        self._commission_per_trade = commission_per_trade
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._connected = False
        self._current_prices: dict[str, float] = {}
        self._persist_portfolio_state_enabled = bool(persist_portfolio_state)
        self._account_id = account_id
        self._processed_fill_ids: set[str] = set()
        self._writer_port = writer_port
        self._writer_mode = writer_mode
        self._writer_run_id = writer_run_id or new_writer_run_id()
        self._state_store = (
            PaperPortfolioStateStore(
                path=portfolio_state_path,
                account_id=account_id,
                writer_port=writer_port,
                writer_mode=writer_mode,
                writer_run_id=self._writer_run_id,
            )
            if self._persist_portfolio_state_enabled
            else None
        )

        # --- New tracking fields ---
        self._trade_history: list[TradeRecord] = []
        self._realized_pnl: float = 0.0
        self._total_commission: float = 0.0
        self._wins: int = 0
        self._losses: int = 0
        self._total_win_amount: float = 0.0
        self._total_loss_amount: float = 0.0
        if self._persist_portfolio_state_enabled:
            self._load_or_create_portfolio_state()

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

    def _compute_slippage(self) -> float:
        """Return a random slippage multiplier in [0.0001, slippage_pct/100].

        The min is clamped to 0.01% (0.0001) and the max to slippage_pct / 100
        (but at least 0.01%).
        """
        min_slip = 0.01  # 0.01% in percentage units
        max_slip = max(min_slip, self._slippage_pct)
        return random.uniform(min_slip, max_slip) / 100.0

    def _compute_commission(self, trade_value: float, quantity: int) -> float:
        """Compute commission: per-trade model or per-share fallback."""
        if self._commission_per_trade > 0:
            return round(max(self._commission_per_trade, 0.001 * trade_value), 3)
        return round(quantity * self._commission_per_share, 3)

    def _record_trade(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        price: float,
        slippage: float,
        commission: float,
        notes: str = "",
    ) -> None:
        """Append a trade record to history."""
        record = TradeRecord(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=round(price, 3),
            slippage=round(slippage, 6),
            commission=round(commission, 3),
            executed_at=datetime.now().isoformat(),
            notes=notes,
        )
        self._trade_history.append(record)

    def _snapshot_runtime_state(self) -> dict[str, object]:
        return {
            "cash": self._cash,
            "orders": copy.deepcopy(self._orders),
            "positions": copy.deepcopy(self._positions),
            "current_prices": copy.deepcopy(self._current_prices),
            "processed_fill_ids": copy.deepcopy(self._processed_fill_ids),
            "trade_history": copy.deepcopy(self._trade_history),
            "realized_pnl": self._realized_pnl,
            "total_commission": self._total_commission,
            "wins": self._wins,
            "losses": self._losses,
            "total_win_amount": self._total_win_amount,
            "total_loss_amount": self._total_loss_amount,
            "connected": self._connected,
        }

    def _restore_runtime_state(self, snapshot: dict[str, object]) -> None:
        self._cash = float(snapshot["cash"])
        self._orders = copy.deepcopy(snapshot["orders"])
        self._positions = copy.deepcopy(snapshot["positions"])
        self._current_prices = copy.deepcopy(snapshot["current_prices"])
        self._processed_fill_ids = copy.deepcopy(snapshot["processed_fill_ids"])
        self._trade_history = copy.deepcopy(snapshot["trade_history"])
        self._realized_pnl = float(snapshot["realized_pnl"])
        self._total_commission = float(snapshot["total_commission"])
        self._wins = int(snapshot["wins"])
        self._losses = int(snapshot["losses"])
        self._total_win_amount = float(snapshot["total_win_amount"])
        self._total_loss_amount = float(snapshot["total_loss_amount"])
        self._connected = bool(snapshot["connected"])

    def _load_or_create_portfolio_state(self) -> None:
        if self._state_store is None:
            return
        initial_state = PaperPortfolioState.initial(
            account_id=self._account_id,
            initial_cash=self._initial_cash,
            execution_mode="paper",
            writer_mode=self._writer_mode,
            writer_run_id=self._writer_run_id,
            writer_port=self._writer_port,
        )
        with self._state_store.locked():
            state = self._state_store.load_or_create(initial_state)
        self._sync_from_portfolio_state(state)

    def _sync_from_portfolio_state(self, state: PaperPortfolioState) -> None:
        self._cash = float(state.cash or 0.0)
        self._realized_pnl = float(state.realized_pnl or 0.0)
        self._total_commission = float(state.total_commission or 0.0)
        self._processed_fill_ids = set(str(item) for item in (state.processed_fill_ids or []) if str(item))
        self._positions = {}
        self._current_prices = {}
        for item in state.positions or []:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
            quantity = int(item.get("quantity") or 0)
            if not ticker or quantity <= 0:
                continue
            avg_entry_price = float(item.get("avg_entry_price", item.get("average_cost")) or 0.0)
            current_price = float(item.get("current_price", item.get("market_price")) or 0.0)
            position = Position(
                ticker=ticker,
                quantity=quantity,
                avg_entry_price=avg_entry_price,
                current_price=current_price,
                market_value=float(item.get("market_value") or quantity * current_price),
                unrealized_pnl=float(item.get("unrealized_pnl") or 0.0),
                unrealized_pnl_pct=float(item.get("unrealized_pnl_pct") or 0.0),
            )
            self._positions[ticker] = position
            self._current_prices[ticker] = current_price

    def _persist_portfolio_state(
        self,
        *,
        last_fill_id: str | None = None,
        last_order_id: str | None = None,
        last_event_id: str | None = None,
    ) -> None:
        if not self._persist_portfolio_state_enabled:
            return
        if self._state_store is None:
            raise PaperPortfolioStateError("portfolio_state_store_unavailable")
        state = PaperPortfolioState.from_account(
            self.get_account(),
            account_id=self._account_id,
            realized_pnl=self._realized_pnl,
            processed_fill_ids=self._processed_fill_ids,
            last_fill_id=last_fill_id,
            last_order_id=last_order_id,
            last_event_id=last_event_id,
            writer_pid=os.getpid(),
            writer_port=self._writer_port,
            writer_mode=self._writer_mode,
            writer_run_id=self._writer_run_id,
            execution_mode="paper",
        )
        with self._state_store.locked():
            saved = self._state_store.save(
                state,
                last_fill_id=last_fill_id,
                last_order_id=last_order_id,
                last_event_id=last_event_id,
            )
        self._sync_from_portfolio_state(saved)

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
        if self._persist_portfolio_state_enabled and self._state_store is not None:
            with self._state_store.locked():
                latest = self._state_store.load()
                if latest is not None:
                    self._sync_from_portfolio_state(latest)
                return self._place_order_unlocked(
                    ticker=ticker,
                    side=side,
                    quantity=quantity,
                    order_type=order_type,
                    limit_price=limit_price,
                    current_bid=current_bid,
                    current_ask=current_ask,
                )
        return self._place_order_unlocked(
            ticker=ticker,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            current_bid=current_bid,
            current_ask=current_ask,
        )

    def _place_order_unlocked(
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

        if quantity <= 0:
            return Order(
                order_id=order_id, ticker=ticker, side=side,
                order_type=order_type, quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Invalid quantity",
            )

        # Guard: require at least one valid price to avoid free trades
        if current_bid <= 0 and current_ask <= 0:
            return Order(
                order_id=order_id, ticker=ticker, side=side,
                order_type=order_type, quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Missing bid/ask prices",
            )

        # Determine fill price with configurable random slippage
        slippage = self._compute_slippage()
        if order_type == OrderType.MARKET:
            if side == OrderSide.BUY:
                fill_price = current_ask * (1 + slippage) if current_ask > 0 else current_bid * (1 + slippage)
            else:
                fill_price = current_bid * (1 - slippage) if current_bid > 0 else current_ask * (1 - slippage)
        else:  # LIMIT
            ref_price = current_bid if side == OrderSide.SELL else current_ask
            if limit_price is None:
                fill_price = ref_price
                slippage = 0.0  # no slippage on limit at reference
            elif side == OrderSide.BUY and limit_price >= ref_price:
                fill_price = min(limit_price, ref_price * (1 + slippage))
            elif side == OrderSide.SELL and limit_price <= ref_price:
                fill_price = max(limit_price, ref_price * (1 - slippage))
            else:
                # Limit order not fillable -> return pending
                order = Order(
                    order_id=order_id, ticker=ticker, side=side,
                    order_type=order_type, quantity=quantity,
                    limit_price=limit_price, status=OrderStatus.PENDING,
                )
                self._orders[order_id] = order
                return order

        trade_value = fill_price * quantity
        commission = self._compute_commission(trade_value, quantity)

        realized_pnl = 0.0
        pre_trade_state = self._snapshot_runtime_state()

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

            # Realized P&L on this sell
            realized_pnl = (fill_price - pos.avg_entry_price) * quantity - commission
            self._realized_pnl += realized_pnl
            if realized_pnl > 0:
                self._wins += 1
                self._total_win_amount += realized_pnl
            else:
                self._losses += 1
                self._total_loss_amount += abs(realized_pnl)

            self._cash += (trade_value - commission)

        self._total_commission += commission

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
            self._positions.pop(ticker, None)

        # Store current price
        self._current_prices[ticker] = fill_price

        # Record trade in history
        self._record_trade(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=fill_price,
            slippage=slippage,
            commission=commission,
            notes=f"order_type={order_type.value}",
        )

        order = Order(
            order_id=order_id, ticker=ticker, side=side,
            order_type=order_type, quantity=quantity,
            filled_quantity=quantity, avg_fill_price=fill_price,
            status=OrderStatus.FILLED, filled_at=datetime.now(),
            commission=commission,
        )
        self._orders[order_id] = order
        try:
            self._persist_portfolio_state(
                last_fill_id=order_id,
                last_order_id=order_id,
                last_event_id=f"paper:{order_id}",
            )
        except Exception as exc:
            self._restore_runtime_state(pre_trade_state)
            order.status = OrderStatus.REJECTED
            order.filled_quantity = 0
            order.avg_fill_price = 0.0
            order.filled_at = None
            order.commission = 0.0
            order.notes = f"PERSIST_FAILED:{exc};ROLLBACK_COMPLETED"
            self._orders[order_id] = order
            logger.warning(
                "PaperBroker persistence failed. Rolling back in-memory state. Order rejected."
            )
            return order

        logger.info(
            f"[PAPER] {side.value} {quantity} {ticker} @ ${fill_price:.2f} "
            f"(commission: ${commission:.2f}, slippage: {slippage*100:.3f}%, "
            f"cash: ${self._cash:.2f})"
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

    def get_trade_history(self) -> list[TradeRecord]:
        """Return the complete trade history."""
        return list(self._trade_history)

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
        self._persist_portfolio_state(last_event_id=f"seed:{ticker}")

        logger.info(
            f"[PAPER] Seeded position: {quantity} {ticker} @ ${avg_price:.2f} "
            f"(cost=${cost:,.2f}, cash=${self._cash:,.2f})"
        )
        return pos

    def get_account(self) -> AccountInfo:
        equity = self._cash + sum(p.market_value for p in self._positions.values())

        total_trades = len(self._trade_history)
        total_wins_losses = self._wins + self._losses
        win_rate = round(self._wins / total_wins_losses, 4) if total_wins_losses > 0 else 0.0
        avg_win = round(self._total_win_amount / self._wins, 3) if self._wins > 0 else 0.0
        avg_loss = round(self._total_loss_amount / self._losses, 3) if self._losses > 0 else 0.0

        return AccountInfo(
            cash=round(self._cash, 2),
            equity=round(equity, 2),
            buying_power=round(self._cash * 2, 2),  # 2x margin
            positions=self.get_positions(),
            total_trades=total_trades,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            total_commission=round(self._total_commission, 3),
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
            self._persist_portfolio_state(last_event_id=f"mark:{ticker}")
