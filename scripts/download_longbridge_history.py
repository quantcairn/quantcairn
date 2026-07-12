#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.data.longbridge_history import LongbridgeHistoryDownloader, LongbridgeHistoryError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download read-only Longbridge historical market data.")
    parser.add_argument("--symbols", required=True, help="Comma separated symbols, e.g. SOXS.US,SOXX.US,SMH.US")
    parser.add_argument("--frequencies", required=True, help="Comma separated frequencies, e.g. 15m,daily")
    parser.add_argument("--intraday-start", required=True, help="Intraday start date, e.g. 2023-12-04")
    parser.add_argument("--daily-start", required=True, help="Daily start date, e.g. 2020-01-01")
    parser.add_argument("--output-dir", required=True, help="Output directory, e.g. data/market/longbridge")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--request-interval", type=float, default=0.25)
    parser.add_argument("--adjustment", default="auto", help="auto, forward_adjust, no_adjust")
    parser.add_argument("--allow-after-hours", action="store_true", help="Allow non-regular sessions")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    symbols = [item.strip() for item in str(args.symbols).split(",") if item.strip()]
    frequencies = [item.strip() for item in str(args.frequencies).split(",") if item.strip()]
    downloader = LongbridgeHistoryDownloader(
        output_dir=Path(args.output_dir),
        page_size=args.page_size,
        max_retries=args.max_retries,
        request_interval_seconds=args.request_interval,
        regular_session_only=not bool(args.allow_after_hours),
        adjustment=args.adjustment,
    )
    try:
        result = downloader.download_many(
            symbols=symbols,
            frequencies=frequencies,
            intraday_start=args.intraday_start,
            daily_start=args.daily_start,
        )
        for artifact in result.get("files", []):
            path = Path(artifact["path"])
            simple_name = f"{artifact['symbol'].replace('.', '_')}_{artifact['frequency']}.csv"
            simple_path = path.with_name(simple_name)
            if path.exists():
                shutil.copy2(path, simple_path)
    except LongbridgeHistoryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
