from __future__ import annotations

import plistlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.utils.trading_schedule import auto_trade_decision


PROJECT_DIR = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _write_mock_project(root: Path) -> None:
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for top_name in ("TOP1", "TOP2", "TOP3"):
        (root / "configs" / f"{top_name}.yaml").write_text("mode: paper\n", encoding="utf-8")
    sleeper = (
        "import signal, time\n"
        "running = True\n"
        "def stop(_signum, _frame):\n"
        "    global running\n"
        "    running = False\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while running:\n"
        "    time.sleep(0.1)\n"
    )
    (root / "run.py").write_text(sleeper, encoding="utf-8")
    (root / "scripts" / "start_combined.py").write_text(sleeper, encoding="utf-8")
    (root / "scripts" / "start_orphan_monitor.py").write_text(sleeper, encoding="utf-8")
    (root / "scripts" / "ai_selector_wrapper.py").write_text(sleeper, encoding="utf-8")


def _spawn(args: list[str], *, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, *args],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def _run_stop_top(project: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "SOXS_PROJECT_DIR": str(project),
            "SOXS_RUNTIME_DIR": str(project / "runtime"),
            "SOXS_LOG_DIR": str(project / "logs"),
            "SOXS_USE_LAUNCHD_TOPS": "0",
            "SOXS_TOP_STOP_TIMEOUT_SECONDS": "1",
        }
    )
    return subprocess.run(
        ["bash", str(PROJECT_DIR / "multi_launch.sh"), "stop-top"],
        cwd=str(PROJECT_DIR),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_scheduled_start_runs_near_us_market_open_in_dst():
    decision = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 21, 9, 25, tzinfo=ET),
    )

    assert decision.should_run is True
    assert decision.reason == "market_open_start_window"
    assert decision.session_date == "2026-07-21"
    assert decision.required_selection_date == "2026-07-20"


def test_scheduled_start_runs_near_us_market_open_in_standard_time():
    decision = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 1, 6, 9, 25, tzinfo=ET),
    )

    assert decision.should_run is True
    assert decision.reason == "market_open_start_window"


def test_scheduled_stop_runs_after_us_market_close():
    decision = auto_trade_decision(
        "scheduled-stop",
        now_et=datetime(2026, 7, 21, 16, 5, tzinfo=ET),
    )

    assert decision.should_run is True
    assert decision.reason == "market_close_stop_window"
    assert decision.required_selection_date == "2026-07-21"


def test_scheduled_stop_uses_known_half_day_close():
    half_day = auto_trade_decision(
        "scheduled-stop",
        now_et=datetime(2026, 11, 27, 13, 5, tzinfo=ET),
    )
    late_full_day_time = auto_trade_decision(
        "scheduled-stop",
        now_et=datetime(2026, 11, 27, 16, 5, tzinfo=ET),
    )

    assert half_day.should_run is True
    assert half_day.reason == "market_close_stop_window"
    assert late_full_day_time.should_run is False
    assert late_full_day_time.reason == "outside_market_close_stop_window"


def test_scheduled_actions_skip_weekends_and_holidays():
    weekend = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 18, 9, 25, tzinfo=ET),
    )
    holiday = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 3, 9, 25, tzinfo=ET),
    )

    assert weekend.should_run is False
    assert weekend.reason == "not_us_market_trading_day"
    assert holiday.should_run is False
    assert holiday.reason == "not_us_market_trading_day"


def test_scheduled_actions_are_idempotent_per_session_marker():
    decision = auto_trade_decision(
        "scheduled-start",
        now_et=datetime(2026, 7, 21, 9, 35, tzinfo=ET),
        already_ran=True,
    )

    assert decision.should_run is False
    assert decision.reason == "already_ran_for_session"


def test_launchd_templates_use_market_calendar_scheduled_entrypoints():
    start_plist = plistlib.loads((PROJECT_DIR / "launchd/com.soxs.arbitrage.plist").read_bytes())
    stop_plist = plistlib.loads((PROJECT_DIR / "launchd/com.soxs.arbitrage.stop.plist").read_bytes())

    assert start_plist["ProgramArguments"][-1] == "scheduled-start"
    assert stop_plist["ProgramArguments"][-1] == "scheduled-stop"
    assert start_plist["StartInterval"] == 60
    assert stop_plist["StartInterval"] == 60
    assert "StartCalendarInterval" not in start_plist
    assert "StartCalendarInterval" not in stop_plist


