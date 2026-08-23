"""TOP supervisor external-config contract tests.

These tests run a copied, immutable supervisor with external configuration and
runtime roots.  They never bind production TOP ports or use repository-local
runtime directories.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = PROJECT_ROOT / "scripts" / "start_top_engines.sh"


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        else:
            path.chmod(stat.S_IRUSR | (stat.S_IXUSR if mode & stat.S_IXUSR else 0))
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)


def _write_external_configs(root: Path, count: int) -> None:
    root.mkdir(parents=True)
    for slot in range(1, count + 1):
        (root / f"TOP{slot}.yaml").write_text(
            f"enabled: true\nticker: TEST{slot}\nmode: paper\n",
            encoding="utf-8",
        )


def _immutable_release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "start_top_engines.sh").write_bytes(SUPERVISOR.read_bytes())
    (scripts / "run_top_engine.sh").write_text(
        "#!/bin/bash\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    os.chmod(scripts / "start_top_engines.sh", 0o755)
    os.chmod(scripts / "run_top_engine.sh", 0o755)
    _make_read_only(release)
    return release


def _start_supervisor(
    release: Path,
    tmp_path: Path,
    config_root: Path | None,
    *,
    config_fallback: Path | None = None,
) -> tuple[subprocess.Popen[str], dict[str, str], Path]:
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    env = os.environ.copy()
    env.update(
        {
            "SOXS_STATE_DIR": str(state),
            "SOXS_LOGS_DIR": str(logs),
            "SOXS_TOP_PORT_OFFSET": "10000",
            "SOXS_TOP_ENGINE_REDIRECT_STDIO": "0",
            "QUANTCAIRN_EXECUTION_MODE": "PAPER",
            "SOXS_DISABLE_LIVE_CREDENTIALS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if config_root is not None:
        env["SOXS_TOP_CONFIG_DIR"] = str(config_root)
    else:
        env.pop("SOXS_TOP_CONFIG_DIR", None)
    if config_fallback is not None:
        env["SOXS_CONFIG_DIR"] = str(config_fallback)
    else:
        env.pop("SOXS_CONFIG_DIR", None)
    process = subprocess.Popen(
        ["bash", str(release / "scripts" / "start_top_engines.sh")],
        cwd=release,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, env, state


def _status_path(state: Path) -> Path:
    return state / "top_supervisor" / "status"


def _read_status(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def _wait_for_final_status(path: Path) -> dict[str, str]:
    deadline = time.time() + 5
    while time.time() < deadline:
        if path.exists():
            status = _read_status(path)
            if status.get("state") != "starting":
                return status
        time.sleep(0.05)
    return _read_status(path)


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


@pytest.mark.parametrize("active_count", [3, 2, 1, 0])
def test_immutable_release_uses_external_top_configs_and_slot_state(
    tmp_path: Path, active_count: int
) -> None:
    release = _immutable_release(tmp_path)
    external = tmp_path / "top-configs"
    _write_external_configs(external, active_count)
    process, _env, state = _start_supervisor(release, tmp_path, external)
    try:
        status_path = _status_path(state)
        deadline = time.time() + 5
        while not status_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert status_path.exists()
        status = _wait_for_final_status(status_path)
        expected_state = "idle_no_selection" if active_count == 0 else "running"
        assert status["state"] == expected_state
        assert status["active_engine_count"] == str(active_count)
        assert status["config_dir"] == str(external.resolve())
        assert process.poll() is None
        assert not list(release.rglob("TOP*.yaml"))
        assert not (release / "state").exists()
        assert not (release / "logs").exists()
        assert not list(release.rglob("*.pid"))
        assert not list(release.rglob("__pycache__"))
    finally:
        _stop(process)


def test_top_config_root_has_priority_over_config_dir_fallback(tmp_path: Path) -> None:
    release = _immutable_release(tmp_path)
    preferred = tmp_path / "preferred"
    fallback = tmp_path / "config-root" / "top_configs"
    _write_external_configs(preferred, 1)
    _write_external_configs(fallback, 1)
    process, _env, state = _start_supervisor(
        release,
        tmp_path,
        preferred,
        config_fallback=fallback.parent,
    )
    try:
        status_path = _status_path(state)
        deadline = time.time() + 5
        while not status_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert _wait_for_final_status(status_path)["config_dir"] == str(preferred.resolve())
    finally:
        _stop(process)


def test_config_dir_fallback_resolves_top_configs(tmp_path: Path) -> None:
    release = _immutable_release(tmp_path)
    config_root = tmp_path / "config-root"
    top_configs = config_root / "top_configs"
    _write_external_configs(top_configs, 1)
    process, _env, state = _start_supervisor(
        release,
        tmp_path,
        None,
        config_fallback=config_root,
    )
    try:
        status_path = _status_path(state)
        deadline = time.time() + 5
        while not status_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert _wait_for_final_status(status_path)["config_dir"] == str(top_configs.resolve())
    finally:
        _stop(process)


def test_missing_external_config_does_not_fall_back_to_release_configs(
    tmp_path: Path,
) -> None:
    release = _immutable_release(tmp_path)
    release.chmod(0o700)
    source_configs = release / "configs"
    source_configs.mkdir()
    (source_configs / "TOP1.yaml").write_text(
        "enabled: true\nticker: SOURCE\nmode: paper\n",
        encoding="utf-8",
    )
    _make_read_only(release)
    process, _env, state = _start_supervisor(release, tmp_path, None)
    try:
        status_path = _status_path(state)
        deadline = time.time() + 5
        while not status_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        status = _wait_for_final_status(status_path)
        assert status["state"] == "idle_no_selection"
        assert status["active_engine_count"] == "0"
        assert status["config_dir"] == ""
    finally:
        _stop(process)
