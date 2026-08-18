"""Offline lifecycle and failure-injection coverage for the TOP supervisor."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SUPERVISOR = PROJECT_DIR / "scripts" / "start_top_engines.sh"
PYTHON = os.environ.get("SOXS_PYTHON_BIN") or os.sys.executable


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "configs").mkdir()
    for slot in (1, 2, 3):
        (tmp_path / "configs" / f"TOP{slot}.yaml").write_text(
            f"ticker: TEST{slot}\nmode: paper\n", encoding="utf-8"
        )
    fake = r'''#!/bin/bash
set -eu
port="$2"
name="$3"
if [ -f "${SOXS_TOP_TEST_FAIL_FILE:-}" ] && grep -qx "$name" "$SOXS_TOP_TEST_FAIL_FILE"; then
  exit 0
fi
exec "${SOXS_TEST_PYTHON}" -c '
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/status":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"mode": "paper", "execution_mode": "paper"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_args): pass
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
' "$port"
'''
    runner = tmp_path / "scripts" / "run_top_engine.sh"
    runner.write_text(fake, encoding="utf-8")
    runner.chmod(0o755)
    launcher = tmp_path / "scripts" / "start_top_engines.sh"
    launcher.write_text((PROJECT_DIR / "scripts" / "start_top_engines.sh").read_text(), encoding="utf-8")
    launcher.chmod(0o755)
    env = {
        **os.environ,
        "SOXS_PROJECT_DIR": str(tmp_path),
        "SOXS_STATE_DIR": str(tmp_path / "state"),
        "SOXS_LOG_DIR": str(tmp_path / "logs"),
        "SOXS_TOP_PORT_OFFSET": "10000",
        "SOXS_TOP_REQUIRE_READINESS": "1",
        "SOXS_TOP_READINESS_TIMEOUT_SECONDS": "5",
        "SOXS_TOP_RESTART_TIMEOUT_SECONDS": "8",
        "SOXS_TEST_PYTHON": PYTHON,
        "SOXS_TOP_TEST_FAIL_FILE": str(tmp_path / "fail-slot"),
        "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
    }
    return launcher, env


def _wait_for_state(state_file: Path, expected: str, timeout: float = 10) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state_file.exists():
            values = dict(
                line.split("=", 1)
                for line in state_file.read_text().splitlines()
                if "=" in line
            )
            if values.get("state") == expected:
                return values
        time.sleep(0.1)
    raise AssertionError(f"state did not become {expected}: {state_file.read_text() if state_file.exists() else 'missing'}")


def _port_owner_count(port: int) -> int:
    result = subprocess.run(
        ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)


def test_supervisor_recovers_three_paper_engines_for_ten_restarts(tmp_path: Path):
    launcher, env = _fixture(tmp_path)
    supervisor = subprocess.Popen(["bash", str(launcher)], cwd=tmp_path, env=env)
    status_file = tmp_path / "state" / "top_supervisor" / "status"
    try:
        initial = _wait_for_state(status_file, "running")
        supervisor_pid = initial["supervisor_pid"]
        for port in (18080, 18081, 18082):
            assert _port_owner_count(port) == 1
        for cycle in range(10):
            result = subprocess.run(
                ["bash", str(launcher), "restart"], cwd=tmp_path, env=env,
                capture_output=True, text=True, timeout=15,
            )
            assert result.returncode == 0, result.stderr
            confirmed = _wait_for_state(status_file, "restart_confirmed")
            assert confirmed["supervisor_pid"] == supervisor_pid
            assert int(confirmed["generation"]) == cycle + 1
            for port in (18080, 18081, 18082):
                assert _port_owner_count(port) == 1
    finally:
        _stop(supervisor)

def test_failed_top3_restart_is_fail_closed_then_recovers(tmp_path: Path):
    launcher, env = _fixture(tmp_path)
    supervisor = subprocess.Popen(["bash", str(launcher)], cwd=tmp_path, env=env)
    status_file = tmp_path / "state" / "top_supervisor" / "status"
    fail_file = tmp_path / "fail-slot"
    try:
        _wait_for_state(status_file, "running")
        fail_file.write_text("top3\n", encoding="utf-8")
        failed = subprocess.run(
            ["bash", str(launcher), "restart"], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=15,
        )
        assert failed.returncode != 0
        assert "state=restart_failed" in status_file.read_text()
        assert all(_port_owner_count(port) == 0 for port in (18080, 18081, 18082))
        fail_file.unlink()
        recovered = subprocess.run(
            ["bash", str(launcher), "restart"], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=15,
        )
        assert recovered.returncode == 0, recovered.stderr
        _wait_for_state(status_file, "restart_confirmed")
        assert all(_port_owner_count(port) == 1 for port in (18080, 18081, 18082))
    finally:
        _stop(supervisor)
