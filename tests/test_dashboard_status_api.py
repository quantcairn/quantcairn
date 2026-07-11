from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

VENV_SITE_PACKAGES = next(
    (path for path in (Path(__file__).resolve().parents[1] / ".venv" / "lib").glob("python*/site-packages") if path.exists()),
    None,
)
if VENV_SITE_PACKAGES is not None and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from src.dashboard import combined as dashboard


def _patch_status_basics(
    monkeypatch,
    *,
    status_map,
    selection_sync,
    ai_selection=None,
    top_modes=None,
    process_count=1,
    live_account=None,
    active_orders=None,
    dashboard_config=None,
):
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: status_map.get(port))
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: selection_sync)
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: ai_selection or {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}})
    monkeypatch.setattr(dashboard, "_load_top_modes", lambda: list(top_modes or ["paper", "paper", "paper"]))
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: process_count)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "execution_count": 0, "buy_count": 0, "sell_count": 0, "decision_count": 0})
    monkeypatch.setattr(dashboard, "load_runtime_config", lambda: SimpleNamespace(allow_fallback_live_entries=False, allow_fallback_paper_entries=True))
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: live_account)
    monkeypatch.setattr(dashboard, "_load_active_orders_summary", lambda tickers: active_orders or {"available": False, "count": 0, "orders": [], "sources": [], "status_label": "no data", "detail": "no data"})
    monkeypatch.setattr(
        dashboard,
        "_load_dashboard_config",
        lambda: dashboard_config
        or SimpleNamespace(
            mode="paper",
            broker=SimpleNamespace(
                longbridge=SimpleNamespace(
                    enabled=False,
                    environment="prod",
                    allow_live_order=False,
                )
            ),
        ),
    )


def test_api_status_returns_json_with_core_fields(monkeypatch):
    _patch_status_basics(
        monkeypatch,
        status_map={
            8091: {"mode": "paper", "price": 18.52, "last_signal": "HOLD", "halted": False},
            8092: {"mode": "paper", "price": 8.42, "last_signal": "BUY", "halted": False},
            8093: {"mode": "paper", "price": 4.12, "last_signal": "SELL", "halted": False},
        },
        selection_sync={
            "ok": True,
            "state_date": "2026-07-09",
            "required_date": "2026-07-09",
            "selection_state_symbols": ["SOFI", "LABD", "F"],
            "current_top_config_symbols": ["SOFI", "LABD", "F"],
            "mismatch_reason": "",
        },
        ai_selection={
            "fallback_used": True,
            "top3": [{"ticker": "SOFI", "fallback_used": False}],
            "settings": {},
        },
    )

    client = dashboard.app.test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["mode"] == "paper"
    assert payload["combined"]["port"] == 8090
    assert payload["combined"]["process_count"] == 1
    assert payload["selection"]["synced"] is True
    assert payload["selection"]["selection_date"] == "2026-07-09"
    assert payload["selection"]["selection_state_tickers"] == ["SOFI", "LABD", "F"]
    assert payload["selection"]["top_config_tickers"] == ["SOFI", "LABD", "F"]
    assert payload["selection"]["fallback_used"] is True
    assert payload["risk"]["fallback_live_allowed"] is False
    assert payload["risk"]["fallback_paper_allowed"] is True
    assert payload["dashboard"]["chart_api_available"] is True
    assert len(payload["top_engines"]) == 3
    assert payload["ai_selection"]["price_band"]["min"] == 4.0
    assert payload["ai_selection"]["price_band"]["max"] == 50.0
    assert payload["system"]["mode"] == "PAPER"
    assert payload["system"]["broker_type"] == "PaperBroker"
    assert payload["system"]["broker_connection"] == "local memory"
    assert payload["system"]["data_source"] == "PaperBroker / TOP engine runtime"
    assert payload["system"]["market_open_label"] in {"开盘中", "已收盘"}
    assert payload["system"]["global_reduce_only"] is False
    assert payload["system"]["live_order_enabled"] is False
    assert payload["system"]["lifecycle"]["weekend_paper"]["status_label"] == "unavailable"
    assert payload["system"]["lifecycle"]["longbridge_sandbox"]["status_label"] == "unavailable"


def test_api_status_handles_offline_top_engine(monkeypatch):
    _patch_status_basics(
        monkeypatch,
        status_map={
            8091: {"mode": "paper", "price": 18.52, "last_signal": "HOLD", "halted": False},
            8092: None,
            8093: {"mode": "paper", "price": 4.12, "last_signal": "SELL", "halted": False},
        },
        selection_sync={
            "ok": True,
            "state_date": "2026-07-09",
            "required_date": "2026-07-09",
            "selection_state_symbols": ["SOFI", "LABD", "F"],
            "current_top_config_symbols": ["SOFI", "LABD", "F"],
            "mismatch_reason": "",
        },
        ai_selection={"fallback_used": False, "top3": [], "settings": {"min_price": 4.0, "max_price": 50.0}},
    )

    client = dashboard.app.test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["top_engines"][1]["online"] is False
    assert payload["top_engines"][1]["signal"] == "OFFLINE"
    assert payload["top_engines"][1]["price"] is None


