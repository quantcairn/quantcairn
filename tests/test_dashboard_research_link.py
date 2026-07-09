from __future__ import annotations

from types import SimpleNamespace

from src.dashboard import combined


def _fake_status(port: int) -> dict:
    return {
        "running": True,
        "status_line": f"TOP{port} ok",
        "price": 10.0,
        "change": 0.0,
        "high_1m": 10.0,
        "low_1m": 10.0,
        "bid": 10.0,
        "ask": 10.0,
        "volume": 100000,
        "last_signal": "HOLD",
        "last_signal_reason": "ok",
        "daily_pnl": 0.0,
        "initial_capital": 1000.0,
        "equity": 1000.0,
        "trades_today": 0,
        "wins": 0,
        "losses": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_pnl": 0.0,
        "halted": False,
        "trade_in_progress": False,
    }


def test_combined_dashboard_exposes_research_link_context(monkeypatch):
    monkeypatch.setattr(
        combined,
        "TICKERS",
        [{"name": "TOP1", "desc": "AI优选第1名", "port": 8091, "config": "TOP1.yaml"}],
    )
    monkeypatch.setattr(
        combined,
        "_load_config_defaults",
        lambda config: {"ticker": "SOFI", "initial_capital": 1000.0, "support": 1.0, "resistance": 2.0, "reduce_only": False},
    )
    monkeypatch.setattr(combined, "_fetch_status", lambda port: _fake_status(port))
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {"mode": "paper", "positions": []})
    monkeypatch.setattr(combined, "_position_lookup", lambda live_account: {})
    monkeypatch.setattr(
        combined,
        "_load_ai_selection_report",
        lambda: {
            "timestamp": "2026-07-09T21:30:00",
            "top3": [{"ticker": "SOFI"}],
            "settings": {"price_band": {"min": 4.0, "max": 50.0}},
            "protected_positions": [],
        },
    )
    monkeypatch.setattr(combined, "_enrich_ticker_descriptions", lambda rows: None)
    monkeypatch.setattr(combined, "_ai_range_lookup", lambda ai: {"SOFI": {"range_low": 4.0, "range_high": 50.0}})
    monkeypatch.setattr(
        combined,
        "_ai_runtime_status",
        lambda: SimpleNamespace(level="live", label="ok", detail="ready"),
    )
    monkeypatch.setattr(
        combined,
        "_selection_sync_status",
        lambda: SimpleNamespace(
            ok=True,
            level="live",
            label="已对齐",
            detail="ok",
            selection_state_symbols=["SOFI"],
            current_top_config_symbols=["SOFI"],
            mismatch_reason=None,
            suggestion="",
        ),
    )
    monkeypatch.setattr(
        combined,
        "summarize_trade_log",
        lambda *args, **kwargs: {
            "broker_unresolved_count": 0,
            "broker_unresolved_oldest_seconds": 0.0,
            "latest_submitted_line": "",
            "latest_line": "",
            "execution_mode": "paper",
            "new_entries_allowed": True,
            "reduce_only": False,
            "notification_reconcile_ok": True,
            "broker_submitted_count": 0,
            "broker_filled_count": 0,
            "broker_partial_filled_count": 0,
        },
    )
    monkeypatch.setattr(combined, "_startup_guard_status", lambda *args, **kwargs: SimpleNamespace(level="live", label="正常"))
    monkeypatch.setattr(combined, "_dashboard_active_symbols", lambda *args, **kwargs: ["SOFI"])
    monkeypatch.setattr(combined, "_load_order_states", lambda *args, **kwargs: {})
    monkeypatch.setattr(combined, "_nearest_trigger", lambda *args, **kwargs: ("TOP1 · SOFI", ""))
    monkeypatch.setattr(combined, "_chart_snapshot_for_ticker", lambda ticker, refresh=True: {"prices": [], "trades": []})
    monkeypatch.setattr(combined, "render_template_string", lambda template, **kwargs: f"research={kwargs.get('research_url')}")

    with combined.app.test_request_context("/"):
        html = combined.index()
    assert "research=/research" in html


def test_research_route_serves_index(monkeypatch, tmp_path):
    index_path = tmp_path / "index.html"
    index_path.write_text("<html><body>research-home</body></html>", encoding="utf-8")
    monkeypatch.setattr(combined, "build_research_site", lambda **kwargs: index_path)

    client = combined.app.test_client()
    response = client.get("/research")

    assert response.status_code == 200
    assert b"research-home" in response.data
