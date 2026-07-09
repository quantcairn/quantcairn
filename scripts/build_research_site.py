#!/usr/bin/env python3
"""Build the static research report site index from generated daily reports."""
from __future__ import annotations

import os
import sys
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

from src.research_report.site import build_research_site


def main() -> int:
    index_path = build_research_site(project_dir=PROJECT_DIR)
    print(f"Generated research site: {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
