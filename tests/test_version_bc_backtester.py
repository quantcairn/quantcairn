from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sin

from src.backtest.models import Bar
from src.backtest.strategy_backtester import StrategyBacktester


def _oscillating_bars(rows: int = 180, base: float = 10.0, amplitude: float = 0.45):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = base + sin(index / 3.5) * amplitude
        bars.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.015, 4),
                low=round(close * 0.985, 4),
                close=round(close, 4),
                volume=180_000,
                bid=round(close * 0.998, 4),
                ask=round(close * 1.002, 4),
            )
        )
    return bars


def test_version_b_generates_layered_entries_and_exits():
    bars = _oscillating_bars()
    backtester = StrategyBacktester(strategy="b", initial_cash=10_000.0, max_position=200)
    result = backtester.run(bars, symbol="SOXS.US")

    assert result.metrics["trade_count"] > 0
    assert result.metrics["fill_count"] == result.metrics["trade_count"]
    assert result.metrics["trade_count_definition"] == "fill_count"
    assert result.metrics["closed_trade_count"] == 1
    assert result.metrics["round_trip_trade_count"] == 1
    assert result.metrics["win_rate"] == 1.0
    assert result.metrics["profit_factor_status"] == "NO_LOSSES"
    assert any(trade["side"] == "BUY" and trade.get("layer_id") for trade in result.trades)
    assert any(trade["side"] == "SELL" and trade.get("layer_id") for trade in result.trades)
    buy_layers = [trade.get("layer_id") for trade in result.trades if trade["side"] == "BUY"]
    assert len(buy_layers) == len(set(buy_layers))
    assert all(order["side"] in {"BUY", "SELL"} for order in result.orders)


def test_version_b_is_deterministic():
    bars = _oscillating_bars()
    backtester = StrategyBacktester(strategy="b", initial_cash=10_000.0, max_position=200)
    left = backtester.run(bars, symbol="SOXS.US")
    right = backtester.run(bars, symbol="SOXS.US")
    left_payload = left.to_dict()
    right_payload = right.to_dict()
    left_payload.pop("run_id", None)
    right_payload.pop("run_id", None)
    assert left_payload == right_payload


def test_version_c_trend_guard_blocks_buy_on_benchmark_uptrend():
    bars = _oscillating_bars()
    benchmark = []
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    for index in range(len(bars)):
        ts = start + timedelta(minutes=index)
        close = 100.0 + index * 0.25
        benchmark.append(
            Bar(
                symbol="SMH.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=220_000,
                bid=round(close * 0.999, 4),
                ask=round(close * 1.001, 4),
            )
        )
    backtester = StrategyBacktester(strategy="c", initial_cash=10_000.0, max_position=200)
    result = backtester.run(bars, symbol="SOXS.US", benchmark_bars=benchmark)

    assert result.metrics["blocked_by_trend_count"] >= 1
    assert result.metrics["trade_count"] == 0 or all(trade["side"] == "SELL" for trade in result.trades)
    assert any("trend_guard" in str(item.get("reason") or "") or "benchmark_strong_uptrend" in str(item.get("reason") or "") for item in result.rejected_signals)


def test_version_c_state_store_failure_blocks_new_buys(monkeypatch):
    bars = _oscillating_bars()
    backtester = StrategyBacktester(strategy="c", initial_cash=10_000.0, max_position=200)

    def _boom(*args, **kwargs):
        raise RuntimeError("state store down")

    monkeypatch.setattr("src.strategy.state_store.StrategyStateStore.save", _boom)
    result = backtester.run(bars, symbol="SOXS.US")

    buy_layers = [trade.get("layer_id") for trade in result.trades if trade["side"] == "BUY"]
    assert len(set(buy_layers)) <= 1
    assert result.metrics["time_stop_signal_count"] <= 1


def test_version_c_inventory_and_cost_filters_can_block_buys(monkeypatch):
    bars = _oscillating_bars()
    benchmark = []
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    for index in range(len(bars)):
        ts = start + timedelta(minutes=index)
        close = 100.0 + sin(index / 12.0) * 0.05
        benchmark.append(
            Bar(
                symbol="SMH.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=220_000,
                bid=round(close * 0.999, 4),
                ask=round(close * 1.001, 4),
            )
        )
    backtester = StrategyBacktester(strategy="c", initial_cash=10_000.0, max_position=200)

    def _inventory_block(*args, **kwargs):
        return {
            "inventory_ratio": 0.95,
            "adjusted_quantity": 0,
            "adjustment_factor": 0.0,
            "allowed": False,
            "reject_reason": "inventory_limit",
    }

    monkeypatch.setattr("src.strategy.inventory_sizing.InventoryAwareSizer.adjust_quantity", _inventory_block)
    result = backtester.run(bars, symbol="SOXS.US", benchmark_bars=benchmark)
    assert result.metrics["blocked_by_inventory_count"] >= 1 or any(item.get("reason") == "inventory_limit" for item in result.rejected_signals)
