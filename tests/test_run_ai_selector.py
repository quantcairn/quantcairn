from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "run_ai_selector.py"


def _formal_candidate_row(ticker: str, score: float, price: float, *, reason: str = "research_complete", source: str = "selector") -> dict:
    return {
        "ticker": ticker,
        "score": score,
        "final_score": score,
        "ai_score": score,
        "range_score": score,
        "current_price": price,
        "range_low": round(price * 0.95, 4),
        "range_high": round(price * 1.05, 4),
        "average_dollar_volume_20d": 250000000.0,
        "atr_20_percentage": 2.5,
        "market_cap": 3000000000.0,
        "ma20": round(price * 0.98, 4),
        "ma50": round(price * 0.95, 4),
        "ma200": round(price * 0.90, 4),
        "quote_timestamp": "2026-07-16T13:00:00Z",
        "quote_age_seconds": 60,
        "daily_data_as_of": "2026-07-15",
        "benchmark_data_as_of": "2026-07-15",
        "benchmark_status": "VALID",
        "benchmark_alignment_status": "VALID",
        "daily_data_status": "VALID",
        "freshness_status": "SAFE",
        "quote_status": "OK",
        "ohlcv_status": "OK",
        "history_status": "OK",
        "history_rows": 30,
        "close_history": [price] * 30,
        "open": round(price * 0.99, 4),
        "high": round(price * 1.01, 4),
        "low": round(price * 0.98, 4),
        "close": price,
        "volume": 1_000_000,
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "current_validation_status": "DATA_VALID",
        "trade_admission_status": "TRADABLE",
        "trade_admission": "TRADABLE",
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_generated_at": "2026-07-16T09:00:00-04:00",
        "score_is_current_run": True,
        "confidence": 0.8,
        "reason": reason,
        "source": source,
    }


