"""Tests for the Learning Governance system.

All tests verify that proposals NEVER auto-activate, and that
ACTIVE requires approved_by_human=True with a non-empty reason.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.candidate_validation.model_governance import (
    CandidateModelManifest,
    CandidateModelRegistry,
    CandidateModelStatus,
)
from src.outcome.governance import (
    LearningGovernance,
    LearningProposal,
    _append_audit,
    _utc_now_iso,
    GOVERNANCE_AUDIT_PATH,
    PROPOSAL_REGISTRY_DIR,
    WEIGHTS_PATH,
)


def _td_paths(tmp_path: Path):
    """Set up isolated governance paths."""
    reg = tmp_path / "candidate_models"
    reg.mkdir(parents=True)
    idx = tmp_path / "proposal_index.json"
    audit = tmp_path / "governance_audit.jsonl"
    return {"registry": reg, "index": idx, "audit": audit}


# ═══════════════════════════════════════════════════════════════════════
# Proposal dataclass
# ═══════════════════════════════════════════════════════════════════════

class TestProposal:
    def test_always_pending_on_creation(self):
        p = LearningProposal(
            proposal_id="prop-1", proposal_type="weight_suggestion",
            model_version="v1", source="weight_advisor",
        )
        assert p.approval_status == "PENDING_HUMAN_APPROVAL"
        assert p.approved_by_human is False

    def test_roundtrip(self):
        p = LearningProposal(
            proposal_id="prop-a", proposal_type="weight_suggestion",
            model_version="w_2026", source="weight_advisor",
            sample_count=30, improved=True,
            proposed_weights={"v": 0.35}, baseline_weights={"v": 0.30},
        )
        d = p.to_dict()
        p2 = LearningProposal.from_dict(d)
        assert p2.proposal_id == p.proposal_id
        assert p2.approval_status == "PENDING_HUMAN_APPROVAL"
        assert p2.improved is True
        assert p2.proposed_weights == {"v": 0.35}


# ═══════════════════════════════════════════════════════════════════════
# Governance lifecycle
# ═══════════════════════════════════════════════════════════════════════

class TestGovernanceLifecycle:
    @pytest.fixture(autouse=True)
    def _isolate_governance(self, tmp_path: Path, monkeypatch):
        """Isolate all governance I/O to tmp_path."""
        self._paths = _td_paths(tmp_path)
        monkeypatch.setattr("src.outcome.governance.GOVERNANCE_AUDIT_PATH", self._paths["audit"])
        monkeypatch.setattr("src.outcome.governance.PROPOSAL_REGISTRY_DIR", self._paths["registry"])
        monkeypatch.setattr("src.outcome.governance.LEARNING_DIR", tmp_path)
        monkeypatch.setattr("src.outcome.governance.WEIGHTS_PATH", tmp_path / "suggested_weights.json")

    def _gov(self):
        return LearningGovernance(registry_root=self._paths["registry"],
                                  proposal_index_path=self._paths["index"])

    def test_list_empty(self):
        gov = self._gov()
        assert gov.list_proposals() == []

    def test_register_proposal_dry_run(self):
        gov = self._gov()
        # Dry-run should not write anything
        # Mock weight advisor to return completed
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 25,
            "proposed_weights": {"volatility_score": 0.35, "volume_score": 0.15,
                                 "trend_fit_score": 0.25, "repeatability_score": 0.10,
                                 "drawdown_safety_score": 0.10, "correlation_bonus": 0.05},
            "baseline_weights": {"volatility_score": 0.30, "volume_score": 0.20,
                                 "trend_fit_score": 0.20, "repeatability_score": 0.15,
                                 "drawdown_safety_score": 0.10, "correlation_bonus": 0.05},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "test", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=True)
            assert p is not None
            assert p.approval_status == "PENDING_HUMAN_APPROVAL"
            assert p.approved_by_human is False
            # Index should be empty (dry-run)
            assert gov.list_proposals() == []

    def test_register_proposal_apply(self):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "test", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None
            assert p.approval_status == "PENDING_HUMAN_APPROVAL"
            assert len(gov.list_proposals()) == 1

    def test_register_insufficient_data(self, tmp_path: Path):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "INSUFFICIENT_DATA", "sample_size": 5,
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is None



    def test_accept_moves_to_review(self):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "test", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None
            pid = p.proposal_id

        # Accept
        r = gov.accept_proposal(pid, reason="looks good")
        assert r["status"] == "OK"
        assert r["new_status"] == CandidateModelStatus.REVIEW_REQUIRED.value

    def test_accept_not_found(self, tmp_path: Path):
        gov = self._gov()
        r = gov.accept_proposal("nonexistent")
        assert r["status"] == "NOT_FOUND"

    def test_approve_requires_reason(self, tmp_path: Path):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None
            gov.accept_proposal(p.proposal_id, reason="accepted")
            r = gov.approve_proposal(p.proposal_id, reason="")  # empty reason
            assert r["status"] == "MISSING_REASON"
            assert "non-empty reason" in r["error"].lower()

    def test_approve_full_lifecycle(self, tmp_path: Path):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None
            pid = p.proposal_id

        # Accept
        r = gov.accept_proposal(pid, reason="ready for review")
        assert r["status"] == "OK"

        # Approve with reason
        r = gov.approve_proposal(pid, reason="Outperforms baseline on 30 samples: R² +0.06, hit rate +7%")
        assert r["status"] == "OK"
        assert r["new_status"] == CandidateModelStatus.ACTIVE.value
        assert r["champion_activated"] is True

        # Verify in registry
        active = gov.active_champion()
        assert active is not None

    def test_approve_from_pending_fails(self):
        """Cannot approve directly from PENDING_HUMAN_APPROVAL without accept step."""
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None

        r = gov.approve_proposal(p.proposal_id, reason="skip review")
        assert r["status"] == "INVALID_STATE"

    def test_reject_proposal(self, tmp_path: Path):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 25,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": False, "baseline": {}, "proposed": {}},
            "explanation": "", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None

        r = gov.reject_proposal(p.proposal_id, reason="no improvement over baseline")
        assert r["status"] == "OK"
        assert r["new_status"] == CandidateModelStatus.REJECTED.value

    def test_cannot_reject_active(self, tmp_path: Path):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "", "feature_importances": {},
        }):
            p = gov.register_weight_proposal(dry_run=False)
            assert p is not None
            gov.accept_proposal(p.proposal_id, reason="ok")
            gov.approve_proposal(p.proposal_id, reason="good model")

        r = gov.reject_proposal(p.proposal_id, reason="changed mind")
        assert r["status"] == "INVALID_STATE"

    def test_governance_snapshot(self, tmp_path: Path):
        gov = self._gov()
        snap = gov.governance_snapshot()
        assert snap["human_approval_required"] is True
        assert snap["auto_activation_blocked"] is True
        assert snap["champion_version"] == "baseline_v1"

    def test_duplicate_registration_idempotent(self, tmp_path: Path):
        gov = self._gov()
        with patch("src.outcome.governance.run_weight_advisor", return_value={
            "status": "COMPLETED", "sample_size": 30,
            "proposed_weights": {"volatility_score": 0.35},
            "baseline_weights": {"volatility_score": 0.30},
            "evaluation_metrics": {"improved": True, "baseline": {}, "proposed": {}},
            "explanation": "", "feature_importances": {},
        }):
            p1 = gov.register_weight_proposal(dry_run=False)
            p2 = gov.register_weight_proposal(dry_run=False)
        assert p1 is not None and p2 is not None
        # Should be idempotent — same model version replaces
        assert len(gov.list_proposals()) == 1


# ═══════════════════════════════════════════════════════════════════════
# Safety: CandidateModelManifest state machine
# ═══════════════════════════════════════════════════════════════════════

class TestManifestStateMachine:
    def test_activate_without_human_approval_fails(self, tmp_path: Path):
        paths = _td_paths(tmp_path)
        reg = CandidateModelRegistry(root_dir=paths["registry"])
        m = CandidateModelManifest(
            model_version="test_v1", parent_version="baseline_v1",
            sample_count=40, weights={"v": 0.3},
            status=CandidateModelStatus.DRAFT.value,
        )
        # Move through the chain
        m = m.transition(CandidateModelStatus.BACKTESTED)
        m = m.transition(CandidateModelStatus.WALK_FORWARD_VALIDATED)
        m = m.transition(CandidateModelStatus.REVIEW_REQUIRED)
        m = m.transition(CandidateModelStatus.APPROVED, approved_by_human=True, reason="test")
        # Transition to ACTIVE without human approval
        with pytest.raises(ValueError, match="invalid_model_transition"):
            m.transition(CandidateModelStatus.ACTIVE, approved_by_human=False)

    def test_activate_with_human_approval_succeeds(self, tmp_path: Path):
        paths = _td_paths(tmp_path)
        reg = CandidateModelRegistry(root_dir=paths["registry"])
        m = CandidateModelManifest(
            model_version="test_v2", parent_version="baseline_v1",
            sample_count=40, weights={"v": 0.35},
            status=CandidateModelStatus.DRAFT.value,
        )
        m = m.transition(CandidateModelStatus.BACKTESTED)
        m = m.transition(CandidateModelStatus.WALK_FORWARD_VALIDATED)
        m = m.transition(CandidateModelStatus.REVIEW_REQUIRED)
        m = m.transition(CandidateModelStatus.APPROVED, approved_by_human=True, reason="test")
        m = m.transition(CandidateModelStatus.ACTIVE, approved_by_human=True, reason="approved by human")
        assert m.status == CandidateModelStatus.ACTIVE.value
        assert m.approved_by_human is True

    def test_cannot_skip_review(self):
        m = CandidateModelManifest(model_version="v1", sample_count=1)
        # DRAFT → ACTIVE is not allowed
        with pytest.raises(ValueError, match="invalid_model_transition"):
            m.transition(CandidateModelStatus.ACTIVE, approved_by_human=True, reason="skip")

    def test_cannot_skip_to_active(self, tmp_path: Path):
        """DRAFT → ACTIVE is never allowed even with human approval."""
        m = CandidateModelManifest(model_version="v1")
        with pytest.raises(ValueError, match="invalid_model_transition"):
            m.transition(CandidateModelStatus.ACTIVE, approved_by_human=True, reason="skip")


# ═══════════════════════════════════════════════════════════════════════
# Safety: no production impact
# ═══════════════════════════════════════════════════════════════════════

class TestSafetyBoundaries:
    def test_no_broker_imports(self):
        source = Path(__import__("src.outcome.governance").__file__).read_text()
        assert "LongBridgeBroker" not in source
        assert "PaperBroker" not in source
        assert "TradeContext" not in source
        assert "place_order" not in source
        assert "submit_order" not in source

    def test_no_selector_modification(self):
        source = Path(__import__("src.outcome.governance").__file__).read_text()
        assert "write_top_configs" not in source
        assert "write_selection_state" not in source
        assert "persist_selection_bundle" not in source
        assert "selected_symbols" not in source

    def test_no_live_gate_modification(self):
        source = Path(__import__("src.outcome.governance").__file__).read_text()
        assert "allow_live_order" not in source
        assert "reduce_only" not in source

    def test_no_trading_engine_reference(self):
        source = Path(__import__("src.outcome.governance").__file__).read_text()
        assert "TradingEngine" not in source
        assert "RiskManager" not in source
        assert "PortfolioManager" not in source


def run_test_direct():
    import tempfile
    td = Path(tempfile.mkdtemp())
    try:
        TestProposal().test_always_pending_on_creation()
        TestProposal().test_roundtrip()
        TestGovernanceLifecycle().test_list_empty(td)
        TestGovernanceLifecycle().test_register_proposal_dry_run(td)
        TestGovernanceLifecycle().test_register_proposal_apply(td)
        TestGovernanceLifecycle().test_register_insufficient_data(td)
        TestGovernanceLifecycle().test_accept_moves_to_review(td)
        TestGovernanceLifecycle().test_accept_not_found(td)
        TestGovernanceLifecycle().test_approve_requires_reason(td)
        TestGovernanceLifecycle().test_approve_full_lifecycle(td)
        TestGovernanceLifecycle().test_approve_from_pending_fails(td)
        TestGovernanceLifecycle().test_reject_proposal(td)
        TestGovernanceLifecycle().test_cannot_reject_active(td)
        TestGovernanceLifecycle().test_governance_snapshot(td)
        TestGovernanceLifecycle().test_duplicate_registration_idempotent(td)
        TestManifestStateMachine().test_activate_without_human_approval_fails(td)
        TestManifestStateMachine().test_activate_with_human_approval_succeeds(td)
        TestManifestStateMachine().test_cannot_skip_review()
        TestManifestStateMachine().test_cannot_skip_to_active(td)
        TestSafetyBoundaries().test_no_broker_imports()
        TestSafetyBoundaries().test_no_selector_modification()
        TestSafetyBoundaries().test_no_live_gate_modification()
        TestSafetyBoundaries().test_no_trading_engine_reference()
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()
