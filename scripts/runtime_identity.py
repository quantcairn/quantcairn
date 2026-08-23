#!/usr/bin/env python3
"""Print deployment identity without touching runtime state or the network."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.runtime_paths import runtime_paths


REQUIRED_SCRIPTS = (
    "scripts/run_ai_selector.py",
    "scripts/status.py",
    "scripts/start_combined.py",
    "scripts/run_candidate_validation_scheduler.py",
    "scripts/run_daily_research.py",
    "scripts/start_top_engines.sh",
    "scripts/start_orphan_monitor.py",
)


def _git_sha(project_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "UNKNOWN"
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _package_version(project_dir: Path) -> str:
    try:
        with (project_dir / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle).get("project", {}).get("version") or "UNKNOWN")
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return "UNKNOWN"


def _secret_status(*names: str) -> bool:
    return any(str(os.environ.get(name, "") or "").strip() for name in names)


def collect_identity(project_dir: Path | None = None) -> dict[str, object]:
    paths = runtime_paths(project_dir)
    project_dir = paths.project_dir
    return {
        "code_root": str(project_dir),
        "git_sha": _git_sha(project_dir),
        "package_version": _package_version(project_dir),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "execution_mode": str(os.environ.get("QUANTCAIRN_EXECUTION_MODE", "RESEARCH") or "RESEARCH").strip().upper(),
        "state_root": str(paths.state_dir),
        "reports_root": str(paths.reports_dir),
        "artifacts_root": str(paths.artifacts_dir),
        "logs_root": str(paths.logs_dir),
        "required_operational_scripts": {
            item: (project_dir / item).is_file() for item in REQUIRED_SCRIPTS
        },
        "telegram": {
            "bot_token_set": _secret_status(
                "SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN", "SOXS_TELEGRAM_BOT_TOKEN"
            ),
            "public_chat_id_set": _secret_status(
                "SOXS_OPENALPHA_TELEGRAM_CHAT_ID", "SOXS_TELEGRAM_CHAT_ID"
            ),
            "admin_chat_id_set": _secret_status(
                "QUANTCAIRN_ADMIN_CHAT_ID", "SOXS_OPENALPHA_ADMIN_CHAT_ID"
            ),
        },
    }


def identity_findings(identity: dict[str, object]) -> dict[str, object]:
    """Return deterministic, read-only deployment identity findings."""
    roots = {
        key: Path(str(identity[key]))
        for key in ("state_root", "reports_root", "artifacts_root", "logs_root")
    }
    code_root = Path(str(identity["code_root"]))
    configured_roots = {
        name: bool(str(os.environ.get(name, "") or "").strip())
        for name in ("SOXS_STATE_DIR", "SOXS_REPORTS_DIR", "SOXS_ARTIFACTS_DIR", "SOXS_LOGS_DIR")
    }
    warnings: list[str] = []
    blockers: list[str] = []
    if not code_root.is_dir():
        blockers.append("code_root_missing")
    for name, path in roots.items():
        if not configured_roots.get({
            "state_root": "SOXS_STATE_DIR",
            "reports_root": "SOXS_REPORTS_DIR",
            "artifacts_root": "SOXS_ARTIFACTS_DIR",
            "logs_root": "SOXS_LOGS_DIR",
        }[name], False) and path == code_root / name.removesuffix("_root"):
            warnings.append(f"implicit_project_local_{name}")
    if identity.get("execution_mode") == "LIVE":
        warnings.append("execution_mode_live_requires_explicit_review")
    scripts = identity.get("required_operational_scripts", {})
    missing = sorted(str(key) for key, present in scripts.items() if not present)
    if missing:
        warnings.append("missing_operational_scripts")
    return {
        "status": "BLOCKED" if blockers else "DEGRADED" if warnings else "HEALTHY",
        "warnings": warnings,
        "blockers": blockers,
        "missing_operational_scripts": missing,
        "explicit_runtime_roots": configured_roots,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()
    identity = collect_identity()
    if args.json:
        print(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for key, value in identity.items():
        print(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
