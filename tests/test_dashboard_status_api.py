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


def _patch_status_basics(monkeypatch, *, status_map, selection_sync, ai_selection=None, top_modes=None, process_count=1):
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: status_map.get(port))
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: selection_sync)
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: ai_selection or {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}})
    monkeypatch.setattr(dashboard, "_load_top_modes", lambda: list(top_modes or ["paper", "paper", "paper"]))
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: process_count)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "execution_count": 0, "buy_count": 0, "sell_count": 0, "decision_count": 0})
    monkeypatch.setattr(dashboard, "load_runtime_config", lambda: SimpleNamespace(allow_fallback_live_entries=False, allow_fallback_paper_entries=True))


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
