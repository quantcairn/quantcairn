"""Phase 4C-2F TOP runtime contract tests.

All engines are local fake PAPER HTTP servers.  No broker, selector, or
production LaunchAgent is involved.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
SUPERVISOR = PROJECT_DIR / "scripts" / "start_top_engines.sh"
PYTHON = os.environ.get("SOXS_PYTHON_BIN") or os.sys.executable
PORT_OFFSET = 10000


FAKE_RUNNER = r'''#!/bin/bash
set -eu
port="$2"
exec "${SOXS_TEST_PYTHON}" - "$port" <<'PY'
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/status":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"mode": "paper", "execution_mode": "paper"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
PY
'''


def _fixture(tmp_path: Path):
    config_dir = tmp_path / "external-top-configs"
    scripts_dir = tmp_path / "release" / "scripts"
    state_dir = tmp_path / "external-state"
    config_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (scripts_dir / "start_top_engines.sh").write_text(SUPERVISOR.read_text(encoding="utf-8"), encoding="utf-8")
    (scripts_dir / "run_top_engine.sh").write_text(FAKE_RUNNER, encoding="utf-8")
    (scripts_dir / "start_top_engines.sh").chmod(0o755)
    (scripts_dir / "run_top_engine.sh").chmod(0o755)
    env = {
        **os.environ,
        "SOXS_PROJECT_DIR": str(scripts_dir.parent),
        "SOXS_STATE_DIR": str(state_dir),
        "SOXS_TOP_CONFIG_DIR": str(config_dir),
        "SOXS_TOP_PORT_OFFSET": str(PORT_OFFSET),
        "SOXS_TOP_REQUIRE_READINESS": "1",
        "SOXS_TOP_READINESS_TIMEOUT_SECONDS": "5",
        "SOXS_TOP_RESTART_TIMEOUT_SECONDS": "8",
        "SOXS_TEST_PYTHON": PYTHON,
        "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return scripts_dir / "start_top_engines.sh", config_dir, state_dir, env


def _write_configs(config_dir: Path, active_slots: set[int]) -> None:
    for slot in (1, 2, 3):
        if slot in active_slots:
            payload = f"enabled: true\nticker: TEST{slot}\nslot: {slot}\nmode: paper\n"
        else:
            payload = f"enabled: false\nticker: null\nslot: {slot}\nreason: top_n_not_filled\nmode: paper\n"
        (config_dir / f"TOP{slot}.yaml").write_text(payload, encoding="utf-8")


def _wait_status(path: Path, state: str, timeout: float = 8.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            values = dict(
                line.split("=", 1)
                for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            if values.get("state") == state:
                return values
        time.sleep(0.05)
    raise AssertionError(path.read_text(encoding="utf-8") if path.exists() else "status missing")


def _owners(port: int) -> list[str]:
    result = subprocess.run(
        ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)


def test_zero_selection_is_healthy_idle_and_restartable(tmp_path: Path):
    launcher, config_dir, state_dir, env = _fixture(tmp_path)
    _write_configs(config_dir, set())
    supervisor = subprocess.Popen(["bash", str(launcher)], cwd=tmp_path, env=env)
    status = state_dir / "top_supervisor" / "status"
    try:
        initial = _wait_status(status, "idle_no_selection")
        assert initial["detail"] == "supervisor_ready"
        assert all(not _owners(port + PORT_OFFSET) for port in (8080, 8081, 8082))
        result = subprocess.run(["bash", str(launcher), "restart"], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        confirmed = _wait_status(status, "restart_confirmed")
        assert confirmed["detail"] == "idle_no_selection"
        assert all(not _owners(port + PORT_OFFSET) for port in (8080, 8081, 8082))
    finally:
        _stop(supervisor)


def test_three_to_two_to_zero_transition_is_slot_aware(tmp_path: Path):
    launcher, config_dir, state_dir, env = _fixture(tmp_path)
    _write_configs(config_dir, {1, 2, 3})
    supervisor = subprocess.Popen(["bash", str(launcher)], cwd=tmp_path, env=env)
    status = state_dir / "top_supervisor" / "status"
    try:
        _wait_status(status, "running")
        assert all(len(_owners(port + PORT_OFFSET)) == 1 for port in (8080, 8081, 8082))
        _write_configs(config_dir, {1, 3})
        result = subprocess.run(["bash", str(launcher), "restart"], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        _wait_status(status, "restart_confirmed")
        assert len(_owners(18080)) == 1
        assert not _owners(18081)
        assert len(_owners(18082)) == 1
        _write_configs(config_dir, set())
        result = subprocess.run(["bash", str(launcher), "restart"], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        _wait_status(status, "restart_confirmed")
        assert all(not _owners(port + PORT_OFFSET) for port in (8080, 8081, 8082))
    finally:
        _stop(supervisor)


def test_invalid_active_empty_and_missing_runtime_root_fail_closed(tmp_path: Path):
    launcher, config_dir, state_dir, env = _fixture(tmp_path)
    _write_configs(config_dir, set())
    (config_dir / "TOP1.yaml").write_text("enabled: true\nticker: null\nmode: paper\n", encoding="utf-8")
    result = subprocess.run(["bash", str(launcher)], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 10
    assert "CONFIG_INVALID" in result.stderr

    missing_env = dict(env)
    missing_env.pop("SOXS_TOP_CONFIG_DIR")
    missing_env.pop("SOXS_STATE_DIR")
    result = subprocess.run(["bash", str(launcher)], cwd=tmp_path, env=missing_env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 12
    assert "TOP_RUNTIME_ROOT_NOT_CONFIGURED" in result.stderr
    assert not (tmp_path / "release" / "state").exists()


def test_runtime_resolver_and_selector_writer_use_external_root(tmp_path: Path, monkeypatch):
    from src.config.runtime_paths import resolve_top_config_dir
    from src.openalpha import selection_state
    from src.openalpha.config_writer import write_top_configs

    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SOXS_TOP_CONFIG_DIR", str(root_a))
    assert resolve_top_config_dir() == root_a.resolve()
    write_top_configs([], selection_run_id="run-a", slot_limit=3)
    assert yaml.safe_load((root_a / "TOP1.yaml").read_text()) ["enabled"] is False

    monkeypatch.setenv("SOXS_TOP_CONFIG_DIR", str(root_b))
    assert resolve_top_config_dir() == root_b.resolve()
    write_top_configs([], selection_run_id="run-b", slot_limit=3)
    assert yaml.safe_load((root_b / "TOP1.yaml").read_text())["selection_run_id"] == "run-b"
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    assert selection_state.current_top_config_run_id() == "run-b"
    assert not (PROJECT_DIR / "configs" / "TOP1.yaml").exists()
