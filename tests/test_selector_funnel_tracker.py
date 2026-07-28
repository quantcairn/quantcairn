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
