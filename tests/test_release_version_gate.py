"""Tests for the release version consistency gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_release_version.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_release_version_gate_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_tag_version_normalization():
    module = _load_module()
    assert module._normalize_release_tag_version("v0.12.13-dashboard-performance-optimization") == "0.12.13"
    assert module._normalize_release_tag_version("refs/tags/v0.12.13-dashboard-performance-optimization") == "0.12.13"
    assert module._normalize_release_tag_version("0.12.13") == "0.12.13"


def test_release_version_matches_pyproject_and_package(tmp_path: Path, monkeypatch):
    module = _load_module()
    project_root = tmp_path
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'quantcairn'\nversion = '0.12.13'\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "_read_package_version", lambda: "0.12.13")
    result = module.check_release_version(tag="v0.12.13-dashboard-performance-optimization", project_root=project_root)

    assert result["pyproject_version"] == "0.12.13"
    assert result["package_version"] == "0.12.13"
    assert result["release_tag_version"] == "0.12.13"


def test_release_version_mismatch_fails_fast(tmp_path: Path, monkeypatch):
    module = _load_module()
    project_root = tmp_path
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'quantcairn'\nversion = '0.12.13'\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "_read_package_version", lambda: "0.12.12")

    with pytest.raises(ValueError, match="package version mismatch"):
        module.check_release_version(tag="v0.12.13-dashboard-performance-optimization", project_root=project_root)


def test_release_tag_version_mismatch_fails_fast(tmp_path: Path, monkeypatch):
    module = _load_module()
    project_root = tmp_path
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'quantcairn'\nversion = '0.12.13'\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "_read_package_version", lambda: "0.12.13")

    with pytest.raises(ValueError, match="release tag version mismatch"):
        module.check_release_version(tag="v0.12.14-dashboard-performance-optimization", project_root=project_root)


def test_main_reports_success_and_failure(tmp_path: Path, monkeypatch, capsys):
    module = _load_module()
    project_root = tmp_path
    (project_root / "pyproject.toml").write_text(
        "[project]\nname = 'quantcairn'\nversion = '0.12.13'\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "_read_package_version", lambda: "0.12.13")

    exit_code = module.main(["--tag", "v0.12.13-dashboard-performance-optimization", "--project-root", str(project_root)])
    out = capsys.readouterr()
    assert exit_code == 0
    assert "Version OK:" in out.out

    monkeypatch.setattr(module, "_read_package_version", lambda: "0.12.12")
    exit_code = module.main(["--tag", "v0.12.13-dashboard-performance-optimization", "--project-root", str(project_root)])
    err = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR: package version mismatch" in err.err
