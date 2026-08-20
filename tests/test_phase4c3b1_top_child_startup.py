from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_top_engine.sh"
SUPERVISOR = ROOT / "scripts" / "start_top_engines.sh"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _adjacent_ports() -> tuple[int, int]:
    for base in range(19080, 19180):
        sockets = []
        try:
            for port in (base, base + 1, base + 2):
                sock = socket.socket()
                sock.bind(("127.0.0.1", port))
                sockets.append(sock)
            return base, base - 8080
        except OSError:
            pass
        finally:
            for sock in sockets:
                sock.close()
    raise RuntimeError("no isolated adjacent ports available")


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("SOXS_PYTHON_BIN", "SOXS_TOP_CONFIG_DIR", "SOXS_CONFIG_DIR"):
        env.pop(key, None)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "SOXS_PROJECT_DIR": str(tmp_path),
            "SOXS_STATE_DIR": str(tmp_path / "state"),
            "SOXS_REPORTS_DIR": str(tmp_path / "reports"),
            "SOXS_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
            "SOXS_LOGS_DIR": str(tmp_path / "logs"),
            "SOXS_TOP_CONFIG_DIR": str(tmp_path / "configs"),
            "SOXS_PYTHON_BIN": sys.executable,
            "SOXS_EXPECTED_PYTHON_VERSION": "3.14.4",
            "SOXS_TOP_ENGINE_REDIRECT_STDIO": "1",
            "QUANTCAIRN_EXECUTION_MODE": "PAPER",
            "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
        }
    )
    return env


def _write_config(path: Path, slot: int = 1) -> None:
    path.write_text(
        "\n".join(
            [
                "enabled: true",
                f"slot: {slot}",
                f"ticker: TEST{slot}",
                "mode: paper",
                "selection_run_id: test-run",
                "selection_bundle_hash: test-hash",
                "selection_date: '2026-08-20'",
                "range:",
                "  mode: auto",
                "position:",
                "  initial_capital: 700.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_http_run_py(path: Path) -> None:
    path.write_text(
        """
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[sys.argv.index('--port') + 1])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'mode': 'paper', 'execution_mode': 'paper'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )


def test_free_port_probe_allows_paper_child_start(tmp_path: Path):
    scripts = tmp_path / "scripts"
    configs = tmp_path / "configs"
    scripts.mkdir()
    configs.mkdir()
    shutil.copy2(RUNNER, scripts / "run_top_engine.sh")
    (scripts / "run_top_engine.sh").chmod(0o755)
    _write_http_run_py(tmp_path / "run.py")
    config = configs / "TOP1.yaml"
    _write_config(config)
    port = _free_port()

    process = subprocess.Popen(
        ["bash", str(scripts / "run_top_engine.sh"), str(config), str(port), "top1"],
        cwd=tmp_path,
        env=_base_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        assert process.poll() is None, process.stderr.read() if process.stderr else ""
        assert (tmp_path / "logs" / "top-engine-runtime.log").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        assert not (tmp_path / "logs" / "top1.failure").exists()


def test_pre_readiness_shell_failure_persists_exact_evidence(tmp_path: Path):
    scripts = tmp_path / "scripts"
    configs = tmp_path / "configs"
    fake_bin = tmp_path / "bin"
    scripts.mkdir()
    configs.mkdir()
    fake_bin.mkdir()
    shutil.copy2(RUNNER, scripts / "run_top_engine.sh")
    (scripts / "run_top_engine.sh").chmod(0o755)
    _write_config(configs / "TOP1.yaml")
    fake_lsof = fake_bin / "lsof"
    fake_lsof.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_lsof.chmod(0o755)
    env = _base_env(tmp_path)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

    process = subprocess.run(
        ["bash", str(scripts / "run_top_engine.sh"), str(configs / "TOP1.yaml"), "19180", "top1"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert process.returncode == 14
    evidence = (tmp_path / "logs" / "top1.failure").read_text(encoding="utf-8")
    assert "slot=top1" in evidence
    assert f"interpreter={sys.executable}" in evidence
    assert "exit_code=14" in evidence
    assert "startup_stage=port_bind" in evidence
    assert "reason=port 19180 is unavailable before engine startup" in evidence
    assert f"config_path={configs / 'TOP1.yaml'}" in evidence
    assert "log_path=" in evidence
    assert "lsof exit=2" in (tmp_path / "logs" / "top1.log").read_text(encoding="utf-8")


def test_supervisor_persists_python_child_failure_and_specific_status(tmp_path: Path):
    scripts = tmp_path / "scripts"
    configs = tmp_path / "configs"
    scripts.mkdir()
    configs.mkdir()
    shutil.copy2(SUPERVISOR, scripts / "start_top_engines.sh")
    shutil.copy2(RUNNER, scripts / "run_top_engine.sh")
    (scripts / "start_top_engines.sh").chmod(0o755)
    (scripts / "run_top_engine.sh").chmod(0o755)
    (tmp_path / "run.py").write_text(
        "raise RuntimeError('fixture engine initialization failure')\n",
        encoding="utf-8",
    )
    for slot in (1, 2, 3):
        _write_config(configs / f"TOP{slot}.yaml", slot)
    base_port, offset = _adjacent_ports()
    env = _base_env(tmp_path)
    env.update(
        {
            "SOXS_TOP_PORT_OFFSET": str(offset),
            "SOXS_TOP_REQUIRE_READINESS": "1",
            "SOXS_TOP_READINESS_TIMEOUT_SECONDS": "5",
        }
    )

    process = subprocess.run(
        ["bash", str(scripts / "start_top_engines.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert process.returncode == 10
    status = (tmp_path / "state" / "top_supervisor" / "status").read_text(encoding="utf-8")
    assert "state=start_failed" in status
    assert "slot=top1" in status
    assert "exit_code=1" in status
    assert "startup_stage=engine_entrypoint" in status
    assert "failure_evidence_path=" in status
    evidence = (tmp_path / "logs" / "top1.failure").read_text(encoding="utf-8")
    assert "child_pid=" in evidence
    assert "exit_code=1" in evidence
    assert "startup_stage=engine_entrypoint" in evidence
    child_log = (tmp_path / "logs" / "top1.log").read_text(encoding="utf-8")
    assert "RuntimeError: fixture engine initialization failure" in child_log
    for port in (base_port, base_port + 1, base_port + 2):
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0
