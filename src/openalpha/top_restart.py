"""Selector-to-TOP supervisor coordination helpers.

This module records runtime restart state separately from the committed
Selection Bundle. The bundle's identity remains stable while engine process
coordination moves from pending to confirmed or failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.runtime_paths import resolve_project_dir, resolve_state_dir


def _project_dir(project_dir: Path | None = None) -> Path:
    return resolve_project_dir(project_dir)


def restart_status_path(project_dir: Path | None = None) -> Path:
    root = resolve_state_dir(_project_dir(project_dir))
    return root / "top_restart_status.json"


def load_restart_status(project_dir: Path | None = None) -> dict[str, Any] | None:
    path = restart_status_path(project_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def record_restart_status(
    *,
    status: str,
    selection_run_id: str = "",
    selection_bundle_hash: str = "",
    error: str = "",
    bundle_sync_status: str = "OK",
    runtime_sync_status: str | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Write only coordination evidence; never alter bundle identity files."""

    path = restart_status_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_status = str(status or "UNKNOWN").upper()
    if runtime_sync_status is None:
        runtime_sync_status = {
            "PENDING": "PENDING",
            "CONFIRMED": "OK",
            "FAILED": "FAILED",
        }.get(normalized_status, normalized_status)
    payload = {
        "status": normalized_status,
        "bundle_sync_status": str(bundle_sync_status or "UNKNOWN").upper(),
        "runtime_sync_status": str(runtime_sync_status or "UNKNOWN").upper(),
        "top_restart_status": normalized_status,
        "top_sync_status_semantics": "RUNTIME_PROCESS_SYNC",
        "selection_run_id": str(selection_run_id or ""),
        "selection_bundle_hash": str(selection_bundle_hash or ""),
        "error": str(error or ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def request_supervisor_restart(project_dir: Path | None = None) -> int:
    """Request restart through the canonical supervisor control client."""

    root = _project_dir(project_dir)
    launcher = root / "scripts" / "start_top_engines.sh"
    if not launcher.is_file():
        return 1
    return subprocess.run(
        ["/bin/bash", str(launcher), "restart"],
        cwd=root,
        check=False,
    ).returncode
