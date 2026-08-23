"""Side-effect-free resolution of QuantCairn runtime roots.

The source tree is the code root. Persistent runtime roots are independently
configurable so moving the code does not create a second operational state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str) -> Path | None:
    raw = str(os.environ.get(name, "") or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def resolve_project_dir(default: Path | None = None) -> Path:
    return (_env_path("SOXS_PROJECT_DIR") or Path(default or CODE_ROOT)).resolve()


def resolve_state_dir(project_dir: Path | None = None) -> Path:
    return (_env_path("SOXS_STATE_DIR") or resolve_project_dir(project_dir) / "state").resolve()


def resolve_reports_dir(project_dir: Path | None = None) -> Path:
    return (_env_path("SOXS_REPORTS_DIR") or resolve_project_dir(project_dir) / "reports").resolve()


def resolve_artifacts_dir(project_dir: Path | None = None) -> Path:
    return (_env_path("SOXS_ARTIFACTS_DIR") or resolve_project_dir(project_dir) / "artifacts").resolve()


def resolve_logs_dir(project_dir: Path | None = None) -> Path:
    return (_env_path("SOXS_LOG_DIR") or _env_path("SOXS_LOGS_DIR") or resolve_project_dir(project_dir) / "logs").resolve()


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved paths; construction never creates directories."""

    project_dir: Path
    state_dir: Path
    reports_dir: Path
    artifacts_dir: Path
    logs_dir: Path

    @classmethod
    def from_environment(cls, project_dir: Path | None = None) -> "RuntimePaths":
        project = resolve_project_dir(project_dir)
        return cls(
            project_dir=project,
            state_dir=resolve_state_dir(project),
            reports_dir=resolve_reports_dir(project),
            artifacts_dir=resolve_artifacts_dir(project),
            logs_dir=resolve_logs_dir(project),
        )


def runtime_paths(project_dir: Path | None = None) -> RuntimePaths:
    return RuntimePaths.from_environment(project_dir)
