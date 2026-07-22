from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "refine_ai_selection_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_refine_ai_selection_report_module", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _formal_row(ticker: str, score: float = 90.0) -> dict:
    return {
        "ticker": ticker,
        "score": score,
        "final_score": score,
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "current_validation_status": "DATA_VALID",
        "trade_admission_status": "TRADABLE",
        "trade_admission": "TRADABLE",
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_generated_at": "2026-07-05T09:00:00",
        "score_is_current_run": True,
    }


def test_merge_refinement_summary_preserves_preliminary_top5():
    module = _load_module()
    preliminary = {
        "timestamp": "2026-07-05T09:00:00",
        "top5": [{"ticker": "SOFI"}],
        "settings": {"selection_stage": "fast_preliminary"},
    }
    refined = {
        "top5": [{"ticker": "NVDA"}, {"ticker": "AAPL"}],
        "top3": [{"ticker": "NVDA"}],
        "top10": [{"ticker": "NVDA"}],
        "report": [{"ticker": "NVDA"}],
        "settings": {"selection_stage": "quality_refined"},
        "quality_filter_report": {"final_selected_symbols": ["NVDA", "AAPL"]},
    }

    merged = module._merge_refinement_summary(preliminary, refined)

    assert [item["ticker"] for item in merged["top5"]] == ["SOFI"]
    assert [item["ticker"] for item in merged["refined_top5"]] == ["NVDA", "AAPL"]
    assert merged["refinement_status"] == "quality_refined"
    assert merged["refinement_selection_stage"] == "quality_refined"


def test_merge_refinement_summary_marks_fast_background_when_refinement_stays_preliminary():
    module = _load_module()
    preliminary = {
        "timestamp": "2026-07-05T09:00:00",
        "top5": [{"ticker": "SOFI"}],
        "settings": {"selection_stage": "fast_preliminary"},
    }
    refined = {
        "top5": [{"ticker": "NVDA"}],
        "top3": [{"ticker": "NVDA"}],
        "top10": [{"ticker": "NVDA"}],
        "report": [{"ticker": "NVDA"}],
        "settings": {"selection_stage": "fast_preliminary"},
        "quality_filter_report": {},
    }

    merged = module._merge_refinement_summary(preliminary, refined)

    assert merged["refinement_status"] == "background_fast_preliminary"
    assert merged["refinement_selection_stage"] == "fast_preliminary"


def test_merge_refined_candidates_preserves_preliminary_fill():
    module = _load_module()
    preliminary = [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}]
    refined = [{"ticker": "NVDA"}]

    merged = module._merge_refined_candidates(preliminary, refined, limit=3)

    assert [item["ticker"] for item in merged] == ["NVDA", "SOFI", "AAPL"]


def test_main_refines_even_if_latest_report_is_finalized():
    module = _load_module()
    written_bundles = []
    source_manifest = {
        "selection_run_id": "run-1",
        "bundle_version": "selection_bundle_v1",
        "selection_bundle_hash": "bundle-hash-1",
        "selection_date": "2026-07-05",
    }

    class FakeSelector:
        selection_size = 4

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0), _formal_row("MSFT", 86.0)],
                "top5": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0), _formal_row("MSFT", 86.0)],
                "top3": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0)],
                "report": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0), _formal_row("MSFT", 86.0)],
                "settings": {"selection_stage": "quality_refined"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected):
            return [
                {"rank": idx, "ticker": row["ticker"], "score": 90.0 - idx}
                for idx, row in enumerate(selected, start=1)
            ]

    original_env = dict(module.os.environ)
    try:
        module._load_latest_report = lambda: {
            "timestamp": "2026-07-05T09:00:00",
            "selection_run_id": "run-1",
            "selection_date": "2026-07-05",
            "generated_at": "2026-07-05T09:00:00",
            "selection_stage": "FINALIZED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_bundle_hash": "bundle-hash-1",
            "selection_bundle_version": "selection_bundle_v1",
            "requested_top_n": 3,
            "target_top_n": 3,
            "top5": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "GOOGL"}],
            "top3": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "settings": {"selection_stage": "fast_preliminary"},
        }
        module._load_current_manifest = lambda: dict(source_manifest)
        module.load_runtime_settings = lambda: {"auto_refresh_minutes": 5, "max_symbols": 20}
        module.resolve_price_band = lambda settings: (10.0, 50.0)
        module.AIStrategySelector = FakeSelector
        module.selector_runner._live_equity_positions = lambda: []
        module.selector_runner._merge_live_position_flags = lambda items, positions: list(items)
        module.selector_runner._pin_live_positions = lambda items, positions, limit=3: list(items)[:limit]
        module.selector_runner.write_selection_bundle_atomic = lambda **payload: written_bundles.append(dict(payload)) or {
            "selection_run_id": payload.get("selection_run_id", "run-1"),
            "selection_bundle_hash": "bundle-hash",
            "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
            "selection_date": payload.get("selection_date", "2026-07-05"),
            "generated_at": payload.get("generated_at", "2026-07-05T09:00:00"),
            "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
            "disabled_slots": [2, 3],
            "selected_symbols": ["NVDA", "SOFI", "AAPL"],
            "audit_path": "state/selection_sync_audit.json",
            "state_path": "state/ai_selection_state.json",
            "report_path": "reports/ai_selection_latest.json",
            "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
        }
        module.os.environ.clear()
        module.os.environ.update(original_env)
        module.os.environ["AI_SELECTOR_EXPECTED_TIMESTAMP"] = "2026-07-05T09:00:00"
        module.main()
    finally:
        module.os.environ.clear()
        module.os.environ.update(original_env)

    assert written_bundles
    bundle = written_bundles[0]
    assert bundle["summary"]["refinement_status"] == "quality_refined"
    assert bundle["summary"]["refinement_selection_stage"] == "quality_refined"
    assert [item["ticker"] for item in bundle["summary"]["top5"]] == ["NVDA", "SOFI", "AAPL"]
    assert [item["ticker"] for item in bundle["summary"]["top3"]] == ["NVDA", "SOFI", "AAPL"]
    assert bundle["top_items"] == bundle["summary"]["top5"]
    assert bundle["selection_state_payload"]["selected_symbols"] == ["NVDA", "SOFI", "AAPL"]
    assert bundle["selection_state_payload"]["disabled_slots"] == []
    assert bundle["summary"]["source_bundle_hash"] == "bundle-hash-1"
    assert bundle["summary"]["source_bundle_version"] == "selection_bundle_v1"
    assert bundle["summary"]["requested_top_n"] == 3
    assert bundle["summary"]["selected_top_n"] == 3
    assert bundle["selection_state_payload"]["requested_top_n"] == 3
    assert bundle["selection_state_payload"]["selected_top_n"] == 3
    assert bundle["selection_state_payload"]["top_slot_count"] == 3


