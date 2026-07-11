from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from src.strategy.dynamic_range import DynamicRangeCalculator


def _make_series(
    *,
    rows: int = 80,
    base: float = 10.0,
    amplitude: float = 0.18,
    drift: float = 0.0,
    spread_factor: float = 0.008,
    start: datetime | None = None,
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    start = start or datetime(2026, 7, 11, 9, 30)
    highs: list[tuple[datetime, float]] = []
    lows: list[tuple[datetime, float]] = []
    closes: list[tuple[datetime, float]] = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = base + math.sin(index / 6.0) * amplitude + index * drift
        highs.append((ts, round(close * (1.0 + spread_factor), 4)))
        lows.append((ts, round(close * (1.0 - spread_factor), 4)))
        closes.append((ts, round(close, 4)))
    return highs, lows, closes


def test_dynamic_range_valid_range_and_serializable():
    highs, lows, closes = _make_series()
    calculator = DynamicRangeCalculator()
    result = calculator.calculate(
        timestamp=closes[-1][0],
        current_price=closes[-1][1],
        highs=highs,
        lows=lows,
        closes=closes,
        atr_period=14,
        ema_period=20,
        rolling_lookback=20,
        minimum_range_pct=1.0,
        maximum_range_pct=12.0,
    )

    assert result["valid"] is True
    assert result["invalid_reason"] is None
    assert result["support"] < result["center"] < result["resistance"]
    assert result["grid_width"] > 0
    assert 1.0 <= result["range_width_pct"] <= 12.0
    assert result["calculation_version"] == "dynamic_range_v1"
    assert isinstance(json.dumps(result), str)


def test_dynamic_range_fails_closed_with_insufficient_data():
    highs, lows, closes = _make_series(rows=10)
    calculator = DynamicRangeCalculator()
    result = calculator.calculate(
        timestamp=closes[-1][0],
        current_price=closes[-1][1],
        highs=highs,
        lows=lows,
        closes=closes,
        atr_period=14,
        ema_period=20,
        rolling_lookback=20,
    )

    assert result["valid"] is False
    assert result["invalid_reason"] == "insufficient_data"


def test_dynamic_range_invalid_when_too_narrow():
    highs, lows, closes = _make_series(rows=80, amplitude=0.001, spread_factor=0.0002)
    calculator = DynamicRangeCalculator()
    result = calculator.calculate(
        timestamp=closes[-1][0],
        current_price=closes[-1][1],
        highs=highs,
        lows=lows,
        closes=closes,
        atr_period=14,
        ema_period=20,
        rolling_lookback=20,
        minimum_range_pct=3.0,
        maximum_range_pct=12.0,
    )

    assert result["valid"] is False
    assert result["invalid_reason"] == "range_too_narrow"


def test_dynamic_range_invalid_when_too_wide():
    highs, lows, closes = _make_series(rows=80, amplitude=1.25)
    calculator = DynamicRangeCalculator()
    result = calculator.calculate(
        timestamp=closes[-1][0],
        current_price=closes[-1][1],
        highs=highs,
        lows=lows,
        closes=closes,
        atr_period=14,
        ema_period=20,
        rolling_lookback=20,
        minimum_range_pct=1.0,
        maximum_range_pct=6.0,
    )

    assert result["valid"] is False
    assert result["invalid_reason"] == "range_too_wide"


def test_dynamic_range_grid_width_expands_with_volatility():
    low_vol = _make_series(rows=80, amplitude=0.05)
    high_vol = _make_series(rows=80, amplitude=0.55)
    calculator = DynamicRangeCalculator()

    low_result = calculator.calculate(
        timestamp=low_vol[2][-1][0],
        current_price=low_vol[2][-1][1],
        highs=low_vol[0],
        lows=low_vol[1],
        closes=low_vol[2],
    )
    high_result = calculator.calculate(
        timestamp=high_vol[2][-1][0],
        current_price=high_vol[2][-1][1],
        highs=high_vol[0],
        lows=high_vol[1],
        closes=high_vol[2],
    )

    assert high_result["grid_width"] > low_result["grid_width"]


def test_dynamic_range_ignores_future_data():
    highs, lows, closes = _make_series(rows=40, amplitude=0.08)
    future_start = closes[-1][0] + timedelta(minutes=1)
    future_highs, future_lows, future_closes = _make_series(
        rows=5,
        base=25.0,
        amplitude=0.5,
        start=future_start,
    )

    combined_highs = highs + future_highs
    combined_lows = lows + future_lows
    combined_closes = closes + future_closes

    calculator = DynamicRangeCalculator()
    base_result = calculator.calculate(
        timestamp=closes[-1][0],
        current_price=closes[-1][1],
        highs=highs,
        lows=lows,
        closes=closes,
    )
    future_result = calculator.calculate(
        timestamp=closes[-1][0],
        current_price=closes[-1][1],
        highs=combined_highs,
        lows=combined_lows,
        closes=combined_closes,
    )

    assert future_result["support"] == base_result["support"]
    assert future_result["resistance"] == base_result["resistance"]
    assert future_result["grid_width"] == base_result["grid_width"]


def run_test_direct():
    test_dynamic_range_valid_range_and_serializable()
    test_dynamic_range_fails_closed_with_insufficient_data()
    test_dynamic_range_invalid_when_too_narrow()
    test_dynamic_range_invalid_when_too_wide()
    test_dynamic_range_grid_width_expands_with_volatility()
    test_dynamic_range_ignores_future_data()


if __name__ == "__main__":
    run_test_direct()
