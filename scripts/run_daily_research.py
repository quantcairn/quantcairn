#!/usr/bin/env python3
"""Run the read-only daily research pipeline."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
VENV_PREFIX = PROJECT_DIR / ".venv"
if (
    VENV_PYTHON.exists()
    and Path(sys.prefix).resolve() != VENV_PREFIX.resolve()
    and os.environ.get("SOXS_SKIP_VENV_REEXEC") != "1"
):
    env = os.environ.copy()
    env["SOXS_SKIP_VENV_REEXEC"] = "1"
    os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), __file__, *sys.argv[1:]], env)

print(f"Using Python: {sys.executable}")
sys.path.insert(0, str(PROJECT_DIR))

from src.candidate_validation.research_scheduler import DailyResearchScheduler


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"invalid --date: {value}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the daily research pipeline.")
    parser.add_argument(
        "--mode",
        choices=("legacy", "independent"),
        default="legacy",
        help="Research execution mode (default: legacy).",
    )
    parser.add_argument("--date", default=None, help="Target research date (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan without writing files.")
    parser.add_argument("--force", action="store_true", help="Regenerate the current day's report.")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    scheduler = DailyResearchScheduler()
    result = scheduler.run(
        _parse_date(args.date),
        mode=args.mode,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
    )

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.mode == "independent" and result.get("status") in {
        "failed",
        "blocked_missing_bundle",
        "blocked_invalid_bundle",
    }:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
