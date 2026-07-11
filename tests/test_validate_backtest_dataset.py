from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.backtest.dataset_validation import validate_backtest_dataset


def _frame(symbol: str, start: datetime, step: timedelta, rows: int, base: float) -> pd.DataFrame:
    data = []
    for index in range(rows):
        ts = start + step * index
        price = base + index * 0.1
        data.append(
            {
                "timestamp": ts.isoformat(),
                "symbol": symbol,
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price + 0.05,
                "volume": 1000 + index,
            }
        )
    return pd.DataFrame(data)


def test_validate_backtest_dataset_accepts_allowed_pair_and_matching_frequency(tmp_path):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    symbol_csv = tmp_path / "soxs_15m.csv"
    benchmark_csv = tmp_path / "smh_15m.csv"
    _frame("SOXS.US", start, timedelta(minutes=15), 8, 10.0).to_csv(symbol_csv, index=False)
    _frame("SMH.US", start, timedelta(minutes=15), 8, 100.0).to_csv(benchmark_csv, index=False)

    report = validate_backtest_dataset(
        symbol_csv=symbol_csv,
        benchmark_csv=benchmark_csv,
        symbol="SOXS.US",
        benchmark="SMH.US",
        expected_frequency="15m",
    )

    assert report.benchmark_mapping_status == "VALID"
    assert report.symbol_frequency == "15m"
    assert report.benchmark_frequency == "15m"
    assert report.formal_backtest_eligible is True
    assert report.fail_closed_reason is None
    assert report.overlap_ratio == 1.0


def test_validate_backtest_dataset_rejects_invalid_benchmark_and_missing_benchmark(tmp_path):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    symbol_csv = tmp_path / "soxs_15m.csv"
    benchmark_csv = tmp_path / "aapl_15m.csv"
    _frame("SOXS.US", start, timedelta(minutes=15), 8, 10.0).to_csv(symbol_csv, index=False)
    _frame("AAPL.US", start, timedelta(minutes=15), 8, 200.0).to_csv(benchmark_csv, index=False)

    invalid = validate_backtest_dataset(
        symbol_csv=symbol_csv,
        benchmark_csv=benchmark_csv,
        symbol="SOXS.US",
        benchmark="AAPL.US",
        expected_frequency="15m",
    )
    assert invalid.benchmark_mapping_status == "INVALID_BENCHMARK"
    assert invalid.formal_backtest_eligible is False
    assert invalid.fail_closed_reason in {"benchmark_symbol_not_permitted", "benchmark_no_time_overlap", "benchmark_frequency_mismatch"}

    missing = validate_backtest_dataset(
        symbol_csv=symbol_csv,
        benchmark_csv=None,
        symbol="SOXS.US",
        benchmark=None,
        expected_frequency="15m",
    )
    assert missing.benchmark_mapping_status == "MISSING_BENCHMARK"
    assert missing.formal_backtest_eligible is False
    assert missing.fail_closed_reason in {"benchmark_missing", "benchmark_csv_empty"}


def test_validate_backtest_dataset_rejects_frequency_mismatch_and_future_data(tmp_path):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    symbol_csv = tmp_path / "soxs_15m.csv"
    benchmark_csv = tmp_path / "smh_daily.csv"
    _frame("SOXS.US", start, timedelta(minutes=15), 8, 10.0).to_csv(symbol_csv, index=False)
    _frame("SMH.US", start, timedelta(days=1), 3, 100.0).to_csv(benchmark_csv, index=False)

    mismatch = validate_backtest_dataset(
        symbol_csv=symbol_csv,
        benchmark_csv=benchmark_csv,
        symbol="SOXS.US",
        benchmark="SMH.US",
        expected_frequency="15m",
    )
    assert mismatch.formal_backtest_eligible is False
    assert mismatch.fail_closed_reason in {"benchmark_frequency_mismatch", "symbol_frequency_mismatch"}

    future_benchmark_csv = tmp_path / "smh_future.csv"
    future_frame = _frame("SMH.US", start, timedelta(minutes=15), 8, 100.0)
    future_frame.loc[len(future_frame)] = {
        "timestamp": (start + timedelta(minutes=15 * 20)).isoformat(),
        "symbol": "SMH.US",
        "open": 200.0,
        "high": 200.2,
        "low": 199.8,
        "close": 200.1,
        "volume": 2000,
    }
    future_frame.to_csv(future_benchmark_csv, index=False)
    future = validate_backtest_dataset(
        symbol_csv=symbol_csv,
        benchmark_csv=future_benchmark_csv,
        symbol="SOXS.US",
        benchmark="SMH.US",
        expected_frequency="15m",
    )
    assert future.future_data_risk is True
    assert future.formal_backtest_eligible is False


def test_validate_backtest_dataset_cli_writes_json(tmp_path):
    start = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    symbol_csv = tmp_path / "soxs_15m.csv"
    benchmark_csv = tmp_path / "smh_15m.csv"
    output = tmp_path / "report.json"
    _frame("SOXS.US", start, timedelta(minutes=15), 8, 10.0).to_csv(symbol_csv, index=False)
    _frame("SMH.US", start, timedelta(minutes=15), 8, 100.0).to_csv(benchmark_csv, index=False)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_backtest_dataset.py",
            "--symbol-csv",
            str(symbol_csv),
            "--benchmark-csv",
            str(benchmark_csv),
            "--symbol",
            "SOXS.US",
            "--benchmark",
            "SMH.US",
            "--expected-frequency",
            "15m",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["formal_backtest_eligible"] is True
    assert payload["benchmark_mapping_status"] == "VALID"
