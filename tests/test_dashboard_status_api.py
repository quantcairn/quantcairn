from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from src.candidate_validation import CandidateRecord, CandidateValidationStore

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


def _write_shadow_artifacts(root: Path, *, symbol="SOXS.US", benchmark_symbols=None, timeframe="15m", strategy_family="range_etf", strategy_version="baseline", symbol_class="inverse_etf", safety_overrides=None, runtime_overrides=None, summary_overrides=None, blocked_rows=None, equity_rows=None, signals_rows=None, orders_rows=None, trades_rows=None, positions_rows=None, daily_rows=None):
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    et = now.astimezone(timezone.utc).replace(tzinfo=timezone.utc).isoformat()
    benchmark_symbols = list(benchmark_symbols or (["SOXX.US", "SMH.US"] if symbol == "SOXS.US" else ["QQQ.US", "SPY.US"]))
    safety = {
        "ok": True,
        "mode": "shadow",
        "quote_api_only": True,
        "trade_api_used": False,
        "trade_context_initialized": False,
        "symbol": symbol,
        "benchmark_symbols": benchmark_symbols,
    }
    runtime = {
        "schema_version": 1,
        "state_version": 1,
        "processed_bars": ["abc"],
        "last_processed_timestamp_utc": now.isoformat(),
        "last_processed_key": "abc",
        "processed_bar_count": 1,
        "last_run_at": now.isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_family": strategy_family,
        "strategy_version": strategy_version,
        "symbol_class": symbol_class,
        "regular_session_only": True,
        "shadow_enabled": True,
        "trading_enabled": False,
        "benchmark_symbols": benchmark_symbols,
        "shadow_title": f"{symbol.split('.')[0]} Shadow Observer",
    }
    summary = {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_family": strategy_family,
        "strategy_version": strategy_version,
        "symbol_class": symbol_class,
        "regular_session_only": True,
        "shadow_enabled": True,
        "trading_enabled": False,
        "benchmark_symbols": benchmark_symbols,
        "shadow_title": f"{symbol.split('.')[0]} Shadow Observer",
        "output_dir": root.name,
        "benchmark_alignment": {"status": "VALID"},
        "benchmark_sensitive": False,
        "eligible_ranking": [{"version": "version_c_soxx"}],
        "strategy_ranking": [{"version": "version_c_soxx"}],
        "strategy_metrics": [
            {
                "version": "version_c_soxx",
                "benchmark_symbol": "SOXX.US",
                "open_position_count": 0,
                "total_return": 0.0123,
                "max_drawdown": 0.0045,
            }
        ],
    }
    if safety_overrides:
        safety.update(safety_overrides)
    if runtime_overrides:
        runtime.update(runtime_overrides)
    if summary_overrides:
        summary.update(summary_overrides)
    (root / "safety_audit.json").write_text(json.dumps(safety), encoding="utf-8")
    (root / "runtime_state.json").write_text(json.dumps(runtime), encoding="utf-8")
    (root / "comparison_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (root / "blocked_reason_counts.csv").write_text("reason,count\ntrend_guard,2\ncost_filter,1\n", encoding="utf-8")
    (root / "shadow_equity.csv").write_text(
        "timestamp_utc,timestamp_et,equity,cash,version\n"
        f"{now.isoformat()},{now.astimezone(timezone.utc).isoformat()},10012.5,9980.0,version_c_soxx\n",
        encoding="utf-8",
    )
    (root / "shadow_signals.csv").write_text(f"timestamp_utc,version,signal\n{now.isoformat()},version_c_soxx,HOLD\n", encoding="utf-8")
    (root / "shadow_simulated_orders.csv").write_text(f"timestamp_utc,version\n{now.isoformat()},version_c_soxx\n", encoding="utf-8")
    (root / "shadow_simulated_trades.csv").write_text(f"timestamp_utc,version\n{now.isoformat()},version_c_soxx\n", encoding="utf-8")
    (root / "shadow_positions.csv").write_text(f"timestamp_utc,version,quantity\n{now.isoformat()},version_c_soxx,0\n", encoding="utf-8")
    (root / "daily_summary.csv").write_text("date,version,bars_received\n2026-07-10,version_c_soxx,26\n", encoding="utf-8")
    if blocked_rows is not None:
        rows = ["reason,count"] + [f"{item['reason']},{item['count']}" for item in blocked_rows]
        (root / "blocked_reason_counts.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    if equity_rows is not None:
        lines = ["timestamp_utc,timestamp_et,equity,cash,version"]
        for row in equity_rows:
            lines.append(
                f"{row['timestamp_utc']},{row['timestamp_et']},{row['equity']},{row['cash']},{row.get('version','version_c_soxx')}"
            )
        (root / "shadow_equity.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if signals_rows is not None:
        lines = ["timestamp_utc,version,signal"]
        for row in signals_rows:
            lines.append(f"{row['timestamp_utc']},{row.get('version','version_c_soxx')},{row.get('signal','HOLD')}")
        (root / "shadow_signals.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if orders_rows is not None:
        lines = ["timestamp_utc,version"]
        for row in orders_rows:
            lines.append(f"{row['timestamp_utc']},{row.get('version','version_c_soxx')}")
        (root / "shadow_simulated_orders.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if trades_rows is not None:
        lines = ["timestamp_utc,version"]
        for row in trades_rows:
            lines.append(f"{row['timestamp_utc']},{row.get('version','version_c_soxx')}")
        (root / "shadow_simulated_trades.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if positions_rows is not None:
        lines = ["timestamp_utc,version,quantity"]
        for row in positions_rows:
            lines.append(f"{row['timestamp_utc']},{row.get('version','version_c_soxx')},{row.get('quantity',0)}")
        (root / "shadow_positions.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if daily_rows is not None:
        lines = ["date,version,bars_received"]
        for row in daily_rows:
            lines.append(f"{row['date']},{row.get('version','version_c_soxx')},{row.get('bars_received',26)}")
        (root / "daily_summary.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_candidate_artifacts(root: Path, *, symbol="SOXS.US", asset_type="inverse_etf", benchmarks=None, strategy_family="range_etf", risk_profile="strict", timeframe="15m", ai_score=91.5, ai_reason="Strong range fit", candidate_score=94.2, liquidity_score=96.0, trend_score=90.0, volatility_score=82.0, risk_score=88.0, strategy_fit_score=97.0, recommended_strategy="trend_following", score_reason="liquidity:strong; trend:strong; volatility:fit; risk:clean; strategy_fit:match"):
    store = CandidateValidationStore(root)
    record = CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=ai_score,
        candidate_score=candidate_score,
        liquidity_score=liquidity_score,
        trend_score=trend_score,
        volatility_score=volatility_score,
        risk_score=risk_score,
        strategy_fit_score=strategy_fit_score,
        recommended_strategy=recommended_strategy,
        score_reason=score_reason,
        ai_reason=ai_reason,
        asset_type=asset_type,
        benchmarks=tuple(benchmarks or (["SOXX.US", "SMH.US"] if symbol == "SOXS.US" else ["QQQ.US", "SPY.US"])),
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe=timeframe,
        market=symbol.split(".")[-1] if "." in symbol else "US",
        metadata={"source": "dashboard_test"},
    )
    store.save_candidates([record])
    return store




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
    assert payload["ai_selection"]["price_band"]["min"] == 5.0
    assert payload["ai_selection"]["price_band"]["max"] == 300.0
    assert payload["candidate_validation"]["state"] == "STALE"
    assert payload["candidate_validation"]["status_label"] == "STALE"
    assert payload["candidate_validation_api_available"] is True
    assert payload["system"]["mode"] == "PAPER"
    assert payload["system"]["broker_type"] == "PaperBroker"
    assert payload["system"]["broker_connection"] == "local memory"
    assert payload["system"]["data_source"] == "PaperBroker / TOP engine runtime"
    assert payload["system"]["market_open_label"] in {"开盘中", "已收盘"}
    assert payload["system"]["global_reduce_only"] is False
    assert payload["system"]["live_order_enabled"] is False
    assert payload["system"]["lifecycle"]["weekend_paper"]["status_label"] == "unavailable"
    assert payload["system"]["lifecycle"]["longbridge_sandbox"]["status_label"] == "unavailable"
    assert payload["shadow"]["title"] == "SOXS Shadow Observer"
    assert payload["shadow"]["symbol"] == "SOXS.US"
    assert payload["shadow"]["timeframe"] == "15m"


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


def test_api_status_does_not_poll_disabled_top_slots(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    configs_dir = project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "TOP1.yaml").write_text("ticker: SOFI\nmode: paper\n", encoding="utf-8")

    calls: list[int] = []

    def _fake_fetch_status(port: int):
        calls.append(port)
        return {"mode": "paper", "price": 18.52, "last_signal": "HOLD", "halted": False} if port == 8091 else None

    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(dashboard, "_fetch_status", _fake_fetch_status)
    monkeypatch.setattr(
        dashboard,
        "_selection_sync_status",
        lambda: {
            "ok": True,
            "state_date": "2026-07-13",
            "required_date": "2026-07-13",
            "selection_state_symbols": ["SOFI"],
            "current_top_config_symbols": ["SOFI"],
            "mismatch_reason": "",
        },
    )
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: {"fallback_used": False, "top3": [{"ticker": "SOFI"}], "settings": {}})
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: 1)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "execution_count": 0, "buy_count": 0, "sell_count": 0, "decision_count": 0})
    monkeypatch.setattr(dashboard, "load_runtime_config", lambda: SimpleNamespace(allow_fallback_live_entries=False, allow_fallback_paper_entries=False))
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(dashboard, "_load_active_orders_summary", lambda tickers: {"available": False, "count": 0, "orders": [], "sources": [], "status_label": "no data", "detail": "no data"})
    monkeypatch.setattr(
        dashboard,
        "_load_dashboard_config",
        lambda: SimpleNamespace(
            mode="paper",
            broker=SimpleNamespace(longbridge=SimpleNamespace(enabled=False, environment="prod", allow_live_order=False)),
        ),
    )

    response = dashboard.app.test_client().get("/api/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert calls == [8091]
    assert payload["top_engines"][0]["configured"] is True
    assert payload["top_engines"][1]["configured"] is False
    assert payload["top_engines"][2]["configured"] is False
    assert payload["top_modes"] == ["paper"]


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
    assert payload["mode_consistency"]["dashboard_mode"] == "sandbox"
    assert payload["mode_consistency"]["top_modes"] == ["sandbox", "sandbox", "sandbox"]
    assert payload["mode_consistency"]["mixed"] is False
    assert payload["system"]["lifecycle"]["weekend_paper"]["status_label"] == "unavailable"
    assert payload["system"]["lifecycle"]["longbridge_sandbox"]["status_label"] == "unavailable"


def test_candidate_validation_status_api_returns_read_only_snapshot(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    candidates_root = project_dir / "artifacts" / "candidates"
    candidates_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
    _write_candidate_artifacts(candidates_root, symbol="AAPL.US", asset_type="common_stock", benchmarks=["QQQ.US", "SPY.US"], strategy_family="equity_mean_reversion", risk_profile="balanced", timeframe="15m", ai_score=88.2, ai_reason="price near support", candidate_score=91.4, liquidity_score=93.0, trend_score=87.5, volatility_score=76.0, risk_score=84.0, strategy_fit_score=95.0, recommended_strategy="mean_reversion", score_reason="liquidity:strong; trend:range; volatility:fit; risk:clean; strategy_fit:match")
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: (_ for _ in ()).throw(AssertionError("broker should not be called")))
    monkeypatch.setattr(dashboard, "_load_dashboard_config", lambda: SimpleNamespace(mode="paper", broker=SimpleNamespace(longbridge=SimpleNamespace(enabled=False, environment="prod", account_type="", allow_live_order=False))))
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "reduce_only": False, "new_entries_allowed": True, "risk_pause_reason": "", "decision_count": 0, "execution_count": 0, "buy_count": 0, "sell_count": 0, "order_qty": 0, "tickers": [], "latest_line": ""})
    monkeypatch.setattr(dashboard, "_load_config_defaults", lambda name: {"ticker": name.replace(".yaml", ""), "initial_capital": 700.0, "support": 10.0, "resistance": 12.0})
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: None)
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: {"ok": True, "level": "green", "label": "已对齐", "detail": "当天配置已对齐（美东 2026-07-09）", "required_date": "2026-07-09", "state_date": "2026-07-09", "selection_state_symbols": ["AAPL"], "current_top_config_symbols": ["AAPL"], "state_top_config_symbols": ["AAPL"]})
    monkeypatch.setattr(dashboard, "has_live_top_configs", lambda: False)

    client = dashboard.app.test_client()
    response = client.get("/api/candidates/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["state"] == "STALE"
    assert payload["status_label"] == "STALE"
    assert payload["candidate_count"] == 1
    assert payload["latest_candidate"]["symbol"] == "AAPL.US"
    assert payload["latest_candidate"]["validation_status"] == "AI_CANDIDATE"
    assert payload["latest_candidate"]["trading_enabled"] is False
    assert payload["latest_candidate"]["shadow_enabled"] is False
    assert payload["latest_candidate"]["paper_enabled"] is False
    assert payload["latest_candidate"]["live_enabled"] is False
    assert payload["latest_candidate"]["deployment_status"] == "INELIGIBLE"
    assert payload["latest_candidate"]["candidate_score"] == 91.4
    assert payload["latest_candidate"]["liquidity_score"] == 93.0
    assert payload["latest_candidate"]["trend_score"] == 87.5
    assert payload["latest_candidate"]["volatility_score"] == 76.0
    assert payload["latest_candidate"]["risk_score"] == 84.0
    assert payload["latest_candidate"]["strategy_fit_score"] == 95.0
    assert payload["latest_candidate"]["recommended_strategy"] == "mean_reversion"
    assert payload["latest_candidate"]["score_reason"] == "liquidity:strong; trend:range; volatility:fit; risk:clean; strategy_fit:match"
    assert payload["performance"]["title"] == "Candidate Ranking Performance"
    assert payload["performance"]["candidate_count"] == 1
    assert payload["performance"]["average_score"] == 91.4
    assert payload["performance"]["high_score_threshold"] == 80.0
    assert payload["performance"]["high_score_candidate_count"] == 1
    assert payload["performance"]["high_score_success_rate"] == 0.0
    assert len(payload["performance"]["score_bucket_distribution"]) == 4
    assert payload["performance"]["score_bucket_distribution"][0]["score_bucket"] == "90-100"
    assert payload["research_report"]["title"] == "AI Candidate Daily Research Report"
    assert payload["research_report"]["candidate_count"] == 1
    assert "APP_KEY" not in response.get_data(as_text=True)

    api_status = client.get("/api/status").get_json()
    assert api_status["candidate_validation"]["state"] == "STALE"
    assert api_status["candidate_validation"]["performance"]["candidate_count"] == 1
    assert api_status["research_report"]["title"] == "AI Candidate Daily Research Report"
    assert api_status["research_status"]["status_label"] == "unavailable"

    research_status_response = client.get("/api/research/status")
    assert research_status_response.status_code == 200
    research_status_payload = research_status_response.get_json()
    assert research_status_payload["status_label"] == "unavailable"

    html = client.get("/").data.decode("utf-8")
    assert "AI Candidate Validation" in html
    assert "AAPL.US" in html
    assert "Candidate Score" in html
    assert "Liquidity Score" in html
    assert "Candidate Ranking Performance" in html
    assert "AI Research Report" in html
    assert "AI Research Scheduler" in html
    assert "Last Research Run" in html
    assert "Recommended Strategy" in html
    assert "AI Ranking Reason" in html
    assert "启动 Shadow" not in html
    assert "批准 Paper" not in html
    assert "批准实盘" not in html
    assert "提交订单" not in html

    performance_response = client.get("/api/candidates/performance")
    assert performance_response.status_code == 200
    performance_payload = performance_response.get_json()
    assert performance_payload["title"] == "Candidate Ranking Performance"
    assert performance_payload["candidate_count"] == 1
    assert performance_payload["average_score"] == 91.4

    research_response = client.get("/api/research/report")
    assert research_response.status_code == 200
    research_payload = research_response.get_json()
    assert research_payload["title"] == "AI Candidate Daily Research Report"
    assert research_payload["candidate_count"] == 1
    assert research_payload["top_candidates"][0]["symbol"] == "AAPL.US"


def test_shadow_status_api_returns_safe_snapshot(monkeypatch, tmp_path):
    shadow_root = tmp_path / "shadow"
    _write_shadow_artifacts(shadow_root)
    monkeypatch.setattr(dashboard, "SHADOW_OBSERVER_DIR", shadow_root)

    client = dashboard.app.test_client()
    response = client.get("/api/shadow/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["state"] == "SAFE"
    assert payload["status_label"] == "SAFE"
    assert payload["quote_api_only"] is True
    assert payload["trade_api_used"] is False
    assert payload["trade_context_initialized"] is False
    assert payload["benchmark_status"] == "VALID"
    assert payload["alignment_status"] == "VALID"
    assert payload["signals_generated"] == 1
    assert payload["simulated_orders"] == 1
    assert payload["simulated_trades"] == 1
    assert payload["open_simulated_positions"] == 0
    assert payload["blocked_reason_top5"][0]["reason"] == "trend_guard"

    summary = client.get("/api/shadow/summary")
    assert summary.status_code == 200
    summary_payload = summary.get_json()
    assert summary_payload["state"] == "SAFE"
    assert summary_payload["daily_summary_rows"] == 1

    blocked = client.get("/api/shadow/blocked-reasons")
    assert blocked.status_code == 200
    blocked_payload = blocked.get_json()
    assert blocked_payload["items"][0]["reason"] == "trend_guard"

    equity = client.get("/api/shadow/equity")
    assert equity.status_code == 200
    equity_payload = equity.get_json()
    assert equity_payload["latest"]["equity"] == 10012.5
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_shadow_status_api_marks_unsafe_on_trade_api_or_context(monkeypatch, tmp_path):
    shadow_root = tmp_path / "shadow"
    _write_shadow_artifacts(
        shadow_root,
        safety_overrides={"trade_api_used": True, "trade_context_initialized": True},
    )
    monkeypatch.setattr(dashboard, "SHADOW_OBSERVER_DIR", shadow_root)

    client = dashboard.app.test_client()
    response = client.get("/api/shadow/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["state"] == "UNSAFE"
    assert payload["status_label"] == "UNSAFE"
    assert payload["detail"] == "安全审计失败"


def test_shadow_status_api_handles_missing_files_and_invalid_json(monkeypatch, tmp_path):
    shadow_root = tmp_path / "shadow_missing"
    shadow_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dashboard, "SHADOW_OBSERVER_DIR", shadow_root)

    client = dashboard.app.test_client()
    missing_response = client.get("/api/shadow/status")
    assert missing_response.status_code == 200
    missing_payload = missing_response.get_json()
    assert missing_payload["state"] == "STALE"
    assert missing_payload["status_label"] == "STALE"
    assert missing_payload["detail"] == "shadow data unavailable"

    (shadow_root / "safety_audit.json").write_text("{invalid", encoding="utf-8")
    invalid_response = client.get("/api/shadow/status")
    assert invalid_response.status_code == 200
    invalid_payload = invalid_response.get_json()
    assert invalid_payload["state"] == "UNSAFE"
    assert invalid_payload["detail"] == "data_invalid"


def test_shadow_status_api_is_get_only_and_page_stays_read_only(monkeypatch, tmp_path):
    shadow_root = tmp_path / "shadow"
    _write_shadow_artifacts(shadow_root)
    monkeypatch.setattr(dashboard, "SHADOW_OBSERVER_DIR", shadow_root)
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: (_ for _ in ()).throw(AssertionError("broker should not be called")))
    monkeypatch.setattr(dashboard, "_load_dashboard_config", lambda: SimpleNamespace(mode="paper", broker=SimpleNamespace(longbridge=SimpleNamespace(enabled=False, environment="prod", account_type="", allow_live_order=False))))
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "reduce_only": False, "new_entries_allowed": True, "risk_pause_reason": "", "decision_count": 0, "execution_count": 0, "buy_count": 0, "sell_count": 0, "order_qty": 0, "tickers": [], "latest_line": ""})
    monkeypatch.setattr(dashboard, "_load_config_defaults", lambda name: {"ticker": name.replace(".yaml", ""), "initial_capital": 700.0, "support": 10.0, "resistance": 12.0})
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: None)
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: {"ok": True, "level": "green", "label": "已对齐", "detail": "当天配置已对齐（美东 2026-07-09）", "required_date": "2026-07-09", "state_date": "2026-07-09", "selection_state_symbols": ["SOXS", "SOXS", "SOXS"], "current_top_config_symbols": ["SOXS", "SOXS", "SOXS"], "state_top_config_symbols": ["SOXS", "SOXS", "SOXS"]})
    monkeypatch.setattr(dashboard, "has_live_top_configs", lambda: False)

    methods = None
    for rule in dashboard.app.url_map.iter_rules():
        if rule.rule == "/api/shadow/status":
            methods = rule.methods
            break
    assert methods is not None
    assert "POST" not in methods

    client = dashboard.app.test_client()
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert "shadow" in payload
    html = client.get("/").data.decode("utf-8")
    assert "SOXS Shadow Observer" in html
    assert "READ-ONLY SHADOW" in html
    assert "AI Candidate Validation" in html
    assert "提交订单" not in html
    assert "一键实盘" not in html
    assert "切换 Prod" not in html
    assert "切换 Paper" not in html
    assert "reset runtime_state" not in html.lower()
    assert "批准 Paper" not in html
    assert "批准实盘" not in html


