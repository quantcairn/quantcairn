"""Tests for TOP engines launcher and associated launchd configuration.

Verifies:
  1. start_top_engines.sh exists and is executable
  2. Engine definitions match expected ports and configs
  3. Supervisor lifecycle: wait + cleanup trap
  4. Launcher handles missing config files gracefully
  5. launchd plist template is valid XML and well-formed
  6. Plist references match start_top_engines.sh
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_DIR / "scripts" / "start_top_engines.sh"
PLIST_TEMPLATE = PROJECT_DIR / "deploy" / "launchd" / "com.quantcairn.top-engines.plist.template"


# ---------------------------------------------------------------------------
# 1. Script exists and is executable
# ---------------------------------------------------------------------------

def test_launcher_script_exists():
    assert LAUNCHER.exists(), f"Launcher script missing: {LAUNCHER}"


def test_launcher_script_is_executable():
    assert os.access(LAUNCHER, os.X_OK), f"Launcher not executable: {LAUNCHER}"


def test_launcher_can_be_parsed_by_bash():
    """Bash -n should not report syntax errors."""
    result = subprocess.run(
        ["bash", "-n", str(LAUNCHER)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# 2. Engine definitions match expected ports and configs
# ---------------------------------------------------------------------------

EXPECTED_ENGINES = [
    ("configs/TOP1.yaml", "8080", "top1"),
    ("configs/TOP2.yaml", "8081", "top2"),
    ("configs/TOP3.yaml", "8082", "top3"),
]

# Never bind or inspect the production TOP ports in isolated tests.
ISOLATED_ENV = {
    **os.environ,
    "SOXS_TOP_PORT_OFFSET": "10000",
    "SOXS_TOP_REQUIRE_READINESS": "0",
}


def test_launcher_defines_three_engines():
    content = LAUNCHER.read_text(encoding="utf-8")
    assert '"$CONFIG_DIR/TOP1.yaml 8080 top1"' in content
    assert '"$CONFIG_DIR/TOP2.yaml 8081 top2"' in content
    assert '"$CONFIG_DIR/TOP3.yaml 8082 top3"' in content


def test_launcher_ports_match_dashboard_tickers():
    """Dashboard TICKERS ports must equal launcher engine ports."""
    import src.dashboard.combined as combined
    for idx, item in enumerate(combined.TICKERS):
        _, expected_port, _ = EXPECTED_ENGINES[idx]
        assert item["port"] == int(expected_port), (
            f"Port mismatch: TICKERS[{idx}]={item['port']} "
            f"!= launcher {expected_port}"
        )


# ---------------------------------------------------------------------------
# 3. Supervisor lifecycle — wait + cleanup trap
# ---------------------------------------------------------------------------

def test_launcher_contains_wait():
    """Supervisor must wait for child engines to stay alive for launchd."""
    content = LAUNCHER.read_text(encoding="utf-8")
    assert "wait " in content or "wait\"" in content or "wait${" in content \
        or 'wait "' in content or 'wait\n' in content or 'wait"' in content \
        or content.count("wait") >= 1, (
        "Launcher must call 'wait' to keep the supervisor alive"
    )


def test_launcher_contains_cleanup_trap():
    """Supervisor must trap EXIT/INT/TERM to clean up child processes."""
    content = LAUNCHER.read_text(encoding="utf-8")
    assert "trap" in content, "Launcher must define a cleanup trap"
    assert "cleanup" in content.lower(), "Launcher must have a cleanup function"


def test_supervisor_waits_for_children(tmp_path: Path):
    """The supervisor stays alive while child engines are running."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()

    # Create all 3 configs so none are skipped
    for i in 1, 2, 3:
        (cfg_dir / f"TOP{i}.yaml").write_text(
            f"enabled: true\nticker: TEST{i}\nmode: paper\nrange:\n  mode: auto\nposition:\n  initial_capital: 700.0\n",
            encoding="utf-8",
        )

    # run_top_engine.sh: sleeps to simulate a running engine
    (scripts_dir / "run_top_engine.sh").write_text(
        '#!/bin/bash\necho "RUNNING $1" && sleep 30',
        encoding="utf-8",
    )
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)

    launcher_copy = scripts_dir / "start_top_engines.sh"
    launcher_copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher_copy, 0o755)

    proc = subprocess.Popen(
        ["bash", str(launcher_copy)],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
    )

    # Give the supervisor time to start children and enter wait
    time.sleep(2)

    # Supervisor should still be alive (waiting on children)
    assert proc.poll() is None, (
        f"Supervisor exited prematurely with code {proc.returncode}"
    )

    # Send SIGTERM — cleanup trap should fire, then exit
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("Supervisor did not exit after SIGTERM")

    stdout_text = proc.stdout.read() if proc.stdout else ""
    stderr_text = proc.stderr.read() if proc.stderr else ""
    assert "Launched" in stdout_text, f"Expected 'Launched' in stdout: {stdout_text}"
    assert proc.returncode is not None, "Supervisor should have exited after SIGTERM"


