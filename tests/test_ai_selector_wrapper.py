from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Load the wrapper module once for patching via import path
SCRIPT_PATH = str(PROJECT_DIR / "scripts" / "ai_selector_wrapper.py")
_AW = "scripts.ai_selector_wrapper"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _setup_base_mocks(monkeypatch, tmp_path):
    """Mock the environment so that the wrapper reaches the report trigger path.

    All mocks target the wrapper module's namespace so the test is about the
    function's behaviour, not about the real system state.
    """
    project = str(tmp_path)

    # Redirect filesystem-sensitive module globals so temp dir is used.
    monkeypatch.setattr(f"{_AW}.PROJECT_DIR", project)
    monkeypatch.setattr(f"{_AW}.STATE_DIR", os.path.join(project, "state"))
    monkeypatch.setattr(f"{_AW}.OUT_LOG", os.path.join(project, "logs", "selector.out.log"))
    monkeypatch.setattr(f"{_AW}.ERR_LOG", os.path.join(project, "logs", "selector.err.log"))
    monkeypatch.setattr(f"{_AW}.SELECTOR", os.path.join(project, "scripts", "run_ai_selector.py"))
    monkeypatch.setattr(f"{_AW}.VENV_PY", sys.executable)

    # Create the selector & report scripts so os.path.exists / real path work.
    scripts_dir = Path(project) / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "run_ai_selector.py").write_text("# dummy", encoding="utf-8")
    (scripts_dir / "generate_daily_research_report.py").write_text("# dummy", encoding="utf-8")
    (scripts_dir / "start_top_engines.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

    # Force the run
    monkeypatch.setenv("FORCE_AI_RUN", "1")

    # Always a trading day, always in market time, never already ran.
    monkeypatch.setattr(f"{_AW}.is_us_market_trading_day", lambda d: True)
    monkeypatch.setattr(f"{_AW}.is_market_time", lambda now_et: True)
    monkeypatch.setattr(f"{_AW}.already_ran_today", lambda now_et: False)

    # mark_ran_today is a no-op (tmp_path doesn't need real state markers).
    monkeypatch.setattr(f"{_AW}.mark_ran_today", lambda now_et: None)

    # Silence _verbose to avoid noise during tests.
    monkeypatch.setattr(f"{_AW}._verbose", lambda msg: None)

    # Suppress the venv re-exec guard so we stay in-process.
    monkeypatch.setattr(f"{_AW}.VENV_PREFIX", sys.prefix)
    monkeypatch.setattr(f"{_AW}.os.path.exists", lambda p: True)


def _mock_selector_success(monkeypatch):
    """Make the selector subprocess.Popen return success (returncode 0)."""
    class _Proc:
        returncode = 0

        @staticmethod
        def wait():
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _Proc())


def _mock_selector_failure(monkeypatch, code=1):
    """Make the selector subprocess.Popen return non-zero."""
    class _Proc:
        returncode = code

        @staticmethod
        def wait():
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _Proc())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_selector_success_triggers_report_generation(monkeypatch, tmp_path):
    """Successful selector run → subprocess.run called with the report script."""
    _setup_base_mocks(monkeypatch, tmp_path)
    _mock_selector_success(monkeypatch)

    run_calls: list[tuple] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: run_calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )

    from scripts.ai_selector_wrapper import _run_selection_if_due
    _run_selection_if_due()

    report_calls = [
        (cmd, kw) for cmd, kw in run_calls
        if any("generate_daily_research_report.py" in str(c) for c in cmd)
    ]
    assert len(report_calls) == 1, f"Expected 1 report call, got {len(report_calls)}"
    cmd, kwargs = report_calls[0]
    assert "--no-site" in cmd
    assert kwargs.get("timeout") == 120
    assert kwargs.get("capture_output") is True


def test_selector_failure_does_not_trigger_report(monkeypatch, tmp_path):
    """Non-zero selector exit means NO report is triggered."""
    _setup_base_mocks(monkeypatch, tmp_path)
    _mock_selector_failure(monkeypatch, code=1)

    run_calls: list[tuple] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: run_calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )

    from scripts.ai_selector_wrapper import _run_selection_if_due

    with pytest.raises(SystemExit) as exc_info:
        _run_selection_if_due()

    assert exc_info.value.code == 1
    report_calls = [
        (cmd, kw) for cmd, kw in run_calls
        if any("generate_daily_research_report.py" in str(c) for c in cmd)
    ]
    assert len(report_calls) == 0, "Report must not be triggered on selector failure"


