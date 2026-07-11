from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backtest.models import Bar
from src.backtest.strategy_backtester import StrategyBacktester


def _bars(start_minute: int, rows: int, base: float):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=start_minute)
    result = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        price = base + ((index % 4) - 1.5) * 0.2
        result.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=100_000,
            )
        )
    return result


def test_future_bars_do_not_change_prior_results():
    prefix = _bars(0, 50, 10.0)
    future = _bars(50, 20, 22.0)
    backtester = StrategyBacktester(strategy="baseline", initial_cash=10_000.0, max_position=200)
    prefix_result = backtester.run(prefix, symbol="SOXS.US")
    combined_result = backtester.run(prefix + future, symbol="SOXS.US")

    prefix_timestamps = {trade["submitted_at"] for trade in prefix_result.trades}
    combined_trades = [trade for trade in combined_result.trades if trade["submitted_at"] in prefix_timestamps]
    assert combined_trades == prefix_result.trades