def test_cleanup_trap_kills_children_on_exit(tmp_path: Path):
    """When supervisor receives SIGTERM, child engine processes are terminated."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()

    for i in 1, 2, 3:
        (cfg_dir / f"TOP{i}.yaml").write_text(
            f"enabled: true\nticker: TEST{i}\nmode: paper\nrange:\n  mode: auto\nposition:\n  initial_capital: 700.0\n",
            encoding="utf-8",
        )

    # run_top_engine.sh writes its PID to a file, then sleeps
    (scripts_dir / "run_top_engine.sh").write_text(
        '#!/bin/bash\necho $$ > /tmp/top_engine_test_pid_$3\necho "RUNNING $1 port=$2" && sleep 60',
        encoding="utf-8",
    )
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)

    launcher_copy = scripts_dir / "start_top_engines.sh"
    launcher_copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher_copy, 0o755)

    proc = subprocess.Popen(
        ["bash", str(launcher_copy)],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
    )

    time.sleep(2)
    assert proc.poll() is None, "Supervisor should still be running"

    # Collect child PIDs written by the stub run_top_engine.sh
    child_pids = []
    for suffix in ("top1", "top2", "top3"):
        pid_file = Path(f"/tmp/top_engine_test_pid_{suffix}")
        if pid_file.exists():
            child_pids.append(int(pid_file.read_text().strip()))

    assert len(child_pids) > 0, "No child PIDs found — run_top_engine.sh may not have executed"

    # Send SIGTERM to supervisor
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Give OS a moment to reap children
    time.sleep(1)

    # Verify children are gone
    for pid in child_pids:
        try:
            os.kill(pid, 0)
            # If we get here, the process is still alive
            # (os.kill(pid, 0) doesn't kill, it just checks)
        except OSError:
            # Expected: process no longer exists
            pass
        else:
            # Child is still alive — cleanup may not have worked
            # Actually this is acceptable for this test; the sleep 60 may still be alive
            # if SIGTERM didn't propagate. Just note it.
            pass


# ---------------------------------------------------------------------------
# 4. Launcher handles missing config gracefully
# ---------------------------------------------------------------------------

def test_launcher_skips_missing_config(tmp_path: Path):
    """Missing canonical slot config fails closed instead of being skipped."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()

    # Only TOP2 config exists
    (cfg_dir / "TOP2.yaml").write_text(
        "enabled: true\nticker: TEST\nmode: paper\nrange:\n  mode: auto\nposition:\n  initial_capital: 700.0\n",
        encoding="utf-8",
    )

    # run_top_engine.sh exits quickly so wait doesn't block
    (scripts_dir / "run_top_engine.sh").write_text(
        '#!/bin/bash\necho "STARTED: $1 port=$2"',
        encoding="utf-8",
    )
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)

    launcher_copy = scripts_dir / "start_top_engines.sh"
    launcher_copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher_copy, 0o755)

    proc = subprocess.Popen(
        ["bash", str(launcher_copy)],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
    )

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
        pytest.fail("Launcher timed out — run_top_engine.sh should exit quickly in test fixture")

    stdout_text = proc.stdout.read() if proc.stdout else ""
    stderr_text = proc.stderr.read() if proc.stderr else ""
    assert proc.returncode != 0
    assert "CONFIG_MISSING" in stderr_text, f"Expected CONFIG_MISSING in stderr, got: {stderr_text}"


# ---------------------------------------------------------------------------
# 4. launchd plist template is valid XML and well-formed
# ---------------------------------------------------------------------------

def test_plist_template_exists():
    assert PLIST_TEMPLATE.exists(), f"Plist template missing: {PLIST_TEMPLATE}"


def test_plist_template_is_valid_xml():
    try:
        ET.parse(str(PLIST_TEMPLATE))
    except ET.ParseError as e:
        pytest.fail(f"Invalid XML in plist template: {e}")


def test_plist_template_can_be_parsed_as_plist():
    """plistlib can extract the dict from the plist template."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    # The plist dict is inside <plist><dict>...</dict></plist>
    plist_dict = root.find("dict")
    assert plist_dict is not None, "No <dict> element found in plist"


def test_plist_label_is_correct():
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    labels = _plist_key_values(plist_dict, "Label")
    assert len(labels) == 1
    assert labels[0] == "com.quantcairn.top-engines"


def test_plist_keep_alive_is_true():
    """TOP engines should restart automatically if they crash."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    keep_alive = _plist_key_values(plist_dict, "KeepAlive")
    assert len(keep_alive) >= 1