def test_restart_compensation_does_not_rerun_selector(monkeypatch, tmp_path):
    """A failed selector restart retries only the supervisor control path."""
    _setup_base_mocks(monkeypatch, tmp_path)
    monkeypatch.delenv("SOXS_STATE_DIR", raising=False)
    _mock_selector_failure(monkeypatch, code=6)
    status_path = Path(tmp_path) / "state" / "top_restart_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        '{"status":"FAILED","selection_run_id":"selection-1",'
        '"selection_bundle_hash":"bundle-1"}',
        encoding="utf-8",
    )
    restart_calls: list[list[str]] = []

    def _restart_only(cmd, **kw):
        restart_calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", _restart_only)

    from scripts.ai_selector_wrapper import _run_selection_if_due

    _run_selection_if_due()
    assert len(restart_calls) == 1
    assert restart_calls[0][-1] == "restart"
    assert status_path.read_text(encoding="utf-8").find('"status": "CONFIRMED"') >= 0


def test_generator_failure_does_not_fail_wrapper(monkeypatch, tmp_path):
    """When subprocess.run raises, the wrapper still completes normally."""
    _setup_base_mocks(monkeypatch, tmp_path)
    _mock_selector_success(monkeypatch)

    call_count = [0]

    def _failing_run(cmd, **kw):
        if any("generate_daily_research_report.py" in str(c) for c in cmd):
            call_count[0] += 1
            raise RuntimeError("simulated report generation failure")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", _failing_run)

    from scripts.ai_selector_wrapper import _run_selection_if_due

    # Must NOT raise
    _run_selection_if_due()
    assert call_count[0] == 1, "Report generation was attempted"


def test_generator_timeout_does_not_fail_wrapper(monkeypatch, tmp_path):
    """When subprocess.run raises TimeoutExpired, the wrapper still succeeds."""
    _setup_base_mocks(monkeypatch, tmp_path)
    _mock_selector_success(monkeypatch)

    call_count = [0]

    def _timeout_run(cmd, **kw):
        if any("generate_daily_research_report.py" in str(c) for c in cmd):
            call_count[0] += 1
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 120))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", _timeout_run)

    from scripts.ai_selector_wrapper import _run_selection_if_due

    _run_selection_if_due()
    assert call_count[0] == 1


def test_wrapper_prints_success_even_when_report_fails(monkeypatch, tmp_path):
    """The completion message prints regardless of report generation outcome."""
    _setup_base_mocks(monkeypatch, tmp_path)
    _mock_selector_success(monkeypatch)

    def _failing_run(cmd, **kw):
        if any("generate_daily_research_report.py" in str(c) for c in cmd):
            raise RuntimeError("simulated failure")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", _failing_run)

    printed: list[str] = []

    def _capture(msg):
        printed.append(msg)

    monkeypatch.setattr("builtins.print", _capture)

    from scripts.ai_selector_wrapper import _run_selection_if_due

    _run_selection_if_due()

    success_lines = [line for line in printed if "AI selector completed" in line]
    assert len(success_lines) > 0, "Success message must print after report failure"


def test_non_trading_day_skips_everything(monkeypatch, tmp_path):
    """On a non-trading day neither the selector nor the report runs."""
    _setup_base_mocks(monkeypatch, tmp_path)
    monkeypatch.setenv("FORCE_AI_RUN", "0")
    monkeypatch.setattr(f"{_AW}.is_us_market_trading_day", lambda d: False)

    popen_called = [False]
    run_called = [False]

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: popen_called.__setitem__(0, True) or type("P", (), {"wait": lambda: None, "returncode": 0})(),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: run_called.__setitem__(0, True) or type("R", (), {"returncode": 0})(),
    )

    from scripts.ai_selector_wrapper import _run_selection_if_due

    _run_selection_if_due()
    assert not popen_called[0], "Selector must not run on non-trading day"
    assert not run_called[0], "Report must not run on non-trading day"
