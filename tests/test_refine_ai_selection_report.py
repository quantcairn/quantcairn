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
    written_reports = []

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
            "top5": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "top3": [{"ticker": "SOFI"}, {"ticker": "AAPL"}, {"ticker": "MSFT"}],
            "settings": {"selection_stage": "fast_preliminary"},
        }
        module.load_runtime_settings = lambda: {"auto_refresh_minutes": 5, "max_symbols": 20}
        module.resolve_price_band = lambda settings: (10.0, 50.0)
        module.AIStrategySelector = FakeSelector
        module.selector_runner._live_equity_positions = lambda: []
        module.selector_runner._merge_live_position_flags = lambda items, positions: list(items)
        module.selector_runner._pin_live_positions = lambda items, positions, limit=3: list(items)[:limit]
        module.selector_runner._write_reports = lambda summary: written_reports.append(dict(summary)) or (Path("/tmp/latest.json"), Path("/tmp/dated.json"))
        module.os.environ.clear()
        module.os.environ.update(original_env)
        module.os.environ["AI_SELECTOR_EXPECTED_TIMESTAMP"] = "2026-07-05T09:00:00"
        module.main()
    finally:
        module.os.environ.clear()
        module.os.environ.update(original_env)

    assert written_reports
    assert written_reports[0]["refinement_status"] == "quality_refined"
    assert written_reports[0]["refinement_selection_stage"] == "quality_refined"
    assert [item["ticker"] for item in written_reports[0]["top5"]] == ["SOFI", "AAPL", "MSFT"]
    assert [item["ticker"] for item in written_reports[0]["refined_top5"]] == ["NVDA", "SOFI", "AAPL"]


def run_test_direct():
    test_merge_refinement_summary_preserves_preliminary_top5()
    test_merge_refinement_summary_marks_fast_background_when_refinement_stays_preliminary()


if __name__ == "__main__":
    run_test_direct()
