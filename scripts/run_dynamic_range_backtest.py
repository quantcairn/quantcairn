#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.backtest import BacktestDataFeed, StrategyBacktester


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a deterministic dynamic range backtest.")
    parser.add_argument("--data", required=True, help="CSV path or JSON path with OHLCV data")
    parser.add_argument("--symbol", required=True, help="Ticker symbol, e.g. SOXS.US")
    parser.add_argument("--strategy", default="baseline", choices=["baseline", "a"], help="Backtest variant")
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--output-dir", default="artifacts/backtests")
    parser.add_argument("--config", help="Optional JSON file with additional parameters")
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
    backtester = StrategyBacktester(strategy=args.strategy, initial_cash=args.initial_cash)
    result = backtester.run(bars, symbol=args.symbol, output_dir=args.output_dir, parameter_set=params)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