def _load_module():
    spec = importlib.util.spec_from_file_location("test_run_ai_selector_module", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_final_selection_funnel_adds_post_filter_and_final_selected_stages():
    module = _load_module()
    tradable = _formal_candidate_row("NVDA", 91.5, 100.0)
    research_only = {
        **_formal_candidate_row("SOFI", 88.0, 10.0),
        "trade_admission_status": "NOT_TRADABLE",
        "trade_admission": "NOT_TRADABLE",
    }
    funnel = {
        "selection_run_id": "run-final",
        "selection_date": "2026-07-16",
        "stages": [
            {
                "stage": "FORMAL_TOP",
                "input_count": 2,
                "output_count": 2,
                "input_symbols": ["NVDA", "SOFI"],
                "output_symbols": ["NVDA", "SOFI"],
                "dropped_symbols": [],
                "dropped": [],
                "drop_reason_counts": {},
                "status": "PASS",
            }
        ],
        "rejection_reason_counts": {},
        "nearest_rejected_candidates": [],
        "pipeline_consistent": True,
    }

    updated = module._append_final_selection_funnel_stages(
        funnel,
        diagnostic_candidates=[tradable, research_only],
        post_filter_candidates=[tradable],
        final_selected_candidates=[tradable],
        trade_rejected=[],
        universe_rejected=[],
        price_rejected=[],
        composition_rejected=[],
        entry_quality_rejected=[],
    )

    assert [stage["stage"] for stage in updated["stages"][-2:]] == ["POST_FILTER", "FINAL_SELECTED"]
    post_filter = updated["stages"][-2]
    final_selected = updated["stages"][-1]
    assert post_filter["input_symbols"] == ["NVDA", "SOFI"]
    assert post_filter["output_symbols"] == ["NVDA"]
    assert post_filter["dropped"][0]["symbol"] == "SOFI"
    assert post_filter["dropped"][0]["reason_code"] == "trade_admission_not_tradable"
    assert final_selected["output_symbols"] == ["NVDA"]
    assert updated["final_selected_symbols"] == ["NVDA"]
    assert updated["pipeline_consistent"] is True


def test_enrich_selection_rows_uses_nested_complete_market_quality_over_stale_top_level_status():
    module = _load_module()
    market_data = {
        "quote_fetch_status": "COMPLETE",
        "ohlcv_fetch_status": "COMPLETE",
        "quote_status": "COMPLETE",
        "ohlcv_status": "COMPLETE",
        "history_status": "COMPLETE",
        "quote_timestamp": "2026-07-20T13:16:25Z",
        "quote_age_seconds": 60,
        "current_price": 17.28,
        "daily_data_as_of": "2026-07-17",
        "daily_data_status": "LATEST_COMPLETED_SESSION",
        "freshness_status": "SAFE",
        "benchmark_status": "VALID",
        "benchmark_alignment_status": "VALID",
        "history_rows": 250,
        "history_available_bars": 250,
        "history_required_bars": 200,
        "history_missing_windows": [],
        "close_history": [17.28] * 250,
        "open": 17.0,
        "high": 17.5,
        "low": 16.9,
        "close": 17.28,
        "volume": 1_000_000,
        "average_dollar_volume_20d": 500_000_000.0,
        "atr_20_percentage": 4.0,
        "ma20": 16.8,
        "ma50": 16.5,
        "ma200": 15.0,
        "market_cap": 6_000_000_000.0,
    }
    row = {
        "ticker": "SOFI",
        "score": 82.3,
        "quote_status": "MISSING",
        "ohlcv_status": "MISSING",
        "history_status": "MISSING",
        "benchmark_status": "",
        "market_data": dict(market_data),
        "trade_market_data": dict(market_data),
    }

    enriched = module._enrich_selection_rows([row])[0]

    assert enriched["quote_status"] == "COMPLETE"
    assert enriched["ohlcv_status"] == "COMPLETE"
    assert enriched["history_status"] == "COMPLETE"
    assert enriched["benchmark_status"] == "VALID"
    assert enriched["data_status"] == "COMPLETE"
    assert enriched["scoring_eligible"] is True
    assert enriched["blocking_reasons"] == []


def test_research_top_candidates_keep_ai_candidate_out_of_tradable_top():
    module = _load_module()
    row = {
        **_formal_candidate_row("SOFI", 71.45, 17.28, reason="needs_validation"),
        "candidate_score": 71.45,
        "formal_candidate_score": 71.45,
        "market_data_sufficiency": "COMPLETE",
        "formal_scoring_eligibility": True,
        "score_type": "FORMAL",
        "score_is_formal": True,
        "score_is_current_run": True,
        "current_validation_status": "AI_CANDIDATE",
        "validation_status": "AI_CANDIDATE",
        "trade_admission_status": "NOT_TRADABLE",
        "trade_admission": "NOT_TRADABLE",
    }

    research_top = module._build_research_top_candidates(
        [row],
        validation_records={
            "SOFI": {
                "candidate_id": "cand_SOFI_US_test",
                "validation_status": "AI_CANDIDATE",
                "current_validation_status": "AI_CANDIDATE",
                "trade_admission_status": "NOT_TRADABLE",
                "evidence_status": "INSUFFICIENT_EVIDENCE",
            }
        },
        requested_top_n=3,
    )

    assert [item["ticker"] for item in research_top] == ["SOFI"]
    assert research_top[0]["candidate_id"] == "cand_SOFI_US_test"
    assert research_top[0]["trade_admission_status"] == "NOT_TRADABLE"
    assert research_top[0]["next_validation_stage"] == "CLASSIFICATION"
    assert research_top[0]["next_validation_stage_label"] == "候选分类"
    assert research_top[0]["paper_live_allowed"] is False
    assert not module.is_formal_selection_eligible(research_top[0])


def test_research_top_rejects_diagnostic_or_stale_scores():
    module = _load_module()
    diagnostic = {
        **_formal_candidate_row("SOFI", 71.45, 17.28),
        "market_data_sufficiency": "COMPLETE",
        "formal_scoring_eligibility": True,
        "score_type": "DIAGNOSTIC",
        "score_is_formal": False,
        "score_is_current_run": True,
        "trade_admission_status": "NOT_TRADABLE",
    }
    stale_score = {
        **_formal_candidate_row("LOWVOL", 70.0, 20.0),
        "market_data_sufficiency": "COMPLETE",
        "formal_scoring_eligibility": True,
        "score_type": "FORMAL",
        "score_is_formal": True,
        "score_source": "PRIOR_BUNDLE",
        "score_is_current_run": False,
        "trade_admission_status": "NOT_TRADABLE",
    }

    research_top = module._build_research_top_candidates([diagnostic, stale_score], requested_top_n=3)

    assert research_top == []


def test_enrich_candidate_quality_rows_uses_nested_market_quality_before_formal_filter():
    module = _load_module()
    market_data = _formal_candidate_row("SOFI", 82.3, 17.28)
    market_data.update(
        {
            "quote_fetch_status": "COMPLETE",
            "ohlcv_fetch_status": "COMPLETE",
            "quote_status": "COMPLETE",
            "ohlcv_status": "COMPLETE",
            "history_status": "COMPLETE",
            "history_rows": 250,
            "history_available_bars": 250,
            "history_required_bars": 200,
            "history_missing_windows": [],
            "close_history": [17.28] * 250,
            "benchmark_status": "VALID",
            "benchmark_alignment_status": "VALID",
        }
    )
    row = {
        "ticker": "SOFI",
        "score": 82.3,
        "quote_status": "MISSING",
        "ohlcv_status": "MISSING",
        "history_status": "MISSING",
        "benchmark_status": "",
        "market_data": dict(market_data),
        "trade_market_data": dict(market_data),
    }

    enriched = module._enrich_candidate_quality_rows([row])[0]

    assert enriched["data_status"] == "COMPLETE"
    assert enriched["scoring_eligible"] is True
    assert enriched["blocking_reasons"] == []
    assert module.is_formal_selection_eligible(enriched) is True


def test_report_rows_backfill_market_snapshot_when_selector_rows_lack_market_fields(monkeypatch):
    module = _load_module()
    snapshot = _formal_candidate_row("AGNC", 82.3, 9.99)
    snapshot.update(
        {
            "quote_fetch_status": "COMPLETE",
            "ohlcv_fetch_status": "COMPLETE",
            "quote_status": "COMPLETE",
            "ohlcv_status": "COMPLETE",
            "history_status": "COMPLETE",
            "history_rows": 250,
            "history_available_bars": 250,
            "history_required_bars": 200,
            "history_missing_windows": [],
            "quote_timestamp": "2026-07-28T13:00:00Z",
            "quote_age_seconds": 60,
            "current_price": 9.99,
            "daily_data_as_of": "2026-07-27",
            "daily_data_status": "LATEST_COMPLETED_SESSION",
            "freshness_status": "SAFE",
            "benchmark_data_as_of": "2026-07-27",
            "benchmark_status": "VALID",
            "benchmark_alignment_status": "VALID",
            "market_data_sufficiency": "COMPLETE",
            "close_history": [9.99] * 250,
        }
    )
    monkeypatch.setattr(module, "build_candidate_market_snapshot", lambda ticker: dict(snapshot) if ticker == "AGNC" else {})

    row = {
        "ticker": "AGNC",
        "score": 82.3,
        "candidate_score": 82.3,
        "final_score": 82.3,
        "score_is_formal": True,
        "score_type": "FORMAL",
        "score_source": "current_run_candidate_ranking",
        "score_is_current_run": True,
    }

    candidate_enriched = module._enrich_candidate_quality_rows([row])[0]
    report_enriched = module._enrich_selection_rows([row])[0]

    for enriched in (candidate_enriched, report_enriched):
        assert enriched["quote_status"] == "COMPLETE"
        assert enriched["ohlcv_status"] == "COMPLETE"
        assert enriched["history_status"] == "COMPLETE"
        assert enriched["benchmark_status"] == "VALID"
        assert enriched["data_status"] == "COMPLETE"
        assert enriched["market_data_sufficiency"] == "COMPLETE"
        assert enriched["quote_timestamp"] == "2026-07-28T13:00:00Z"
        assert enriched["current_price"] == 9.99


def test_write_selection_filter_log_is_repeatable_without_fd_growth(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "LOG_DIR", tmp_path)
    baseline_fd_count = len(os.listdir("/dev/fd")) if Path("/dev/fd").exists() else None
    report = {
        "generated_at": "2026-07-16T09:00:00-04:00",
        "total_candidates_before_filters": 1,
        "removed_by_volume_filter": 0,
        "removed_by_spread_filter": 0,
        "removed_by_volatility_filter": 0,
        "removed_due_to_missing_data": 0,
        "final_selected_symbols": ["SOFI"],
        "backfilled_symbols": [],
        "existing_real_positions_preserved": [],
        "selection_stage": "FINALIZED",
        "timed_out": False,
        "rows": [{"ticker": "SOFI", "score": 92.5}],
    }

    for _ in range(50):
        path = module.write_selection_filter_log(report, now=datetime(2026, 7, 16, 9, 0, 0))
        assert path.exists()

    if baseline_fd_count is not None:
        after_fd_count = len(os.listdir("/dev/fd"))
        assert after_fd_count <= baseline_fd_count + 3


def test_run_ai_selector_emits_preview_without_writing_configs_when_no_finalized_symbols():
    module = _load_module()
    captured_bundles: list[dict] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None, selection_run_id=None):
            return {
                "selection_run_id": selection_run_id or "run-1",
                "selection_funnel": {"selection_run_id": selection_run_id or "run-1", "stages": []},
                "top10": [],
                "top5": [],
                "top3": [],
                "report": [],
                "settings": {},
                "quality_filter_report": {},
            }

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    original_run_integrated = module._run_integrated_ai_selector
    original_bundle_writer = module.write_selection_bundle_atomic
    raised = None
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5}
        module.write_selection_filter_log = lambda payload: None
        module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
            "selection_run_id": payload.get("selection_run_id", "run-1"),
            "selection_bundle_hash": "bundle-hash",
            "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
            "selection_date": payload.get("selection_date", "2026-07-16"),
            "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
            "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
            "disabled_slots": [],
            "selected_symbols": [],
            "audit_path": "state/selection_sync_audit.json",
            "state_path": "state/ai_selection_state.json",
            "report_path": "reports/ai_selection_latest.json",
            "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
        }
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [],
            "preferred_symbols": [],
            "signal_map": {},
            "providers_used": [],
            "providers_disabled": ["openbb", "fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }

        try:
            module.main()
        except SystemExit as exc:
            raised = exc
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log
        module._run_integrated_ai_selector = original_run_integrated
        module.write_selection_bundle_atomic = original_bundle_writer

    assert raised is None
    assert captured_bundles
    assert captured_bundles[0]["selection_state_payload"]["selected_symbols"] == []
    assert captured_bundles[0]["selection_state_payload"]["configured_top_symbols"] == []
    assert captured_bundles[0]["top_items"] == []


def test_run_ai_selector_rejects_cross_run_identity_mismatch():
    module = _load_module()
    captured_bundles: list[dict] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None, selection_run_id=None):
            return {
                "selection_run_id": "run-selector-other",
                "selection_funnel": {"selection_run_id": "run-selector-other", "stages": []},
                "top10": [],
                "top5": [],
                "top3": [],
                "report": [],
                "settings": {},
                "quality_filter_report": {},
            }

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    original_run_integrated = module._run_integrated_ai_selector
    original_bundle_writer = module.write_selection_bundle_atomic
    raised = None
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5}
        module.write_selection_filter_log = lambda payload: None
        module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or payload
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [],
            "preferred_symbols": [],
            "signal_map": {},
            "providers_used": [],
            "providers_disabled": [],
            "fmp_enabled": False,
            "fallback_used": False,
        }

        try:
            module.main()
        except SystemExit as exc:
            raised = exc
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log
        module._run_integrated_ai_selector = original_run_integrated
        module.write_selection_bundle_atomic = original_bundle_writer

    assert raised is not None
    assert getattr(raised, "code", None) == 1
    assert not captured_bundles


