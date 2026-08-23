"""Tests for scripts/system_health.py — read-only diagnostic tool."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "system_health.py"


class TestSystemHealthScript:
    """The script is importable and runs without crashing."""

    def test_script_syntax_valid(self):
        source = SCRIPT.read_text()
        compile(source, str(SCRIPT), "exec")

    def test_script_can_be_imported(self):
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "system_health", str(SCRIPT)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)

    def test_json_mode_runs(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"Script failed:\n{result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, dict)
        required_keys = {
            "scheduler", "ai_selector", "market",
            "execution_mode", "notifier", "paper_portfolio", "orphan_monitor", "processes",
        }
        assert required_keys.issubset(set(data.keys())), \
            f"Missing keys: {required_keys - set(data.keys())}"


class TestSchedulerCheck:
    """_check_scheduler() handles all file states."""

    def test_handles_missing_log_file(self, tmp_path):
        from scripts.system_health import _check_scheduler
        result = _check_scheduler(project_dir=tmp_path)
        assert not result["active"]
        assert result["last_decision_reason"] == "no_log_file"

    def test_parses_scheduler_decisions(self, tmp_path):
        from scripts.system_health import _check_scheduler

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True)
        log = logs_dir / "ai_selector.err.log"
        today = date.today().isoformat()
        log.write_text(
            f"[SCHEDULER] ai_selector decision=skipped reason=non_trading_day(weekend) "
            f"et_date={today} et_time=14:30 force=False pid=12345\n"
            f"[SCHEDULER] ai_selector decision=run "
            f"et_date=2026-07-24 et_time=09:00 trading_day=True force=False pid=12346\n"
        )
        result = _check_scheduler(project_dir=tmp_path)
        assert result["active"]
        assert result["last_decision"] == "run"
        assert result["today_skipped_count"] >= 1


class TestMarketCheck:
    """_check_market() is read-only and safe."""

    def test_returns_valid_structure(self):
        from scripts.system_health import _check_market
        result = _check_market()

        if "error" not in result:
            assert "session_label" in result
            assert "market_open" in result
            assert isinstance(result["market_open"], bool)


class TestExecutionModeCheck:
    """_check_execution_mode() detects live trading correctly."""

    def test_default_is_disabled(self):
        from scripts.system_health import _check_execution_mode
        result = _check_execution_mode()
        assert "execution_mode" in result
        gate = result["live_order_gate"]
        # By default live trading must be disabled
        assert "ENABLED" not in gate["effective_live_trading"], \
            f"Live trading unexpectedly enabled: {gate}"


class TestNotifierCheck:
    """_check_notifier() handles missing state."""

    def test_handles_missing_state_file(self, tmp_path):
        from scripts.system_health import _check_notifier
        result = _check_notifier(project_dir=tmp_path)
        assert result["dedup_state"] == "missing"
        assert result["records"] == 0

    def test_reads_valid_state(self, tmp_path):
        from scripts.system_health import _check_notifier

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "trade_notification_state.json"
        state_file.write_text(json.dumps({
            "schema_version": "trade_notification_state.v1",
            "sent_keys": ["paper:NVDA:BUY:fill:abc123", "live:SOXS:SELL:fill:xyz"],
            "notifications": {
                "paper:NVDA:BUY:fill:abc123": {
                    "ticker": "NVDA", "side": "BUY",
                    "created_at": "2026-07-24T10:00:00-04:00",
                },
                "live:SOXS:SELL:fill:xyz": {
                    "ticker": "SOXS", "side": "SELL",
                    "created_at": "2026-07-22T14:00:00-04:00",
                },
            },
            "updated_at": "2026-07-24T10:00:01-04:00",
        }))

        result = _check_notifier(project_dir=tmp_path)
        assert result["dedup_state"] == "active"
        assert result["records"] == 2
        assert result["paper_records"] == 1
        assert result["live_records"] == 1


class TestPaperPortfolioCheck:
    """_check_paper_portfolio() handles missing or malformed state."""

    def test_handles_missing_state(self, tmp_path):
        from scripts.system_health import _check_paper_portfolio
        result = _check_paper_portfolio(project_dir=tmp_path)
        assert result["state"] == "missing"

    def test_reads_valid_portfolio(self, tmp_path):
        from scripts.system_health import _check_paper_portfolio

        state_dir = tmp_path / "state" / "paper" / "paper-default"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "portfolio_state.json"
        state_file.write_text(json.dumps({
            "cash": 9500.0,
            "equity": 10500.0,
            "buying_power": 19000.0,
            "realized_pnl": 125.50,
            "unrealized_pnl": 500.0,
            "positions": [
                {"symbol": "NVDA", "quantity": 5, "market_value": 1000.0},
            ],
            "total_trades": 3,
            "updated_at": "2026-07-24T16:00:00Z",
        }))

        result = _check_paper_portfolio(project_dir=tmp_path)
        assert result["state"] == "valid"
        assert result["cash"] == 9500.0
        assert result["equity"] == 10500.0
        assert result["positions"] == 1
        assert result["position_symbols"] == ["NVDA"]



class TestOrphanMonitorCheck:
    """_check_orphan_monitor() handles missing and populated states."""

    def test_handles_missing_installation_and_logs(self, monkeypatch, tmp_path):
        import subprocess
        import scripts.system_health as module

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        def _fake_check_output(cmd, text=True, stderr=None):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "check_output", _fake_check_output)

        result = module._check_orphan_monitor(project_dir=tmp_path)
        assert result["installed"] is False
        assert result["loaded"] is False
        assert result["running"] is False
        assert result["logs_present"] is False
        assert result["last_log_file"] is None

    def test_reads_installed_loaded_and_logs(self, monkeypatch, tmp_path):
        import subprocess
        import scripts.system_health as module

        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        plist = launch_agents / "com.quantcairn.orphan-monitor.plist"
        plist.write_text("<plist />")

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True)
        err_file = logs_dir / "orphan-monitor.err.log"
        err_file.write_text("stderr line\n")
        log_file = logs_dir / "orphan-monitor.log"
        log_file.write_text("line 1\nline 2\norphan monitor ready\n")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        def _fake_check_output(cmd, text=True, stderr=None):
            if cmd[:2] == ["launchctl", "print"]:
                return "pid = 4321\nstate = running\n"
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "check_output", _fake_check_output)

        result = module._check_orphan_monitor(project_dir=tmp_path)
        assert result["installed"] is True
        assert result["loaded"] is True
        assert result["logs_present"] is True
        assert result["last_log_file"] == str(log_file)
        assert result["last_log_excerpt"] == "orphan monitor ready"
        assert result["log_files"][0]["exists"] is True
        assert result["log_files"][1]["exists"] is True


class TestReportGeneration:
    """generate_report() always returns a structured dict."""

    def test_all_sections_present(self):
        from scripts.system_health import generate_report

        report = generate_report()
        assert isinstance(report, dict)
        assert "scheduler" in report
        assert "ai_selector" in report
        assert "market" in report
        assert "execution_mode" in report
        assert "notifier" in report
        assert "paper_portfolio" in report
        assert "orphan_monitor" in report
        assert "processes" in report

    def test_render_text_does_not_crash(self):
        from scripts.system_health import generate_report, render_text

        report = generate_report()
        text = render_text(report)
        assert "QuantCairn Health Report" in text
        assert "Scheduler:" in text
        assert "Orphan Monitor:" in text
        assert "Live Trading:" in text


class TestNoSideEffects:
    """system_health.py must never modify any file on disk."""

    def test_no_state_files_modified(self, tmp_path):
        """Running generate_report must not create or modify files."""
        import subprocess

        before_files = set()
        if (PROJECT_ROOT / "state").exists():
            for p in (PROJECT_ROOT / "state").rglob("*"):
                if p.is_file():
                    stat = p.stat()
                    before_files.add((str(p), stat.st_mtime, stat.st_size))

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

        after_files = set()
        if (PROJECT_ROOT / "state").exists():
            for p in (PROJECT_ROOT / "state").rglob("*"):
                if p.is_file():
                    stat = p.stat()
                    after_files.add((str(p), stat.st_mtime, stat.st_size))

        # No file should have changed
        changed = before_files.symmetric_difference(after_files)
        # Filter: only check files that existed before AND after
        before_paths = {p for p, _, _ in before_files}
        after_paths = {p for p, _, _ in after_files}
        for path in before_paths & after_paths:
            before_info = next((m, s) for p, m, s in before_files if p == path)
            after_info = next((m, s) for p, m, s in after_files if p == path)
            assert before_info == after_info, \
                f"File modified by health check: {path}\n  before: {before_info}\n  after: {after_info}"


class TestSlice6Diagnostics:
    def test_runtime_identity_reports_external_roots_without_creating_them(self, monkeypatch, tmp_path):
        from scripts.runtime_identity import collect_identity, identity_findings

        code = tmp_path / "code"
        code.mkdir()
        for name in ("state", "reports", "artifacts", "logs"):
            (tmp_path / "runtime" / name).mkdir(parents=True)
        monkeypatch.setenv("SOXS_PROJECT_DIR", str(code))
        monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "runtime" / "state"))
        monkeypatch.setenv("SOXS_REPORTS_DIR", str(tmp_path / "runtime" / "reports"))
        monkeypatch.setenv("SOXS_ARTIFACTS_DIR", str(tmp_path / "runtime" / "artifacts"))
        monkeypatch.setenv("SOXS_LOGS_DIR", str(tmp_path / "runtime" / "logs"))
        identity = collect_identity(code)
        assert identity["state_root"] == str(tmp_path / "runtime" / "state")
        assert identity_findings(identity)["status"] != "BLOCKED"
        assert not (code / "state").exists()

    def test_launchd_drift_detects_orphan_live_configuration(self, monkeypatch, tmp_path):
        import plistlib
        import scripts.system_health as module

        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        plist = launch_agents / "com.quantcairn.orphan-monitor.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": "com.quantcairn.orphan-monitor",
            "ProgramArguments": ["/bin/bash", str(tmp_path / "scripts" / "start_orphan_monitor.py")],
            "EnvironmentVariables": {"SOXS_PROJECT_DIR": str(tmp_path), "QUANTCAIRN_EXECUTION_MODE": "LIVE"},
        }))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = module._check_launchd_drift(tmp_path)
        orphan = result["services"]["com.quantcairn.orphan-monitor"]
        assert result["status"] == "MISCONFIGURED"
        assert "orphan_execution_mode_not_paper" in orphan["issues"]

    def test_missing_optional_artifacts_are_safe_and_preflight_is_sample_scoped(self, tmp_path):
        from scripts.system_health import generate_report, render_text

        artifact = tmp_path / "artifacts" / "selection"
        artifact.mkdir(parents=True)
        (artifact / "preflight.json").write_text(json.dumps({
            "quote_coverage_pct": 100.0, "ohlcv_coverage_pct": 100.0,
            "scan_timed_out": True, "scan_errors": [], "run_mode": "DEGRADED",
        }))
        report = generate_report(tmp_path)
        assert report["preflight"]["coverage_scope"] == "sample"
        assert report["preflight"]["status"] == "DEGRADED"
        assert "Selection Bundle" in render_text(report)

    def test_selection_bundle_identity_and_mismatch_are_reported(self, tmp_path):
        from scripts.system_health import _check_selection_bundle

        state = tmp_path / "state"
        bundle = state / "selection_bundles" / "run-1"
        bundle.mkdir(parents=True)
        (state / "selection_bundle_manifest.json").write_text(json.dumps({
            "selection_run_id": "run-1", "selection_date": "2026-08-15", "selection_bundle_hash": "hash-1",
        }))
        (state / "ai_selection_state.json").write_text(json.dumps({
            "selection_run_id": "run-2", "selection_date": "2026-08-15",
        }))
        result = _check_selection_bundle(tmp_path)
        assert result["selection_run_id"] == "run-1"
        assert "selection_run_id_mismatch" in result["issues"]

    def test_validation_safety_and_research_identity_are_read_only(self, tmp_path):
        from scripts.system_health import _check_candidate_validation, _check_research

        candidate_root = tmp_path / "artifacts" / "candidates"
        candidate_root.mkdir(parents=True)
        (candidate_root / "validation_scheduler_runs.jsonl").write_text(json.dumps({
            "validation_run_id": "v-1", "selection_run_id": "s-1", "bundle_hash": "b-1",
            "mode": "dry_run", "candidates_scanned": 3, "candidates_advanced": 0,
            "transitions": [], "errors": [],
        }) + "\n")
        validation = _check_candidate_validation(tmp_path)
        assert validation["validation_run_id"] == "v-1"
        assert validation["status"] == "HEALTHY"

        research_day = tmp_path / "artifacts" / "research" / "daily" / "2026-08-15"
        research_day.mkdir(parents=True)
        (research_day / "research_run_audit.json").write_text(json.dumps({
            "research_run_id": "r-1", "mode": "independent", "selector_invoked": False,
            "selection_run_id": "s-1", "status": "completed",
        }))
        (research_day / "daily_candidate_report.json").write_text(json.dumps({"candidate_count": 0}))
        research = _check_research(tmp_path)
        assert research["research_run_id"] == "r-1"
        assert research["selector_invoked"] is False

    def test_top_runtime_missing_control_is_stale(self, tmp_path):
        from scripts.system_health import _check_top_runtime

        result = _check_top_runtime(tmp_path)
        assert result["status"] == "STALE"
        assert result["ownership_verified"] is False
