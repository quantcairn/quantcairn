"""Test-only isolation for operational runtime persistence."""

from __future__ import annotations

import hashlib
import os
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT_NAMES = ("state", "reports", "artifacts", "logs", "runtime")


def _file_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in RUNTIME_ROOT_NAMES[:-1]:
        root = PROJECT_ROOT / name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            snapshot[str(path.relative_to(PROJECT_ROOT))] = digest.hexdigest()
    return snapshot


def _isolated_path(value: Path, roots: dict[str, Path]) -> Path | None:
    resolved = value.resolve()
    for name in RUNTIME_ROOT_NAMES:
        source_root = (PROJECT_ROOT / name).resolve()
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            continue
        return roots[name] / relative
    return None


def _isolated_string(value: str, roots: dict[str, Path]) -> str | None:
    for name in RUNTIME_ROOT_NAMES:
        source_root = str((PROJECT_ROOT / name).resolve())
        if value == source_root or value.startswith(source_root + os.sep):
            return str(roots[name]) + value[len(source_root) :]
    return None


def _remap_loaded_module_paths(monkeypatch: pytest.MonkeyPatch, roots: dict[str, Path]) -> None:
    for module in list(sys.modules.values()):
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            Path(module_file).resolve().relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            continue
        for name, value in list(vars(module).items()):
            replacement: Any | None = None
            if isinstance(value, Path):
                replacement = _isolated_path(value, roots)
            elif isinstance(value, str) and value.startswith(str(PROJECT_ROOT)):
                replacement = _isolated_string(value, roots)
            if replacement is not None:
                monkeypatch.setattr(module, name, replacement, raising=False)


@pytest.fixture(autouse=True)
def isolated_runtime_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    roots = {
        name: tmp_path / name
        for name in RUNTIME_ROOT_NAMES
    }
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(PROJECT_ROOT))
    monkeypatch.setenv("SOXS_STATE_DIR", str(roots["state"]))
    monkeypatch.setenv("SOXS_REPORTS_DIR", str(roots["reports"]))
    monkeypatch.setenv("SOXS_ARTIFACTS_DIR", str(roots["artifacts"]))
    monkeypatch.setenv("SOXS_LOGS_DIR", str(roots["logs"]))
    monkeypatch.setenv("SOXS_RUNTIME_DIR", str(roots["runtime"]))
    monkeypatch.setenv("SOXS_RUNTIME_AUDIT_DIR", str(roots["logs"]))
    _remap_loaded_module_paths(monkeypatch, roots)
    shadow_universe = sys.modules.get("src.shadow.universe")
    if shadow_universe is not None:
        monkeypatch.setattr(shadow_universe, "PROJECT_DIR", roots["state"].parent, raising=False)
    yield roots


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    session.config._quantcairn_runtime_snapshot = _file_snapshot()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    before = getattr(session.config, "_quantcairn_runtime_snapshot", {})
    after = _file_snapshot()
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(path for path in set(before) & set(after) if before[path] != after[path])
        warnings.warn(
            "QCRUNTIME001: test suite modified source-root runtime persistence: "
            f"added={added}, removed={removed}, changed={changed}",
            RuntimeWarning,
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
