from __future__ import annotations

from datetime import datetime, timezone

from src.backtest.execution import BacktestExecutionModel
from src.backtest.models import Bar, BacktestOrder


def _bar(open_, high, low, close, volume=1000):
    return Bar(
        symbol="SOXS.US",
        timestamp=datetime(2026, 7, 1, 9, 31, tzinfo=timezone.utc),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def test_market_buy_fills_at_open_with_slippage():
    model = BacktestExecutionModel(slippage_bps=10.0)
    order = BacktestOrder(order_id="1", symbol="SOXS.US", side="BUY", quantity=10)
    result = model.simulate_fill(order, _bar(10.0, 10.2, 9.8, 10.1), available_cash=200.0, current_position=0)

    assert result["status"] == "FILLED"
    assert result["filled_quantity"] == 10
    assert result["filled_price"] > 10.0


def test_limit_buy_waits_until_touched_and_respects_price():
    model = BacktestExecutionModel(slippage_bps=0.0)
    order = BacktestOrder(order_id="2", symbol="SOXS.US", side="BUY", order_type="LIMIT", quantity=5, limit_price=9.9)
    pending = model.simulate_fill(order, _bar(10.2, 10.3, 10.0, 10.1), available_cash=200.0, current_position=0)
    filled = model.simulate_fill(order, _bar(10.1, 10.2, 9.8, 10.0), available_cash=200.0, current_position=0)

    assert pending["status"] == "PENDING"
    assert filled["status"] == "FILLED"
    assert filled["filled_price"] == 9.9


def test_partial_fill_uses_participation_limit():
    model = BacktestExecutionModel(slippage_bps=0.0, participation_limit=0.25)
    order = BacktestOrder(order_id="3", symbol="SOXS.US", side="BUY", quantity=100)
    result = model.simulate_fill(order, _bar(10.0, 10.1, 9.9, 10.0, volume=100), available_cash=1000.0, current_position=0)

    assert result["status"] == "PARTIALLY_FILLED"
    assert result["filled_quantity"] == 25


def test_buy_rejected_when_cash_insufficient():
    model = BacktestExecutionModel(slippage_bps=0.0)
    order = BacktestOrder(order_id="4", symbol="SOXS.US", side="BUY", quantity=50)
    result = model.simulate_fill(order, _bar(10.0, 10.1, 9.9, 10.0), available_cash=10.0, current_position=0)

    assert result["status"] == "REJECTED"
    assert result["reject_reason"] == "insufficient_cash"


def test_sell_cannot_exceed_position():
    model = BacktestExecutionModel(slippage_bps=0.0)
    order = BacktestOrder(order_id="5", symbol="SOXS.US", side="SELL", quantity=20)
    result = model.simulate_fill(order, _bar(10.0, 10.1, 9.9, 10.0), available_cash=1000.0, current_position=8)

    assert result["status"] == "PARTIALLY_FILLED"
    assert result["filled_quantity"] == 8
