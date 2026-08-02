"""Dashboard snapshot helpers.

These helpers provide a small, read/write API for dashboard-only snapshots.
Snapshots are display metadata and must not be used as trading inputs.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]


def _state_dir() -> Path:
    return Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))


def _snapshot_name(name: str) -> str | None:
    snapshot_name = str(name or "").strip()
    if not snapshot_name or Path(snapshot_name).name != snapshot_name:
        return None
    if snapshot_name.endswith(".json"):
        return None
    return snapshot_name


def dashboard_snapshot_path(name: str, state_dir: Path | None = None) -> Path:
    """Return the canonical path for a dashboard snapshot name."""
    snapshot_name = _snapshot_name(name)
    if snapshot_name is None:
        raise ValueError("invalid dashboard snapshot name")
    root = Path(state_dir) if state_dir is not None else _state_dir()
    return root / "dashboard_snapshots" / f"{snapshot_name}.json"


def load_dashboard_snapshot(name: str, state_dir: Path | None = None) -> dict[str, object] | None:
    """Load dashboard snapshot data, returning None for any invalid snapshot."""
    try:
        path = dashboard_snapshot_path(name, state_dir=state_dir)
    except ValueError:
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data")
    return dict(data) if isinstance(data, dict) else None


def write_dashboard_snapshot(
    name: str,
    data: Mapping[str, Any],
    *,
    source_run_id: str | None = None,
    generated_at: str | None = None,
    state_dir: Path | None = None,
) -> Path:
    """Write a dashboard snapshot envelope atomically."""
    if not isinstance(data, Mapping):
        raise TypeError("dashboard snapshot data must be a mapping")
    path = dashboard_snapshot_path(name, state_dir=state_dir)
    envelope = {
        "generated_at": str(generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "source_run_id": str(source_run_id or ""),
        "data": dict(data),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return path
