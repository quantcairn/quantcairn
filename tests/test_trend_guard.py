from __future__ import annotations

from datetime import datetime, timedelta

from src.strategy.trend_guard import TrendGuard


def _series(*, rows: int = 30, base: float = 10.0, step: float = 0.0, amplitude: float = 0.0) -> list[float]:
    values = []
    for i in range(rows):
        values.append(base + i * step + ((-1) ** i) * amplitude)
    return values


def test_range_regime_allows_buy_and_sell():
    guard = TrendGuard()
    closes = _series(rows=30, base=10.0, step=0.01, amplitude=0.03)
    result = guard.evaluate(
        timestamp=datetime(2026, 7, 11, 9, 30),
        current_price=closes[-1],
        closes=closes,
        symbol="SOFI",
    )

    assert result["regime"] == "RANGE"
    assert result["buy_allowed"] is True
    assert result["sell_allowed"] is True
    assert result["symbol_reduce_only"] is False


def test_strong_uptrend_blocks_buy_but_allows_sell():
    guard = TrendGuard()
    closes = _series(rows=30, base=10.0, step=0.2, amplitude=0.0)
    result = guard.evaluate(
        timestamp=datetime(2026, 7, 11, 9, 30),
        current_price=closes[-1],
        closes=closes,
        symbol="SOFI",
    )

    assert result["regime"] == "STRONG_UPTREND"
    assert result["buy_allowed"] is False
    assert result["sell_allowed"] is True
    assert "strong_uptrend" in result["trigger_reasons"]


def test_strong_downtrend_blocks_buy_but_allows_sell():
    guard = TrendGuard()
    closes = _series(rows=30, base=20.0, step=-0.2, amplitude=0.0)
    result = guard.evaluate(
        timestamp=datetime(2026, 7, 11, 9, 30),
        current_price=closes[-1],
        closes=closes,
        symbol="SOFI",
    )

    assert result["regime"] == "STRONG_DOWNTREND"
    assert result["buy_allowed"] is False
    assert result["sell_allowed"] is True


def test_high_volatility_blocks_new_buy():
    guard = TrendGuard()
    closes = _series(rows=30, base=10.0, step=0.0, amplitude=0.7)
    result = guard.evaluate(
        timestamp=datetime(2026, 7, 11, 9, 30),
        current_price=closes[-1],
        closes=closes,
        symbol="SOFI",
    )

    assert result["regime"] == "HIGH_VOLATILITY"
    assert result["buy_allowed"] is False
    assert result["sell_allowed"] is True


def test_invalid_or_insufficient_data_fails_closed():
    guard = TrendGuard()
    result = guard.evaluate(
        timestamp=datetime(2026, 7, 11, 9, 30),
        current_price=10.0,
        closes=[10.0, 10.1, 10.2],
        symbol="SOFI",
    )

    assert result["regime"] == "UNKNOWN"
    assert result["buy_allowed"] is False
    assert result["sell_allowed"] is True


def test_soxs_benchmark_uptrend_triggers_reduce_only():
    guard = TrendGuard()
    closes = _series(rows=30, base=5.0, step=0.005, amplitude=0.01)
    benchmark = _series(rows=30, base=4000.0, step=8.0, amplitude=0.0)
    result = guard.evaluate(
        timestamp=datetime(2026, 7, 11, 9, 30),
        current_price=closes[-1],
        closes=closes,
        benchmark_closes=benchmark,
        symbol="SOXS",
    )

    assert result["symbol_reduce_only"] is True
    assert result["buy_allowed"] is False
    assert result["sell_allowed"] is True
    assert "benchmark_strong_uptrend_blocks_soxs_buy" in result["trigger_reasons"]


def run_test_direct():
    test_range_regime_allows_buy_and_sell()
    test_strong_uptrend_blocks_buy_but_allows_sell()
    test_strong_downtrend_blocks_buy_but_allows_sell()
    test_high_volatility_blocks_new_buy()
    test_invalid_or_insufficient_data_fails_closed()
    test_soxs_benchmark_uptrend_triggers_reduce_only()


if __name__ == "__main__":
    run_test_direct()