def test_shadow_status_api_uses_dynamic_title_for_custom_symbol(monkeypatch, tmp_path):
    shadow_root = tmp_path / "shadow_aapl"
    _write_shadow_artifacts(
        shadow_root,
        symbol="AAPL.US",
        benchmark_symbols=["QQQ.US", "SPY.US"],
        timeframe="15m",
        strategy_family="equity_mean_reversion",
        strategy_version="baseline",
        symbol_class="common_stock",
    )
    monkeypatch.setattr(dashboard, "SHADOW_OBSERVER_DIR", shadow_root)
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: (_ for _ in ()).throw(AssertionError("broker should not be called")))
    monkeypatch.setattr(dashboard, "_load_dashboard_config", lambda: SimpleNamespace(mode="paper", broker=SimpleNamespace(longbridge=SimpleNamespace(enabled=False, environment="prod", account_type="", allow_live_order=False))))
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "reduce_only": False, "new_entries_allowed": True, "risk_pause_reason": "", "decision_count": 0, "execution_count": 0, "buy_count": 0, "sell_count": 0, "order_qty": 0, "tickers": [], "latest_line": ""})
    monkeypatch.setattr(dashboard, "_load_config_defaults", lambda name: {"ticker": name.replace(".yaml", ""), "initial_capital": 700.0, "support": 10.0, "resistance": 12.0})
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: None)
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: {"ok": True, "level": "green", "label": "已对齐", "detail": "当天配置已对齐（美东 2026-07-09）", "required_date": "2026-07-09", "state_date": "2026-07-09", "selection_state_symbols": ["AAPL"], "current_top_config_symbols": ["AAPL"], "state_top_config_symbols": ["AAPL"]})
    monkeypatch.setattr(dashboard, "has_live_top_configs", lambda: False)

    client = dashboard.app.test_client()
    response = client.get("/api/status")
    payload = response.get_json()
    assert payload["shadow"]["title"] == "AAPL Shadow Observer"
    assert payload["shadow"]["benchmark_symbols"] == ["QQQ.US", "SPY.US"]
    html = client.get("/").data.decode("utf-8")
    assert "AAPL Shadow Observer" in html
    assert "SOXS Shadow Observer" not in html
