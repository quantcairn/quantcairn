#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.backtest import BacktestDataFeed, StrategyBacktester, WalkForwardConfig, WalkForwardEvaluator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a deterministic walk-forward evaluation.")
    parser.add_argument("--data", required=True, help="CSV path with OHLCV data")
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--strategy",
        default="a",
        choices=["baseline", "a", "b", "c", "version_a", "version_b", "version_c"],
    )
    parser.add_argument("--config", required=True, help="JSON file describing walk-forward windows and parameter grid")
    parser.add_argument("--output-dir", default="artifacts/backtests")
    parser.add_argument("--max-candidates", type=int, default=50)
    return parser


def _load_config(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("config file must contain a JSON object")
    return payload


def _config_int(cfg: dict, *names: str, default: int | None = None) -> int:
    for name in names:
        if name in cfg and cfg[name] is not None:
            return int(cfg[name])
    if default is not None:
        return int(default)
    raise SystemExit(f"missing required config field: one of {', '.join(names)}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    cfg = _load_config(args.config)
    wf_cfg = WalkForwardConfig(
        train_size=_config_int(cfg, "train_size", "train_days"),
        validation_size=_config_int(cfg, "validation_size", "validation_days"),
        test_size=_config_int(cfg, "test_size", "test_days"),
        step_size=_config_int(cfg, "step_size", "step_days"),
        anchored=bool(cfg.get("anchored", False)),
        purge_gap=int(cfg.get("purge_gap", 0)),
        embargo_gap=int(cfg.get("embargo_gap", 0)),
    )
    parameter_grid = cfg.get("parameter_grid") or [{}]
    if not isinstance(parameter_grid, list):
        raise SystemExit("parameter_grid must be a list")
    feed = BacktestDataFeed()
    bars = feed.load(Path(args.data), symbol=args.symbol)
    evaluator = WalkForwardEvaluator(wf_cfg, backtester=StrategyBacktester(strategy=args.strategy))
    result = evaluator.evaluate(
        bars,
        symbol=args.symbol,
        strategy=args.strategy,
        parameter_grid=parameter_grid,
        output_dir=args.output_dir,
        max_candidates=args.max_candidates,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