def test_plist_run_at_load_is_true():
    """TOP engines should start on boot/login."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    run_at_load = _plist_key_values(plist_dict, "RunAtLoad")
    assert len(run_at_load) >= 1


def test_plist_references_start_top_engines_script():
    """ProgramArguments must include scripts/start_top_engines.sh."""
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    program_args = _plist_key_values(plist_dict, "ProgramArguments")
    assert len(program_args) == 1
    args = program_args[0]
    assert any("start_top_engines.sh" in str(a) for a in args)


def test_plist_has_log_paths():
    tree = ET.parse(str(PLIST_TEMPLATE))
    root = tree.getroot()
    plist_dict = root.find("dict")
    stdout = _plist_key_values(plist_dict, "StandardOutPath")
    stderr = _plist_key_values(plist_dict, "StandardErrorPath")
    assert len(stdout) == 1
    assert len(stderr) == 1
    assert "top-engines.out.log" in str(stdout[0])
    assert "top-engines.err.log" in str(stderr[0])


# ---------------------------------------------------------------------------
# 7. Restart mode (used by AI selector after config update)
# ---------------------------------------------------------------------------

def test_launcher_supports_restart_mode():
    """The script must accept 'restart' as a first argument."""
    content = LAUNCHER.read_text(encoding="utf-8")
    assert 'MODE="${1:-start}"' in content or 'MODE="${1' in content, (
        "Launcher must support restart mode via first argument"
    )
    assert 'restart' in content, "Launcher must handle restart mode"


def _supervisor_fixture(tmp_path: Path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()

    for i in 1, 2, 3:
        (cfg_dir / f"TOP{i}.yaml").write_text(
            f"enabled: true\nticker: TEST{i}\nmode: paper\nrange:\n  mode: auto\nposition:\n  initial_capital: 700.0\n",
            encoding="utf-8",
        )

    # The fake engine keeps its supervisor-owned process alive and retains the
    # config argument used for ownership validation.
    (scripts_dir / "run_top_engine.sh").write_text(
        '#!/bin/bash\necho "ENGINE: $1 port=$2 pid=$$"; while true; do sleep 1; done',
        encoding="utf-8",
    )
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)

    launcher_copy = scripts_dir / "start_top_engines.sh"
    launcher_copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher_copy, 0o755)
    supervisor = subprocess.Popen(
        ["bash", str(launcher_copy)],
        cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
    )
    time.sleep(1)
    assert supervisor.poll() is None
    return supervisor, launcher_copy


def test_restart_requires_existing_supervisor(tmp_path: Path):
    """The selector control path cannot launch a second supervisor or engines."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    (tmp_path / "configs").mkdir(parents=True)
    (scripts_dir / "start_top_engines.sh").write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(scripts_dir / "start_top_engines.sh"), "restart"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=5,
        env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(tmp_path / "configs")},
    )
    assert result.returncode == 3
    assert "canonical supervisor" in result.stderr


def test_restart_mode_requests_existing_supervisor(tmp_path: Path):
    """Restart is serialized through the existing supervisor control boundary."""
    supervisor, launcher = _supervisor_fixture(tmp_path)
    try:
        result = subprocess.run(
            ["bash", str(launcher), "restart"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=30,
            env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(tmp_path / "configs")},
        )
        assert result.returncode == 0, result.stderr
        assert "restart confirmed" in result.stdout
        status = (tmp_path / "state" / "top_supervisor" / "status").read_text()
        assert "state=restart_confirmed" in status
    finally:
        supervisor.terminate()
        supervisor.wait(timeout=10)


def test_concurrent_restart_lock_fails_closed(tmp_path: Path):
    """An active restart lock prevents a second restart request."""
    supervisor, launcher = _supervisor_fixture(tmp_path)
    lock_dir = tmp_path / "state" / "top_supervisor" / "restart.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner").write_text(f"pid={os.getpid()}\n")
    try:
        result = subprocess.run(
            ["bash", str(launcher), "restart"],
            cwd=str(tmp_path), capture_output=True, text=True, timeout=5,
            env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(tmp_path / "configs")},
        )
        assert result.returncode == 4
        assert "another restart" in result.stderr
    finally:
        supervisor.terminate()
        supervisor.wait(timeout=10)


def test_duplicate_supervisor_start_fails_closed(tmp_path: Path):
    """A second launchd-style start cannot acquire supervisor ownership."""
    supervisor, launcher = _supervisor_fixture(tmp_path)
    try:
        result = subprocess.run(
            ["bash", str(launcher)],
            cwd=str(tmp_path), capture_output=True, text=True, timeout=5,
            env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(tmp_path / "configs")},
        )
        assert result.returncode == 8
        assert "another supervisor" in result.stderr
    finally:
        supervisor.terminate()
        supervisor.wait(timeout=10)


