#!/usr/bin/env python3
"""Generate a read-only daily research report and refresh the research site index."""
from __future__ import annotations

import argparse
import os
import subprocess
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

from src.research_report.generator import generate_daily_research_report
from src.research_report.site import build_research_site


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily research report outputs.")
    parser.add_argument("--date", default=None, help="Target report date (YYYY-MM-DD).")
    parser.add_argument("--project-dir", default=str(PROJECT_DIR), help="Repository root.")
    parser.add_argument("--reports-dir", default=None, help="Research report output dir.")
    parser.add_argument("--site-dir", default=None, help="Research site dir.")
    parser.add_argument("--no-site", action="store_true", help="Skip site rebuild.")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    reports_dir = Path(args.reports_dir).resolve() if args.reports_dir else project_dir / "reports" / "research"
    site_dir = Path(args.site_dir).resolve() if args.site_dir else project_dir / "site" / "research"

    report = generate_daily_research_report(
        _parse_date(args.date),
        project_dir=project_dir,
        reports_dir=reports_dir,
    )
    if not args.no_site:
        build_research_site(project_dir=project_dir, reports_dir=reports_dir, site_dir=site_dir)

    print(f"Generated research report: {report['output_paths']['json']}")
    print(f"Generated research report: {report['output_paths']['markdown']}")
    print(f"Generated research report: {report['output_paths']['html']}")
    if not args.no_site:
        print(f"Generated research site: {site_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
