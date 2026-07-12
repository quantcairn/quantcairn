#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.shadow import ShadowMarketDataSource, ShadowObserver, ShadowObservationError, ShadowRuntimeConfig, ShadowSafetyConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only shadow observation mode.")
    parser.add_argument("--output-dir", default=None, help="Shadow output directory")
    parser.add_argument("--symbol", default=None, help="Primary symbol, default SOXS.US")
    parser.add_argument(
        "--benchmarks",
        default=None,
        help="Comma separated benchmark symbols, default SOXX.US,SMH.US",
    )
    parser.add_argument("--frequency", default=None, help="Bar frequency, default 15m")
    parser.add_argument("--timeframe", default=None, help="Timeframe label, default 15m")
    parser.add_argument("--strategy-version", default=None, help="Strategy version label, default baseline")
    parser.add_argument("--strategy-family", default=None, help="Strategy family label, default from symbol catalog")
    parser.add_argument("--risk-profile", default=None, help="Risk profile label, default from symbol catalog")
    parser.add_argument("--symbol-class", default=None, help="Symbol class label, default from symbol catalog")
    parser.add_argument("--lookback-days", type=int, default=None, help="History warm-up days")
    parser.add_argument("--initial-cash", type=float, default=None, help="Simulated cash starting balance")
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--request-interval", type=float, default=None)
    parser.add_argument("--loop", action="store_true", help="Continuously observe until interrupted")
    return parser


def _merge_runtime_config(args: argparse.Namespace) -> ShadowRuntimeConfig:
    config = ShadowRuntimeConfig.from_env()
    symbol = str(args.symbol).strip().upper() if args.symbol else config.symbol
    timeframe = str(args.timeframe or args.frequency or config.timeframe).strip().lower() or config.timeframe
    strategy_family = str(args.strategy_family).strip() if args.strategy_family else config.strategy_family
    strategy_version = str(args.strategy_version).strip() if args.strategy_version else config.strategy_version
    risk_profile = str(args.risk_profile).strip().lower() if args.risk_profile else config.risk_profile
    symbol_class = str(args.symbol_class).strip().lower() if args.symbol_class else config.symbol_class
    return ShadowRuntimeConfig(
        output_dir=Path(args.output_dir) if args.output_dir else config.output_dir,
        symbol=symbol,
        benchmark_symbols=tuple(
            item.strip().upper() for item in str(args.benchmarks).split(",") if item.strip()
        )
        if args.benchmarks
        else config.benchmark_symbols,
        frequency=timeframe,
        timeframe=timeframe,
        strategy_version=strategy_version,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        regular_session_only=config.regular_session_only,
        shadow_enabled=config.shadow_enabled,
        trading_enabled=config.trading_enabled,
        symbol_class=symbol_class,
        lookback_days=int(args.lookback_days) if args.lookback_days is not None else config.lookback_days,
        initial_cash=float(args.initial_cash) if args.initial_cash is not None else config.initial_cash,
        page_size=int(args.page_size) if args.page_size is not None else config.page_size,
        max_retries=int(args.max_retries) if args.max_retries is not None else config.max_retries,
        request_interval_seconds=float(args.request_interval) if args.request_interval is not None else config.request_interval_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
        run_once=not bool(args.loop),
    )


def main() -> int:
    args = _build_parser().parse_args()
    safety = ShadowSafetyConfig.from_env()
    runtime = _merge_runtime_config(args)
    source = ShadowMarketDataSource(
        page_size=runtime.page_size,
        max_retries=runtime.max_retries,
        request_interval_seconds=runtime.request_interval_seconds,
        regular_session_only=True,
    )
    observer = ShadowObserver(safety_config=safety, runtime_config=runtime, market_source=source)
    try:
        if runtime.run_once:
            result = observer.run_once()
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        else:
            while True:
                result = observer.run_once()
                print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
                time.sleep(runtime.poll_interval_seconds)
    except ShadowObservationError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "safety": safety.to_audit_dict()}, indent=2, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}, indent=2, ensure_ascii=False))
        return 130
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