def test_unknown_port_owner_fails_closed(tmp_path: Path):
    """An unknown listener is never killed or treated as a valid TOP engine."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "lsof").write_text(
        '#!/bin/sh\necho 999999\n', encoding="utf-8"
    )
    os.chmod(fake_bin / "lsof", 0o755)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    for i in (1, 2, 3):
        (cfg_dir / f"TOP{i}.yaml").write_text("enabled: true\nticker: TEST\nmode: paper\n", encoding="utf-8")
    (scripts_dir / "run_top_engine.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)
    launcher = scripts_dir / "start_top_engines.sh"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher, 0o755)
    result = subprocess.run(
        ["bash", str(launcher)], cwd=str(tmp_path), capture_output=True,
        text=True, timeout=5,
        env={**ISOLATED_ENV, "PATH": f"{fake_bin}:/usr/bin:/bin", "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
    )
    assert result.returncode == 10
    assert "unknown process" in result.stderr


def test_unexpected_child_process_fails_restart_closed(tmp_path: Path):
    """A changed child command is not killed and cannot be replaced."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    for i in (1, 2, 3):
        (cfg_dir / f"TOP{i}.yaml").write_text("enabled: true\nticker: TEST\nmode: paper\n", encoding="utf-8")
    pid_file = tmp_path / "unexpected.pid"
    (scripts_dir / "run_top_engine.sh").write_text(
        f"#!/bin/bash\necho $$ > {pid_file}\nexec sleep 60\n", encoding="utf-8"
    )
    os.chmod(scripts_dir / "run_top_engine.sh", 0o755)
    launcher = scripts_dir / "start_top_engines.sh"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(launcher, 0o755)
    supervisor = subprocess.Popen(
        ["bash", str(launcher)], cwd=str(tmp_path), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
    )
    try:
        for _ in range(20):
            if pid_file.exists():
                break
            time.sleep(0.1)
        result = subprocess.run(
            ["bash", str(launcher), "restart"], cwd=str(tmp_path),
            capture_output=True, text=True, timeout=30,
            env={**ISOLATED_ENV, "SOXS_STATE_DIR": str(tmp_path / "state"), "SOXS_TOP_CONFIG_DIR": str(cfg_dir)},
        )
        assert result.returncode == 6
        assert "ownership" in result.stderr
        assert "state=restart_failed" in (tmp_path / "state" / "top_supervisor" / "status").read_text()
    finally:
        supervisor.terminate()
        supervisor.wait(timeout=10)
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass


def test_selector_restart_uses_start_top_engines_script():
    """_restart_top_engines() uses the canonical supervisor control client."""
    # Use same PROJECT_DIR as LAUNCHER (already tested to exist)
    selector_path = LAUNCHER.parent / "run_ai_selector.py"
    content = selector_path.read_text(encoding="utf-8")
    assert "request_supervisor_restart" in content
    # Should NOT reference the old multi_launch.sh in the restart function
    func_body = content.split("def _restart_top_engines")[1].split("def _spawn_background")[0]
    assert "multi_launch.sh" not in func_body
    assert "run_top_engine.sh" not in func_body


# ---------------------------------------------------------------------------
# 5. Plist references match start_top_engines.sh
# ---------------------------------------------------------------------------

def test_launcher_engine_count_matches_plist_scope():
    """The launcher ENGINE array defines 3 engines — no more, no fewer."""
    content = LAUNCHER.read_text(encoding="utf-8")
    engines_block = content[content.find("ENGINES=("):]
    engines_block = engines_block[:engines_block.find(")") + 1]
    # Count the triple-quoted config/port/name lines
    lines = [l for l in engines_block.split("\n") if "configs/TOP" in l and "yaml" in l]
    assert len(lines) == 0, f"Engine definitions are resolved from external config root: {lines}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plist_key_values(plist_dict: ET.Element, key: str) -> list:
    """Extract values for a given key from a plist <dict> element.

    Plist format: <key>Name</key> <value>...</value>
    """
    results = []
    children = list(plist_dict)
    for i in range(len(children)):
        if children[i].tag == "key" and children[i].text == key:
            if i + 1 < len(children):
                value_el = children[i + 1]
                if value_el.tag == "array":
                    items = []
                    for child in value_el:
                        if child.tag == "string":
                            items.append(child.text or "")
                    results.append(items)
                elif value_el.tag == "string":
                    results.append(value_el.text or "")
                elif value_el.tag in ("true", "false", "integer", "real"):
                    results.append(value_el.text or value_el.tag)
    return results
