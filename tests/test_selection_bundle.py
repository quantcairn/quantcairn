from __future__ import annotations

import json
from pathlib import Path

import yaml

from src.ai_selector import config_writer, selection_state
from src.ai_selector.selection_bundle import build_selection_bundle, persist_selection_bundle


def _patch_bundle_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(config_writer, "BASE", str(tmp_path))
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)


def _base_summary(selection_stage: str, result_quality: str, research_admission: str) -> dict:
    return {
        "timestamp": "2026-07-16T09:00:00-04:00",
        "generated_at": "2026-07-16T09:00:00-04:00",
        "selection_date": "2026-07-16",
        "selection_stage": selection_stage,
        "processing_phase": "fast_preliminary",
        "selection_run_id": "run-1",
        "top_sync_run_id": "run-1",
        "top_sync_status": "OK",
        "top_sync_error": "",
        "top10": [],
        "top5": [],
        "top3": [],
        "selection_count": 0,
        "candidate_count": 0,
        "target_top_n": 3,
        "top_n_filled": False,
        "missing_slots": 3,
        "fallback_used": False,
        "disabled_configs": ["TOP1.yaml", "TOP2.yaml", "TOP3.yaml"],
        "selection_funnel": {"final_selected": 0},
        "settings": {
            "selection_stage": selection_stage,
            "fallback_used": False,
        },
        "result_quality": result_quality,
        "research_admission": research_admission,
        "execution_status": "COMPLETED",
    }


