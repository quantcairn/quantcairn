from __future__ import annotations

import json
from pathlib import Path

import yaml
import pytest

from src.openalpha import config_writer, selection_state
from src.openalpha.selection_bundle import build_selection_bundle, persist_selection_bundle
from src.openalpha.selection_report import load_latest_ai_selection_state


def _patch_bundle_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(config_writer, "BASE", str(tmp_path))
    monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmp_path))
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


def _formal_top_item(ticker: str, score: float, selection_date: str = "2026-07-16", **extra) -> dict:
    payload = {
        "ticker": ticker,
        "score": score,
        "final_score": score,
        "selection_date": selection_date,
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "current_validation_status": "DATA_VALID",
        "trade_admission_status": "TRADABLE",
        "trade_admission": "TRADABLE",
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_generated_at": f"{selection_date}T09:00:00-04:00",
        "score_is_current_run": True,
    }
    payload.update(extra)
    return payload


def test_selection_bundle_persists_report_state_top_and_manifest(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"),
            "top3": [_formal_top_item("NVDA", 91.5)],
            "top5": [_formal_top_item("NVDA", 91.5)],
            "top10": [_formal_top_item("NVDA", 91.5)],
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
            _formal_top_item(
                "NVDA",
                91.5,
                current_price=100.0,
                range_low=95.0,
                range_high=105.0,
                risk={"stop_loss_pct": 1.5},
                size=5,
            )
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
    bundle_root = tmp_path / "state" / "selection_bundles" / "run-1" / "selection_bundle_v1"
    assert bundle_root.exists()
    assert (bundle_root / "ai_selection_report.json").exists()
    assert (bundle_root / "ai_selection_state.json").exists()
    assert (bundle_root / "selection_sync_audit.json").exists()
    assert (bundle_root / "bundle_metadata.json").exists()
    assert (tmp_path / "configs" / "TOP1.yaml").exists()
    assert (tmp_path / "configs" / "TOP2.yaml").exists()
    assert (tmp_path / "configs" / "TOP3.yaml").exists()
    assert (bundle_root / "TOP1.yaml").exists()
    assert (bundle_root / "TOP2.yaml").exists()
    assert (bundle_root / "TOP3.yaml").exists()

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
    assert state["selection_bundle_root_path"] == "state/selection_bundles/run-1/selection_bundle_v1"
    assert state["selected_symbols"] == ["NVDA"]
    assert report["selection_run_id"] == "run-1"
    assert report["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"
    assert report["selection_bundle_hash"] == manifest["selection_bundle_hash"]
    assert report["selection_bundle_root_path"] == "state/selection_bundles/run-1/selection_bundle_v1"
    assert manifest["selection_run_id"] == "run-1"
    assert manifest["selection_stage"] == "FINALIZED"
    assert manifest["result_quality"] == "DEGRADED"
    assert manifest["research_admission"] == "RESEARCH_ONLY"
    assert manifest["requested_top_n"] == 3
    assert manifest["selected_top_n"] == 1
    assert manifest["top_slot_count"] == 3
    assert manifest["bundle_root"] == "state/selection_bundles/run-1/selection_bundle_v1"
    assert manifest["paths"]["bundle_root"] == "state/selection_bundles/run-1/selection_bundle_v1"
    assert manifest["paths"]["bundle_report"] == "state/selection_bundles/run-1/selection_bundle_v1/ai_selection_report.json"
    assert manifest["paths"]["state"] == "state/ai_selection_state.json"
    assert manifest["paths"]["manifest"] == "state/selection_bundle_manifest.json"
    assert manifest["hashes"]["report_latest"] == manifest["hashes"]["report_dated"]
    assert manifest["selection_symbols"] == ["NVDA"]
    assert result["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"
    assert result["bundle_root_path"] == "state/selection_bundles/run-1/selection_bundle_v1"


def test_selection_bundle_manifest_includes_funnel_summary(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "COMPLETE", "RESEARCH_READY"),
            "top3": [_formal_top_item("NVDA", 91.5)],
            "selection_funnel": {
                "selection_run_id": "run-funnel",
                "selection_date": "2026-07-16",
                "stages": [
                    {
                        "stage": "FORMAL_TOP",
                        "input_count": 1,
                        "output_count": 1,
                        "input_symbols": ["NVDA"],
                        "output_symbols": ["NVDA"],
                        "dropped_symbols": [],
                        "dropped": [],
                        "drop_reason_counts": {},
                    }
                ],
            },
            "rejection_reason_counts": {"top_n_not_filled": 2},
            "nearest_rejected_candidates": [
                {"symbol": "SOFI", "stage": "FORMAL_ELIGIBILITY", "reason_code": "trade_admission_not_tradable"}
            ],
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "COMPLETE",
            "research_admission": "RESEARCH_READY",
            "selection_run_id": "run-funnel",
            "selection_symbols": ["NVDA"],
            "configured_top_symbols": ["NVDA"],
        },
        top_items=[_formal_top_item("NVDA", 91.5)],
        selection_run_id="run-funnel",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="COMPLETE",
        research_admission="RESEARCH_READY",
        processing_phase="fast_preliminary",
    )

    persist_selection_bundle(bundle)

    manifest = json.loads((tmp_path / "state" / "selection_bundle_manifest.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "reports" / "ai_selection_latest.json").read_text(encoding="utf-8"))
    assert manifest["selection_funnel"]["stages"][0]["stage"] == "FORMAL_TOP"
    assert manifest["rejection_reason_counts"] == {"top_n_not_filled": 2}
    assert manifest["nearest_rejected_candidates"][0]["symbol"] == "SOFI"
    assert report["selection_funnel"]["stages"][0]["stage"] == "FORMAL_TOP"


def test_selection_bundle_loads_report_from_manifest_pinned_bundle(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={**_base_summary("FINALIZED", "COMPLETE", "RESEARCH_READY"), "top3": [_formal_top_item("NVDA", 91.5)]},
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "COMPLETE",
            "research_admission": "RESEARCH_READY",
            "selection_run_id": "run-11",
            "selection_symbols": ["NVDA"],
            "configured_top_symbols": ["NVDA"],
        },
        top_items=[
            _formal_top_item("NVDA", 91.5, current_price=100.0, range_low=95.0, range_high=105.0, risk={"stop_loss_pct": 1.5}, size=5),
        ],
        selection_run_id="run-11",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="COMPLETE",
        research_admission="RESEARCH_READY",
        processing_phase="fast_preliminary",
    )

    persist_selection_bundle(bundle)
    latest_path = tmp_path / "reports" / "ai_selection_latest.json"
    latest_path.unlink()
    state_path = tmp_path / "state" / "ai_selection_state.json"
    state_path.unlink()

    loaded = load_latest_ai_selection_state(tmp_path)

    assert loaded["selection_run_id"] == "run-11"
    assert loaded["selection_bundle_manifest_path"] == "state/selection_bundle_manifest.json"
    assert loaded["selection_bundle_root_path"] == "state/selection_bundles/run-11/selection_bundle_v1"
    assert loaded["source_path"].endswith("state/selection_bundles/run-11/selection_bundle_v1/ai_selection_report.json")


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


def test_validate_top_config_sync_rejects_empty_bundle_with_live_top_yaml(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    (tmp_path / "configs" / "TOP1.yaml").write_text(
        yaml.safe_dump(
            {
                "ticker": "SOXS",
                "mode": "live",
                "selection_date": "2026-07-21",
                "selection": {
                    "source": "manual_override",
                    "selection_date": "2026-07-21",
                    "symbol": "SOXS",
                    "mode": "live",
                },
                "broker": {"longbridge": {"enabled": True, "allow_live_order": False}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = selection_state.validate_top_config_sync(
        selected_symbols=[],
        selection_date="2026-07-22",
        selection_run_id="run-empty",
        limit=3,
    )

    assert result["top_sync_status"] == "NOT_OK"
    assert any("TOP1" in item for item in result["mismatches"])


def test_validate_top_config_sync_accepts_matching_bundle_and_top_yaml(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    config_writer.write_top_configs(
        [
            _formal_top_item(
                "NVDA",
                91.5,
                current_price=100.0,
                range_low=95.0,
                range_high=105.0,
                risk={"stop_loss_pct": 1.5},
                size=5,
            )
        ],
        selection_run_id="run-match",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        slot_limit=3,
    )

    result = selection_state.validate_top_config_sync(
        selected_symbols=["NVDA"],
        selection_date="2026-07-16",
        selection_run_id="run-match",
        top_items=[{"ticker": "NVDA"}],
        limit=3,
    )

    assert result["top_sync_status"] == "OK"
    assert result["mismatches"] == []


def test_selection_bundle_marks_top_sync_not_ok_when_live_top_preserved(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    (tmp_path / "configs" / "TOP1.yaml").write_text(
        yaml.safe_dump(
            {
                "ticker": "SOXS",
                "mode": "live",
                "selection_date": "2026-07-15",
                "selection": {
                    "source": "manual_override",
                    "selection_date": "2026-07-15",
                    "symbol": "SOXS",
                    "mode": "live",
                },
                "broker": {"longbridge": {"enabled": True, "allow_live_order": False}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"),
            "top3": [_formal_top_item("NVDA", 91.5)],
            "selection_count": 1,
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_run_id": "run-live-preserved",
            "selection_symbols": ["NVDA"],
            "configured_top_symbols": ["NVDA"],
        },
        top_items=[
            _formal_top_item(
                "NVDA",
                91.5,
                current_price=100.0,
                range_low=95.0,
                range_high=105.0,
                risk={"stop_loss_pct": 1.5},
                size=5,
            )
        ],
        selection_run_id="run-live-preserved",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        processing_phase="fast_preliminary",
    )

    result = persist_selection_bundle(bundle)

    report = json.loads((tmp_path / "reports" / "ai_selection_latest.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / "state" / "ai_selection_state.json").read_text(encoding="utf-8"))
    top1 = yaml.safe_load((tmp_path / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))

    assert top1["ticker"] == "SOXS"
    assert result["top_sync_status"] == "NOT_OK"
    assert report["top_sync_status"] == "NOT_OK"
    assert state["top_sync_status"] == "NOT_OK"
    assert "TOP1:symbol_mismatch" in result["top_sync_error"]


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


def test_selection_bundle_rejects_ineligible_formal_top_items(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "INVALID", "BLOCKED"),
            "requested_top_n": 3,
            "top3": [
                {
                    "ticker": "SOFI",
                    "score": 70.0,
                    "final_score": 70.0,
                    "selection_date": "2026-07-16",
                    "data_status": "INVALID",
                    "scoring_eligible": False,
                    "quote_status": "MISSING",
                    "ohlcv_status": "MISSING",
                    "history_status": "MISSING",
                    "benchmark_status": "INVALID",
                    "fallback_scope": "CRITICAL_MARKET_DATA",
                    "fallback_severity": "CRITICAL",
                }
            ],
            "top5": [
                {
                    "ticker": "SOFI",
                    "score": 70.0,
                    "final_score": 70.0,
                    "selection_date": "2026-07-16",
                    "data_status": "INVALID",
                    "scoring_eligible": False,
                    "quote_status": "MISSING",
                    "ohlcv_status": "MISSING",
                    "history_status": "MISSING",
                    "benchmark_status": "INVALID",
                    "fallback_scope": "CRITICAL_MARKET_DATA",
                    "fallback_severity": "CRITICAL",
                }
            ],
            "top10": [
                {
                    "ticker": "SOFI",
                    "score": 70.0,
                    "final_score": 70.0,
                    "selection_date": "2026-07-16",
                    "data_status": "INVALID",
                    "scoring_eligible": False,
                    "quote_status": "MISSING",
                    "ohlcv_status": "MISSING",
                    "history_status": "MISSING",
                    "benchmark_status": "INVALID",
                    "fallback_scope": "CRITICAL_MARKET_DATA",
                    "fallback_severity": "CRITICAL",
                }
            ],
            "selection_count": 1,
            "candidate_count": 1,
            "top_n_filled": False,
            "missing_slots": 2,
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["SOFI"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "INVALID",
            "research_admission": "BLOCKED",
            "selection_run_id": "run-4",
            "selection_symbols": ["SOFI"],
            "configured_top_symbols": ["SOFI"],
            "requested_top_n": 3,
            "selected_top_n": 1,
            "top_slot_count": 3,
        },
        top_items=[
            {
                "ticker": "SOFI",
                "score": 70.0,
                "final_score": 70.0,
                "selection_date": "2026-07-16",
                "data_status": "INVALID",
                "scoring_eligible": False,
                "quote_status": "MISSING",
                "ohlcv_status": "MISSING",
                "history_status": "MISSING",
                "benchmark_status": "INVALID",
                "fallback_scope": "CRITICAL_MARKET_DATA",
                "fallback_severity": "CRITICAL",
            }
        ],
        selection_run_id="run-4",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="INVALID",
        research_admission="BLOCKED",
        processing_phase="fast_preliminary",
        requested_top_n=3,
    )

    try:
        persist_selection_bundle(bundle)
    except ValueError as exc:
        assert "bundle_validation_failed" in str(exc)
        assert "formal_top_ineligible:SOFI" in str(exc)
    else:
        raise AssertionError("expected bundle validation failure")

    assert not (tmp_path / "reports" / "ai_selection_latest.json").exists()
    assert not (tmp_path / "state" / "selection_bundle_manifest.json").exists()
    assert not (tmp_path / "configs" / "TOP4.yaml").exists()
    audit = json.loads((tmp_path / "state" / "selection_sync_audit.json").read_text(encoding="utf-8"))
    assert audit["validation_status"] == "failed"
    assert "formal_top_ineligible:SOFI" in audit["validation_error_codes"]


def test_selection_bundle_keeps_research_top_separate_from_tradable_top(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)
    research_candidate = {
        **_formal_top_item("SOFI", 71.45),
        "candidate_id": "cand_SOFI_US_test",
        "validation_status": "AI_CANDIDATE",
        "current_validation_status": "AI_CANDIDATE",
        "trade_admission_status": "NOT_TRADABLE",
        "research_rank": 1,
        "next_validation_stage": "CLASSIFICATION",
        "next_validation_stage_label": "候选分类",
    }

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"),
            "top3": [],
            "research_top_candidates": [research_candidate],
            "research_selected_top_n": 1,
            "research_requested_top_n": 3,
            "tradable_top_candidates": [],
            "tradable_selected_top_n": 0,
            "tradable_requested_top_n": 3,
            "next_validation_stage": "CLASSIFICATION",
            "next_validation_stage_label": "候选分类",
            "validation_pipeline_summary": {
                "research_candidate_count": 1,
                "tradable_candidate_count": 0,
                "next_validation_stage": "CLASSIFICATION",
                "paper_live_blocked": True,
            },
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": [],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_run_id": "run-research-only",
            "selection_symbols": [],
            "configured_top_symbols": [],
        },
        top_items=[],
        selection_run_id="run-research-only",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        processing_phase="fast_preliminary",
        requested_top_n=3,
    )

    result = persist_selection_bundle(bundle)

    report = json.loads((tmp_path / "reports" / "ai_selection_latest.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / "state" / "ai_selection_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "state" / "selection_bundle_manifest.json").read_text(encoding="utf-8"))
    assert result["selected_symbols"] == []
    assert report["selected_symbols"] == []
    assert report["selected_top_n"] == 0
    assert report["tradable_selected_top_n"] == 0
    assert report["research_selected_top_n"] == 1
    assert report["research_top_candidates"][0]["ticker"] == "SOFI"
    assert state["selected_top_n"] == 0
    assert state["research_selected_top_n"] == 1
    assert manifest["selected_top_n"] == 0
    assert manifest["research_selected_top_n"] == 1
    assert manifest["tradable_selected_top_n"] == 0
    assert manifest["selection_symbols"] == []


def test_selection_bundle_rejects_cross_run_identity_mismatch(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    bundle = build_selection_bundle(
        summary={
            **_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"),
            "selection_run_id": "run-b",
            "selection_funnel": {"selection_run_id": "run-b", "stages": []},
            "top3": [_formal_top_item("NVDA", 91.5)],
        },
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_run_id": "run-b",
            "selection_symbols": ["NVDA"],
            "configured_top_symbols": ["NVDA"],
        },
        top_items=[_formal_top_item("NVDA", 91.5)],
        selection_run_id="run-a",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        processing_phase="fast_preliminary",
    )

    with pytest.raises(ValueError) as excinfo:
        persist_selection_bundle(bundle)

    assert "bundle_validation_failed" in str(excinfo.value)
    assert "selection_run_id_mismatch" in str(excinfo.value)
    assert not (tmp_path / "reports" / "ai_selection_latest.json").exists()
    assert not (tmp_path / "state" / "selection_bundle_manifest.json").exists()


def test_selection_bundle_rolls_back_when_compat_sync_fails_after_manifest_commit(tmp_path, monkeypatch):
    _patch_bundle_roots(tmp_path, monkeypatch)

    success_bundle = build_selection_bundle(
        summary={**_base_summary("FINALIZED", "DEGRADED", "RESEARCH_ONLY"), "top3": [_formal_top_item("NVDA", 91.5)]},
        selection_state_payload={
            "et_date": "2026-07-16",
            "generated_at": "2026-07-16T09:00:00-04:00",
            "selected_symbols": ["NVDA"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "selection_run_id": "run-20",
            "selection_symbols": ["NVDA"],
            "configured_top_symbols": ["NVDA"],
        },
        top_items=[
            _formal_top_item("NVDA", 91.5, current_price=100.0, range_low=95.0, range_high=105.0, risk={"stop_loss_pct": 1.5}, size=5),
        ],
        selection_run_id="run-20",
        selection_date="2026-07-16",
        generated_at="2026-07-16T09:00:00-04:00",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        processing_phase="fast_preliminary",
    )
    persist_selection_bundle(success_bundle)

    failing_bundle = build_selection_bundle(
        summary={**_base_summary("FINALIZED", "COMPLETE", "RESEARCH_READY"), "top3": [_formal_top_item("SOFI", 92.5, "2026-07-17")]},
        selection_state_payload={
            "et_date": "2026-07-17",
            "generated_at": "2026-07-17T09:00:00-04:00",
            "selected_symbols": ["SOFI"],
            "selection_stage": "FINALIZED",
            "processing_phase": "fast_preliminary",
            "result_quality": "COMPLETE",
            "research_admission": "RESEARCH_READY",
            "selection_run_id": "run-21",
            "selection_symbols": ["SOFI"],
            "configured_top_symbols": ["SOFI"],
        },
        top_items=[
            _formal_top_item("SOFI", 92.5, "2026-07-17", current_price=20.0, range_low=19.0, range_high=21.0, risk={"stop_loss_pct": 1.5}, size=5),
        ],
        selection_run_id="run-21",
        selection_date="2026-07-17",
        generated_at="2026-07-17T09:00:00-04:00",
        result_quality="COMPLETE",
        research_admission="RESEARCH_READY",
        processing_phase="fast_preliminary",
    )

    original_write_selection_state = selection_state.write_selection_state

    def _boom(**kwargs):
        raise RuntimeError("compat sync failed")

    monkeypatch.setattr(selection_state, "write_selection_state", _boom)

    try:
        try:
            persist_selection_bundle(failing_bundle)
        except RuntimeError as exc:
            assert "compat sync failed" in str(exc)
        else:
            raise AssertionError("expected compat sync failure")
    finally:
        monkeypatch.setattr(selection_state, "write_selection_state", original_write_selection_state)

    latest = json.loads((tmp_path / "reports" / "ai_selection_latest.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / "state" / "ai_selection_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "state" / "selection_bundle_manifest.json").read_text(encoding="utf-8"))
    top1 = yaml.safe_load((tmp_path / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))
    assert latest["selection_run_id"] == "run-20"
    assert state["selection_run_id"] == "run-20"
    assert manifest["selection_run_id"] == "run-20"
    assert top1["selection_run_id"] == "run-20"
