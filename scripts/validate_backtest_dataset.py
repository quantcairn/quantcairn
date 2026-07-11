#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.backtest.dataset_validation import validate_backtest_dataset


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate offline backtest datasets without running trades.")
    parser.add_argument("--symbol-csv", required=True, help="Symbol OHLCV CSV file")
    parser.add_argument("--benchmark-csv", help="Benchmark OHLCV CSV file")
    parser.add_argument("--symbol", required=True, help="Symbol ticker, e.g. SOXS.US")
    parser.add_argument("--benchmark", help="Benchmark ticker, e.g. SOXX.US")
    parser.add_argument("--expected-frequency", required=True, help="Expected data frequency, e.g. 15m or daily")
    parser.add_argument("--output", help="Optional JSON output file")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = validate_backtest_dataset(
        symbol_csv=Path(args.symbol_csv),
        benchmark_csv=Path(args.benchmark_csv) if args.benchmark_csv else None,
        symbol=args.symbol,
        benchmark=args.benchmark,
        expected_frequency=str(args.expected_frequency),
    )
    payload = report.to_dict()
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0 if report.formal_backtest_eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
