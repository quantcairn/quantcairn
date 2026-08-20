"""Regression coverage for immutable-release TOP runtime paths."""

from __future__ import annotations

import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
def _make_read_only_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        else:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | (stat.S_IXUSR if mode & stat.S_IXUSR else 0))
    root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _write_fake_engine(release: Path) -> None:
    (release / "run.py").write_text(
        """import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from src.engine.trading_engine import append_runtime_audit

append_runtime_audit({"phase": "top_child_smoke", "execution_mode": "paper"})
port = int(sys.argv[sys.argv.index("--port") + 1])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps({"mode": "paper", "execution_mode": "paper"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        pass

server = HTTPServer(("127.0.0.1", port), Handler)

def stop(*_args):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
server.serve_forever()
""",
        encoding="utf-8",
    )


def _write_top_configs(release: Path) -> None:
    config_root = release / "configs"
    config_root.mkdir()
    for slot, ticker in ((1, "AIG"), (2, "AGNC"), (3, "ACGL")):
        (config_root / f"TOP{slot}.yaml").write_text(
            f"enabled: true\nslot: {slot}\nticker: {ticker}\nmode: paper\n",
            encoding="utf-8",
        )


def _smoke_env(tmp_path: Path, state_root: Path, logs_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "SOXS_PROJECT_DIR": str(tmp_path / "release"),
            "SOXS_STATE_DIR": str(state_root),
            "SOXS_REPORTS_DIR": str(tmp_path / "reports"),
            "SOXS_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
            "SOXS_LOGS_DIR": str(logs_root),
            "SOXS_PYTHON_BIN": sys.executable,
            "SOXS_TOP_PORT_OFFSET": "10000",
            "SOXS_TOP_REQUIRE_READINESS": "1",
            "SOXS_TOP_READINESS_TIMEOUT_SECONDS": "10",
            "SOXS_TOP_ENGINE_REDIRECT_STDIO": "1",
            "QUANTCAIRN_EXECUTION_MODE": "PAPER",
            "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PROJECT_ROOT),
        }
    )
    env.pop("SOXS_RUNTIME_AUDIT_DIR", None)
    env.pop("SOXS_LOG_DIR", None)
    return env


def test_audit_path_precedence_and_no_source_fallback(monkeypatch, tmp_path):
    from src.engine import trading_engine

    release = tmp_path / "immutable-release"
    release.mkdir()
    monkeypatch.setattr(trading_engine, "PROJECT_DIR", release)
    audit = tmp_path / "audit"
    logs = tmp_path / "logs"
    legacy_logs = tmp_path / "legacy-logs"

    monkeypatch.setenv("SOXS_RUNTIME_AUDIT_DIR", str(audit))
    monkeypatch.setenv("SOXS_LOGS_DIR", str(logs))
    monkeypatch.setenv("SOXS_LOG_DIR", str(legacy_logs))
    assert trading_engine._audit_log_path().parent == audit

    monkeypatch.delenv("SOXS_RUNTIME_AUDIT_DIR")
    assert trading_engine._audit_log_path().parent == logs

    monkeypatch.delenv("SOXS_LOGS_DIR")
    assert trading_engine._audit_log_path().parent == legacy_logs

    monkeypatch.delenv("SOXS_LOG_DIR")
    with pytest.raises(RuntimeError, match="runtime audit root must be configured"):
        trading_engine._audit_log_path()
    assert not (release / "logs").exists()


def test_immutable_release_audit_writes_only_external_logs(monkeypatch, tmp_path):
    from src.engine import trading_engine

    release = tmp_path / "immutable-release"
    release.mkdir()
    logs = tmp_path / "external-logs"
    monkeypatch.setattr(trading_engine, "PROJECT_DIR", release)
    monkeypatch.setenv("SOXS_LOGS_DIR", str(logs))
    monkeypatch.delenv("SOXS_RUNTIME_AUDIT_DIR", raising=False)
    monkeypatch.delenv("SOXS_LOG_DIR", raising=False)

    trading_engine.append_runtime_audit({"phase": "immutable_release_test", "execution_mode": "paper"})

    assert list(logs.glob("trades-*.jsonl"))
    assert not (release / "logs").exists()
    assert not (release / "runtime").exists()
    assert not list(release.rglob("*.pid"))
    assert not list(release.rglob("__pycache__"))


def test_three_paper_children_reach_readiness_from_immutable_release(tmp_path):
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "start_top_engines.sh", release / "scripts" / "start_top_engines.sh")
    shutil.copy2(PROJECT_ROOT / "scripts" / "run_top_engine.sh", release / "scripts" / "run_top_engine.sh")
    os.chmod(release / "scripts" / "start_top_engines.sh", 0o755)
    os.chmod(release / "scripts" / "run_top_engine.sh", 0o755)
    _write_fake_engine(release)

    state_root = tmp_path / "state"
    logs_root = tmp_path / "logs"
    _write_top_configs(release)
    env = _smoke_env(tmp_path, state_root, logs_root)
    _make_read_only_tree(release)

    ports = (10800, 10801, 10802)
    children = [
        subprocess.Popen(
            [
                "bash",
                str(release / "scripts" / "run_top_engine.sh"),
                str(release / "configs" / f"TOP{slot}.yaml"),
                str(port),
                f"top{slot}",
            ],
            cwd=release,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for slot, port in enumerate(ports, 1)
    ]
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            ready = True
            for port in ports:
                try:
                    from urllib.request import urlopen

                    with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    ready = ready and response.status == 200 and payload.get("mode") == "paper"
                except Exception:
                    ready = False
            if ready:
                break
            if any(child.poll() is not None for child in children):
                break
            time.sleep(0.2)

        assert all(child.poll() is None for child in children), [
            child.stderr.read() if child.stderr else child.returncode for child in children
        ]
        assert len(list(logs_root.glob("top?.log"))) == 3
        assert list(logs_root.glob("trades-*.jsonl"))
        assert not (release / "logs").exists()
        assert not list(release.rglob("*.pid"))
        assert not list(release.rglob("__pycache__"))
        assert not (release / "runtime").exists()
    finally:
        for child in children:
            child.send_signal(signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