def test_run_ai_selector_succeeds_with_openbb_flag_enabled():
    module = _load_module()
    captured_bundles: list[dict] = []
    written_logs: list[dict] = []
    spawned_refinement: list[str] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None, selection_run_id=None):
            return {
                "selection_run_id": selection_run_id or "run-1",
                "selection_funnel": {"selection_run_id": selection_run_id or "run-1", "stages": []},
                "top10": [
                    _formal_candidate_row("NVDA", 91.5, 100.0, reason="research_complete"),
                    _formal_candidate_row("MSFT", 88.2, 100.0, reason="research_complete"),
                ],
                "top5": [
                    _formal_candidate_row("NVDA", 91.5, 100.0, reason="research_complete"),
                    _formal_candidate_row("MSFT", 88.2, 100.0, reason="research_complete"),
                ],
                "top3": [],
                "report": [],
                "settings": {"selection_stage": "fast_preliminary"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected: list[dict]):
            return [
                {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                for idx, row in enumerate(selected)
            ]

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    original_restart = module._restart_top_engines
    original_spawn = module._spawn_background_refinement
    original_load_local_env = module.load_local_ai_env
    original_run_integrated = module._run_integrated_ai_selector
    original_split_selected = module._split_selected_and_protected_positions
    original_annotate = module._annotate_with_ai_signals
    original_apply_range_scores = module._apply_range_scores
    original_build_report_top10 = module._build_report_top10
    original_finalize_price_band = module._finalize_price_band
    original_apply_trade_filter = module._apply_trade_filter
    original_apply_composition_filter = module._apply_composition_filter
    original_bundle_writer = module.write_selection_bundle_atomic
    original_env = os.environ.copy()
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5, "max_symbols": 20}
        module.write_selection_filter_log = lambda payload: written_logs.append(dict(payload))
        module._restart_top_engines = lambda: 0
        module._spawn_background_refinement = lambda timestamp: spawned_refinement.append(timestamp)
        module.load_local_ai_env = lambda: None
        module._annotate_with_ai_signals = lambda rows, signal_map: [dict(item) for item in rows]
        module._apply_range_scores = lambda rows: [dict(item) for item in rows]
        module._enrich_candidate_quality_rows = lambda rows, provider_audit=None, provider_outputs=None: [dict(item) for item in rows]
        module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: [dict(item) for item in (selector_top10 or selected or [])]
        module._finalize_price_band = lambda candidates, min_price, max_price: ([dict(item) for item in candidates], [])
        module._apply_trade_filter = lambda rows: ([dict(item) for item in rows], {"rejected": [], "fallback_used": False})
        module._apply_composition_filter = lambda rows, top_n=3: ([dict(item) for item in rows], {"rejected": [], "warnings": []})
        module._split_selected_and_protected_positions = lambda candidates, positions, limit=5: (list(candidates)[:limit], [])
        module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
            "selection_run_id": payload.get("selection_run_id", "run-1"),
            "selection_bundle_hash": "bundle-hash",
            "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
            "selection_date": payload.get("selection_date", "2026-07-16"),
            "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
            "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
            "disabled_slots": [2, 3],
            "selected_symbols": ["NVDA", "MSFT"],
            "audit_path": "state/selection_sync_audit.json",
            "state_path": "state/ai_selection_state.json",
            "report_path": "reports/ai_selection_latest.json",
            "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
        }
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [
                {"ticker": "NVDA", "score": 91.5, "confidence": 0.8, "reason": "research_complete", "source": "selector", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0, "ma20": 98.0, "ma50": 95.0, "ma200": 90.0, "quote_timestamp": "2026-07-16T13:00:00Z", "quote_age_seconds": 60, "daily_data_as_of": "2026-07-15", "benchmark_data_as_of": "2026-07-15", "benchmark_status": "VALID", "daily_data_status": "VALID", "freshness_status": "SAFE", "quote_status": "OK", "ohlcv_status": "OK", "history_status": "OK", "history_rows": 30, "close_history": [100.0] * 30, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000000, "data_status": "COMPLETE", "scoring_eligible": True},
                {"ticker": "MSFT", "score": 88.2, "confidence": 0.75, "reason": "research_complete", "source": "selector", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0, "ma20": 98.0, "ma50": 95.0, "ma200": 90.0, "quote_timestamp": "2026-07-16T13:00:00Z", "quote_age_seconds": 60, "daily_data_as_of": "2026-07-15", "benchmark_data_as_of": "2026-07-15", "benchmark_status": "VALID", "daily_data_status": "VALID", "freshness_status": "SAFE", "quote_status": "OK", "ohlcv_status": "OK", "history_status": "OK", "history_rows": 30, "close_history": [100.0] * 30, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000000, "data_status": "COMPLETE", "scoring_eligible": True},
            ],
            "preferred_symbols": ["NVDA", "MSFT"],
            "signal_map": {
                "NVDA": {"ticker": "NVDA", "score": 91.5, "confidence": 0.8, "reason": "research_complete", "source": "selector", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0, "ma20": 98.0, "ma50": 95.0, "ma200": 90.0, "quote_timestamp": "2026-07-16T13:00:00Z", "quote_age_seconds": 60, "daily_data_as_of": "2026-07-15", "benchmark_data_as_of": "2026-07-15", "benchmark_status": "VALID", "daily_data_status": "VALID", "freshness_status": "SAFE", "quote_status": "OK", "ohlcv_status": "OK", "history_status": "OK", "history_rows": 30, "close_history": [100.0] * 30, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000000, "data_status": "COMPLETE", "scoring_eligible": True},
                "MSFT": {"ticker": "MSFT", "score": 88.2, "confidence": 0.75, "reason": "research_complete", "source": "selector", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0, "ma20": 98.0, "ma50": 95.0, "ma200": 90.0, "quote_timestamp": "2026-07-16T13:00:00Z", "quote_age_seconds": 60, "daily_data_as_of": "2026-07-15", "benchmark_data_as_of": "2026-07-15", "benchmark_status": "VALID", "daily_data_status": "VALID", "freshness_status": "SAFE", "quote_status": "OK", "ohlcv_status": "OK", "history_status": "OK", "history_rows": 30, "close_history": [100.0] * 30, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000000, "data_status": "COMPLETE", "scoring_eligible": True},
            },
            "providers_used": ["openbb"],
            "providers_disabled": ["fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }

        os.environ["SOXS_OPENBB_ENABLED"] = "1"
        os.environ["OPENALPHA_RESTART_TOP"] = "0"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "1"

        module.main()
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log
        module._restart_top_engines = original_restart
        module._spawn_background_refinement = original_spawn
        module.load_local_ai_env = original_load_local_env
        module._run_integrated_ai_selector = original_run_integrated
        module._split_selected_and_protected_positions = original_split_selected
        module._annotate_with_ai_signals = original_annotate
        module._apply_range_scores = original_apply_range_scores
        module._build_report_top10 = original_build_report_top10
        module._finalize_price_band = original_finalize_price_band
        module._apply_trade_filter = original_apply_trade_filter
        module._apply_composition_filter = original_apply_composition_filter
        module.write_selection_bundle_atomic = original_bundle_writer
        os.environ.clear()
        os.environ.update(original_env)

    assert captured_bundles
    assert written_logs
    assert written_logs[0]["final_selected_symbols"] == ["NVDA", "MSFT"]
    bundle = captured_bundles[0]
    assert bundle["selection_state_payload"]["selected_symbols"] == ["NVDA", "MSFT"]
    assert bundle["summary"]["top3"][0]["ticker"] == "NVDA"
    assert bundle["summary"]["selection_stage"] == "FINALIZED"
    assert bundle["summary"]["processing_phase"] == "fast_preliminary"
    top_item = bundle["summary"]["top3"][0]
    assert isinstance(top_item.get("entry"), dict)
    for key in [
        "entry_proximity_score",
        "good_for_entry_now",
        "entry_quality",
        "entry_reason",
        "range_position",
        "dist_to_support",
        "dist_to_resistance",
    ]:
        assert key in top_item["entry"]
        assert key in top_item
    assert "selector_core" in bundle["summary"]["providers_used"]
    assert "yfinance" in bundle["summary"]["providers_used"]
    assert "openbb" in bundle["summary"]["providers_used"]
    assert "fmp" in bundle["summary"]["providers_disabled"]
    assert bundle["summary"]["fmp_enabled"] is False
    assert spawned_refinement


def test_run_ai_selector_filters_ineligible_candidates_before_bundle_publish():
    module = _load_module()
    captured_bundles: list[dict] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None, selection_run_id=None):
            return {
                "selection_run_id": selection_run_id or "run-1",
                "selection_funnel": {"selection_run_id": selection_run_id or "run-1", "stages": []},
                "top10": [
                    _formal_candidate_row("NVDA", 91.5, 100.0, reason="research_complete"),
                    {
                        "ticker": "SOFI",
                        "score": 70.0,
                        "current_price": 18.0,
                        "range_low": 17.0,
                        "range_high": 19.0,
                        "average_dollar_volume_20d": 1000000.0,
                        "atr_20_percentage": 4.0,
                        "market_cap": 15000000000.0,
                        "data_status": "INVALID",
                        "scoring_eligible": False,
                        "quote_status": "MISSING",
                        "ohlcv_status": "MISSING",
                        "history_status": "MISSING",
                        "benchmark_status": "INVALID",
                        "score_reason": "stub",
                    },
                ],
                "top5": [
                    _formal_candidate_row("NVDA", 91.5, 100.0, reason="research_complete"),
                    {
                        "ticker": "SOFI",
                        "score": 70.0,
                        "current_price": 18.0,
                        "range_low": 17.0,
                        "range_high": 19.0,
                        "average_dollar_volume_20d": 1000000.0,
                        "atr_20_percentage": 4.0,
                        "market_cap": 15000000000.0,
                        "data_status": "INVALID",
                        "scoring_eligible": False,
                        "quote_status": "MISSING",
                        "ohlcv_status": "MISSING",
                        "history_status": "MISSING",
                        "benchmark_status": "INVALID",
                        "score_reason": "stub",
                    },
                ],
                "top3": [],
                "report": [],
                "settings": {"selection_stage": "fast_preliminary"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected: list[dict]):
            return [
                {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                for idx, row in enumerate(selected)
            ]

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    original_restart = module._restart_top_engines
    original_spawn = module._spawn_background_refinement
    original_load_local_env = module.load_local_ai_env
    original_run_integrated = module._run_integrated_ai_selector
    original_split_selected = module._split_selected_and_protected_positions
    original_annotate = module._annotate_with_ai_signals
    original_apply_range_scores = module._apply_range_scores
    original_build_report_top10 = module._build_report_top10
    original_finalize_price_band = module._finalize_price_band
    original_apply_trade_filter = module._apply_trade_filter
    original_apply_composition_filter = module._apply_composition_filter
    original_bundle_writer = module.write_selection_bundle_atomic
    original_env = os.environ.copy()
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5, "max_symbols": 20}
        module.write_selection_filter_log = lambda payload: None
        module._restart_top_engines = lambda: 0
        module._spawn_background_refinement = lambda timestamp: None
        module.load_local_ai_env = lambda: None
        module._annotate_with_ai_signals = lambda rows, signal_map: [dict(item) for item in rows]
        module._apply_range_scores = lambda rows: [dict(item) for item in rows]
        module._enrich_candidate_quality_rows = lambda rows, provider_audit=None, provider_outputs=None: [dict(item) for item in rows]
        module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: [dict(item) for item in (selector_top10 or selected or [])]
        module._finalize_price_band = lambda candidates, min_price, max_price: ([dict(item) for item in candidates], [])
        module._apply_trade_filter = lambda rows: ([dict(item) for item in rows], {"rejected": [], "fallback_used": False})
        module._apply_composition_filter = lambda rows, top_n=3: ([dict(item) for item in rows], {"rejected": [], "warnings": []})
        module._split_selected_and_protected_positions = lambda candidates, positions, limit=5: (list(candidates)[:limit], [])
        module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
            "selection_run_id": payload.get("selection_run_id", "run-1"),
            "selection_bundle_hash": "bundle-hash",
            "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
            "selection_date": payload.get("selection_date", "2026-07-16"),
            "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
            "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
            "disabled_slots": [2, 3],
            "selected_symbols": ["NVDA"],
            "audit_path": "state/selection_sync_audit.json",
            "state_path": "state/ai_selection_state.json",
            "report_path": "reports/ai_selection_latest.json",
            "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
        }
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [
                {
                    "ticker": "NVDA",
                    "score": 91.5,
                    "confidence": 0.8,
                    "reason": "research_complete",
                    "source": "selector",
                    "average_dollar_volume_20d": 250000000.0,
                    "atr_20_percentage": 2.5,
                    "market_cap": 3000000000.0,
                    "ma20": 98.0,
                    "ma50": 95.0,
                    "ma200": 90.0,
                    "quote_timestamp": "2026-07-16T13:00:00Z",
                    "quote_age_seconds": 60,
                    "daily_data_as_of": "2026-07-15",
                    "benchmark_data_as_of": "2026-07-15",
                    "benchmark_status": "VALID",
                    "daily_data_status": "VALID",
                    "freshness_status": "SAFE",
                    "quote_status": "OK",
                    "ohlcv_status": "OK",
                    "history_status": "OK",
                    "history_rows": 30,
                    "close_history": [100.0] * 30,
                    "open": 99.0,
                    "high": 101.0,
                    "low": 98.0,
                    "close": 100.0,
                    "volume": 1000000,
                    "data_status": "COMPLETE",
                    "scoring_eligible": True,
                },
                {
                    "ticker": "SOFI",
                    "score": 70.0,
                    "confidence": 0.6,
                    "reason": "stub",
                    "source": "stub",
                    "data_status": "INVALID",
                    "scoring_eligible": False,
                    "quote_status": "MISSING",
                    "ohlcv_status": "MISSING",
                    "history_status": "MISSING",
                    "benchmark_status": "INVALID",
                },
            ],
            "preferred_symbols": ["NVDA", "SOFI"],
            "signal_map": {
                "NVDA": {
                    "ticker": "NVDA",
                    "score": 91.5,
                    "confidence": 0.8,
                    "reason": "research_complete",
                    "source": "selector",
                    "average_dollar_volume_20d": 250000000.0,
                    "atr_20_percentage": 2.5,
                    "market_cap": 3000000000.0,
                    "ma20": 98.0,
                    "ma50": 95.0,
                    "ma200": 90.0,
                    "quote_timestamp": "2026-07-16T13:00:00Z",
                    "quote_age_seconds": 60,
                    "daily_data_as_of": "2026-07-15",
                    "benchmark_data_as_of": "2026-07-15",
                    "benchmark_status": "VALID",
                    "daily_data_status": "VALID",
                    "freshness_status": "SAFE",
                    "quote_status": "OK",
                    "ohlcv_status": "OK",
                    "history_status": "OK",
                    "history_rows": 30,
                    "close_history": [100.0] * 30,
                    "open": 99.0,
                    "high": 101.0,
                    "low": 98.0,
                    "close": 100.0,
                    "volume": 1000000,
                    "data_status": "COMPLETE",
                    "scoring_eligible": True,
                },
                "SOFI": {
                    "ticker": "SOFI",
                    "score": 70.0,
                    "confidence": 0.6,
                    "reason": "stub",
                    "source": "stub",
                    "data_status": "INVALID",
                    "scoring_eligible": False,
                    "quote_status": "MISSING",
                    "ohlcv_status": "MISSING",
                    "history_status": "MISSING",
                    "benchmark_status": "INVALID",
                },
            },
            "providers_used": ["openbb"],
            "providers_disabled": ["fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }

        os.environ["SOXS_OPENBB_ENABLED"] = "1"
        os.environ["OPENALPHA_RESTART_TOP"] = "0"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"

        module.main()
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log
        module._restart_top_engines = original_restart
        module._spawn_background_refinement = original_spawn
        module.load_local_ai_env = original_load_local_env
        module._run_integrated_ai_selector = original_run_integrated
        module._split_selected_and_protected_positions = original_split_selected
        module._annotate_with_ai_signals = original_annotate
        module._apply_range_scores = original_apply_range_scores
        module._build_report_top10 = original_build_report_top10
        module._finalize_price_band = original_finalize_price_band
        module._apply_trade_filter = original_apply_trade_filter
        module._apply_composition_filter = original_apply_composition_filter
        module.write_selection_bundle_atomic = original_bundle_writer
        os.environ.clear()
        os.environ.update(original_env)

    assert captured_bundles
    bundle = captured_bundles[0]
    assert bundle["selection_run_id"] == bundle["selection_state_payload"]["selection_run_id"]
    assert bundle["selection_run_id"] == bundle["summary"]["selection_run_id"]
    assert bundle["selection_run_id"] == bundle["summary"]["selection_funnel"]["selection_run_id"]
    assert [item["ticker"] for item in bundle["top_items"]] == ["NVDA"]
    assert bundle["selection_state_payload"]["selected_symbols"] == ["NVDA"]
    assert bundle["summary"]["selection_count"] == 1
    assert bundle["summary"]["final_selected_symbols"] == ["NVDA"]


def test_run_ai_selector_backfills_top10_when_selector_top10_empty():
    module = _load_module()
    captured_bundles: list[dict] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None, selection_run_id=None):
            return {
                "selection_run_id": selection_run_id or "run-1",
                "selection_funnel": {"selection_run_id": selection_run_id or "run-1", "stages": []},
                "top10": [],
                "top5": [
                    _formal_candidate_row("NVDA", 91.5, 100.0, reason="research_complete"),
                    _formal_candidate_row("MSFT", 88.2, 100.0, reason="research_complete"),
                ],
                "top3": [],
                "report": [],
                "settings": {"selection_stage": "fast_preliminary"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected: list[dict]):
            return [
                {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                for idx, row in enumerate(selected)
            ]

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    original_restart = module._restart_top_engines
    original_spawn = module._spawn_background_refinement
    original_load_local_env = module.load_local_ai_env
    original_run_integrated = module._run_integrated_ai_selector
    original_split_selected = module._split_selected_and_protected_positions
    original_annotate = module._annotate_with_ai_signals
    original_apply_range_scores = module._apply_range_scores
    original_build_report_top10 = module._build_report_top10
    original_finalize_price_band = module._finalize_price_band
    original_apply_trade_filter = module._apply_trade_filter
    original_apply_composition_filter = module._apply_composition_filter
    original_bundle_writer = module.write_selection_bundle_atomic
    original_env = os.environ.copy()
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5, "max_symbols": 20}
        module.write_selection_filter_log = lambda payload: None
        module._restart_top_engines = lambda: 0
        module._spawn_background_refinement = lambda timestamp: None
        module.load_local_ai_env = lambda: None
        module._annotate_with_ai_signals = lambda rows, signal_map: [dict(item) for item in rows]
        module._apply_range_scores = lambda rows: [dict(item) for item in rows]
        module._enrich_candidate_quality_rows = lambda rows, provider_audit=None, provider_outputs=None: [dict(item) for item in rows]
        module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: [dict(item) for item in (selector_top10 or selected or [])]
        module._finalize_price_band = lambda candidates, min_price, max_price: ([dict(item) for item in candidates], [])
        module._apply_trade_filter = lambda rows: ([dict(item) for item in rows], {"rejected": [], "fallback_used": False})
        module._apply_composition_filter = lambda rows, top_n=3: ([dict(item) for item in rows], {"rejected": [], "warnings": []})
        module._split_selected_and_protected_positions = lambda candidates, positions, limit=5: (list(candidates)[:limit], [])
        module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
            "selection_run_id": payload.get("selection_run_id", "run-1"),
            "selection_bundle_hash": "bundle-hash",
            "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
            "selection_date": payload.get("selection_date", "2026-07-16"),
            "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
            "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
            "disabled_slots": [2, 3],
            "selected_symbols": ["NVDA", "MSFT"],
            "audit_path": "state/selection_sync_audit.json",
            "state_path": "state/ai_selection_state.json",
            "report_path": "reports/ai_selection_latest.json",
            "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
        }
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [],
            "preferred_symbols": [],
            "signal_map": {},
            "providers_used": [],
            "providers_disabled": ["openbb", "fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }

        os.environ["OPENALPHA_RESTART_TOP"] = "0"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"

        module.main()
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log
        module._restart_top_engines = original_restart
        module._spawn_background_refinement = original_spawn
        module.load_local_ai_env = original_load_local_env
        module._run_integrated_ai_selector = original_run_integrated
        module._split_selected_and_protected_positions = original_split_selected
        module._annotate_with_ai_signals = original_annotate
        module._apply_range_scores = original_apply_range_scores
        module._build_report_top10 = original_build_report_top10
        module._finalize_price_band = original_finalize_price_band
        module._apply_trade_filter = original_apply_trade_filter
        module._apply_composition_filter = original_apply_composition_filter
        module.write_selection_bundle_atomic = original_bundle_writer
        os.environ.clear()
        os.environ.update(original_env)

    assert captured_bundles
    assert captured_bundles[0]["selection_state_payload"]["selected_symbols"] == ["NVDA", "MSFT"]
    assert [item["ticker"] for item in captured_bundles[0]["summary"]["top10"]] == ["NVDA", "MSFT"]


def test_selector_run_mode_helpers_toggle_environment():
    module = _load_module()
    original_env = dict(os.environ)
    try:
        module._apply_selector_run_mode("fast_preliminary")
        assert os.environ["OPENALPHA_FAST_START_ONLY"] == "1"
        assert os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] == "1"

        module._apply_selector_run_mode("quality_refined")
        assert os.environ["OPENALPHA_FAST_START_ONLY"] == "0"
        assert os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] == "0"

        module._apply_selector_run_mode("full")
        assert os.environ["OPENALPHA_FAST_START_ONLY"] == "0"
        assert os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] == "1"
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def test_selector_rejection_trace_counts_rows_and_reasons():
    module = _load_module()
    trace, counts = module._build_rejection_trace(
        [
            ("UNIVERSE", [{"ticker": "SOFI", "reason": "price_out_of_range", "asset_type": "common_stock", "data_status": "INVALID", "scoring_eligible": False, "candidate_score": 10.0}]),
            ("TRADE_FILTER", [{"ticker": "SOFI", "reason": "fallback_used_blocked", "asset_type": "common_stock", "data_status": "INVALID", "scoring_eligible": False, "candidate_score": 10.0}]),
        ]
    )

    assert [item["stage"] for item in trace] == ["UNIVERSE", "TRADE_FILTER"]
    assert counts["price_out_of_range"] == 1
    assert counts["fallback_used_blocked"] == 1


def run_test_direct():
    test_run_ai_selector_emits_preview_without_writing_configs_when_no_finalized_symbols()
    test_run_ai_selector_succeeds_with_openbb_flag_enabled()
    test_run_ai_selector_backfills_top10_when_selector_top10_empty()


if __name__ == "__main__":
    run_test_direct()
