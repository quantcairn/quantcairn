from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

from src.config.runtime_paths import RuntimePaths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_ROOT = PROJECT_ROOT / "deploy" / "launchd"


def _load_snapshot_script():
    path = PROJECT_ROOT / "scripts" / "generate_daily_runtime_snapshot.py"
    spec = importlib.util.spec_from_file_location("daily_snapshot_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        project_dir=tmp_path / "release",
        state_dir=tmp_path / "persistent" / "state",
        reports_dir=tmp_path / "persistent" / "reports",
        artifacts_dir=tmp_path / "persistent" / "artifacts",
        logs_dir=tmp_path / "persistent" / "logs",
    )


def test_snapshot_lock_uses_external_state_root(tmp_path: Path) -> None:
    module = _load_snapshot_script()
    paths = _snapshot_paths(tmp_path)
    lock_path = module.snapshot_lock_path(paths)

    with module.acquire_snapshot_lock(lock_path) as acquired:
        assert acquired
        assert lock_path == paths.state_dir / "locks" / "daily_runtime_snapshot.lock"
        assert not (paths.project_dir / "state").exists()


def test_snapshot_lock_serializes_concurrent_processes(tmp_path: Path) -> None:
    module_path = PROJECT_ROOT / "scripts" / "generate_daily_runtime_snapshot.py"
    lock_path = tmp_path / "state" / "locks" / "daily_runtime_snapshot.lock"
    code = f"""
import importlib.util
import pathlib
import time

p = pathlib.Path({str(module_path)!r})
s = importlib.util.spec_from_file_location("snapshot", p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
with m.acquire_snapshot_lock(pathlib.Path({str(lock_path)!r})) as ok:
    print(ok, flush=True)
    time.sleep(0.5)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "True"

    module = _load_snapshot_script()
    with module.acquire_snapshot_lock(lock_path) as acquired:
        assert not acquired
    assert child.wait(timeout=5) == 0

    with module.acquire_snapshot_lock(lock_path) as acquired:
        assert acquired


def test_snapshot_lock_is_released_after_context_exit(tmp_path: Path) -> None:
    module = _load_snapshot_script()
    lock_path = tmp_path / "state" / "locks" / "daily_runtime_snapshot.lock"
    with module.acquire_snapshot_lock(lock_path) as acquired:
        assert acquired
    with module.acquire_snapshot_lock(lock_path) as acquired:
        assert acquired


def _load_template(name: str) -> dict:
    return plistlib.loads((LAUNCHD_ROOT / name).read_bytes())


def test_snapshot_launchagent_is_scheduled_and_non_persistent() -> None:
    plist = _load_template("com.quantcairn.daily-runtime-snapshot.plist.template")
    assert plist["Label"] == "com.quantcairn.daily-runtime-snapshot"
    assert plist["ProgramArguments"] == [
        "REPLACE_WITH_PYTHON_PATH",
        "REPLACE_WITH_PROJECT_ROOT/scripts/generate_daily_runtime_snapshot.py",
    ]
    assert plist["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}
    assert plist["RunAtLoad"] is False
    assert plist["KeepAlive"] is False
    env = plist["EnvironmentVariables"]
    assert env == {
        "QUANTCAIRN_HOME": "REPLACE_WITH_PROJECT_ROOT",
        "SOXS_PROJECT_DIR": "REPLACE_WITH_PROJECT_ROOT",
        "SOXS_RELEASE_SHA": "REPLACE_WITH_RELEASE_SHA",
        "SOXS_STATE_DIR": "REPLACE_WITH_STATE_ROOT",
        "SOXS_REPORTS_DIR": "REPLACE_WITH_REPORTS_ROOT",
        "SOXS_ARTIFACTS_DIR": "REPLACE_WITH_ARTIFACTS_ROOT",
        "SOXS_LOG_DIR": "REPLACE_WITH_LOGS_ROOT",
        "SOXS_LOGS_DIR": "REPLACE_WITH_LOGS_ROOT",
        "QUANTCAIRN_EXECUTION_MODE": "PAPER",
    }
    assert not any("TOKEN" in key or "CHAT_ID" in key for key in env)
    assert "/Users/chenwei/quantcairn" not in str(plist)


def test_all_operational_templates_carry_release_identity_placeholder() -> None:
    for path in sorted(LAUNCHD_ROOT.glob("*.plist.template")):
        plist = plistlib.loads(path.read_bytes())
        env = plist.get("EnvironmentVariables", {})
        assert env.get("SOXS_PROJECT_DIR") == "REPLACE_WITH_PROJECT_ROOT", path.name
        assert env.get("SOXS_RELEASE_SHA") == "REPLACE_WITH_RELEASE_SHA", path.name
        assert "/Users/chenwei/quantcairn" not in path.read_text(encoding="utf-8")


def test_research_template_arguments_match_current_cli() -> None:
    plist = _load_template("com.quantcairn.research.plist.template")
    assert plist["ProgramArguments"][1:] == [
        "REPLACE_WITH_PROJECT_ROOT/scripts/run_daily_research.py",
        "--mode",
        "independent",
    ]
    path = PROJECT_ROOT / "scripts" / "run_daily_research.py"
    spec = importlib.util.spec_from_file_location("research_entrypoint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module._build_parser().parse_args(["--mode", "independent"])
    assert args.mode == "independent"
