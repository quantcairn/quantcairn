from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sin

from src.backtest.comparison import compare_versions
from src.backtest.models import Bar


def _series(*, symbol: str, rows: int = 180, base: float = 10.0, amplitude: float = 0.7, drift: float = 0.0):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(rows):
        ts = start + timedelta(minutes=index)
        close = base + sin(index / 3.0) * amplitude + index * drift
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=round(close * 0.999, 4),
                high=round(close * 1.01, 4),
                low=round(close * 0.99, 4),
                close=round(close, 4),
                volume=150_000,
            )
        )
    return bars


def test_soxs_rejects_aapl_as_benchmark_and_fails_closed():
    bars = _series(symbol="SOXS.US")
    benchmark = _series(symbol="AAPL.US", base=180.0, amplitude=1.2)

    result = compare_versions(
        bars,
        symbol="SOXS.US",
        benchmark_bars=benchmark,
        versions=["baseline", "a", "b", "c"],
        initial_cash=10_000.0,
        minimum_trade_count_for_ranking=0,
    )

    assert result.summary["benchmark_status"] == "INVALID_BENCHMARK"
    assert result.summary["benchmark_validation"]["reason"] == "benchmark_symbol_not_permitted"
    assert result.summary["eligible_ranking"] == []
    assert result.summary["ranking_status"] == "INVALID_BENCHMARK"
    assert result.summary["best_version"] is None
    assert "invalid_benchmark" in result.warnings


def test_missing_benchmark_fails_closed_without_formal_ranking():
    bars = _series(symbol="SOXS.US")

    result = compare_versions(
        bars,
        symbol="SOXS.US",
        benchmark_bars=None,
        versions=["baseline", "a", "b", "c"],
        initial_cash=10_000.0,
        minimum_trade_count_for_ranking=0,
    )

    assert result.summary["benchmark_status"] == "MISSING_BENCHMARK"
    assert result.summary["eligible_ranking"] == []
    assert result.summary["ranking_status"] == "INVALID_BENCHMARK"
    assert result.summary["benchmark_validation"]["reason"] == "benchmark_missing"


def test_allowed_benchmark_keeps_eligible_ranking_and_alignment():
    bars = _series(symbol="SOXS.US")
    benchmark = _series(symbol="SMH.US", base=400.0, amplitude=0.8)

    result = compare_versions(
        bars,
        symbol="SOXS.US",
        benchmark_bars=benchmark,
        versions=["baseline", "a", "b", "c"],
        initial_cash=10_000.0,
        minimum_trade_count_for_ranking=0,
    )

    assert result.summary["benchmark_status"] == "VALID"
    assert result.summary["benchmark_validation"]["benchmark_symbol"] == "SMH"
    assert result.summary["eligible_ranking"]
    assert result.summary["ranking_status"] == "ELIGIBLE"
    assert result.summary["best_version"] is not None


def test_aapl_accepts_qqq_or_spy_benchmark():
    bars = _series(symbol="AAPL.US", base=180.0, amplitude=1.2)
    benchmark = _series(symbol="QQQ.US", base=400.0, amplitude=0.8)

    result = compare_versions(
        bars,
        symbol="AAPL.US",
        benchmark_bars=benchmark,
        versions=["baseline", "a", "b", "c"],
        initial_cash=10_000.0,
        minimum_trade_count_for_ranking=0,
    )

    assert result.summary["benchmark_status"] == "VALID"
    assert result.summary["benchmark_validation"]["benchmark_symbol"] == "QQQ"


def test_low_trade_count_stays_in_insufficient_evidence_bucket():
    bars = _series(symbol="SOXS.US", rows=40, amplitude=0.05)
    benchmark = _series(symbol="SMH.US", rows=40, base=400.0, amplitude=0.03)

    result = compare_versions(
        bars,
        symbol="SOXS.US",
        benchmark_bars=benchmark,
        versions=["baseline", "a", "b", "c"],
        initial_cash=10_000.0,
    )

    assert result.summary["benchmark_status"] == "VALID"
    assert result.summary["ranking_status"] in {"ELIGIBLE", "INSUFFICIENT_EVIDENCE"}
    if result.summary["ranking_status"] == "INSUFFICIENT_EVIDENCE":
        assert result.summary["eligible_ranking"] == []
        assert "insufficient_evidence" in result.warnings
