from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts.validate_top_runtime import validate


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
