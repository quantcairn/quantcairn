from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class PositionState:
    quantity: int = 0
    average_cost: float = 0.0
    last_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "average_cost": round(self.average_cost, 6),
            "last_price": round(self.last_price, 6),
            "market_value": round(self.market_value, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
        }


@dataclass
class BacktestPortfolio:
    initial_cash: float
    available_cash: float | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 0.0
    exposure: float = 0.0
    peak_equity: float = 0.0
    drawdown: float = 0.0
    active_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    order_history: list[dict[str, Any]] = field(default_factory=list)
    fill_history: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    drawdown_curve: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.available_cash = float(self.initial_cash if self.available_cash is None else self.available_cash)
        self.total_equity = float(self.initial_cash)
        self.peak_equity = float(self.initial_cash)

    def get_position(self, symbol: str) -> PositionState:
        symbol = str(symbol or "").strip().upper()
        return self.positions.setdefault(symbol, PositionState())

    def position_value(self, symbol: str) -> float:
        pos = self.positions.get(str(symbol or "").strip().upper())
        return float(pos.market_value if pos else 0.0)

    def position_quantity(self, symbol: str) -> int:
        pos = self.positions.get(str(symbol or "").strip().upper())
        return int(pos.quantity if pos else 0)

    def has_position(self, symbol: str) -> bool:
        return self.position_quantity(symbol) > 0

    def has_active_order(self, symbol: str, side: str | None = None) -> bool:
        symbol = str(symbol or "").strip().upper()
        for order in self.active_orders.values():
            if str(order.get("symbol") or "").strip().upper() != symbol:
                continue
            if side and str(order.get("side") or "").strip().upper() != str(side).strip().upper():
                continue
            status = str(order.get("status") or "").strip().upper()
            if status in {"PENDING", "PARTIALLY_FILLED"}:
                return True
        return False

    def register_order(self, order: dict[str, Any]) -> None:
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("order_id is required")
        self.active_orders[order_id] = dict(order)
        self.order_history.append(dict(order))

    def mark_to_market(self, prices: dict[str, float], timestamp: datetime | str | None = None) -> dict[str, Any]:
        total_position_value = 0.0
        unrealized_total = 0.0
        for symbol, position in self.positions.items():
            last_price = float(prices.get(symbol, position.last_price or position.average_cost or 0.0) or 0.0)
            position.last_price = last_price
            position.market_value = round(position.quantity * last_price, 6)
            position.unrealized_pnl = round((last_price - position.average_cost) * position.quantity, 6)
            total_position_value += position.market_value
            unrealized_total += position.unrealized_pnl
        self.unrealized_pnl = round(unrealized_total, 6)
        self.total_equity = round(float(self.available_cash or 0.0) + total_position_value, 6)
        if self.total_equity > self.peak_equity:
            self.peak_equity = self.total_equity
        self.drawdown = round(self.peak_equity - self.total_equity, 6)
        self.exposure = round(total_position_value / self.total_equity, 6) if self.total_equity > 0 else 0.0
        snapshot = {
            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
            "available_cash": round(float(self.available_cash or 0.0), 6),
            "total_equity": round(self.total_equity, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "exposure": round(self.exposure, 6),
            "drawdown": round(self.drawdown, 6),
        }
        self.equity_curve.append(snapshot)
        self.drawdown_curve.append(
            {
                "timestamp": snapshot["timestamp"],
                "drawdown": snapshot["drawdown"],
                "peak_equity": round(self.peak_equity, 6),
                "equity": round(self.total_equity, 6),
            }
        )
        return snapshot

    def apply_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        symbol = str(fill.get("symbol") or "").strip().upper()
        side = str(fill.get("side") or "").strip().upper()
        quantity = int(fill.get("filled_quantity") or fill.get("quantity") or 0)
        price = float(fill.get("filled_price") or fill.get("price") or 0.0)
        commission = float(fill.get("commission") or 0.0)
        fees = float(fill.get("fees") or 0.0)
        if quantity <= 0 or price <= 0 or not symbol:
            return fill

        position = self.get_position(symbol)
        if side == "BUY":
            cost = quantity * price + commission + fees
            self.available_cash = round(float(self.available_cash or 0.0) - cost, 6)
            new_qty = position.quantity + quantity
            if new_qty > 0:
                position.average_cost = round(
                    ((position.average_cost * position.quantity) + (price * quantity)) / new_qty,
                    6,
                )
            position.quantity = new_qty
        elif side == "SELL":
            sell_qty = min(quantity, position.quantity)
            proceeds = sell_qty * price - commission - fees
            realized = (price - position.average_cost) * sell_qty
            self.available_cash = round(float(self.available_cash or 0.0) + proceeds, 6)
            self.realized_pnl = round(self.realized_pnl + realized, 6)
            position.quantity = max(0, position.quantity - sell_qty)
            if position.quantity == 0:
                position.average_cost = 0.0
        position.last_price = price
        position.market_value = round(position.quantity * price, 6)
        position.unrealized_pnl = round((price - position.average_cost) * position.quantity, 6)
        fill_record = dict(fill)
        fill_record.setdefault("timestamp", fill.get("submitted_at") or fill.get("filled_at"))
        self.fill_history.append(fill_record)
        order_id = str(fill.get("order_id") or "").strip()
        if order_id and order_id in self.active_orders:
            self.active_orders[order_id] = {**self.active_orders[order_id], **fill_record}
        return fill_record

    def snapshot(self, timestamp: datetime | str | None = None) -> dict[str, Any]:
        return {
            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
            "available_cash": round(float(self.available_cash or 0.0), 6),
            "total_equity": round(self.total_equity, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "exposure": round(self.exposure, 6),
            "drawdown": round(self.drawdown, 6),
            "positions": {symbol: position.to_dict() for symbol, position in self.positions.items()},
        }
