from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.candidate_validation import (
    CandidateRecord,
    CandidateTransitionError,
    CandidateValidationStore,
    DeploymentStatus,
    EvidenceStatus,
    ProfitabilityStatus,
    ValidationStatus,
    is_valid_transition,
)


def _write_latest_candidate(store: CandidateValidationStore, payload: dict) -> None:
    store.candidates_path.parent.mkdir(parents=True, exist_ok=True)
    store.candidates_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_candidate(**kwargs) -> CandidateRecord:
    base = {
        "symbol": "SOXS.US",
        "selected_at": "2026-07-10T21:32:41+08:00",
        "source": "ai_selector",
        "ai_score": 91.5,
        "ai_reason": "Strong range fit",
        "asset_type": "inverse_etf",
        "benchmarks": ("SOXX.US", "SMH.US"),
        "strategy_family": "range_etf",
        "risk_profile": "strict",
        "timeframe": "15m",
        "market": "US",
        "metadata": {"source": "unit_test"},
    }
    base.update(kwargs)
    return CandidateRecord.from_ai_candidate(**base)


def test_new_candidate_defaults_are_safe_and_disabled():
    candidate = _make_candidate()

    assert candidate.validation_status == ValidationStatus.AI_CANDIDATE.value
    assert candidate.trading_enabled is False
    assert candidate.shadow_enabled is False
    assert candidate.paper_enabled is False
    assert candidate.live_enabled is False
    assert candidate.deployment_status == DeploymentStatus.INELIGIBLE.value
    assert candidate.evidence_status == EvidenceStatus.INSUFFICIENT_EVIDENCE.value
    assert candidate.profitability_status == ProfitabilityStatus.INELIGIBLE.value
    assert candidate.validate_initial_state() == []


@pytest.mark.parametrize(
    "new_status",
    [ValidationStatus.SHADOW_OBSERVING, ValidationStatus.PAPER_ELIGIBLE, ValidationStatus.LIVE_ELIGIBLE],
)
def test_ai_candidate_cannot_skip_to_shadow_or_paper(new_status):
    assert is_valid_transition(ValidationStatus.AI_CANDIDATE.value, new_status.value) is False
    with pytest.raises(CandidateTransitionError):
        from src.candidate_validation import assert_transition_allowed

        assert_transition_allowed(ValidationStatus.AI_CANDIDATE.value, new_status.value)


