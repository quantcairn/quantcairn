from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ShadowOrder:
    order_id: str
    strategy_version: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float | None = None
    submitted_at_utc: str | None = None
    submitted_at_et: str | None = None
    filled_quantity: int = 0
    filled_price: float | None = None
    status: str = "SIMULATED"
    reject_reason: str = ""
    simulated: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "submitted_at_utc": self.submitted_at_utc,
            "submitted_at_et": self.submitted_at_et,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "simulated": self.simulated,
            "notes": self.notes,
        }


@dataclass(slots=True)
class ShadowTrade:
    order_id: str
    strategy_version: str
    symbol: str
    side: str
    quantity: int
    fill_price: float
    filled_at_utc: str
    filled_at_et: str
    status: str = "FILLED"
    commission: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    spread_cost: float = 0.0
    simulated: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "filled_at_utc": self.filled_at_utc,
            "filled_at_et": self.filled_at_et,
            "status": self.status,
            "commission": self.commission,
            "fees": self.fees,
            "slippage": self.slippage,
            "spread_cost": self.spread_cost,
            "simulated": self.simulated,
            "notes": self.notes,
        }


@dataclass(slots=True)
class ShadowBarSnapshot:
    strategy_version: str
    timestamp_utc: str
    timestamp_et: str
    symbol: str
    symbol_ohlcv: dict[str, Any]
    benchmarks: dict[str, dict[str, Any]]
    benchmark_alignment_status: str
    signal: str
    intended_action: str
    intended_quantity: int
    simulated_fill_price: float | None
    simulated_position: int
    simulated_cash: float
    simulated_equity: float
    blocked_reason: str
    range_width: float | None
    layer_count: int
    trend_regime: str
    time_stop_state: str
    data_freshness: str
    data_gap_status: str
    simulated: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "strategy_version": self.strategy_version,
            "timestamp_utc": self.timestamp_utc,
            "timestamp_et": self.timestamp_et,
            "symbol": self.symbol,
            "symbol_ohlcv": dict(self.symbol_ohlcv),
            "benchmarks": {key: dict(value) for key, value in self.benchmarks.items()},
            "benchmark_alignment_status": self.benchmark_alignment_status,
            "signal": self.signal,
            "intended_action": self.intended_action,
            "intended_quantity": self.intended_quantity,
            "simulated_fill_price": self.simulated_fill_price,
            "simulated_position": self.simulated_position,
            "simulated_cash": self.simulated_cash,
            "simulated_equity": self.simulated_equity,
            "blocked_reason": self.blocked_reason,
            "range_width": self.range_width,
            "layer_count": self.layer_count,
            "trend_regime": self.trend_regime,
            "time_stop_state": self.time_stop_state,
            "data_freshness": self.data_freshness,
            "data_gap_status": self.data_gap_status,
            "simulated": self.simulated,
        }
        payload.update(self.extra)
        return payload

