from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backtest.models import Bar
from src.backtest.strategy_backtester import StrategyBacktester


def test_dynamic_range_paper_lifecycle_runs_end_to_end():
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(90):
        ts = start + timedelta(minutes=index)
        price = 10.0 + ((index % 18) - 9) * 0.05
        bars.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=round(price * 0.999, 4),
                high=round(price * 1.01, 4),
                low=round(price * 0.99, 4),
                close=round(price, 4),
                volume=100_000,
            )
        )

    backtester = StrategyBacktester(strategy="a", initial_cash=10_000.0, max_position=200)
    result = backtester.run(bars, symbol="SOXS.US")

    assert result.metrics["ending_equity"] > 0
    assert result.summary["bars"] == 90
    assert isinstance(result.trades, list)