def test_selection_bundle_persists_report_state_top_and_manifest(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"),
            "top3": [{"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"}],
            "top5": [{"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"}],
            "top10": [{"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"}],
            "selection_count": 1,
            "candidate_count": 1,
            "top_n_filled": False,
            "missing_slots": 2,
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_run_id": "run-1",
            "selection_symbols": ["NVDA"],
            "configured_top_symbols": ["NVDA"],
        },
        top_items=[
            {
                "ticker": "NVDA",
                "score": 91.5,
                "final_score": 91.5,
                "selection_date": "2026-07-16",
                "current_price": 100.0,
                "range_low": 95.0,
                "range_high": 105.0,
                "risk": {"stop_loss_pct": 1.5},
                "size": 5,
            }
        ],
        selection_run_id="run-1",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        processing_phase="fast_preliminary",
    )

    result = persist_selection_bundle(bundle)

    latest = tmp_path / "reports" / "ai_selection_latest.json"
    dated = tmp_path / "reports" / "ai_selection_2026-07-16.json"
    state_path = tmp_path / "state" / "ai_selection_state.json"
    audit_path = tmp_path / "state" / "selection_sync_audit.json"
    manifest_path = tmp_path / "state" / "selection_bundle_manifest.json"

    assert latest.exists()
    assert dated.exists()
    assert state_path.exists()
    assert audit_path.exists()
    assert manifest_path.exists()
    assert (tmp_path / "configs" / "TOP1.yaml").exists()
    assert (tmp_path / "configs" / "TOP2.yaml").exists()
    assert (tmp_path / "configs" / "TOP3.yaml").exists()

    top1 = yaml.safe_load((tmp_path / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))
    top2 = yaml.safe_load((tmp_path / "configs" / "TOP2.yaml").read_text(encoding="utf-8"))
    top3 = yaml.safe_load((tmp_path / "configs" / "TOP3.yaml").read_text(encoding="utf-8"))
    assert top1["enabled"] is True
    assert top1["ticker"] == "NVDA"
    assert top1["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"
    assert top1["selection_bundle_version"] == "selection_bundle_v1"
    assert top2["enabled"] is False
    assert top2["reason"] == "top_n_not_filled"
    assert top3["enabled"] is False
    assert top3["reason"] == "top_n_not_filled"

    state = json.loads(state_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(latest.read_text(encoding="utf-8"))

    assert state["selection_run_id"] == "run-1"
    assert state["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"
    assert state["selection_bundle_hash"] == manifest["selection_bundle_hash"]
    assert state["selected_symbols"] == ["NVDA"]
    assert report["selection_run_id"] == "run-1"
    assert report["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"
    assert report["selection_bundle_hash"] == manifest["selection_bundle_hash"]
    assert manifest["selection_run_id"] == "run-1"
    assert manifest["selection_stage"] == "FINALIZED"
    assert manifest["result_quality"] == "DEGRADED"
    assert manifest["research_admission"] == "RESEARCH_ONLY"
    assert manifest["requested_top_n"] == 3
    assert manifest["selected_top_n"] == 1
    assert manifest["top_slot_count"] == 3
    assert manifest["paths"]["state"] == "state/ai_selection_state.json"
    assert manifest["paths"]["manifest"] == "state/selection_bundle_manifest.json"
    assert manifest["hashes"]["report_latest"] == manifest["hashes"]["report_dated"]
    assert manifest["selection_symbols"] == ["NVDA"]
    assert result["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"


def test_selection_bundle_marks_empty_slots_as_selection_blocked_when_run_blocked(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary=_base_summary("FINALIZED", "INVALID", "BLOCKED"),
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": [],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "INVALID",
            "research_admission": "BLOCKED",
            "selection_run_id": "run-2",
        },
        top_items=[],
        selection_run_id="run-2",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="INVALID",
        research_admission="BLOCKED",
        processing_phase="fast_preliminary",
    )

    persist_selection_bundle(bundle)

    top1 = yaml.safe_load((tmp_path / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))
    top2 = yaml.safe_load((tmp_path / "configs" / "TOP2.yaml").read_text(encoding="utf-8"))
    top3 = yaml.safe_load((tmp_path / "configs" / "TOP3.yaml").read_text(encoding="utf-8"))

    assert top1["enabled"] is False
    assert top1["reason"] == "selection_blocked"
    assert top2["enabled"] is False
    assert top3["enabled"] is False
    manifest = json.loads((tmp_path / "state" / "selection_bundle_manifest.json").read_text(encoding="utf-8"))
    assert manifest["disabled_slots"] == [1, 2, 3]
    assert manifest["requested_top_n"] == 3
    assert manifest["selected_top_n"] == 0
    assert manifest["top_slot_count"] == 3


def test_selection_bundle_rejects_top_items_over_requested_top_n(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"),
            "requested_top_n": 3,
            "top3": [
                {"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"},
                {"ticker": "SOFI", "score": 90.0, "final_score": 90.0, "selection_date": "2026-07-16"},
                {"ticker": "AAPL", "score": 89.0, "final_score": 89.0, "selection_date": "2026-07-16"},
            ],
            "top5": [
                {"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"},
                {"ticker": "SOFI", "score": 90.0, "final_score": 90.0, "selection_date": "2026-07-16"},
                {"ticker": "AAPL", "score": 89.0, "final_score": 89.0, "selection_date": "2026-07-16"},
                {"ticker": "MSFT", "score": 88.0, "final_score": 88.0, "selection_date": "2026-07-16"},
            ],
            "top10": [
                {"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"},
                {"ticker": "SOFI", "score": 90.0, "final_score": 90.0, "selection_date": "2026-07-16"},
                {"ticker": "AAPL", "score": 89.0, "final_score": 89.0, "selection_date": "2026-07-16"},
                {"ticker": "MSFT", "score": 88.0, "final_score": 88.0, "selection_date": "2026-07-16"},
            ],
            "selection_count": 4,
            "candidate_count": 4,
            "top_n_filled": False,
            "missing_slots": 0,
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA", "SOFI", "AAPL", "MSFT"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_run_id": "run-3",
            "selection_symbols": ["NVDA", "SOFI", "AAPL", "MSFT"],
            "configured_top_symbols": ["NVDA", "SOFI", "AAPL", "MSFT"],
            "requested_top_n": 3,
            "selected_top_n": 4,
            "top_slot_count": 3,
        },
        top_items=[
            {"ticker": "NVDA", "score": 91.5, "final_score": 91.5, "selection_date": "2026-07-16"},
            {"ticker": "SOFI", "score": 90.0, "final_score": 90.0, "selection_date": "2026-07-16"},
            {"ticker": "AAPL", "score": 89.0, "final_score": 89.0, "selection_date": "2026-07-16"},
            {"ticker": "MSFT", "score": 88.0, "final_score": 88.0, "selection_date": "2026-07-16"},
        ],
        selection_run_id="run-3",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        processing_phase="fast_preliminary",
        requested_top_n=3,
    )

    try:
        persist_selection_bundle(bundle)
    except ValueError as exc:
        assert "bundle_validation_failed" in str(exc)
        assert "selected_top_n_exceeds_requested" in str(exc)
    else:
        raise AssertionError("expected bundle validation failure")

    assert not (tmp_path / "reports" / "ai_selection_latest.json").exists()
    assert not (tmp_path / "reports" / "ai_selection_2026-07-16.json").exists()
    assert not (tmp_path / "state" / "selection_bundle_manifest.json").exists()
    assert not (tmp_path / "configs" / "TOP4.yaml").exists()
    audit = json.loads((tmp_path / "state" / "selection_sync_audit.json").read_text(encoding="utf-8"))
    assert audit["validation_status"] == "failed"
    assert "selected_top_n_exceeds_requested" in audit["validation_error_codes"]
