from __future__ import annotations

import os
import plistlib
from pathlib import Path

from src.config.runtime_paths import resolve_runtime_dir


def test_runtime_dir_resolves_at_operation_time_and_is_cwd_independent(tmp_path, monkeypatch):
    root_a = tmp_path / "a" / "state"
    root_b = tmp_path / "b" / "state"
    monkeypatch.delenv("SOXS_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("SOXS_STATE_DIR", str(root_a))
    first_cwd = Path.cwd()
    assert resolve_runtime_dir() == (root_a / "runtime").resolve()
    monkeypatch.chdir(tmp_path)
    assert resolve_runtime_dir() == (root_a / "runtime").resolve()
    monkeypatch.setenv("SOXS_STATE_DIR", str(root_b))
    assert resolve_runtime_dir() == (root_b / "runtime").resolve()
    monkeypatch.setenv("SOXS_RUNTIME_DIR", str(tmp_path / "explicit-runtime"))
    assert resolve_runtime_dir() == (tmp_path / "explicit-runtime").resolve()
    monkeypatch.chdir(first_cwd)


def test_combined_pid_path_is_external_and_runtime_resolved(tmp_path, monkeypatch):
    import src.dashboard.combined as combined

    monkeypatch.setattr(combined, "COMBINED_PID_FILE", None)
    monkeypatch.setattr(combined, "RUNTIME_DIR", None)
    root_a = tmp_path / "a" / "state"
    root_b = tmp_path / "b" / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(root_a))
    monkeypatch.delenv("SOXS_RUNTIME_DIR", raising=False)
    assert combined._combined_pid_file_path() == (root_a / "runtime" / "combined.pid").resolve()
    monkeypatch.setenv("SOXS_STATE_DIR", str(root_b))
    assert combined._combined_pid_file_path() == (root_b / "runtime" / "combined.pid").resolve()
    combined._write_pid_file(12345)
    pid_path = root_b / "runtime" / "combined.pid"
    assert pid_path.read_text(encoding="utf-8") == "12345"
    assert not (Path(combined.PROJECT_DIR) / "runtime" / "combined.pid").exists()
    combined._remove_pid_file()
    assert not pid_path.exists()


def test_paper_launchd_templates_disable_bytecode_writes():
    template_root = Path(__file__).resolve().parents[1] / "deploy" / "launchd"
    names = (
        "com.quantcairn.combined.plist.template",
        "com.quantcairn.top-engines.plist.template",
        "com.quantcairn.ai-selector.plist.template",
        "com.quantcairn.candidate-validation.plist.template",
        "com.quantcairn.research.plist.template",
    )
    for name in names:
        with (template_root / name).open("rb") as handle:
            plist = plistlib.load(handle)
        assert plist["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_pid_lifecycle_does_not_write_release_tree(tmp_path, monkeypatch):
    import src.dashboard.combined as combined

    release_root = tmp_path / "release"
    release_root.mkdir()
    marker = release_root / "marker.txt"
    marker.write_text("immutable", encoding="utf-8")
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(release_root))
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "external-state"))
    monkeypatch.delenv("SOXS_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(combined, "COMBINED_PID_FILE", None)
    monkeypatch.setattr(combined, "RUNTIME_DIR", None)
    combined._write_pid_file(os.getpid())
    assert marker.read_text(encoding="utf-8") == "immutable"
    assert (tmp_path / "external-state" / "runtime" / "combined.pid").exists()
    assert not list(release_root.rglob("*.pid"))
    combined._remove_pid_file()
