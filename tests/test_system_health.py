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
            "execution_mode", "notifier", "paper_portfolio", "processes",
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
        assert "processes" in report

    def test_render_text_does_not_crash(self):
        from scripts.system_health import generate_report, render_text

        report = generate_report()
        text = render_text(report)
        assert "QuantCairn Health Report" in text
        assert "Scheduler:" in text
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
