from __future__ import annotations

import sys
import tempfile
import subprocess
import os
from pathlib import Path

import yaml

VENV_SITE_PACKAGES = next(
    (path for path in (Path(__file__).resolve().parents[1] / ".venv" / "lib").glob("python*/site-packages") if path.exists()),
    None,
)
if VENV_SITE_PACKAGES is not None and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from src.openalpha import selection_state
from src.dashboard import combined as dashboard


def _write_order_state(path: Path, *, ticker: str, reason: str, timestamp: str, runtime_scope: str | None = None, blocked: bool = False) -> None:
    payload = {
        "ticker": ticker,
        "updated_at": timestamp,
        "failed_orders_today": [
            {
                "ticker": ticker,
                "timestamp": timestamp,
                "reason": reason,
                "quantity": 0,
                "price": 0.0,
                "buying_power": 0.0,
            }
        ],
    }
    if runtime_scope:
        payload["runtime_scope"] = runtime_scope
    if blocked:
        payload["blocked"] = {
            "blocked_until": "2026-07-09T11:10:25",
            "reason": reason,
            "buying_power_at_block": 329.72,
        }
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_selection_mismatch_shows_exact_symbol_lists(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        for folder in ("configs", "reports", "logs", "state", "runtime"):
            (project_dir / folder).mkdir()
        (project_dir / "configs" / "TOP1.yaml").write_text("ticker: SOXS\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP2.yaml").write_text("ticker: YINN\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP3.yaml").write_text("ticker: LABD\nmode: paper\n", encoding="utf-8")

        monkeypatch.setattr(selection_state, "PROJECT_DIR", project_dir)
        monkeypatch.setenv("SOXS_STATE_DIR", str(project_dir / "state"))
        selection_state.write_selection_state(
            et_date="2026-07-09",
            generated_at="2026-07-09T22:24:41.648117",
            selected_symbols=["AAPL", "SOFI", "DRIP"],
            report_path=str(project_dir / "reports" / "ai_selection_latest.json"),
        )

        monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
        monkeypatch.setattr(dashboard, "STATE_DIR", project_dir / "state")
        monkeypatch.setattr(dashboard, "TRADING_FLAGS_PATH", project_dir / "state" / "trading_flags.json")
        monkeypatch.setattr(dashboard, "RUNTIME_DIR", project_dir / "runtime")
        monkeypatch.setattr(dashboard, "COMBINED_PID_FILE", project_dir / "runtime" / "combined.pid")
        monkeypatch.setattr(dashboard, "_fetch_status", lambda port: {"price": 5.0, "change": 0.0, "high_1m": 5.0, "low_1m": 5.0, "bid": 5.0, "ask": 5.0, "support": 4.8, "resistance": 5.2, "spread_pct": 0.4, "range_ready": True, "range_source": "test", "last_signal": "HOLD", "last_signal_reason": "test", "initial_capital": 1000.0, "cash": 1000.0, "position_shares": 0, "daily_pnl": 0.0, "equity": 1000.0, "trades_today": 0, "win_rate": 0.0, "wins": 0, "losses": 0, "best_trade": 0.0, "worst_trade": 0.0, "avg_pnl": 0.0, "halted": False, "trade_in_progress": False} if port != 8093 else None)
        monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: {"timestamp": "2026-07-09T22:24:41.648117", "top3": [{"ticker": "AAPL"}, {"ticker": "SOFI"}, {"ticker": "DRIP"}], "top10": [], "settings": {"min_price": 4.0, "max_price": 50.0, "auto_refresh_minutes": 5, "max_symbols": 20, "data_mode": "live", "selection_stage": "quality_backfilled", "fallback_used": False}, "protected_positions": [], "report": []})
        monkeypatch.setattr(dashboard, "_ai_runtime_status", lambda: {"level": "green", "label": "ok", "detail": "ok"})
        monkeypatch.setattr(dashboard, "_startup_guard_status", lambda selection_sync: {"level": "warn", "label": "启动校验待命", "detail": "当前没有 live TOP 配置，今天的启动校验不会触发。"})
        monkeypatch.setattr(dashboard, "_selected_stock_positions_count", lambda live_account, selected_tickers: 0)
        monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: {"mode": "paper", "positions": [], "equity": 1000.0, "cash": 1000.0, "buying_power": 1000.0})
        monkeypatch.setattr(dashboard, "_position_lookup", lambda live_account: {})
        monkeypatch.setattr(dashboard, "_load_top_modes", lambda: ["paper", "paper", "paper"])
        monkeypatch.setattr(dashboard, "_load_order_states", lambda active_symbols=None: {"blocked_tickers": [], "failed_orders_today": 0, "failed_orders_total_today": 0, "ticker_details": [], "historical_ticker_details": [], "historical_failed_orders_today": 0, "historical_failed_orders_total_today": 0})
        monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"broker_unresolved_count": 0, "broker_unresolved_oldest_seconds": 0.0, "latest_submitted_line": "", "latest_line": "", "execution_mode": "paper", "new_entries_allowed": True, "reduce_only": False, "unresolved_alert_threshold_seconds": 120, "unresolved_alert": False})
        monkeypatch.setattr(dashboard, "latest_trade_activity_day", lambda *args, **kwargs: "20260709")
        monkeypatch.setattr(dashboard, "latest_trade_log_day", lambda *args, **kwargs: "20260709")
        monkeypatch.setattr(dashboard, "load_trade_records", lambda *args, **kwargs: [])
        monkeypatch.setattr(dashboard, "_desired_audit_mode", lambda: "paper")
        monkeypatch.setattr(dashboard, "_enrich_ticker_descriptions", lambda top3_list: top3_list)

        client = dashboard.app.test_client()
        body = client.get("/").get_data(as_text=True)

        assert "selection_state tickers: ['AAPL', 'SOFI', 'DRIP']" in body
        assert "current TOP config tickers: ['SOXS', 'YINN', 'LABD']" in body
        assert any(
            reason in body
            for reason in (
                "等待本次选股",
                "awaiting_current_session_selection",
                "selection_state_date_mismatch",
                "上一完整交易日状态仍可继续使用",
            )
        )
        assert "请重新运行 AI Selector 或重新写入 TOP 配置" in body


