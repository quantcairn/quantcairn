from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.candidate_validation.selection_freshness import evaluate_selection_freshness
from src.openalpha.selection_bundle import build_selection_bundle
from src.openalpha.top_restart import load_restart_status, record_restart_status
from src.openalpha.selector import AIStrategySelector


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "scripts" / "start_top_engines.sh"
ENGINE = ROOT / "scripts" / "run_top_engine.sh"
PYTHON = Path(sys.executable)


def _runtime_env(tmp_path: Path, *, python: str | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in {
        "SOXS_PYTHON_BIN", "SOXS_TOP_CONFIG_DIR", "SOXS_CONFIG_DIR",
    }}
    env.update({
        "PYTHONPATH": str(ROOT),
        "SOXS_STATE_DIR": str(tmp_path / "state"),
        "SOXS_LOGS_DIR": str(tmp_path / "logs"),
        "SOXS_TOP_PORT_OFFSET": "10000",
        "SOXS_TOP_REQUIRE_READINESS": "0",
        "QUANTCAIRN_EXECUTION_MODE": "PAPER",
        "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
    })
    if python is not None:
        env["SOXS_PYTHON_BIN"] = python
    return env


def _copy_supervisor(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "start_top_engines.sh"
    shutil.copy2(START, target)
    target.chmod(0o755)
    return target


def _write_slots(root: Path, *, enabled: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for slot in range(1, 4):
        ticker = f"TEST{slot}" if enabled else ""
        (root / f"TOP{slot}.yaml").write_text(
            f"enabled: {'true' if enabled else 'false'}\n"
            f"ticker: {ticker}\nmode: paper\nrange:\n  mode: auto\n",
            encoding="utf-8",
        )


def test_supervisor_requires_explicit_python_before_starting_children(tmp_path: Path):
    launcher = _copy_supervisor(tmp_path)
    config = tmp_path / "configs"
    _write_slots(config)
    result = subprocess.run(
        ["bash", str(launcher)],
        cwd=tmp_path,
        env=_runtime_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 12
    assert "python_runtime_invalid" in result.stderr
    assert not (tmp_path / "state" / "top_supervisor" / "supervisor.pid").exists()
    assert "state=python_runtime_invalid" in (tmp_path / "state" / "top_supervisor" / "status").read_text()
    assert "python_runtime_invalid" in (tmp_path / "logs" / "top-supervisor-runtime.log").read_text()


def test_supervisor_dependency_preflight_fails_closed(tmp_path: Path):
    launcher = _copy_supervisor(tmp_path)
    config = tmp_path / "configs"
    _write_slots(config)
    env = _runtime_env(tmp_path, python=str(PYTHON))
    env["SOXS_EXTRA_REQUIRED_MODULES"] = ""
    env["SOXS_TOP_EXTRA_REQUIRED_MODULES"] = "quantcairn_module_that_does_not_exist"
    result = subprocess.run(
        ["bash", str(launcher)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 12
    assert "dependency_preflight_failed" in result.stderr
    assert not (tmp_path / "state" / "top_supervisor" / "supervisor.pid").exists()
    assert "state=dependency_preflight_failed" in (tmp_path / "state" / "top_supervisor" / "status").read_text()


def test_supervisor_uses_current_manifest_bundle_not_static_config_root(tmp_path: Path):
    launcher = _copy_supervisor(tmp_path)
    stale = tmp_path / "stale-configs"
    current = tmp_path / "state" / "selection_bundles" / "run-b" / "selection_bundle_v1"
    _write_slots(stale)
    _write_slots(current)
    manifest = {
        "selection_run_id": "run-b",
        "selection_bundle_hash": "hash-b",
        "bundle_root": "state/selection_bundles/run-b/selection_bundle_v1",
    }
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "selection_bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    env = _runtime_env(tmp_path, python=str(PYTHON))
    env["SOXS_TOP_CONFIG_DIR"] = str(stale)
    process = subprocess.Popen(
        ["bash", str(launcher)], cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        status_path = state / "top_supervisor" / "status"
        for _ in range(40):
            if status_path.exists():
                break
            import time
            time.sleep(0.1)
        status = status_path.read_text(encoding="utf-8")
        assert f"config_dir={current.resolve()}" in status
        assert "selection_run_id=run-b" in status
        assert "selection_bundle_hash=hash-b" in status
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_engine_requires_explicit_python(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    engine = scripts / "run_top_engine.sh"
    shutil.copy2(ENGINE, engine)
    engine.chmod(0o755)
    cfg = tmp_path / "TOP1.yaml"
    cfg.write_text("enabled: false\nticker: ''\nmode: paper\n", encoding="utf-8")
    env = _runtime_env(tmp_path)
    result = subprocess.run(
        ["bash", str(engine), str(cfg), "18080", "top1"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 12
    assert "python_runtime_invalid" in result.stderr


def test_restart_status_separates_bundle_and_runtime_sync(tmp_path: Path):
    record_restart_status(
        status="FAILED",
        selection_run_id="run-1",
        selection_bundle_hash="hash-1",
        error="restart_exit_6",
        project_dir=tmp_path,
    )
    status = load_restart_status(tmp_path)
    assert status["bundle_sync_status"] == "OK"
    assert status["runtime_sync_status"] == "FAILED"
    assert status["top_restart_status"] == "FAILED"


def test_selection_freshness_distinguishes_active_stale_and_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTCAIRN_SELECTION_FRESHNESS_GATE", "1")
    now = datetime(2026, 8, 19, 21, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    stale = {"manifest": {"selection_run_id": "old", "generated_at": "2026-08-19T21:44:00"}}
    fresh = {"manifest": {"selection_run_id": "new", "generated_at": "2026-08-19T21:46:00"}}
    assert evaluate_selection_freshness(stale, now=now, state_root=tmp_path)["status"] == "STALE"
    assert evaluate_selection_freshness(fresh, now=now, state_root=tmp_path)["status"] == "READY"
    monkeypatch.setenv("QUANTCAIRN_SELECTOR_ACTIVE", "1")
    assert evaluate_selection_freshness(fresh, now=now, state_root=tmp_path)["reason"] == "selector_active"


def test_selection_bundle_exposes_non_overlapping_sync_statuses():
    bundle = build_selection_bundle(
        summary={"result_quality": "COMPLETE", "research_admission": "RESEARCH_READY"},
        selection_state_payload={},
        top_items=[],
        selection_run_id="run-1",
        selection_date="2026-08-19",
        generated_at="2026-08-19T21:46:00+08:00",
        runtime_sync_status="PENDING",
    )
    report = bundle.report_payload()
    audit = bundle.audit_payload()
    assert report["bundle_sync_status"] == "OK"
    assert report["runtime_sync_status"] == "PENDING"
    assert report["top_sync_status_semantics"] == "BUNDLE_CONFIG_ONLY"
    assert audit["runtime_sync_status"] == "PENDING"


def test_selector_accepts_explicit_orchestration_date():
    signature = inspect.signature(AIStrategySelector.run_selection)
    assert "selection_date" in signature.parameters