def test_main_truncates_refined_candidates_to_requested_top_n():
    module = _load_module()
    written_bundles = []
    source_manifest = {
        "selection_run_id": "run-1",
        "bundle_version": "selection_bundle_v1",
        "selection_bundle_hash": "bundle-hash-1",
        "selection_date": "2026-07-05",
    }

    class FakeSelector:
        selection_size = 4

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0), _formal_row("MSFT", 86.0)],
                "top5": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0), _formal_row("MSFT", 86.0)],
                "top3": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0)],
                "report": [_formal_row("NVDA", 89.0), _formal_row("SOFI", 88.0), _formal_row("AAPL", 87.0), _formal_row("MSFT", 86.0)],
                "settings": {"selection_stage": "quality_refined"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected):
            return [
                {"rank": idx, "ticker": row["ticker"], "score": 90.0 - idx}
                for idx, row in enumerate(selected, start=1)
            ]

    original_env = dict(module.os.environ)
    try:
        module._load_latest_report = lambda: {
            "timestamp": "2026-07-05T09:00:00",
            "selection_run_id": "run-1",
            "selection_date": "2026-07-05",
            "generated_at": "2026-07-05T09:00:00",
            "selection_stage": "FINALIZED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_bundle_hash": "bundle-hash-1",
            "selection_bundle_version": "selection_bundle_v1",
            "requested_top_n": 3,
            "target_top_n": 3,
            "top5": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "GOOGL"}],
            "top3": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "settings": {"selection_stage": "fast_preliminary"},
        }
        module._load_current_manifest = lambda: dict(source_manifest)
        module.load_runtime_settings = lambda: {"auto_refresh_minutes": 5, "max_symbols": 20}
        module.resolve_price_band = lambda settings: (10.0, 50.0)
        module.AIStrategySelector = FakeSelector
        module.selector_runner._live_equity_positions = lambda: []
        module.selector_runner._merge_live_position_flags = lambda items, positions: list(items)
        module.selector_runner._pin_live_positions = lambda items, positions, limit=3: list(items)[:limit]
        module.selector_runner.write_selection_bundle_atomic = lambda **payload: written_bundles.append(dict(payload)) or {
            "selection_run_id": payload.get("selection_run_id", "run-1"),
            "selection_bundle_hash": "bundle-hash",
            "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
            "selection_date": payload.get("selection_date", "2026-07-05"),
            "generated_at": payload.get("generated_at", "2026-07-05T09:00:00"),
            "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
            "disabled_slots": [2, 3],
            "selected_symbols": ["NVDA", "SOFI", "AAPL"],
            "audit_path": "state/selection_sync_audit.json",
            "state_path": "state/ai_selection_state.json",
            "report_path": "reports/ai_selection_latest.json",
            "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
        }
        module.os.environ.clear()
        module.os.environ.update(original_env)
        module.os.environ["AI_SELECTOR_EXPECTED_TIMESTAMP"] = "2026-07-05T09:00:00"
        module.main()
    finally:
        module.os.environ.clear()
        module.os.environ.update(original_env)

    assert written_bundles
    bundle = written_bundles[0]
    assert len(bundle["top_items"]) == 3
    assert [item["ticker"] for item in bundle["top_items"]] == ["NVDA", "SOFI", "AAPL"]
    assert bundle["requested_top_n"] == 3
    assert bundle["selection_state_payload"]["requested_top_n"] == 3
    assert bundle["selection_state_payload"]["selected_top_n"] == 3
    assert bundle["selection_state_payload"]["disabled_slots"] == []