def test_benchmark_empty_blocks_data_validation(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = CandidateRecord(
        candidate_id="",
        symbol="AAPL.US",
        market="US",
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=88.1,
        ai_reason="price near support",
        asset_type="common_stock",
        benchmarks=(),
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        timeframe="15m",
    )
    _write_latest_candidate(
        store,
        {
            **candidate.to_dict(),
            "validation_status": ValidationStatus.CLASSIFIED.value,
        },
    )

    with pytest.raises(CandidateTransitionError, match="benchmark_required"):
        store.transition(
            candidate.candidate_id,
            ValidationStatus.BENCHMARK_ASSIGNED,
            metadata={},
        )


def test_strategy_family_empty_blocks_data_validation(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = CandidateRecord(
        candidate_id="",
        symbol="AAPL.US",
        market="US",
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=88.1,
        ai_reason="price near support",
        asset_type="common_stock",
        benchmarks=("QQQ.US", "SPY.US"),
        strategy_family="",
        risk_profile="balanced",
        timeframe="15m",
    )
    _write_latest_candidate(
        store,
        {
            **candidate.to_dict(),
            "validation_status": ValidationStatus.BENCHMARK_ASSIGNED.value,
        },
    )

    with pytest.raises(CandidateTransitionError, match="strategy_family_required"):
        store.transition(
            candidate.candidate_id,
            ValidationStatus.STRATEGY_ASSIGNED,
            metadata={},
        )


@pytest.mark.parametrize("asset_type", ["inverse_etf", "leveraged_etf"])
def test_leveraged_or_inverse_etf_missing_risk_profile_rejected(asset_type):
    candidate = CandidateRecord(
        candidate_id="",
        symbol="SOXS.US" if asset_type == "inverse_etf" else "SOXL.US",
        market="US",
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=90.0,
        ai_reason="risk profile missing",
        asset_type=asset_type,
        benchmarks=("SOXX.US", "SMH.US"),
        strategy_family="range_etf",
        risk_profile="balanced",
        timeframe="15m",
    )
    errors = candidate.validate_initial_state()
    assert "missing_or_weak_risk_profile" in errors

    store = CandidateValidationStore(Path("/tmp") / f"candidate_validation_{asset_type}")
    with pytest.raises(CandidateTransitionError, match="missing_or_weak_risk_profile"):
        store.save_candidates([candidate])


def test_data_invalid_cannot_enter_backtest(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = _make_candidate()
    store.save_candidates([candidate])
    record = store.transition(
        candidate.candidate_id,
        ValidationStatus.CLASSIFIED,
        metadata={},
    )
    record = store.transition(
        candidate.candidate_id,
        ValidationStatus.BENCHMARK_ASSIGNED,
        metadata={},
    )
    record = store.transition(
        candidate.candidate_id,
        ValidationStatus.STRATEGY_ASSIGNED,
        metadata={},
    )
    record = store.transition(
        candidate.candidate_id,
        ValidationStatus.PENDING_DATA_VALIDATION,
        metadata={},
    )
    record = store.transition(
        candidate.candidate_id,
        ValidationStatus.DATA_INVALID,
        reason="benchmark_invalid",
        metadata={"reason": "benchmark_invalid"},
    )
    assert record.validation_status == ValidationStatus.DATA_INVALID.value
    with pytest.raises(CandidateTransitionError):
        store.transition(
            candidate.candidate_id,
            ValidationStatus.PENDING_BACKTEST,
            metadata={"data_status": ValidationStatus.DATA_INVALID.value},
        )


def test_reconciliation_non_ok_blocks_shadow(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = _make_candidate()
    store.save_candidates([candidate])
    for status in [
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
        ValidationStatus.DATA_VALID,
        ValidationStatus.PENDING_BACKTEST,
        ValidationStatus.BACKTEST_COMPLETE,
        ValidationStatus.PENDING_WALK_FORWARD,
        ValidationStatus.WALK_FORWARD_COMPLETE,
    ]:
        metadata = {
            "benchmark_valid": True,
            "eligible_for_backtest": True,
            "future_data_risk": False,
            "reconciliation_status": "OK",
            "backtest_complete": True,
            "walk_forward_complete": True,
        }
        if status == ValidationStatus.PENDING_WALK_FORWARD:
            metadata = {"backtest_status": ValidationStatus.BACKTEST_COMPLETE.value}
        elif status == ValidationStatus.WALK_FORWARD_COMPLETE:
            metadata = {"walk_forward_complete": True}
        elif status == ValidationStatus.PENDING_BACKTEST:
            metadata = {"data_status": ValidationStatus.DATA_VALID.value}
        store.transition(candidate.candidate_id, status, metadata=metadata)

    with pytest.raises(CandidateTransitionError, match="reconciliation_must_be_ok"):
        store.transition(
            candidate.candidate_id,
            ValidationStatus.PENDING_SHADOW,
            metadata={
                "dataset_benchmark_status": "VALID",
                "eligible_for_backtest": True,
                "future_data_risk": False,
                "reconciliation_status": "FAILED",
                "evidence_status": EvidenceStatus.ELIGIBLE.value,
                "backtest_status": ValidationStatus.BACKTEST_COMPLETE.value,
                "walk_forward_status": ValidationStatus.WALK_FORWARD_COMPLETE.value,
            },
        )


def test_profitability_not_eligible_blocks_paper(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = _make_candidate()
    store.save_candidates([candidate])
    _write_latest_candidate(
        store,
        {
            **candidate.to_dict(),
            "validation_status": ValidationStatus.SHADOW_COMPLETE.value,
            "evidence_status": EvidenceStatus.ELIGIBLE.value,
            "profitability_status": ProfitabilityStatus.INELIGIBLE.value,
            "deployment_status": DeploymentStatus.INELIGIBLE.value,
        },
    )
    with pytest.raises(CandidateTransitionError, match="profitability_not_eligible"):
        store.transition(
            candidate.candidate_id,
            ValidationStatus.PAPER_ELIGIBLE,
            metadata={
                "evidence_status": EvidenceStatus.ELIGIBLE.value,
                "profitability_status": ProfitabilityStatus.INELIGIBLE.value,
                "deployment_status": DeploymentStatus.ELIGIBLE.value,
                "aggregate_oos_return": 0.01,
                "benchmark_sensitive": False,
                "shadow_complete": True,
                "shadow_reconciliation_status": "OK",
                "broker_write_called": False,
                "safety_alert": False,
            },
        )


def test_human_approval_required_for_live(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = _make_candidate()
    store.save_candidates([candidate])
    _write_latest_candidate(
        store,
        {
            **candidate.to_dict(),
            "validation_status": ValidationStatus.PAPER_ELIGIBLE.value,
            "evidence_status": EvidenceStatus.ELIGIBLE.value,
            "profitability_status": ProfitabilityStatus.ELIGIBLE.value,
            "deployment_status": DeploymentStatus.ELIGIBLE.value,
        },
    )
    with pytest.raises(CandidateTransitionError, match="human_approval_required"):
        store.transition(
            candidate.candidate_id,
            ValidationStatus.LIVE_ELIGIBLE,
            metadata={
                "paper_status": ValidationStatus.PAPER_ELIGIBLE.value,
                "paper_approved_by_human": False,
                "paper_total_return": 0.08,
                "paper_max_drawdown": 0.03,
                "paper_max_drawdown_limit": 0.2,
            },
        )


def test_history_is_appended_and_summary_written(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    candidate = _make_candidate()
    store.save_candidates([candidate])
    store.transition(candidate.candidate_id, ValidationStatus.CLASSIFIED, metadata={})
    store.transition(candidate.candidate_id, ValidationStatus.BENCHMARK_ASSIGNED, metadata={})

    history = store.load_latest_history()
    assert len(history) >= 3
    assert store.summary_path.exists()
    summary = store.summary_path.read_text(encoding="utf-8")
    assert "candidate_id" in summary
    assert candidate.candidate_id in summary


def test_ingest_ai_selection_report_creates_candidate_records_and_keeps_disabled_flags_false(tmp_path):
    store = CandidateValidationStore(tmp_path / "candidates")
    report = {
        "generated_at": "2026-07-10T21:32:41+08:00",
        "timestamp": "2026-07-10T21:32:41+08:00",
        "settings": {"top_n": 2, "selection_stage": "fast_preliminary"},
        "top3": [
            {
                "symbol": "SOXS.US",
                "ai_score": 92.5,
                "ai_reason": "inverse etf fit",
                "asset_type": "inverse_etf",
                "benchmarks": ["SOXX.US", "SMH.US"],
                "strategy_family": "range_etf",
                "risk_profile": "strict",
                "timeframe": "15m",
                "fallback_used": False,
            },
            {
                "ticker": "AAPL.US",
                "final_score": 81.2,
                "reason": "equity fit",
                "asset_type": "common_stock",
                "benchmark_symbols": ["QQQ.US", "SPY.US"],
                "strategy_family": "equity_mean_reversion",
                "risk_profile": "balanced",
                "timeframe": "15m",
            },
        ],
    }

    records = store.ingest_ai_selection_report(report)

    assert len(records) == 2
    loaded = store.load_latest_candidates()
    assert len(loaded) == 2
    assert all(record.trading_enabled is False for record in loaded)
    assert all(record.shadow_enabled is False for record in loaded)
    assert all(record.paper_enabled is False for record in loaded)
    assert all(record.live_enabled is False for record in loaded)
    assert all(record.deployment_status == DeploymentStatus.INELIGIBLE.value for record in loaded)
    assert store.history_path.exists()
    assert store.summary_path.exists()
    assert any(row.symbol == "SOXS.US" for row in loaded)
