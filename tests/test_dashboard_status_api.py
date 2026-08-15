from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
import yaml

from src.candidate_validation import CandidateRecord, CandidateValidationStore
from src.config.loader import PositionPolicyConfig
from src.dashboard.snapshots import dashboard_snapshot_path, load_dashboard_snapshot, write_dashboard_snapshot
from src.openalpha import selection_state

VENV_SITE_PACKAGES = next(
    (path for path in (Path(__file__).resolve().parents[1] / ".venv" / "lib").glob("python*/site-packages") if path.exists()),
    None,
)
if VENV_SITE_PACKAGES is not None and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from src.dashboard import combined as dashboard


@pytest.fixture(autouse=True)
def _isolate_dashboard_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "STATE_DIR", tmp_path / "state")


def test_execution_mode_mapping_preserves_paper_sandbox_live():
    assert dashboard._execution_mode_code("paper") == "paper"
    assert dashboard._execution_mode_code("sandbox") == "sandbox"
    assert dashboard._execution_mode_code("live") == "live"
    assert dashboard._mode_label("paper") == "PAPER"
    assert dashboard._mode_label("sandbox") == "SANDBOX"
    assert dashboard._mode_label("live") == "LIVE"
    assert dashboard._broker_environment_label("longbridge_live") == "LongBridge Live"


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


def _write_dashboard_snapshot(root: Path, name: str, data: dict, *, generated_at: str = "2026-08-02T10:00:00+08:00") -> Path:
    path = root / "dashboard_snapshots" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "source_run_id": "snapshot-run",
                "data": data,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_dashboard_snapshot_write_and_load_roundtrip(tmp_path):
    state_dir = tmp_path / "state"

    path = write_dashboard_snapshot(
        "candidate_validation",
        {"state": "SAFE", "marker": "snapshot"},
        source_run_id="run-123",
        generated_at="2026-08-02T10:00:00+08:00",
        state_dir=state_dir,
    )

    assert path == state_dir / "dashboard_snapshots" / "candidate_validation.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["generated_at"] == "2026-08-02T10:00:00+08:00"
    assert envelope["source_run_id"] == "run-123"
    assert envelope["data"] == {"state": "SAFE", "marker": "snapshot"}
    assert load_dashboard_snapshot("candidate_validation", state_dir=state_dir) == {"state": "SAFE", "marker": "snapshot"}


def test_dashboard_snapshot_path_rejects_unsafe_names(tmp_path):
    state_dir = tmp_path / "state"

    assert dashboard_snapshot_path("shadow_status", state_dir=state_dir) == state_dir / "dashboard_snapshots" / "shadow_status.json"
    for unsafe in ("../shadow_status", "nested/shadow_status", "shadow_status.json", ""):
        try:
            dashboard_snapshot_path(unsafe, state_dir=state_dir)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe snapshot name accepted: {unsafe!r}")
        assert load_dashboard_snapshot(unsafe, state_dir=state_dir) is None


def test_write_dashboard_snapshot_rejects_non_mapping(tmp_path):
    try:
        write_dashboard_snapshot("candidate_validation", ["invalid"], state_dir=tmp_path / "state")  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("non-mapping dashboard snapshot data accepted")


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