def test_auto_trade_scheduled_stop_uses_top_only_shutdown():
    text = (PROJECT_DIR / "auto_trade.sh").read_text(encoding="utf-8")

    scheduled_stop = text.split("    scheduled-stop)", 1)[1].split(";;", 1)[0]
    assert '"$MULTI_LAUNCH" stop-top' in scheduled_stop
    assert '"$0" stop' not in scheduled_stop


def test_multi_launch_keeps_manual_full_stop_separate_from_stop_top():
    text = (PROJECT_DIR / "multi_launch.sh").read_text(encoding="utf-8")

    assert "stop-top)" in text
    assert "stop_top_scheduled_only" in text
    manual_stop = text.split("stop)", 1)[1].split(";;", 1)[0]
    top_only_stop = text.split("stop-top)", 1)[1].split(";;", 1)[0]
    assert "stop_existing" in manual_stop
    assert "stop_existing" not in top_only_stop
    assert "stop_combined" not in top_only_stop
    assert "start_orphan_monitor.py" not in top_only_stop


def test_stop_top_stops_only_top_processes_and_leaves_services_running(tmp_path):
    _write_mock_project(tmp_path)
    top_processes = []
    service_processes = []
    try:
        for idx, top_name in enumerate(("TOP1", "TOP2", "TOP3"), start=1):
            proc = _spawn(
                [
                    str(tmp_path / "run.py"),
                    "--config",
                    str(tmp_path / "configs" / f"{top_name}.yaml"),
                    "--paper",
                    "--dashboard",
                    "--anytime",
                    "--port",
                    str(8090 + idx),
                ],
                cwd=tmp_path,
            )
            top_processes.append(proc)
            (tmp_path / "runtime" / f"top{idx}.pid").write_text(str(proc.pid), encoding="utf-8")
        service_processes = [
            _spawn([str(tmp_path / "scripts" / "start_combined.py")], cwd=tmp_path),
            _spawn([str(tmp_path / "scripts" / "start_orphan_monitor.py")], cwd=tmp_path),
            _spawn([str(tmp_path / "scripts" / "ai_selector_wrapper.py")], cwd=tmp_path),
        ]

        result = _run_stop_top(tmp_path)
        time.sleep(0.5)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "slot=TOP1" in result.stdout
        assert "slot=TOP2" in result.stdout
        assert "slot=TOP3" in result.stdout
        assert all(not _alive(proc) for proc in top_processes)
        assert all(_alive(proc) for proc in service_processes)
    finally:
        for proc in top_processes + service_processes:
            _terminate(proc)


def test_stop_top_is_successful_when_already_stopped(tmp_path):
    _write_mock_project(tmp_path)

    result = _run_stop_top(tmp_path)

    assert result.returncode == 0
    assert result.stdout.count("result=already_stopped") == 3


def test_stop_top_handles_stale_pid_without_killing_unrelated_process(tmp_path):
    _write_mock_project(tmp_path)
    unrelated = _spawn([str(tmp_path / "scripts" / "start_combined.py")], cwd=tmp_path)
    try:
        (tmp_path / "runtime" / "top1.pid").write_text("99999999\n", encoding="utf-8")

        result = _run_stop_top(tmp_path)

        assert result.returncode == 0
        assert "slot=TOP1 pid=99999999 result=stale_pid" in result.stdout
        assert _alive(unrelated)
    finally:
        _terminate(unrelated)


def test_stop_top_rejects_pid_identity_mismatch_without_killing_process(tmp_path):
    _write_mock_project(tmp_path)
    unrelated = _spawn([str(tmp_path / "scripts" / "start_combined.py")], cwd=tmp_path)
    try:
        (tmp_path / "runtime" / "top1.pid").write_text(f"{unrelated.pid}\n", encoding="utf-8")

        result = _run_stop_top(tmp_path)

        assert result.returncode != 0
        assert f"slot=TOP1 pid={unrelated.pid} result=identity_mismatch" in result.stdout
        assert _alive(unrelated)
    finally:
        _terminate(unrelated)
