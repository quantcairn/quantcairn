from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass(slots=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    bid: float | None = None
    ask: float | None = None
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "bid": self.bid,
            "ask": self.ask,
            "source": self.source,
        }


@dataclass(slots=True)
class BacktestOrder:
    order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"] = "MARKET"
    quantity: int = 0
    limit_price: float | None = None
    stop_price: float | None = None
    submitted_at: datetime | str | None = None
    eligible_at: datetime | str | None = None
    trigger_price: float | None = None
    submitted_price: float | None = None
    filled_price: float | None = None
    requested_quantity: int = 0
    filled_quantity: int = 0
    commission: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    status: Literal["PENDING", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED"] = "PENDING"
    reject_reason: str = ""
    strategy_version: str = ""
    layer_id: int | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "submitted_at": _to_iso(self.submitted_at),
            "eligible_at": _to_iso(self.eligible_at),
            "trigger_price": self.trigger_price,
            "submitted_price": self.submitted_price,
            "filled_price": self.filled_price,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "commission": self.commission,
            "fees": self.fees,
            "slippage": self.slippage,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "strategy_version": self.strategy_version,
            "layer_id": self.layer_id,
            "notes": self.notes,
        }


@dataclass(slots=True)
class BacktestTrade:
    order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: str
    submitted_at: str | None
    eligible_at: str | None
    trigger_price: float | None
    submitted_price: float | None
    filled_price: float | None
    requested_quantity: int
    filled_quantity: int
    commission: float
    fees: float
    slippage: float
    status: str
    reject_reason: str
    strategy_version: str
    layer_id: int | None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "submitted_at": self.submitted_at,
            "eligible_at": self.eligible_at,
            "trigger_price": self.trigger_price,
            "submitted_price": self.submitted_price,
            "filled_price": self.filled_price,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "commission": self.commission,
            "fees": self.fees,
            "slippage": self.slippage,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "strategy_version": self.strategy_version,
            "layer_id": self.layer_id,
            "notes": self.notes,
        }


@dataclass(slots=True)
class BacktestResult:
    run_id: str
    strategy: str
    symbol: str
    data_start: str | None
    data_end: str | None
    configuration: dict[str, Any]
    summary: dict[str, Any]
    metrics: dict[str, Any]
    trades: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    drawdown_curve: list[dict[str, Any]] = field(default_factory=list)
    rejected_signals: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parameter_set: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "configuration": self.configuration,
            "summary": self.summary,
            "metrics": self.metrics,
            "trades": self.trades,
            "orders": self.orders,
            "equity_curve": self.equity_curve,
            "drawdown_curve": self.drawdown_curve,
            "rejected_signals": self.rejected_signals,
            "warnings": self.warnings,
            "parameter_set": self.parameter_set,
        }


@dataclass(slots=True)
class WalkForwardWindowResult:
    train_range: dict[str, str]
    validation_range: dict[str, str]
    test_range: dict[str, str]
    selected_parameters: dict[str, Any]
    validation_score: float
    test_metrics: dict[str, Any]
    trade_count: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_range": self.train_range,
            "validation_range": self.validation_range,
            "test_range": self.test_range,
            "selected_parameters": self.selected_parameters,
            "validation_score": self.validation_score,
            "test_metrics": self.test_metrics,
            "trade_count": self.trade_count,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class WalkForwardResult:
    strategy: str
    symbol: str
    windows: list[WalkForwardWindowResult]
    stitched_oos_equity: list[dict[str, Any]]
    aggregate_oos_metrics: dict[str, Any]
    parameter_stability: dict[str, Any]
    window_failure_count: int
    no_trade_window_count: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "windows": [window.to_dict() for window in self.windows],
            "stitched_oos_equity": self.stitched_oos_equity,
            "aggregate_oos_metrics": self.aggregate_oos_metrics,
            "parameter_stability": self.parameter_stability,
            "window_failure_count": self.window_failure_count,
            "no_trade_window_count": self.no_trade_window_count,
            "warnings": self.warnings,
        }
