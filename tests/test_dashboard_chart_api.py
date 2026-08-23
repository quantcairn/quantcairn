from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

VENV_SITE_PACKAGES = next(
    (path for path in (Path(__file__).resolve().parents[1] / ".venv" / "lib").glob("python*/site-packages") if path.exists()),
    None,
)
if VENV_SITE_PACKAGES is not None and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from src.dashboard import combined as dashboard


def _filled_order_record(order_id: str, ticker: str, side: str, price: float, qty: int, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "broker": "longbridge",
        "environment": "prod",
        "region": "cn",
        "action": "get_order",
        "ok": True,
        "request": {"order_id": order_id},
        "response": {
            "order": (
                'OrderDetail { order_id: "%s", status: Filled, quantity: %d, executed_quantity: %d, '
                'executed_price: Some(%s), submitted_at: "%s", side: %s, symbol: "%s.US" }'
                % (order_id, qty, qty, price, timestamp, side, ticker)
            ),
            "mapped_status": "FILLED",
        },
    }


def _patch_chart_basics(
    monkeypatch,
    price_map: dict[int, dict | None],
    trades: list[dict],
    project_dir: Path | None = None,
    trade_day: str = "20260709",
):
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: price_map.get(port))
    monkeypatch.setattr(
        dashboard,
        "_load_order_states",
        lambda: {
            "blocked_tickers": [],
            "failed_orders_today": 0,
            "failed_orders_total_today": 0,
            "ticker_details": [],
        },
    )
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: {"mode": "paper", "positions": [], "equity": 1000.0, "cash": 1000.0, "buying_power": 1000.0})
    monkeypatch.setattr(dashboard, "_position_lookup", lambda live_account: {})
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}})
    monkeypatch.setattr(dashboard, "_ai_runtime_status", lambda: {"level": "green", "label": "ok", "detail": "ok"})
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: {"ok": True, "level": "green", "label": "ok", "detail": "ok"})
    monkeypatch.setattr(dashboard, "_startup_guard_status", lambda selection_sync: {"level": "green", "label": "ok", "detail": "ok"})
    monkeypatch.setattr(dashboard, "_selected_stock_positions_count", lambda live_account, selected_tickers: 0)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"broker_unresolved_count": 0, "broker_unresolved_oldest_seconds": 0.0, "latest_submitted_line": "", "latest_line": "", "execution_mode": "paper", "new_entries_allowed": True, "reduce_only": False, "unresolved_alert_threshold_seconds": 120, "unresolved_alert": False})
    monkeypatch.setattr(dashboard, "load_trade_records", lambda *args, **kwargs: list(trades))
    monkeypatch.setattr(dashboard, "_chart_trade_day", lambda: trade_day)
    monkeypatch.setattr(dashboard, "_desired_audit_mode", lambda: "paper")
    monkeypatch.setattr(dashboard, "_enrich_ticker_descriptions", lambda top3_list: top3_list)
    monkeypatch.setattr(
        dashboard,
        "_load_config_defaults",
        lambda config_name: {
            "TOP1.yaml": {"ticker": "SOXS", "initial_capital": 1000.0, "support": 4.0, "resistance": 5.5, "reduce_only": False},
            "TOP2.yaml": {"ticker": "SOFI", "initial_capital": 1000.0, "support": 8.0, "resistance": 9.5, "reduce_only": False},
            "TOP3.yaml": {"ticker": "DRIP", "initial_capital": 1000.0, "support": 4.0, "resistance": 5.0, "reduce_only": False},
        }.get(config_name, {"ticker": config_name.replace(".yaml", ""), "initial_capital": 0.0, "support": 0.0, "resistance": 0.0, "reduce_only": False}),
    )
    dashboard._CHART_PRICE_HISTORY.clear()
    if project_dir is not None:
        monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
        monkeypatch.setattr(dashboard, "STATE_DIR", project_dir / "state")
        monkeypatch.setattr(dashboard, "TRADING_FLAGS_PATH", project_dir / "state" / "trading_flags.json")
        monkeypatch.setattr(dashboard, "RUNTIME_DIR", project_dir / "runtime")
        monkeypatch.setattr(dashboard, "COMBINED_PID_FILE", project_dir / "runtime" / "combined.pid")


