from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from src.backtest.strategy_backtester import StrategyBacktester
from src.backtest.models import Bar


def _make_bars(rows: int = 80, base: float = 10.0, amplitude: float = 0.25, drift: float = 0.0):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = base + math.sin(index / 4.0) * amplitude + index * drift
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


def test_baseline_and_version_a_run_without_future_data():
    bars = _make_bars()
    baseline = StrategyBacktester(strategy="baseline", initial_cash=10_000.0, max_position=200)
    version_a = StrategyBacktester(strategy="a", initial_cash=10_000.0, max_position=200)

    baseline_result = baseline.run(bars, symbol="SOXS.US")
    version_a_result = version_a.run(bars, symbol="SOXS.US")

    assert baseline_result.metrics["ending_equity"] > 0
    assert version_a_result.metrics["ending_equity"] > 0
    assert isinstance(baseline_result.to_dict(), dict)
    assert isinstance(version_a_result.to_dict(), dict)


def test_backtester_is_deterministic_and_lookahead_safe():
    bars = _make_bars()
    future_bars = _make_bars(rows=10, base=25.0, amplitude=1.0, drift=0.1)
    future_shifted = []
    for bar in future_bars:
        future_shifted.append(
            Bar(
                symbol=bar.symbol,
                timestamp=bar.timestamp + timedelta(days=1),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        )

    prefix = bars[:60]
    combined = prefix + future_shifted
    backtester = StrategyBacktester(strategy="a", initial_cash=10_000.0, max_position=200)
    prefix_result = backtester.run(prefix, symbol="SOXS.US")
    combined_result = backtester.run(combined, symbol="SOXS.US")

    prefix_trades = prefix_result.trades
    combined_trades = [trade for trade in combined_result.trades if trade["timestamp"] <= prefix[-1].timestamp.isoformat()]
    assert prefix_trades == combined_trades


def test_backtester_writes_artifacts(tmp_path):
    bars = _make_bars()
    backtester = StrategyBacktester(strategy="a", initial_cash=10_000.0, max_position=200)
    result = backtester.run(bars, symbol="SOXS.US", output_dir=tmp_path)

    artifact_dir = tmp_path / result.run_id
    assert (artifact_dir / "summary.json").exists()
    assert (artifact_dir / "metrics.json").exists()
    assert (artifact_dir / "equity_curve.csv").exists()


def test_version_c_fails_closed_without_benchmark():
    bars = _make_bars()
    backtester = StrategyBacktester(strategy="c", initial_cash=10_000.0, max_position=200)
    result = backtester.run(bars, symbol="SOXS.US")

    assert result.summary["benchmark_status"] == "MISSING_BENCHMARK"
    assert result.metrics["trade_count"] == 0
    assert any("invalid_benchmark" in warning or "benchmark_missing" in warning for warning in result.warnings)