def test_api_status_reports_selection_mismatch_without_error(monkeypatch):
    _patch_status_basics(
        monkeypatch,
        status_map={8091: None, 8092: None, 8093: None},
        selection_sync={
            "ok": False,
            "state_date": "2026-07-09",
            "required_date": "2026-07-09",
            "selection_state_symbols": ["AAPL", "SOFI", "DRIP"],
            "current_top_config_symbols": ["SOXS", "YINN", "LABD"],
            "mismatch_reason": "top_config_symbols_do_not_match_selection_state",
        },
        ai_selection={"fallback_used": False, "top3": [], "settings": {}},
    )

    client = dashboard.app.test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["selection"]["synced"] is False
    assert payload["selection"]["reason"] == "top_config_symbols_do_not_match_selection_state"
    assert payload["selection"]["selection_state_tickers"] == ["AAPL", "SOFI", "DRIP"]
    assert payload["selection"]["top_config_tickers"] == ["SOXS", "YINN", "LABD"]
    assert payload["combined"]["process_count"] == 1


def test_api_status_prefers_top_config_fallback_flags(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    configs_dir = project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    for name in ["TOP1.yaml", "TOP2.yaml", "TOP3.yaml"]:
        (configs_dir / name).write_text(
            "\n".join(
                [
                    "ticker: SOXS",
                    "mode: paper",
                    "ai_selector:",
                    "  allow_fallback_paper_entries: true",
                    "  allow_fallback_live_entries: false",
                    "  fallback_paper_position_multiplier: 0.25",
                ]
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: {"mode": "paper", "price": 4.12, "last_signal": "HOLD", "halted": False})
    monkeypatch.setattr(
        dashboard,
        "_selection_sync_status",
        lambda: {
            "ok": True,
            "state_date": "2026-07-10",
            "required_date": "2026-07-10",
            "selection_state_symbols": ["SOXS", "SOXS", "SOXS"],
            "current_top_config_symbols": ["SOXS", "SOXS", "SOXS"],
            "mismatch_reason": "",
        },
    )
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: {"fallback_used": False, "top3": [], "settings": {}})
    monkeypatch.setattr(dashboard, "_load_top_modes", lambda: ["paper", "paper", "paper"])
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: 1)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "execution_count": 0, "buy_count": 0, "sell_count": 0, "decision_count": 0})
    monkeypatch.setattr(dashboard, "load_runtime_config", lambda: SimpleNamespace(allow_fallback_live_entries=False, allow_fallback_paper_entries=False))

    client = dashboard.app.test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["risk"]["fallback_paper_allowed"] is True
    assert payload["risk"]["fallback_live_allowed"] is False


def test_api_status_shows_sandbox_snapshot_without_paper_fallback(monkeypatch):
    _patch_status_basics(
        monkeypatch,
        status_map={
            8091: {"mode": "sandbox", "price": 18.52, "last_signal": "HOLD", "halted": False},
            8092: {"mode": "sandbox", "price": 8.42, "last_signal": "BUY", "halted": False},
            8093: {"mode": "sandbox", "price": 4.12, "last_signal": "SELL", "halted": False},
        },
        selection_sync={
            "ok": True,
            "state_date": "2026-07-09",
            "required_date": "2026-07-09",
            "selection_state_symbols": ["SOFI", "LABD", "F"],
            "current_top_config_symbols": ["SOFI", "LABD", "F"],
            "mismatch_reason": "",
        },
        ai_selection={"fallback_used": False, "top3": [], "settings": {"min_price": 4.0, "max_price": 50.0}},
        live_account={
            "cash": 1000.0,
            "equity": 1000.0,
            "buying_power": 1000.0,
            "positions": [
                {
                    "ticker": "SOFI",
                    "quantity": 6,
                    "avg_entry_price": 10.98,
                    "current_price": 11.2,
                    "market_value": 67.2,
                    "unrealized_pnl": 1.32,
                    "unrealized_pnl_pct": 2.0,
                }
            ],
            "positions_count": 1,
            "mode": "sandbox",
        },
        dashboard_config=SimpleNamespace(
            mode="sandbox",
            broker=SimpleNamespace(
                longbridge=SimpleNamespace(
                    enabled=True,
                    environment="sandbox",
                    account_type="paper",
                    allow_live_order=False,
                )
            ),
        ),
    )

    client = dashboard.app.test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["system"]["mode"] == "SANDBOX"
    assert payload["system"]["broker_type"] == "LongBridge"
    assert payload["system"]["broker_connection"] == "connected"
    assert payload["system"]["data_source"] == "LongBridge sandbox snapshot"
    assert payload["system"]["account_source"] == "LongBridge sandbox account"
    assert payload["system"]["live_order_enabled"] is False
    assert payload["system"]["lifecycle"]["weekend_paper"]["status_label"] == "unavailable"
    assert payload["system"]["lifecycle"]["longbridge_sandbox"]["status_label"] == "unavailable"