def test_main_skips_refine_when_current_manifest_changes_mid_run():
    module = _load_module()
    written_bundles = []
    manifest_reads = [
        {
            "selection_run_id": "run-1",
            "bundle_version": "selection_bundle_v1",
            "selection_bundle_hash": "bundle-hash-1",
            "selection_date": "2026-07-05",
        },
        {
            "selection_run_id": "run-2",
            "bundle_version": "selection_bundle_v1",
            "selection_bundle_hash": "bundle-hash-2",
            "selection_date": "2026-07-05",
        },
    ]

    class FakeSelector:
        selection_size = 3

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [{"ticker": "NVDA"}],
                "top5": [{"ticker": "NVDA"}],
                "top3": [{"ticker": "NVDA"}],
                "report": [{"ticker": "NVDA"}],
                "settings": {"selection_stage": "quality_refined"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected):
            return [{"rank": 1, "ticker": "NVDA", "score": 90.0}]

    original_env = dict(module.os.environ)
    try:
        module._load_latest_report = lambda: {
            "timestamp": "2026-07-05T09:00:00",
            "selection_run_id": "run-1",
            "selection_date": "2026-07-05",
            "generated_at": "2026-07-05T09:00:00",
            "selection_stage": "FINALIZED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_bundle_hash": "bundle-hash-1",
            "selection_bundle_version": "selection_bundle_v1",
            "top5": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "top3": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "settings": {"selection_stage": "fast_preliminary"},
        }
        module._load_current_manifest = lambda: manifest_reads.pop(0)
        module.load_runtime_settings = lambda: {"auto_refresh_minutes": 5, "max_symbols": 20}
        module.resolve_price_band = lambda settings: (10.0, 50.0)
        module.AIStrategySelector = FakeSelector
        module.selector_runner._live_equity_positions = lambda: []
        module.selector_runner._merge_live_position_flags = lambda items, positions: list(items)
        module.selector_runner._pin_live_positions = lambda items, positions, limit=3: list(items)[:limit]
        module.selector_runner.write_selection_bundle_atomic = lambda **payload: written_bundles.append(dict(payload)) or {}
        module.os.environ.clear()
        module.os.environ.update(original_env)
        module.os.environ["AI_SELECTOR_EXPECTED_TIMESTAMP"] = "2026-07-05T09:00:00"
        module.main()
    finally:
        module.os.environ.clear()
        module.os.environ.update(original_env)

    assert not written_bundles


def test_main_skips_refine_when_refined_output_is_empty():
    module = _load_module()
    written_bundles = []

    class FakeSelector:
        selection_size = 3

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [],
                "top5": [],
                "top3": [],
                "report": [],
                "settings": {"selection_stage": "quality_refined"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected):
            return []

    original_env = dict(module.os.environ)
    try:
        module._load_latest_report = lambda: {
            "timestamp": "2026-07-05T09:00:00",
            "selection_run_id": "run-1",
            "selection_date": "2026-07-05",
            "generated_at": "2026-07-05T09:00:00",
            "selection_stage": "FINALIZED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_bundle_hash": "bundle-hash-1",
            "selection_bundle_version": "selection_bundle_v1",
            "top5": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "top3": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "settings": {"selection_stage": "fast_preliminary"},
        }
        module._load_current_manifest = lambda: {
            "selection_run_id": "run-1",
            "bundle_version": "selection_bundle_v1",
            "selection_bundle_hash": "bundle-hash-1",
            "selection_date": "2026-07-05",
        }
        module.load_runtime_settings = lambda: {"auto_refresh_minutes": 5, "max_symbols": 20}
        module.resolve_price_band = lambda settings: (10.0, 50.0)
        module.AIStrategySelector = FakeSelector
        module.selector_runner._live_equity_positions = lambda: []
        module.selector_runner._merge_live_position_flags = lambda items, positions: list(items)
        module.selector_runner._pin_live_positions = lambda items, positions, limit=3: list(items)[:limit]
        module.selector_runner.write_selection_bundle_atomic = lambda **payload: written_bundles.append(dict(payload)) or {}
        module.os.environ.clear()
        module.os.environ.update(original_env)
        module.os.environ["AI_SELECTOR_EXPECTED_TIMESTAMP"] = "2026-07-05T09:00:00"
        module.main()
    finally:
        module.os.environ.clear()
        module.os.environ.update(original_env)

    assert not written_bundles


def run_test_direct():
    test_merge_refinement_summary_preserves_preliminary_top5()
    test_merge_refinement_summary_marks_fast_background_when_refinement_stays_preliminary()


if __name__ == "__main__":
    run_test_direct()