def test_dashboard_snapshot_helper_returns_data_for_valid_snapshot(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    _write_dashboard_snapshot(state_dir, "candidate_validation", {"state": "SAFE", "marker": "snapshot"})

    assert dashboard._load_dashboard_snapshot("candidate_validation") == {"state": "SAFE", "marker": "snapshot"}


def test_dashboard_snapshot_helper_returns_none_for_missing_and_malformed(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    snapshot_dir = state_dir / "dashboard_snapshots"
    snapshot_dir.mkdir(parents=True)

    assert dashboard._load_dashboard_snapshot("missing") is None

    invalid_cases = {
        "broken_json": "{",
        "top_level_list": "[]",
        "missing_data": json.dumps({"generated_at": "2026-08-02T10:00:00+08:00"}),
        "null_data": json.dumps({"data": None}),
        "list_data": json.dumps({"data": []}),
        "string_data": json.dumps({"data": "invalid"}),
    }
    for name, content in invalid_cases.items():
        (snapshot_dir / f"{name}.json").write_text(content, encoding="utf-8")
        assert dashboard._load_dashboard_snapshot(name) is None


def test_candidate_validation_payload_prefers_snapshot(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    _write_dashboard_snapshot(state_dir, "candidate_validation", {"state": "SAFE", "status_label": "SNAPSHOT"})
    monkeypatch.setattr(
        dashboard,
        "_candidate_validation_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("candidate validation fallback should not run")),
    )

    assert dashboard._candidate_validation_payload()["status_label"] == "SNAPSHOT"


def test_candidate_validation_payload_falls_back_when_snapshot_missing_or_broken(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    monkeypatch.setattr(dashboard, "_candidate_validation_snapshot", lambda: {"state": "FALLBACK"})

    assert dashboard._candidate_validation_payload()["state"] == "FALLBACK"

    broken = state_dir / "dashboard_snapshots" / "candidate_validation.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{", encoding="utf-8")
    assert dashboard._candidate_validation_payload()["state"] == "FALLBACK"


def test_candidate_model_evaluation_payload_prefers_snapshot(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    _write_dashboard_snapshot(state_dir, "candidate_model_evaluation", {"title": "Snapshot Model", "approval_status": "READY"})
    monkeypatch.setattr(
        dashboard,
        "_candidate_model_evaluation_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("candidate model evaluation fallback should not run")),
    )

    assert dashboard._candidate_model_evaluation_payload()["title"] == "Snapshot Model"


def test_candidate_model_evaluation_payload_falls_back_when_snapshot_missing_or_broken(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    monkeypatch.setattr(dashboard, "_candidate_model_evaluation_snapshot", lambda: {"title": "Fallback Model"})

    assert dashboard._candidate_model_evaluation_payload()["title"] == "Fallback Model"

    broken = state_dir / "dashboard_snapshots" / "candidate_model_evaluation.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{", encoding="utf-8")
    assert dashboard._candidate_model_evaluation_payload()["title"] == "Fallback Model"


def _candidate_research_fallback_payload() -> dict[str, object]:
    return {
        "available": True,
        "state": "SAFE",
        "status_label": "SAFE",
        "detail": "daily research report ready",
        "title": "AI Candidate Daily Research Report",
        "display_title": "AI Research Report",
        "generated_at": "2026-08-02T10:00:00+00:00",
        "candidate_count": 1,
        "average_score": 91.5,
        "score_distribution": [{"score_bucket": "90-100", "candidate_count": 1}],
        "top_candidates": [{"symbol": "NVDA.US", "candidate_score": 91.5}],
        "failure_analysis": {"statuses": {"DATA_INVALID": 0}},
        "market_regime": {},
        "strategy_selection": {},
        "candidate_strategy_matrix": [],
        "portfolio_composition": {},
        "final_selected": [],
        "final_selected_count": 0,
        "selection_outcome": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "actionable_candidate_status": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "selection_execution_status": "COMPLETED",
        "selection_result_quality": "COMPLETE",
        "selection_research_admission": "RESEARCH_READY",
        "selection_stage": "FINALIZED",
        "selection_top_n_complete": False,
        "selection_top_n_missing_count": 0,
        "selection_fallback_used": False,
        "selection_provider_audit": {},
        "selection_provider_outputs": {},
        "selection_warnings_structured": [],
        "selection_warnings": [],
        "high_score_success_rate": 0.0,
        "high_score_threshold": 80.0,
        "performance": {"candidate_count": 1},
        "run_id": "fallback-run",
        "source_run_id": "fallback-run",
        "source": "candidate_daily_research_report",
        "snapshot_name": "candidate_research_report",
    }


def test_candidate_research_report_payload_prefers_snapshot(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    snapshot_data = dict(_candidate_research_fallback_payload())
    snapshot_data.update({"run_id": "snapshot-run", "source_run_id": "snapshot-run", "candidate_count": 2})
    _write_dashboard_snapshot(state_dir, "candidate_research_report", snapshot_data)
    monkeypatch.setattr(
        dashboard,
        "_candidate_research_report_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("candidate research fallback should not run")),
    )

    payload = dashboard._candidate_research_report_payload()

    assert payload["run_id"] == "snapshot-run"
    assert payload["source_run_id"] == "snapshot-run"
    assert payload["candidate_count"] == 2


def test_candidate_research_report_payload_falls_back_for_missing_and_invalid_snapshots(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    monkeypatch.setattr(dashboard, "_candidate_research_report_snapshot", _candidate_research_fallback_payload)

    assert dashboard._candidate_research_report_payload()["run_id"] == "fallback-run"

    snapshot_dir = state_dir / "dashboard_snapshots"
    snapshot_dir.mkdir(parents=True)
    invalid_cases = {
        "broken": "{",
        "top_list": "[]",
        "missing_data": json.dumps({"generated_at": "2026-08-02T10:00:00+00:00"}),
        "list_data": json.dumps({"data": []}),
    }
    for content in invalid_cases.values():
        (snapshot_dir / "candidate_research_report.json").write_text(content, encoding="utf-8")
        assert dashboard._candidate_research_report_payload()["run_id"] == "fallback-run"


def test_candidate_research_snapshot_and_fallback_share_key_schema(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    fallback = _candidate_research_fallback_payload()
    _write_dashboard_snapshot(state_dir, "candidate_research_report", dict(fallback))
    monkeypatch.setattr(dashboard, "_candidate_research_report_snapshot", lambda: dict(fallback))

    snapshot_payload = dashboard._candidate_research_report_payload()
    (state_dir / "dashboard_snapshots" / "candidate_research_report.json").unlink()
    fallback_payload = dashboard._candidate_research_report_payload()

    key_subset = {
        "available",
        "state",
        "status_label",
        "title",
        "generated_at",
        "candidate_count",
        "top_candidates",
        "failure_analysis",
        "run_id",
        "source_run_id",
        "selection_outcome",
    }
    assert key_subset <= set(snapshot_payload)
    assert key_subset <= set(fallback_payload)
    for key in key_subset:
        assert type(snapshot_payload[key]) is type(fallback_payload[key])


def _fallback_shadow_snapshot() -> dict[str, object]:
    return {
        "state": "STALE",
        "state_label": "STALE",
        "safety_gate": "STALE",
        "detail": "fallback",
        "mode": "READ-ONLY SHADOW",
        "title": "Shadow",
        "symbol": "SOXS.US",
        "timeframe": "15m",
        "strategy_family": "",
        "strategy_version": "",
        "symbol_class": "",
        "regular_session_only": True,
        "shadow_enabled": True,
        "trading_enabled": False,
        "benchmark_symbols": [],
        "output_directory": "shadow",
        "quote_api_only": False,
        "trade_api_used": None,
        "trade_context_initialized": None,
        "last_run_at": None,
        "latest_processed_bar_utc": None,
        "latest_processed_bar_et": None,
        "data_freshness": "unavailable",
        "benchmark_status": "unavailable",
        "alignment_status": "unavailable",
        "signals_generated": 0,
        "simulated_orders": 0,
        "simulated_trades": 0,
        "open_simulated_positions": 0,
        "simulated_equity": None,
        "simulated_return": None,
        "simulated_drawdown": None,
        "blocked_reason_top5": [],
        "benchmark_sensitive": None,
        "benchmark_symbol": None,
        "available": False,
        "processed_bar_count": 0,
    }


def test_shadow_status_payload_prefers_snapshot(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    _write_dashboard_snapshot(state_dir, "shadow_status", {"state": "SAFE", "status_label": "SNAPSHOT", "detail": "snapshot"})
    monkeypatch.setattr(
        dashboard,
        "_shadow_status_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("shadow fallback should not run")),
    )

    assert dashboard._shadow_status_payload()["status_label"] == "SNAPSHOT"


def test_shadow_status_payload_falls_back_when_snapshot_missing_or_broken(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    monkeypatch.setattr(dashboard, "_shadow_status_snapshot", _fallback_shadow_snapshot)

    assert dashboard._shadow_status_payload()["detail"] == "fallback"

    broken = state_dir / "dashboard_snapshots" / "shadow_status.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{", encoding="utf-8")
    assert dashboard._shadow_status_payload()["detail"] == "fallback"


def test_api_status_uses_independent_dashboard_snapshots(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    (state_dir / "dashboard_snapshots").mkdir(parents=True)
    (state_dir / "dashboard_snapshots" / "candidate_validation.json").write_text("{", encoding="utf-8")
    _write_dashboard_snapshot(state_dir, "candidate_model_evaluation", {"title": "Snapshot Model", "approval_status": "READY"})
    _write_dashboard_snapshot(state_dir, "shadow_status", {"state": "SAFE", "status_label": "SNAPSHOT", "detail": "snapshot"})
    monkeypatch.setattr(dashboard, "_candidate_validation_snapshot", lambda: {"state": "FALLBACK", "status_label": "FALLBACK"})
    monkeypatch.setattr(
        dashboard,
        "_candidate_model_evaluation_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("model fallback should not run")),
    )
    monkeypatch.setattr(
        dashboard,
        "_shadow_status_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("shadow fallback should not run")),
    )
    _patch_status_basics(
        monkeypatch,
        status_map={},
        selection_sync={
            "ok": True,
            "state_date": "2026-07-09",
            "required_date": "2026-07-09",
            "selection_state_symbols": [],
            "current_top_config_symbols": [],
            "mismatch_reason": "",
        },
    )

    payload = dashboard.app.test_client().get("/api/status").get_json()

    assert payload["candidate_validation"]["status_label"] == "FALLBACK"
    assert payload["candidate_model_evaluation"]["title"] == "Snapshot Model"
    assert payload["shadow"]["status_label"] == "SNAPSHOT"
    assert payload["ok"] is True


def _write_minimal_selection_bundle(project_dir: Path, *, run_id: str = "run-1", symbol: str = "AAPL") -> dict[str, Path]:
    bundle_root = project_dir / "state" / "selection_bundles" / run_id / "selection_bundle_v1"
    bundle_root.mkdir(parents=True, exist_ok=True)
    manifest_path = project_dir / "state" / "selection_bundle_manifest.json"
    report_path = bundle_root / "ai_selection_report.json"
    state_path = bundle_root / "ai_selection_state.json"
    audit_path = bundle_root / "selection_sync_audit.json"
    metadata_path = bundle_root / "bundle_metadata.json"
    manifest = {
        "bundle_root": str(bundle_root.relative_to(project_dir)),
        "bundle_version": "selection_bundle_v1",
        "selection_bundle_hash": "hash-1",
    }
    report = {
        "selection_run_id": run_id,
        "selection_date": "2026-08-02",
        "selection_stage": "FINALIZED",
        "execution_status": "COMPLETED",
        "result_quality": "COMPLETE",
        "research_admission": "RESEARCH_READY",
        "top3": [{"ticker": symbol, "score": 91.0}],
        "selected_symbols": [symbol],
        "final_selected_symbols": [symbol],
        "selected_top_n": 1,
        "candidate_layers": {"research_candidates": [], "trade_candidates": [], "watchlist_candidates": []},
    }
    state = {
        "et_date": "2026-08-02",
        "selection_run_id": run_id,
        "top_sync_run_id": run_id,
        "selected_symbols": [symbol],
        "selection_symbols": [symbol],
        "top_config_symbols": [symbol],
        "current_top_config_symbols": [symbol],
        "disabled_slots": [],
    }
    for path, payload in (
        (manifest_path, manifest),
        (report_path, report),
        (state_path, state),
        (audit_path, {"ok": True}),
        (metadata_path, {"bundle_version": "selection_bundle_v1"}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "manifest": manifest_path,
        "report": report_path,
        "state": state_path,
        "audit": audit_path,
        "metadata": metadata_path,
    }


def _write_top_configs(project_dir: Path, *, symbol: str = "AAPL", run_id: str = "run-1") -> list[Path]:
    paths: list[Path] = []
    for index, ticker in enumerate([symbol, "MSFT", "NVDA"], start=1):
        path = project_dir / "configs" / f"TOP{index}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    "enabled: true",
                    f"ticker: {ticker}",
                    "mode: paper",
                    "selection_date: '2026-08-02'",
                    f"selection_run_id: {run_id}",
                    f"top_sync_run_id: {run_id}",
                    "result_quality: COMPLETE",
                    "research_admission: RESEARCH_READY",
                    "selection:",
                    f"  ticker: {ticker}",
                    "  mode: paper",
                    "  selection_date: '2026-08-02'",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def test_api_status_request_scope_cache_dedupes_pure_reads(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    state_dir = project_dir / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    bundle_paths = _write_minimal_selection_bundle(project_dir)
    top_paths = _write_top_configs(project_dir)

    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: None)
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: 1)
    monkeypatch.setattr(dashboard, "_load_dashboard_config", lambda: SimpleNamespace(mode="paper", broker=SimpleNamespace(longbridge=SimpleNamespace(enabled=False))))
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "execution_count": 0})
    monkeypatch.setattr(dashboard, "load_runtime_config", lambda: SimpleNamespace(top_n=3, allow_fallback_live_entries=False, allow_fallback_paper_entries=True))
    monkeypatch.setattr(dashboard, "_load_active_orders_summary", lambda tickers: {"available": False, "count": 0, "orders": []})
    monkeypatch.setattr(dashboard, "_system_status_snapshot", lambda **kwargs: {"mode_key": "paper", "broker_connected": False, "market_open": False})
    monkeypatch.setattr(dashboard, "_shadow_status_payload", lambda: {"state": "SAFE", "status_label": "SAFE"})
    monkeypatch.setattr(dashboard, "_candidate_validation_payload", lambda: {"state": "SAFE", "status_label": "SAFE"})
    monkeypatch.setattr(dashboard, "_candidate_model_evaluation_payload", lambda: {"approval_status": "DRAFT"})
    monkeypatch.setattr(dashboard, "_research_status_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_candidate_research_report_payload", lambda: _candidate_research_fallback_payload())
    monkeypatch.setattr(dashboard, "_market_regime_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_regime_shadow_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_universe_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_read_unified_paper_portfolio_state", lambda: {"cash": 10000, "equity": 10000, "buying_power": 20000, "positions": []})
    monkeypatch.setattr(dashboard, "_paper_position_policy_snapshot", lambda **kwargs: {"enabled": False})
    monkeypatch.setattr(dashboard, "verify_selection_state", lambda required_et_date=None, state=None: (True, "ok", state))

    governance_calls = {"count": 0}

    def _governance():
        governance_calls["count"] += 1
        return {
            "champion_version": "baseline_v1",
            "champion_status": "ACTIVE",
            "approval_status": "WAITING_FOR_DATA",
            "human_approval_required": True,
            "auto_activation_blocked": True,
        }

    monkeypatch.setattr(dashboard, "_governance_payload", _governance)

    read_counts: dict[Path, int] = {}
    original_read_text = Path.read_text

    def counted_read_text(path: Path, *args, **kwargs):
        resolved = path.resolve()
        if resolved in {*(p.resolve() for p in top_paths), *(p.resolve() for p in bundle_paths.values())}:
            read_counts[resolved] = read_counts.get(resolved, 0) + 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)

    payload = dashboard._api_status_payload()

    assert payload["ok"] is True
    assert governance_calls["count"] == 1
    for path in top_paths:
        assert read_counts.get(path.resolve(), 0) == 1
    assert read_counts.get(bundle_paths["manifest"].resolve(), 0) == 1
    assert read_counts.get(bundle_paths["report"].resolve(), 0) == 1
    assert read_counts.get(bundle_paths["state"].resolve(), 0) == 1
    assert read_counts.get(bundle_paths["audit"].resolve(), 0) == 1
    assert read_counts.get(bundle_paths["metadata"].resolve(), 0) == 1


def test_api_status_request_scope_cache_does_not_cross_requests(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    state_dir = project_dir / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(dashboard, "STATE_DIR", state_dir)
    _write_minimal_selection_bundle(project_dir, symbol="AAPL")
    top_paths = _write_top_configs(project_dir, symbol="AAPL")

    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: None)
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: 1)
    monkeypatch.setattr(dashboard, "_load_dashboard_config", lambda: SimpleNamespace(mode="paper", broker=SimpleNamespace(longbridge=SimpleNamespace(enabled=False))))
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "execution_count": 0})
    monkeypatch.setattr(dashboard, "load_runtime_config", lambda: SimpleNamespace(top_n=3, allow_fallback_live_entries=False, allow_fallback_paper_entries=True))
    monkeypatch.setattr(dashboard, "_load_active_orders_summary", lambda tickers: {"available": False, "count": 0, "orders": []})
    monkeypatch.setattr(dashboard, "_system_status_snapshot", lambda **kwargs: {"mode_key": "paper", "broker_connected": False, "market_open": False})
    monkeypatch.setattr(dashboard, "_shadow_status_payload", lambda: {"state": "SAFE", "status_label": "SAFE"})
    monkeypatch.setattr(dashboard, "_candidate_validation_payload", lambda: {"state": "SAFE", "status_label": "SAFE"})
    monkeypatch.setattr(dashboard, "_candidate_model_evaluation_payload", lambda: {"approval_status": "DRAFT"})
    monkeypatch.setattr(dashboard, "_research_status_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_candidate_research_report_payload", lambda: _candidate_research_fallback_payload())
    monkeypatch.setattr(dashboard, "_market_regime_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_regime_shadow_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_universe_payload", lambda: {"available": False})
    monkeypatch.setattr(dashboard, "_read_unified_paper_portfolio_state", lambda: {"cash": 10000, "equity": 10000, "buying_power": 20000, "positions": []})
    monkeypatch.setattr(dashboard, "_paper_position_policy_snapshot", lambda **kwargs: {"enabled": False})
    monkeypatch.setattr(dashboard, "verify_selection_state", lambda required_et_date=None, state=None: (True, "ok", state))
    monkeypatch.setattr(dashboard, "_governance_payload", lambda: {"champion_status": "ACTIVE", "human_approval_required": True, "auto_activation_blocked": True})

    first = dashboard._api_status_payload()
    top_paths[0].write_text(top_paths[0].read_text(encoding="utf-8").replace("mode: paper", "mode: sandbox", 1), encoding="utf-8")
    second = dashboard._api_status_payload()

    assert first["top_modes"][0] == "paper"
    assert second["top_modes"][0] == "sandbox"


def test_selection_state_explicit_slots_match_default_verify_result(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    state_dir = project_dir / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", project_dir)
    configs_dir = project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    for index, ticker in enumerate(("SOFI", "NVDA", "AAPL"), start=1):
        (configs_dir / f"TOP{index}.yaml").write_text(
            yaml.safe_dump(
                {
                    "enabled": True,
                    "ticker": ticker,
                    "mode": "paper",
                    "reason": "selected",
                    "selection_run_id": "run-1",
                    "top_sync_run_id": "run-1",
                    "selection_date": "2026-07-02",
                    "generated_at": "2026-07-02T08:30:00-04:00",
                    "result_quality": "COMPLETE",
                    "research_admission": "RESEARCH_READY",
                    "selection": {
                        "ticker": ticker,
                        "mode": "paper",
                        "selection_date": "2026-07-02",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    selection_state.write_selection_state(
        et_date="2026-07-02",
        generated_at="2026-07-02T08:30:00-04:00",
        selected_symbols=["SOFI", "NVDA", "AAPL"],
        report_path="/tmp/ai_selection_latest.json",
        selection_run_id="run-1",
        top_sync_run_id="run-1",
        result_quality="COMPLETE",
        research_admission="RESEARCH_READY",
    )

    slots = selection_state.current_top_config_slots(limit=3)
    default_result = selection_state.verify_selection_state(required_et_date="2026-07-02")
    explicit_result = selection_state.verify_selection_state(required_et_date="2026-07-02", top_slots=slots)

    assert explicit_result == default_result
    assert explicit_result[0] is True
    assert explicit_result[1] == "ok"


def test_selection_state_explicit_slots_preserve_mismatch_errors(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    state_dir = project_dir / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", project_dir)
    configs_dir = project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 4):
        payload = {
            "enabled": False,
            "ticker": "",
            "mode": "paper",
            "reason": "top_n_not_filled",
            "selection_run_id": "run-1",
            "top_sync_run_id": "run-1",
            "selection_date": "2026-07-02",
            "generated_at": "2026-07-02T08:30:00-04:00",
        }
        if index == 1:
            payload.update({"enabled": True, "ticker": "NVDA", "reason": "selected"})
        (configs_dir / f"TOP{index}.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    selection_state.write_selection_state(
        et_date="2026-07-02",
        generated_at="2026-07-02T08:30:00-04:00",
        selected_symbols=["SOFI"],
        report_path="/tmp/ai_selection_latest.json",
        selection_run_id="run-1",
        top_sync_run_id="run-1",
    )

    slots = selection_state.current_top_config_slots(limit=3)
    default_result = selection_state.verify_selection_state(required_et_date="2026-07-02")
    explicit_result = selection_state.verify_selection_state(required_et_date="2026-07-02", top_slots=slots)

    assert explicit_result == default_result
    assert explicit_result[0] is False
    assert explicit_result[1] == "top_config_symbols_mismatch"


def test_validate_top_config_sync_explicit_slots_preserve_invalid_slot_errors(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    state_dir = project_dir / "state"
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", project_dir)
    configs_dir = project_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "TOP1.yaml").write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "ticker": "SOFI",
                "mode": "paper",
                "selection_date": "2026-07-02",
                "selection_run_id": "run-1",
                "selection": {
                    "ticker": "SOFI",
                    "mode": "paper",
                    "selection_date": "2026-07-02",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (configs_dir / "TOP2.yaml").write_text(
        yaml.safe_dump(
            {
                "enabled": False,
                "ticker": "STALE",
                "mode": "live",
                "selection_date": "2026-07-02",
                "selection_run_id": "run-1",
                "selection": {
                    "ticker": "STALE",
                    "mode": "live",
                    "selection_date": "2026-07-02",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    slots = selection_state.current_top_config_slots(limit=3)
    default_result = selection_state.validate_top_config_sync(
        selected_symbols=["SOFI"],
        selection_date="2026-07-02",
        selection_run_id="run-1",
        limit=3,
    )
    explicit_result = selection_state.validate_top_config_sync(
        selected_symbols=["SOFI"],
        selection_date="2026-07-02",
        selection_run_id="run-1",
        limit=3,
        slots=slots,
    )

    assert explicit_result == default_result
    assert explicit_result["top_sync_status"] == "NOT_OK"
    assert "TOP2:stale_symbol:STALE" in explicit_result["mismatches"]
    assert "TOP2:disabled_mode_mismatch:expected=paper:actual=live" in explicit_result["mismatches"]


def _write_candidate_artifacts(root: Path, *, symbol="SOXS.US", asset_type="inverse_etf", benchmarks=None, strategy_family="range_etf", risk_profile="strict", timeframe="15m", ai_score=91.5, ai_reason="Strong range fit", candidate_score=94.2, liquidity_score=96.0, trend_score=90.0, volatility_score=82.0, risk_score=88.0, strategy_fit_score=97.0, recommended_strategy="trend_following", score_reason="liquidity:strong; trend:strong; volatility:fit; risk:clean; strategy_fit:match", trade_filter_passed=True):
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
        metadata={
            "source": "dashboard_test",
            "data_mode": "live",
            "data_freshness": "fresh",
            "data_status": "VALID",
            "scoring_eligible": True,
            "scoring_block_reason": "",
            "missing_fields": [],
            "trade_filter_passed": trade_filter_passed,
        },
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
            "selection_run_id": "1234567890abcdef1234567890abcdef",
            "bundle_version": "selection_bundle_v1",
            "bundle_hash": "abcdef1234567890abcdef1234567890",
            "selection_date": "2026-07-09",
            "market_state": "MARKET_OPEN",
            "run_mode": "FULL",
            "data_mode": "INTRADAY",
            "selection_stage": "FINALIZED",
            "selected_top_n": 1,
            "requested_top_n": 3,
            "selected_symbols": ["SOFI"],
            "final_selected_symbols": ["SOFI"],
            "execution_status": "COMPLETED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "top_n_complete": False,
            "top_n_missing_count": 2,
            "fallback_used": True,
            "provider_fallback_used": True,
            "warnings_structured": [
                {
                    "warning_code": "top_n_not_filled",
                    "stage": "FINALIZED",
                    "requested_count": 3,
                    "selected_count": 1,
                    "missing_count": 2,
                    "selected_symbols": ["SOFI"],
                    "missing_slots": ["TOP2", "TOP3"],
                    "details": "final TOP still below requested count",
                },
                {
                    "warning_code": "top_n_not_filled",
                    "stage": "FINALIZED",
                    "requested_count": 3,
                    "selected_count": 1,
                    "missing_count": 2,
                    "selected_symbols": ["SOFI"],
                    "missing_slots": ["TOP2", "TOP3"],
                    "details": "final TOP still below requested count",
                },
            ],
            "provider_audit": {
                "tradingagents": {
                    "provider_name": "tradingagents",
                    "attempted": 3,
                    "success": 0,
                    "failure": 3,
                    "timed_out": 1,
                    "fallback_used": 1,
                    "mock_used": 1,
                    "contributed_fields": ["score", "reason"],
                },
                "finrobot": {
                    "provider_name": "finrobot",
                    "attempted": 3,
                    "success": 2,
                    "failure": 1,
                    "timed_out": 0,
                    "fallback_used": 1,
                    "mock_used": 1,
                    "contributed_fields": ["score", "reason"],
                },
                "openbb": {
                    "provider_name": "openbb",
                    "attempted": 3,
                    "success": 3,
                    "failure": 0,
                    "timed_out": 0,
                    "fallback_used": 0,
                    "mock_used": 0,
                    "contributed_fields": ["score", "reason"],
                },
            },
            "provider_outputs": {
                "tradingagents": {"SOFI": {"ticker": "SOFI", "fallback": True, "source": "tradingagents_mock"}},
                "finrobot": {"SOFI": {"ticker": "SOFI", "fallback": True, "source": "finrobot_mock"}},
                "openbb": {"SOFI": {"ticker": "SOFI", "fallback": False, "source": "openbb"}},
            },
            "top3": [
                {
                    "ticker": "SOFI",
                    "fallback_used": True,
                    "candidate_fallback": True,
                    "fallback_sources": ["tradingagents", "finrobot"],
                    "mock_used": True,
                    "mock_sources": ["tradingagents", "finrobot"],
                    "data_status": "INVALID",
                    "current_validation_status": "AI_CANDIDATE",
                    "trade_admission_status": "NOT_TRADABLE",
                }
            ],
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
    assert payload["ai_selection"]["execution_status"] == "COMPLETED"
    assert payload["ai_selection"]["result_quality"] == "DEGRADED"
    assert payload["ai_selection"]["research_admission"] == "RESEARCH_ONLY"
    assert payload["ai_selection"]["selection_run_id"] == "1234567890abcdef1234567890abcdef"
    assert payload["ai_selection"]["selection_date"] == "2026-07-09"
    assert payload["ai_selection"]["market_state"] == "MARKET_OPEN"
    assert payload["ai_selection"]["run_mode"] == "FULL"
    assert payload["ai_selection"]["data_mode"] == "INTRADAY"
    assert payload["ai_selection"]["selected_top_n"] == 1
    assert payload["ai_selection"]["requested_top_n"] == 3
    assert payload["ai_selection"]["selected_symbols"] == ["SOFI"]
    assert payload["ai_selection"]["final_selected_symbols"] == ["SOFI"]
    assert payload["ai_selection"]["bundle_version"] == "selection_bundle_v1"
    assert payload["ai_selection"]["bundle_hash"] == "abcdef1234567890abcdef1234567890"
    assert payload["ai_selection"]["top_n_complete"] is False
    assert payload["ai_selection"]["top_n_missing_count"] == 2
    assert len(payload["ai_selection"]["warnings_structured"]) == 2
    assert payload["ai_selection"]["provider_audit_sections"]["attempted"] == "finrobot / openbb / tradingagents"
    assert payload["ai_selection"]["provider_audit_sections"]["success"] == "finrobot / openbb"
    assert payload["ai_selection"]["provider_audit_sections"]["failure"] == "无"
    assert payload["ai_selection"]["provider_audit_sections"]["timeout"] == "tradingagents"
    assert payload["ai_selection"]["provider_audit_sections"]["fallback"] == "finrobot / tradingagents"
    assert payload["ai_selection"]["provider_audit_sections"]["mock"] == "finrobot / tradingagents"
    assert payload["ai_selection"]["provider_audit_sections"]["contributor"] == "openbb"
    assert payload["ai_selection"]["provider_audit_sections"]["mock_contributor"] == "finrobot / tradingagents"
    assert "stages" in payload["ai_selection"]["selection_funnel"]
    assert isinstance(payload["ai_selection"]["selection_funnel"].get("stages"), list)
    assert payload["ai_selection"]["rejection_reason_counts"]["top_n_not_filled"] == 1
    assert "本次结果仅供研究" in payload["ai_selection"]["research_admission_notice"]
    assert payload["ai_selection"]["top3"][0]["candidate_fallback"] is True
    assert payload["ai_selection"]["top3"][0]["trade_admission_status"] == "NOT_TRADABLE"
    assert payload["risk"]["fallback_live_allowed"] is False
    assert payload["risk"]["fallback_paper_allowed"] is True
    assert payload["dashboard"]["chart_api_available"] is True
    assert len(payload["top_engines"]) == 3
    assert payload["ai_selection"]["price_band"]["min"] == 5.0
    assert payload["ai_selection"]["price_band"]["max"] == 300.0
    assert payload["candidate_validation"]["state"] == "SAFE"
    assert payload["candidate_validation"]["status_label"] == "SAFE"
    assert payload["candidate_validation_api_available"] is True
    assert payload["system"]["mode"] == "PAPER"
    assert payload["system"]["broker_type"] == "PaperBroker"
    assert payload["system"]["broker_connection"] == "local memory"
    assert payload["system"]["data_source"] == "PaperBroker / TOP engine runtime"
    assert payload["system"]["market_open_label"] in {"开盘中", "已收盘"}
    assert payload["system"]["global_reduce_only"] is False
    assert payload["system"]["live_order_enabled"] is False
    assert payload["system"]["lifecycle"]["weekend_paper"]["status_label"] == "unavailable"

    html = client.get("/").get_data(as_text=True)
    assert "AI 选股结果总览" in html
    assert "已入选 1 / 目标 3" in html
    assert "降级（DEGRADED）" in html
    assert "仅供研究（RESEARCH_ONLY）" in html
    assert "正式候选不足" in html
    assert "展开技术详情" in html
    assert "已尝试数据源" in html
    assert "Provider Fallback" not in html
    assert "12345678…abcdef" in html
    assert 'title="1234567890abcdef1234567890abcdef"' in html


def test_api_status_includes_ranked_paper_position_policy(monkeypatch):
    policy = PositionPolicyConfig(
        mode="ranked_aggressive",
        paper_position_policy_enabled=True,
        live_position_policy_enabled=False,
    )
    _patch_status_basics(
        monkeypatch,
        status_map={
            8091: {"mode": "paper", "price": 10.0, "last_signal": "HOLD", "cash": 10_000.0, "equity": 10_000.0, "buying_power": 10_000.0, "position_shares": 0},
            8092: {"mode": "paper", "price": 20.0, "last_signal": "HOLD", "cash": 0.0, "equity": 0.0, "buying_power": 0.0, "position_shares": 0},
            8093: {"mode": "paper", "price": 30.0, "last_signal": "HOLD", "cash": 0.0, "equity": 0.0, "buying_power": 0.0, "position_shares": 0},
        },
        selection_sync={
            "ok": True,
            "state_date": "2026-07-09",
            "required_date": "2026-07-09",
            "selection_state_symbols": ["SOFI", "AAPL", "SOXS"],
            "current_top_config_symbols": ["SOFI", "AAPL", "SOXS"],
            "mismatch_reason": "",
        },
        ai_selection={
            "selection_stage": "FINALIZED",
            "execution_status": "COMPLETED",
            "result_quality": "COMPLETE",
            "research_admission": "RESEARCH_READY",
            "top_n_missing_count": 0,
            "top3": [
                {"ticker": "SOFI", "asset_type": "common_stock", "current_price": 10.0, "data_status": "COMPLETE", "scoring_eligible": True, "candidate_score": 90.0},
                {"ticker": "AAPL", "asset_type": "common_stock", "current_price": 20.0, "data_status": "COMPLETE", "scoring_eligible": True, "candidate_score": 88.0},
                {"ticker": "SOXS", "asset_type": "inverse_etf", "current_price": 30.0, "data_status": "COMPLETE", "scoring_eligible": True, "candidate_score": 86.0},
            ],
            "settings": {},
        },
    )
    monkeypatch.setattr(
        dashboard,
        "load_runtime_config",
        lambda: SimpleNamespace(
            allow_fallback_live_entries=False,
            allow_fallback_paper_entries=True,
            position_policy=policy,
        ),
    )

    payload = dashboard.app.test_client().get("/api/status").get_json()

    position_policy = payload["ai_selection"]["position_policy"]
    assert position_policy["enabled"] is True
    assert position_policy["mode"] == "ranked_aggressive"
    assert position_policy["live_position_policy_enabled"] is False
    assert position_policy["target_allocations"][0]["symbol"] == "SOFI"
    assert position_policy["target_allocations"][0]["capped_target_weight"] == 0.35
    assert position_policy["target_allocations"][2]["symbol"] == "SOXS"
    assert position_policy["target_allocations"][2]["capped_target_weight"] == 0.15
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
        return {"mode": "paper", "price": 18.52, "last_signal": "HOLD", "halted": False} if port == 8080 else None

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
    assert calls == [8080]
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
        top_modes=["sandbox", "sandbox", "sandbox"],
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
    assert payload["mode_consistency"]["dashboard_display_mode"] == "sandbox"
    assert payload["mode_consistency"]["dashboard_execution_mode"] == "sandbox"
    assert payload["mode_consistency"]["top_modes"] == ["sandbox", "sandbox", "sandbox"]
    assert payload["mode_consistency"]["top_execution_modes"] == ["sandbox", "sandbox", "sandbox"]
    assert payload["mode_consistency"]["mixed"] is False
    assert payload["mode_consistency"]["display_mismatch"] is False
    assert payload["system"]["dashboard_display_mode"] == "sandbox"
    assert payload["system"]["dashboard_execution_mode"] == "sandbox"
    assert payload["system"]["dashboard_broker_environment"] == "longbridge_sandbox"
    assert payload["system"]["lifecycle"]["weekend_paper"]["status_label"] == "unavailable"
    assert payload["system"]["lifecycle"]["longbridge_sandbox"]["status_label"] == "unavailable"


def test_api_status_distinguishes_display_mode_from_execution_mode(monkeypatch):
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
        ai_selection={"fallback_used": False, "top3": [], "settings": {"min_price": 4.0, "max_price": 50.0}},
        top_modes=["paper", "paper", "paper"],
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
        live_account={
            "cash": 1000.0,
            "equity": 1000.0,
            "buying_power": 1000.0,
            "positions": [],
            "positions_count": 0,
            "mode": "sandbox",
        },
    )
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "sandbox", "execution_count": 0, "buy_count": 0, "sell_count": 0, "decision_count": 0})

    payload = dashboard.app.test_client().get("/api/status").get_json()

    assert payload["mode_consistency"]["dashboard_mode"] == "sandbox"
    assert payload["mode_consistency"]["dashboard_display_mode"] == "sandbox"
    assert payload["mode_consistency"]["dashboard_execution_mode"] == "sandbox"
    assert payload["mode_consistency"]["top_modes"] == ["paper", "paper", "paper"]
    assert payload["mode_consistency"]["top_execution_modes"] == ["paper", "paper", "paper"]
    assert payload["mode_consistency"]["mixed"] is True
    assert payload["mode_consistency"]["display_mismatch"] is False
    assert payload["mode_consistency"]["severity"] == "BLOCKED"
    assert payload["system"]["dashboard_display_mode"] == "sandbox"
    assert payload["system"]["dashboard_execution_mode"] == "sandbox"
    assert payload["system"]["dashboard_broker_environment"] == "longbridge_sandbox"


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
    assert payload["latest_candidate"]["data_mode"] == "live"
    assert payload["latest_candidate"]["data_status"] == "VALID"
    assert payload["latest_candidate"]["trade_filter_passed"] is True
    assert payload["latest_candidate"]["scoring_eligible"] is True
    assert payload["latest_candidate"]["scoring_block_reason"] == ""
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
    assert "AI 候选验证" in html
    assert "AAPL.US" in html
    assert "候选总分" in html
    assert "数据模式" in html
    assert "股票池筛选" in html
    assert "数据完整度" in html
    assert "是否可评分" in html
    assert "交易准入" in html
    assert "评分阻断原因" in html
    assert "流动性评分" in html
    assert "候选评分效果" in html
    assert "AI 研究日报" in html
    assert "AI 研究调度" in html
    assert "上次运行时间（北京时间）" in html
    assert "推荐策略" in html
    assert "AI 排名原因" in html
    assert "已尝试数据源" in html
    assert "成功数据源" in html
    assert "超时数据源" in html
    assert "候选可进入独立数据验证" in html
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
    assert "SOXS 只读模拟观察" in html
    assert "READ-ONLY SHADOW" in html
    assert "AI 候选验证" in html
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
    assert "AAPL 只读模拟观察" in html
    assert "SOXS 只读模拟观察" not in html
