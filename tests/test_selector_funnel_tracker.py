from __future__ import annotations

import json

from src.openalpha.funnel_tracker import FUNNEL_STAGES, FunnelTracker, dropped_record


def test_funnel_stage_contract_includes_final_selection_stages():
    formal_top_index = FUNNEL_STAGES.index("FORMAL_TOP")
    assert FUNNEL_STAGES[formal_top_index + 1:formal_top_index + 3] == [
        "POST_FILTER",
        "FINAL_SELECTED",
    ]


def test_funnel_tracker_records_stage_invariants_and_reason_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmp_path))
    tracker = FunnelTracker(selection_run_id="run-a", selection_date="2026-07-17")

    tracker.add_stage(
        "DATA_QUALITY",
        ["SOFI", "SMR", "NVDA"],
        ["NVDA"],
        dropped=[
            dropped_record("SOFI", "quote_missing", "quote unavailable"),
            dropped_record("SMR", "history_insufficient", "need 20 bars"),
        ],
    )

    payload = tracker.to_dict()
    stage = payload["stages"][0]
    assert stage["input_count"] == 3
    assert stage["output_count"] == 1
    assert stage["dropped_symbols"] == ["SOFI", "SMR"]
    assert stage["drop_reason_counts"] == {"history_insufficient": 1, "quote_missing": 1}
    assert payload["rejection_reason_counts"] == {"history_insufficient": 1, "quote_missing": 1}


def test_funnel_tracker_keeps_unknown_reason_without_guessing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmp_path))
    tracker = FunnelTracker(selection_run_id="run-b", selection_date="2026-07-17")

    tracker.add_stage("REFINEMENT", ["SOFI"], [])

    dropped = tracker.to_dict()["stages"][0]["dropped"][0]
    assert dropped["symbol"] == "SOFI"
    assert dropped["reason_code"] == "unknown"
    assert tracker.to_dict()["rejection_reason_counts"] == {}
    assert tracker.to_dict()["nearest_rejected_candidates"] == []


def test_funnel_tracker_counts_structured_unknown_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmp_path))
    tracker = FunnelTracker(selection_run_id="run-b2", selection_date="2026-07-17")

    tracker.add_stage(
        "REFINEMENT",
        ["SOFI"],
        [],
        dropped=[dropped_record("SOFI", "unknown", "provider returned no structured reason")],
    )

    payload = tracker.to_dict()
    assert payload["rejection_reason_counts"] == {"unknown": 1}
    assert payload["nearest_rejected_candidates"][0]["symbol"] == "SOFI"


def test_funnel_tracker_writes_run_isolated_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmp_path))
    first = FunnelTracker(selection_run_id="run-a", selection_date="2026-07-17")
    second = FunnelTracker(selection_run_id="run-b", selection_date="2026-07-17")
    first.add_stage("FORMAL_TOP", ["SOFI"], ["SOFI"])
    second.add_stage("FORMAL_TOP", ["NVDA"], [])

    first_path = first.write_report()
    second_path = second.write_report()

    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()
    assert json.loads(first_path.read_text(encoding="utf-8"))["selection_run_id"] == "run-a"
    assert json.loads(second_path.read_text(encoding="utf-8"))["selection_run_id"] == "run-b"


def test_data_quality_drop_records_have_reason_codes():
    """DATA_QUALITY dropped items must include reason_code from quality filter."""
    from src.openalpha.selector import _apply_quality_filters_with_report
    from unittest.mock import patch, MagicMock

    # Build a mock quality context that returns valid quote metrics
    with patch("src.openalpha.selector._QualityFilterContext") as MockCtx:
        ctx_instance = MagicMock()
        ctx_instance.history_metrics.return_value = (1_000_000, 0.5, 50.0)
        ctx_instance.quote_metrics.return_value = (50.0, 49.8, 50.2, True)
        MockCtx.return_value = ctx_instance

        candidates = [
            {"ticker": "TEST_A", "score": 90, "data_source": "live",
             "avg_daily_volume_hint": 2_000_000, "price_midpoint_hint": 50},
            {"ticker": "TEST_B", "score": 85, "data_source": "live",
             "avg_daily_volume_hint": 300_000, "price_midpoint_hint": 50},
        ]
        filtered, report = _apply_quality_filters_with_report(
            candidates, run_mode="FULL"
        )

    # TEST_B should be dropped by volume_filter
    removed = [r for r in report["rows"] if r.get("removed")]
    assert len(removed) >= 1, "Expected at least one dropped candidate"
    for r in removed:
        assert "reason" in r, f"Missing 'reason' in dropped row: {r}"
        assert r["reason"] != "passed", f"Dropped row should not have reason='passed'"
        assert r["reason"] != "unknown", (
            f"Dropped row has unknown reason — quality filter should set a specific reason"
        )
