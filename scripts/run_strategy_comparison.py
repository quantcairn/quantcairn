#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.backtest import BacktestDataFeed
from src.backtest.comparison import compare_versions


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an offline comparison across strategy versions.")
    parser.add_argument("--data", required=True, help="CSV or JSON path with OHLCV data")
    parser.add_argument("--benchmark-data", help="Optional CSV or JSON path with benchmark OHLCV data")
    parser.add_argument("--benchmark-symbol", help="Optional benchmark symbol override, e.g. SMH.US")
    parser.add_argument("--symbol", required=True, help="Ticker symbol, e.g. SOXS.US")
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--versions", default="baseline,a,b,c", help="Comma separated list of versions")
    parser.add_argument("--output-dir", default="artifacts/backtests")
    parser.add_argument("--config", help="Optional JSON file with additional parameters")
    parser.add_argument("--scenario", help="Optional scenario label stored in the report")
    return parser


def _load_params(path: str | None) -> dict:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("config file must contain a JSON object")
    return payload


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    params = _load_params(args.config)
    feed = BacktestDataFeed()
    bars = feed.load(Path(args.data), symbol=args.symbol)
    benchmark_bars = None
    if args.benchmark_data:
        benchmark_bars = feed.load(Path(args.benchmark_data), symbol=args.benchmark_symbol) if args.benchmark_symbol else feed.load(Path(args.benchmark_data))
    versions = [part.strip() for part in str(args.versions).split(",") if part.strip()]
    result = compare_versions(
        bars,
        symbol=args.symbol,
        benchmark_bars=benchmark_bars,
        versions=versions,
        initial_cash=args.initial_cash,
        parameter_set=params,
        output_dir=args.output_dir,
        scenario_name=args.scenario,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