def test_selection_match_does_not_show_mismatch(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        for folder in ("configs", "reports", "logs", "state", "runtime"):
            (project_dir / folder).mkdir()
        (project_dir / "configs" / "TOP1.yaml").write_text("ticker: AAPL\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP2.yaml").write_text("ticker: SOFI\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP3.yaml").write_text("ticker: DRIP\nmode: paper\n", encoding="utf-8")

        monkeypatch.setattr(selection_state, "PROJECT_DIR", project_dir)
        monkeypatch.setenv("SOXS_STATE_DIR", str(project_dir / "state"))
        selection_state.write_selection_state(
            et_date="2026-07-09",
            generated_at="2026-07-09T22:24:41.648117",
            selected_symbols=["AAPL", "SOFI", "DRIP"],
            report_path=str(project_dir / "reports" / "ai_selection_latest.json"),
        )

        ok, reason, state = selection_state.verify_selection_state(required_et_date="2026-07-09")
        assert ok is True
        assert reason == "ok"
        assert state is not None


def test_health_check_prints_selection_and_top_symbols(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        for folder in ("configs", "reports", "logs", "state", "runtime"):
            (project_dir / folder).mkdir()
        (project_dir / "configs" / "TOP1.yaml").write_text("ticker: SOXS\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP2.yaml").write_text("ticker: YINN\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP3.yaml").write_text("ticker: LABD\nmode: paper\n", encoding="utf-8")

        monkeypatch.setattr(selection_state, "PROJECT_DIR", project_dir)
        monkeypatch.setenv("SOXS_STATE_DIR", str(project_dir / "state"))
        selection_state.write_selection_state(
            et_date="2026-07-09",
            generated_at="2026-07-09T22:24:41.648117",
            selected_symbols=["AAPL", "SOFI", "DRIP"],
            report_path=str(project_dir / "reports" / "ai_selection_latest.json"),
        )

        env = os.environ.copy()
        env["SOXS_PROJECT_DIR"] = str(project_dir)
        env["SOXS_PYTHON_BIN"] = str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python")
        proc = subprocess.run(
            ["bash", str(Path(__file__).resolve().parents[1] / "health_check.sh")],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0
        assert "selection_state tickers: ['AAPL', 'SOFI', 'DRIP']" in proc.stdout
        assert "current TOP config tickers: ['SOXS', 'YINN', 'LABD']" in proc.stdout
        assert any(
            reason in proc.stdout
            for reason in (
                "等待本次选股",
                "awaiting_current_session_selection",
                "selection_state_date_mismatch",
                "上一完整交易日状态仍可继续使用",
            )
        )
