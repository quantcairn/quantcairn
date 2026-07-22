from __future__ import annotations

from src.dashboard import combined


def test_selection_dashboard_view_separates_research_and_tradable_candidates():
    view = combined._selection_dashboard_view(
        {
            "selection_date": "2026-07-21",
            "selection_stage": "FINALIZED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selected_top_n": 0,
            "requested_top_n": 3,
            "top_n_missing_count": 3,
            "top3": [],
            "research_top_candidates": [
                {
                    "ticker": "SOFI",
                    "candidate_score": 71.45,
                    "validation_status": "AI_CANDIDATE",
                    "trade_admission_status": "NOT_TRADABLE",
                    "next_validation_stage": "CLASSIFICATION",
                    "next_validation_stage_label": "候选分类",
                }
            ],
            "research_selected_top_n": 1,
            "research_requested_top_n": 3,
            "tradable_selected_top_n": 0,
            "tradable_requested_top_n": 3,
            "next_validation_stage": "CLASSIFICATION",
            "next_validation_stage_label": "候选分类",
        },
        {"ok": True, "detail": "ok"},
    )

    assert view["selected_count"] == 0
    assert view["research_selected_count"] == 1
    assert view["research_symbols"] == ["SOFI"]
    assert view["tradable_selected_count"] == 0
    assert view["next_validation_stage"] == "候选分类（CLASSIFICATION）"
    assert view["paper_live_status"] == "阻断"
