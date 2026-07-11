from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from src.backtest.models import Bar
from src.backtest.walk_forward import WalkForwardConfig, WalkForwardEvaluator


def _make_bars(rows: int = 90):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = 10.0 + math.sin(index / 5.0) * 0.3
        bars.append(
            Bar(
                symbol="SOXS.US",
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=100_000,
            )
        )
    return bars


def test_walk_forward_selects_params_and_reports_windows():
    bars = _make_bars()
    evaluator = WalkForwardEvaluator(
        WalkForwardConfig(train_size=30, validation_size=15, test_size=15, step_size=15, purge_gap=2, embargo_gap=2)
    )
    result = evaluator.evaluate(
        bars,
        symbol="SOXS.US",
        strategy="a",
        parameter_grid=[
            {"minimum_range_pct": 1.0, "maximum_range_pct": 12.0},
            {"minimum_range_pct": 0.5, "maximum_range_pct": 8.0},
        ],
    )

    assert result.windows
    assert result.aggregate_oos_metrics is not None
    assert "selected_parameters" in result.windows[0].to_dict()
    assert result.no_trade_window_count >= 0


def test_walk_forward_reproducible():
    bars = _make_bars()
    evaluator = WalkForwardEvaluator(
        WalkForwardConfig(train_size=30, validation_size=15, test_size=15, step_size=15, purge_gap=2, embargo_gap=2)
    )
    left = evaluator.evaluate(bars, symbol="SOXS.US", strategy="a", parameter_grid=[{"minimum_range_pct": 1.0}])
    right = evaluator.evaluate(bars, symbol="SOXS.US", strategy="a", parameter_grid=[{"minimum_range_pct": 1.0}])
    assert left.to_dict() == right.to_dict()
