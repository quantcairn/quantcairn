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


def run_test_direct():
    test_merge_refinement_summary_preserves_preliminary_top5()
    test_merge_refinement_summary_marks_fast_background_when_refinement_stays_preliminary()


if __name__ == "__main__":
    run_test_direct()
