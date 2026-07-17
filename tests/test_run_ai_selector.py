from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "run_ai_selector.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_run_ai_selector_module", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
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


def test_run_ai_selector_succeeds_with_openbb_flag_enabled():
    module = _load_module()
    captured_bundles: list[dict] = []
    written_logs: list[dict] = []
    spawned_refinement: list[str] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [
                    {"ticker": "NVDA", "score": 91.5, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0, "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
                    {"ticker": "MSFT", "score": 88.2, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0, "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
                ],
                "top5": [
                    {"ticker": "NVDA", "score": 91.5, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0, "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
                    {"ticker": "MSFT", "score": 88.2, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0, "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
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
                {"ticker": "NVDA", "score": 91.5, "confidence": 0.8, "reason": "stub", "source": "stub", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
                {"ticker": "MSFT", "score": 88.2, "confidence": 0.75, "reason": "stub", "source": "stub", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
            ],
            "preferred_symbols": ["NVDA", "MSFT"],
            "signal_map": {
                "NVDA": {"ticker": "NVDA", "score": 91.5, "confidence": 0.8, "reason": "stub", "source": "stub", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
                "MSFT": {"ticker": "MSFT", "score": 88.2, "confidence": 0.75, "reason": "stub", "source": "stub", "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
            },
            "providers_used": ["openbb"],
            "providers_disabled": ["fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }

        os.environ["SOXS_OPENBB_ENABLED"] = "1"
        os.environ["AI_SELECTOR_RESTART_TOP"] = "0"
        os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] = "1"

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


def test_run_ai_selector_backfills_top10_when_selector_top10_empty():
    module = _load_module()
    captured_bundles: list[dict] = []

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
            "top10": [],
            "top5": [
                        {"ticker": "NVDA", "score": 91.5, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0, "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
                        {"ticker": "MSFT", "score": 88.2, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0, "average_dollar_volume_20d": 250000000.0, "atr_20_percentage": 2.5, "market_cap": 3000000000.0},
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

        os.environ["AI_SELECTOR_RESTART_TOP"] = "0"
        os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] = "0"

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
        assert os.environ["AI_SELECTOR_FAST_START_ONLY"] == "1"
        assert os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] == "1"

        module._apply_selector_run_mode("quality_refined")
        assert os.environ["AI_SELECTOR_FAST_START_ONLY"] == "0"
        assert os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] == "0"

        module._apply_selector_run_mode("full")
        assert os.environ["AI_SELECTOR_FAST_START_ONLY"] == "0"
        assert os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] == "1"
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
