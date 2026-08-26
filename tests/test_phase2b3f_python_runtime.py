from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

from scripts.validate_top_runtime import (
    RuntimeTrustRoots,
    _classify_import_origin,
    validate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_paper_runtime_extra_contains_complete_top_runtime_dependencies() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extra = metadata["project"]["optional-dependencies"]["paper-runtime"]
    assert extra == ["quantcairn[broker,dashboard]"]
    assert "longbridge>=4.0" in metadata["project"]["optional-dependencies"]["broker"]
    assert "flask>=3.0" in metadata["project"]["optional-dependencies"]["dashboard"]


def test_current_interpreter_imports_top_required_modules() -> None:
    result = validate(PROJECT_ROOT)
    assert result["import_failures"] == []
    assert "longbridge" in result["third_party_imports"]
    assert "flask" in result["third_party_imports"]
    assert set(result["top_required_imports"]) == {
        "run", "src.engine.trading_engine", "src.dashboard.server"
    }


def test_import_gate_reports_editable_install_as_non_production() -> None:
    result = validate(PROJECT_ROOT)
    assert result["editable_install_present"] is True
    assert result["status"] == "BLOCKED"


def test_gate_cli_reports_missing_dependency_without_side_effects(tmp_path: Path) -> None:
    project = tmp_path / "release"
    project.mkdir()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "SOXS_PROJECT_DIR": str(project)}
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts/validate_top_runtime.py"), "--json", "--project-root", str(project)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert '"status": "BLOCKED"' in result.stdout
    assert not (project / "state").exists()


def test_import_origin_classes_allow_release_venv_and_standard_library(tmp_path: Path) -> None:
    release = tmp_path / "releases" / "sha-a"
    venv_site = tmp_path / "venvs" / "sha-a" / "lib" / "python3.14" / "site-packages"
    stdlib = Path(sysconfig.get_paths()["stdlib"]).resolve()
    roots = RuntimeTrustRoots(
        project_root=release,
        python_bin=tmp_path / "venvs" / "sha-a" / "bin" / "python",
        python_prefix=tmp_path / "venvs" / "sha-a",
        site_package_roots=(venv_site,),
        standard_library_roots=(stdlib,),
    )

    assert _classify_import_origin(str(release / "src" / "module.py"), roots) == "PROJECT_RELEASE"
    assert _classify_import_origin(str(venv_site / "yaml" / "__init__.py"), roots) == "APPROVED_VENV"
    assert _classify_import_origin(str(stdlib / "json" / "__init__.py"), roots) == "STANDARD_LIBRARY"
    assert _classify_import_origin("BUILTIN", roots) == "STANDARD_LIBRARY"


def test_validate_accepts_release_and_associated_venv_origins(monkeypatch, tmp_path: Path) -> None:
    release = tmp_path / "releases" / "sha-a"
    venv = tmp_path / "venvs" / "sha-a"
    site_packages = venv / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True)
    (release / "src").mkdir(parents=True)
    python_bin = venv / "bin" / "python"
    monkeypatch.setenv("SOXS_PYTHON_BIN", str(python_bin))
    monkeypatch.setenv("SOXS_RELEASE_SHA", "sha-a")

    def fake_import(names, project_root):
        locations = {
            name: str(site_packages / f"{name}.py") if name in {"yaml", "numpy", "pandas", "flask", "longbridge"}
            else str(release / f"{name.replace('.', '/')}.py")
            for name in names
        }
        return list(names), locations, []

    monkeypatch.setattr("scripts.validate_top_runtime._import_modules", fake_import)
    monkeypatch.setattr("scripts.validate_top_runtime._editable_install", lambda project_root: False)

    result = validate(release)

    assert result["status"] == "PASS"
    assert result["imports_outside_release"] == {}
    assert set(result["import_origin_classes"].values()) == {"PROJECT_RELEASE", "APPROVED_VENV"}
    assert result["release_venv_identity"]["status"] == "PASS"


def test_validate_rejects_development_source_and_venv_origins(monkeypatch, tmp_path: Path) -> None:
    release = tmp_path / "releases" / "sha-a"
    venv = tmp_path / "venvs" / "sha-a"
    development = tmp_path / "quantcairn"
    development_venv = development / ".venv" / "lib" / "python3.14" / "site-packages"
    monkeypatch.setenv("SOXS_PYTHON_BIN", str(venv / "bin" / "python"))
    monkeypatch.setenv("SOXS_RELEASE_SHA", "sha-a")

    def fake_import(names, project_root):
        locations = {
            name: str(development / "src" / f"{name.replace('.', '/')}.py")
            if index == 0
            else str(development_venv / f"{name}.py")
            for index, name in enumerate(names)
        }
        return list(names), locations, []

    monkeypatch.setattr("scripts.validate_top_runtime._import_modules", fake_import)
    monkeypatch.setattr("scripts.validate_top_runtime._editable_install", lambda project_root: False)

    result = validate(release)

    assert result["status"] == "BLOCKED"
    assert set(result["import_origin_classes"].values()) == {"FORBIDDEN_EXTERNAL"}
    assert result["imports_outside_release"]


def test_validate_rejects_mismatched_release_associated_venv(monkeypatch, tmp_path: Path) -> None:
    release = tmp_path / "releases" / "sha-a"
    venv = tmp_path / "venvs" / "sha-b"
    site_packages = venv / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setenv("SOXS_PYTHON_BIN", str(venv / "bin" / "python"))
    monkeypatch.setenv("SOXS_RELEASE_SHA", "sha-a")

    def fake_import(names, project_root):
        return list(names), {name: str(site_packages / f"{name}.py") for name in names}, []

    monkeypatch.setattr("scripts.validate_top_runtime._import_modules", fake_import)
    monkeypatch.setattr("scripts.validate_top_runtime._editable_install", lambda project_root: False)

    result = validate(release)

    assert result["status"] == "BLOCKED"
    assert result["release_venv_identity"]["status"] == "FAIL"


def test_supervisor_records_child_exit_evidence(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    configs = tmp_path / "configs"
    control = tmp_path / "state" / "top_supervisor"
    scripts.mkdir()
    configs.mkdir()
    for index in range(1, 4):
        (configs / f"TOP{index}.yaml").write_text("mode: paper\n", encoding="utf-8")
    child = scripts / "run_top_engine.sh"
    child.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    child.chmod(0o755)
    supervisor = scripts / "start_top_engines.sh"
    supervisor.write_text(
        (PROJECT_ROOT / "scripts/start_top_engines.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    supervisor.chmod(0o755)

    result = subprocess.run(
        ["bash", str(supervisor)],
        cwd=tmp_path,
        env={
            **os.environ,
            "SOXS_TOP_CONFIG_DIR": str(configs),
            "SOXS_TOP_CONTROL_DIR": str(control),
            "SOXS_TOP_PORT_OFFSET": "10000",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 11
    evidence = _parse_key_values(control / "children" / "top1.status")
    assert evidence["slot"] == "top1"
    assert evidence["state"] == "exited"
    assert evidence["exit_code"] == "7"
    assert evidence["exit_signal"] == "0"
    assert evidence["exit_timestamp"]
    assert evidence["pid"].isdigit()
