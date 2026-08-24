#!/usr/bin/env python3
"""Generate a read-only daily PAPER runtime snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.config.runtime_paths import runtime_paths
from src.reports.daily_runtime_snapshot import collect_daily_runtime_snapshot, write_daily_runtime_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect existing evidence into a daily runtime snapshot.")
    parser.add_argument("--date", dest="snapshot_date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    paths = runtime_paths(PROJECT_DIR)
    snapshot = collect_daily_runtime_snapshot(snapshot_date=args.snapshot_date, paths=paths)
    json_path, markdown_path = write_daily_runtime_snapshot(snapshot, reports_dir=paths.reports_dir)
    if args.json_output:
        print(json.dumps({"snapshot": snapshot, "json_path": str(json_path), "markdown_path": str(markdown_path)}, ensure_ascii=False, indent=2))
    else:
        print(f"Daily runtime snapshot: {json_path}")
        print(f"Markdown summary: {markdown_path}")
        print(f"Overall status: {snapshot['overall_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