def test_chart_api_returns_prices_and_only_filled_trades(monkeypatch):
    _patch_chart_basics(
        monkeypatch,
        {
            8081: {"price": 8.42, "timestamp": "2026-07-09T21:35:00+08:00"},
        },
        [
            _filled_order_record("1", "SOFI", "Buy", 8.42, 20, "2026-07-09T21:36:10+08:00"),
            _filled_order_record("2", "SOFI", "Sell", 8.58, 20, "2026-07-09T21:37:10+08:00"),
            {
                "timestamp": "2026-07-09T21:38:10+08:00",
                "action": "get_order",
                "request": {"order_id": "3"},
                "response": {
                    "order": 'OrderDetail { order_id: "3", status: Rejected, quantity: 20, executed_quantity: 0, executed_price: None, submitted_at: "2026-07-09T21:38:10+08:00", side: Buy, symbol: "SOFI.US" }',
                    "mapped_status": "REJECTED",
                },
            },
            _filled_order_record("4", "AAPL", "Buy", 309.4, 1, "2026-07-09T21:39:10+08:00"),
        ],
    )

    client = dashboard.app.test_client()
    response = client.get("/api/chart/SOFI")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ticker"] == "SOFI"
    assert payload["prices"]
    assert payload["prices"][-1]["price"] == 8.42
    assert len(payload["trades"]) == 2
    assert all(trade["status"] == "FILLED" for trade in payload["trades"])
    assert all(trade["ticker"] == "SOFI" for trade in payload["trades"])
    assert {trade["side"] for trade in payload["trades"]} == {"BUY", "SELL"}


def test_chart_api_unknown_ticker_is_safe(monkeypatch):
    _patch_chart_basics(monkeypatch, {}, [])

    client = dashboard.app.test_client()
    response = client.get("/api/chart/UNKNOWN")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ticker"] == "UNKNOWN"
    assert payload["prices"] == []
    assert payload["trades"] == []


def test_chart_api_ignores_previous_day_trades_when_today_log_is_empty(monkeypatch):
    def load_trade_records_for_day(*args, **kwargs):
        day = kwargs.get("day")
        if day == "20260710":
            return []
        if day == "20260709":
            return [
                _filled_order_record("1", "SOFI", "Buy", 8.42, 20, "2026-07-09T21:36:10+08:00"),
                _filled_order_record("2", "SOFI", "Sell", 8.58, 20, "2026-07-09T21:37:10+08:00"),
            ]
        return []

    _patch_chart_basics(
        monkeypatch,
        {
            8081: {"price": 8.42, "timestamp": "2026-07-10T21:35:00+08:00"},
        },
        [],
        trade_day="20260710",
    )
    monkeypatch.setattr(dashboard, "load_trade_records", load_trade_records_for_day)

    client = dashboard.app.test_client()
    response = client.get("/api/chart/SOFI")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ticker"] == "SOFI"
    assert payload["prices"]
    assert payload["trades"] == []


def test_index_renders_when_top3_config_is_missing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        for folder in ("configs", "reports", "logs", "state", "runtime"):
            (project_dir / folder).mkdir()
        (project_dir / "configs" / "TOP1.yaml").write_text("ticker: SOXS\nmode: paper\n", encoding="utf-8")
        (project_dir / "configs" / "TOP2.yaml").write_text("ticker: SOFI\nmode: paper\n", encoding="utf-8")

        _patch_chart_basics(
            monkeypatch,
            {
                8080: {"price": 4.84, "timestamp": "2026-07-09T21:35:00+08:00"},
                8081: {"price": 8.42, "timestamp": "2026-07-09T21:35:10+08:00"},
                8082: None,
            },
            [],
            project_dir=project_dir,
        )

        client = dashboard.app.test_client()
        response = client.get("/")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "轻量图" in body
        assert "暂无图表数据" in body
