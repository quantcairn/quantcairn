"""Learning Governance — proposal registry and human-approval gate.

All model proposals (weight suggestions, regime evaluations, etc.)
must remain in PENDING_HUMAN_APPROVAL until explicitly approved by a
human.  This module enforces that boundary at every code path.

Key invariants:
  - proposal.approval_status never changes without explicit human action
  - ACTIVE transitions require approved_by_human=True
  - Production selector weights are never auto-modified
  - Full audit trail for every governance action
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.candidate_validation.model_governance import (
    CandidateModelManifest,
    CandidateModelRegistry,
    CandidateModelStatus,
)
from src.outcome.weight_advisor import (
    BASELINE_WEIGHTS,
    FEATURE_NAMES,
    MIN_SAMPLE_SIZE,
    MODEL_VERSION,
    run_weight_advisor,
)
from src.config.runtime_paths import resolve_artifacts_dir

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_DIR = resolve_artifacts_dir(PROJECT_DIR) / "learning"
GOVERNANCE_AUDIT_PATH = LEARNING_DIR / "governance_audit.jsonl"
WEIGHTS_PATH = LEARNING_DIR / "suggested_weights.json"
PROPOSAL_REGISTRY_DIR = PROJECT_DIR / "config" / "candidate_models"
GOVERNANCE_LOCK_PATH = LEARNING_DIR / ".governance.lock"

# ═══════════════════════════════════════════════════════════════════════════
# Governance audit
# ═══════════════════════════════════════════════════════════════════════════

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _append_audit(event: dict[str, Any]) -> None:
    """Append a governance event to the audit log."""
    GOVERNANCE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "event_id": uuid.uuid4().hex[:12],
        "timestamp": _utc_now_iso(),
        "pid": os.getpid(),
        **event,
    }
    fd, tmp = tempfile.mkstemp(
        prefix=".gov_audit.", suffix=".tmp", dir=str(GOVERNANCE_AUDIT_PATH.parent)
    )
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            existing = ""
            if GOVERNANCE_AUDIT_PATH.exists():
                try:
                    existing = GOVERNANCE_AUDIT_PATH.read_text(encoding="utf-8")
                except Exception:
                    pass
            # Write atomically: existing + new line
            path_tmp = str(Path(tmp))
            with open(path_tmp, "w", encoding="utf-8") as out:
                out.write(existing)
                out.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            os.replace(path_tmp, str(GOVERNANCE_AUDIT_PATH))
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Proposal types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class LearningProposal:
    """A single proposal registered in the governance system."""

    proposal_id: str
    proposal_type: str  # "weight_suggestion", "regime_shadow", etc.
    model_version: str
    source: str  # "weight_advisor", "regime_evaluator", etc.
    created_at: str = field(default_factory=_utc_now_iso)
    approval_status: str = "PENDING_HUMAN_APPROVAL"
    approved_by_human: bool = False
    approval_reason: str = ""
    manifest_path: str = ""
    manifest_hash: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    parent_version: str | None = None
    sample_count: int = 0
    proposed_weights: dict[str, float] = field(default_factory=dict)
    baseline_weights: dict[str, float] = field(default_factory=dict)
    evaluation_metrics: dict[str, Any] = field(default_factory=dict)
    improved: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_type": self.proposal_type,
            "model_version": self.model_version,
            "source": self.source,
            "created_at": self.created_at,
            "approval_status": self.approval_status,
            "approved_by_human": self.approved_by_human,
            "approval_reason": self.approval_reason,
            "manifest_path": self.manifest_path,
            "manifest_hash": self.manifest_hash,
            "summary": dict(self.summary),
            "parent_version": self.parent_version,
            "sample_count": self.sample_count,
            "proposed_weights": dict(self.proposed_weights),
            "baseline_weights": dict(self.baseline_weights),
            "evaluation_metrics": dict(self.evaluation_metrics),
            "improved": self.improved,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LearningProposal":
        return cls(
            proposal_id=_safe_text(d.get("proposal_id") or ""),
            proposal_type=_safe_text(d.get("proposal_type") or "weight_suggestion"),
            model_version=_safe_text(d.get("model_version") or ""),
            source=_safe_text(d.get("source") or ""),
            created_at=_safe_text(d.get("created_at") or ""),
            approval_status=_safe_text(d.get("approval_status") or "PENDING_HUMAN_APPROVAL").upper(),
            approved_by_human=bool(d.get("approved_by_human", False)),
            approval_reason=_safe_text(d.get("approval_reason") or ""),
            manifest_path=_safe_text(d.get("manifest_path") or ""),
            manifest_hash=_safe_text(d.get("manifest_hash") or ""),
            summary=dict(d.get("summary") or {}),
            parent_version=_safe_text(d.get("parent_version") or "") or None,
            sample_count=int(d.get("sample_count", 0) or 0),
            proposed_weights={str(k): float(v or 0) for k, v in dict(d.get("proposed_weights") or {}).items()},
            baseline_weights={str(k): float(v or 0) for k, v in dict(d.get("baseline_weights") or {}).items()},
            evaluation_metrics=dict(d.get("evaluation_metrics") or {}),
            improved=bool(d.get("improved", False)),
            notes=_safe_text(d.get("notes") or ""),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Proposal registry
# ═══════════════════════════════════════════════════════════════════════════

class LearningGovernance:
    """Central governance for all learning proposals.

    Reads Weight Advisor output and registers proposals as
    CandidateModelManifests.  All state transitions require explicit
    approval_reason and the ACTIVE state is gated on
    approved_by_human=True.
    """

    def __init__(
        self,
        *,
        registry_root: str | Path | None = None,
        proposal_index_path: str | Path | None = None,
    ) -> None:
        self.registry = CandidateModelRegistry(
            root_dir=Path(registry_root) if registry_root else PROPOSAL_REGISTRY_DIR
        )
        self.proposal_index_path = (
            Path(proposal_index_path) if proposal_index_path else LEARNING_DIR / "proposal_index.json"
        )

    # ── Proposal index ──────────────────────────────────────────────────

    def _load_proposals(self) -> list[LearningProposal]:
        if not self.proposal_index_path.exists():
            return []
        try:
            data = json.loads(self.proposal_index_path.read_text(encoding="utf-8"))
            items = data.get("proposals") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return []
            return [LearningProposal.from_dict(item) for item in items if isinstance(item, dict)]
        except Exception:
            return []

    def _save_proposals(self, proposals: list[LearningProposal]) -> None:
        self.proposal_index_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".proposals.", suffix=".tmp", dir=str(self.proposal_index_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "proposals": [p.to_dict() for p in proposals],
                        "updated_at": _utc_now_iso(),
                    },
                    f, ensure_ascii=False, indent=2, sort_keys=True, default=str,
                )
            os.replace(tmp, self.proposal_index_path)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _find_existing(self, proposals: list[LearningProposal], proposal_id: str) -> int:
        for i, p in enumerate(proposals):
            if p.proposal_id == proposal_id:
                return i
        return -1

    # ── Registration ────────────────────────────────────────────────────

    def register_weight_proposal(
        self,
        *,
        dry_run: bool = False,
    ) -> LearningProposal | None:
        """Read suggested_weights.json and register it as a governance proposal.

        Returns None when data is insufficient or weights file is missing.
        Never auto-activates — status remains PENDING_HUMAN_APPROVAL.
        """
        # Run the weight advisor to get fresh data
        wa_result = run_weight_advisor(dry_run=True)
        if wa_result.get("status") != "COMPLETED":
            return None

        wa_weight_dict = wa_result.get("proposed_weights") or {}
        wa_baseline_dict = wa_result.get("baseline_weights") or BASELINE_WEIGHTS

        proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
        model_version = f"weight_advisor_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        sample_count = int(wa_result.get("sample_size", 0) or 0)

        proposal = LearningProposal(
            proposal_id=proposal_id,
            proposal_type="weight_suggestion",
            model_version=model_version,
            source="weight_advisor",
            sample_count=sample_count,
            proposed_weights={str(k): float(v or 0) for k, v in wa_weight_dict.items()},
            baseline_weights={str(k): float(v or 0) for k, v in wa_baseline_dict.items()},
            evaluation_metrics=dict(wa_result.get("evaluation_metrics") or {}),
            improved=bool((wa_result.get("evaluation_metrics") or {}).get("improved", False)),
            summary={
                "sample_count": sample_count,
                "feature_importances": dict(wa_result.get("feature_importances") or {}),
                "explanation": str(wa_result.get("explanation") or "")[:200],
            },
            notes=str(wa_result.get("explanation") or ""),
        )

        # Register as a CandidateModelManifest for compatibility with existing tools
        if not dry_run:
            self._sync_to_manifest(proposal)

        # Append to proposal index
        if not dry_run:
            proposals = self._load_proposals()
            # Deduplicate — replace if same model version already exists
            existing = [p for p in proposals if p.model_version == model_version]
            if existing:
                proposals = [p for p in proposals if p.model_version != model_version]
            proposals.append(proposal)
            self._save_proposals(proposals)

            _append_audit({
                "action": "register_proposal",
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "model_version": proposal.model_version,
                "approval_status": proposal.approval_status,
                "sample_count": proposal.sample_count,
                "improved": proposal.improved,
                "source": proposal.source,
            })

        return proposal

    def _sync_to_manifest(self, proposal: LearningProposal) -> CandidateModelManifest:
        """Write proposal data as a CandidateModelManifest YAML for backward compat.

        Weight Advisor proposals are backed by real outcome data, so they
        start at BACKTESTED in the manifest state machine.  This allows
        accept() → REVIEW_REQUIRED without requiring a separate backtest step.
        """
        initial_status = (
            CandidateModelStatus.BACKTESTED.value
            if proposal.source == "weight_advisor"
            else CandidateModelStatus.DRAFT.value
        )
        manifest = CandidateModelManifest(
            model_version=proposal.model_version,
            parent_version="baseline_v1",
            sample_count=proposal.sample_count,
            feature_names=list(FEATURE_NAMES),
            weights=proposal.proposed_weights,
            target_definition="pnl_pct",
            validation_metrics=proposal.evaluation_metrics,
            approval_status=initial_status,
            status=initial_status,
            approved_by_human=False,
            notes=f"Auto-generated from {proposal.source}. Proposal ID: {proposal.proposal_id}",
            challenger=True,
        )
        path = self.registry.save_manifest(manifest)
        proposal.manifest_path = str(path)
        return manifest

    # ── Governance actions ──────────────────────────────────────────────

    def list_proposals(self) -> list[dict[str, Any]]:
        """Return all proposals with their current governance status."""
        proposals = self._load_proposals()
        # Also check registry for manifests
        manifests = self.registry.list_manifests()
        manifest_map = {m.model_version: m for m in manifests}

        result: list[dict[str, Any]] = []
        for p in proposals:
            entry = {**p.to_dict()}
            m = manifest_map.get(p.model_version)
            if m:
                entry["manifest_status"] = m.status
                entry["manifest_champion"] = m.champion
                entry["manifest_challenger"] = m.challenger
            else:
                entry["manifest_status"] = "no_manifest"
            result.append(entry)
        return result

    def active_champion(self) -> dict[str, Any] | None:
        """Return the currently active (production) champion model."""
        snapshot = self.registry.active_champion_challenger_snapshot()
        active_model = snapshot.get("active_model")
        if isinstance(active_model, dict):
            return dict(active_model)
        return None

    def latest_challenger(self) -> dict[str, Any] | None:
        """Return the most recent challenger proposal."""
        snapshot = self.registry.active_champion_challenger_snapshot()
        challenger = snapshot.get("challenger_model")
        if isinstance(challenger, dict):
            return dict(challenger)
        # Fallback: check proposals index
        proposals = self._load_proposals()
        if proposals:
            latest = proposals[-1]
            return latest.to_dict()
        return None

    def accept_proposal(
        self,
        proposal_id: str,
        *,
        reason: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Move a proposal to REVIEW_REQUIRED (ready for human approval).

        Does NOT activate — that requires the separate approve() call.
        """
        proposals = self._load_proposals()
        idx = self._find_existing(proposals, proposal_id)
        if idx < 0:
            return {"status": "NOT_FOUND", "proposal_id": proposal_id}

        proposal = proposals[idx]
        if proposal.approval_status not in {"PENDING_HUMAN_APPROVAL", "DRAFT"}:
            return {
                "status": "INVALID_STATE",
                "proposal_id": proposal_id,
                "current_status": proposal.approval_status,
                "error": f"Cannot accept from {proposal.approval_status}",
            }

        if not dry_run:
            proposal.approval_status = CandidateModelStatus.REVIEW_REQUIRED.value
            proposal.approval_reason = reason or "accepted_for_review"
            proposals[idx] = proposal
            self._save_proposals(proposals)

            # Also update manifest
            manifest = self.registry.load_manifest(proposal.model_version)
            if manifest:
                manifest = manifest.transition(
                    CandidateModelStatus.REVIEW_REQUIRED,
                    approved_by_human=False,  # Accept is not approval
                    reason=reason or "accepted_for_review",
                )
                self.registry.save_manifest(manifest)

            _append_audit({
                "action": "accept_proposal",
                "proposal_id": proposal_id,
                "model_version": proposal.model_version,
                "new_status": CandidateModelStatus.REVIEW_REQUIRED.value,
                "reason": reason or "accepted_for_review",
            })

        return {
            "status": "OK",
            "proposal_id": proposal_id,
            "model_version": proposal.model_version,
            "new_status": CandidateModelStatus.REVIEW_REQUIRED.value,
            "next_step": "human_approval_required",
        }

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        reason: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Human approves and activates a proposal as the new champion.

        This is the ONLY path to ACTIVE.  Requires explicit call with
        approval_reason — the code path is never auto-triggered.

        Retires the previous ACTIVE champion.
        """
        proposals = self._load_proposals()
        idx = self._find_existing(proposals, proposal_id)
        if idx < 0:
            return {"status": "NOT_FOUND", "proposal_id": proposal_id}

        proposal = proposals[idx]
        if proposal.approval_status not in {
            CandidateModelStatus.REVIEW_REQUIRED.value,
            CandidateModelStatus.BACKTESTED.value,
            CandidateModelStatus.WALK_FORWARD_VALIDATED.value,
        }:
            return {
                "status": "INVALID_STATE",
                "proposal_id": proposal_id,
                "current_status": proposal.approval_status,
                "error": f"Cannot approve from {proposal.approval_status} — must be accepted first",
            }

        if not reason.strip():
            return {
                "status": "MISSING_REASON",
                "proposal_id": proposal_id,
                "error": "Human approval requires a non-empty reason",
            }

        if not dry_run:
            # 1. Retire existing active champion
            existing_active = self.registry.active_model()
            if existing_active:
                existing_active = existing_active.transition(
                    CandidateModelStatus.RETIRED,
                    approved_by_human=True,
                    reason=f"replaced_by:{proposal.model_version}",
                )
                self.registry.save_manifest(existing_active)

            # 2. Transition through APPROVED → ACTIVE with human approval
            manifest = self.registry.load_manifest(proposal.model_version)
            if manifest is None:
                # Need to create manifest first
                manifest = self._sync_to_manifest(proposal)
                # Re-load to get fresh state
                manifest = self.registry.load_manifest(proposal.model_version)
                if manifest is None:
                    return {"status": "ERROR", "error": "Failed to create manifest"}

            # If not at REVIEW_REQUIRED yet, move there first
            if manifest.status not in {
                CandidateModelStatus.REVIEW_REQUIRED.value,
                CandidateModelStatus.BACKTESTED.value,
                CandidateModelStatus.WALK_FORWARD_VALIDATED.value,
            }:
                manifest = manifest.transition(
                    CandidateModelStatus.REVIEW_REQUIRED,
                    approved_by_human=False,
                    reason="pre_approval",
                )

            # APPROVED
            manifest = manifest.transition(
                CandidateModelStatus.APPROVED,
                approved_by_human=True,
                reason=reason,
            )
            # ACTIVE — the final gate: requires approved_by_human=True
            manifest = manifest.transition(
                CandidateModelStatus.ACTIVE,
                approved_by_human=True,
                reason=reason,
            )
            # Mark as champion
            manifest_dict = manifest.to_dict()
            manifest_dict["champion"] = True
            manifest_dict["challenger"] = False
            manifest = CandidateModelManifest.from_dict(manifest_dict)
            self.registry.save_manifest(manifest)

            # 3. Mark proposal as ACTIVE
            proposal.approval_status = CandidateModelStatus.ACTIVE.value
            proposal.approved_by_human = True
            proposal.approval_reason = reason
            proposals[idx] = proposal
            self._save_proposals(proposals)

            _append_audit({
                "action": "approve_proposal",
                "proposal_id": proposal_id,
                "model_version": proposal.model_version,
                "new_status": CandidateModelStatus.ACTIVE.value,
                "reason": reason,
                "human_approved": True,
            })

        return {
            "status": "OK",
            "proposal_id": proposal_id,
            "model_version": proposal.model_version,
            "new_status": CandidateModelStatus.ACTIVE.value,
            "champion_activated": True,
        }

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        reason: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reject a proposal. Irreversible."""
        proposals = self._load_proposals()
        idx = self._find_existing(proposals, proposal_id)
        if idx < 0:
            return {"status": "NOT_FOUND", "proposal_id": proposal_id}

        proposal = proposals[idx]
        if proposal.approval_status in {
            CandidateModelStatus.ACTIVE.value,
            CandidateModelStatus.RETIRED.value,
            CandidateModelStatus.REJECTED.value,
        }:
            return {
                "status": "INVALID_STATE",
                "proposal_id": proposal_id,
                "current_status": proposal.approval_status,
                "error": f"Cannot reject from {proposal.approval_status}",
            }

        if not dry_run:
            proposal.approval_status = CandidateModelStatus.REJECTED.value
            proposal.approval_reason = reason or "rejected_by_human"
            proposals[idx] = proposal
            self._save_proposals(proposals)

            # Update manifest
            manifest = self.registry.load_manifest(proposal.model_version)
            if manifest:
                manifest = manifest.transition(
                    CandidateModelStatus.REJECTED,
                    approved_by_human=True,
                    reason=reason or "rejected_by_human",
                )
                self.registry.save_manifest(manifest)

            _append_audit({
                "action": "reject_proposal",
                "proposal_id": proposal_id,
                "model_version": proposal.model_version,
                "reason": reason or "rejected_by_human",
            })

        return {
            "status": "OK",
            "proposal_id": proposal_id,
            "model_version": proposal.model_version,
            "new_status": CandidateModelStatus.REJECTED.value,
        }

    def governance_snapshot(self) -> dict[str, Any]:
        """Full governance snapshot for dashboard display."""
        active = self.active_champion() or {}
        challenger = self.latest_challenger() or {}
        proposals = self.list_proposals()

        champion_weight = {}
        if isinstance(active.get("weights"), dict):
            champion_weight = {str(k): float(v or 0) for k, v in active["weights"].items()}
        if not champion_weight:
            champion_weight = dict(BASELINE_WEIGHTS)

        challenger_weight = {}
        if isinstance(challenger.get("proposed_weights"), dict):
            challenger_weight = {str(k): float(v or 0) for k, v in challenger["proposed_weights"].items()}
        if not challenger_weight and isinstance(challenger.get("weights"), dict):
            challenger_weight = {str(k): float(v or 0) for k, v in challenger["weights"].items()}

        approval_status = _safe_text(
            challenger.get("approval_status")
            or challenger.get("status")
            or "NO_PROPOSAL"
        )

        return {
            "generated_at": _utc_now_iso(),
            "champion_version": _safe_text(active.get("model_version") or "baseline_v1"),
            "champion_weights": champion_weight or dict(BASELINE_WEIGHTS),
            "champion_status": _safe_text(active.get("status") or "ACTIVE"),
            "challenger_version": _safe_text(
                challenger.get("model_version")
                or challenger.get("proposal_id")
                or ""
            ),
            "challenger_weights": challenger_weight,
            "challenger_status": _safe_text(challenger.get("status") or ""),
            "challenger_improved": bool(challenger.get("improved", False)),
            "challenger_sample_count": int(challenger.get("sample_count", 0) or 0),
            "approval_status": approval_status,
            "approved_by_human": bool(challenger.get("approved_by_human", False)),
            "approval_reason": _safe_text(challenger.get("approval_reason") or ""),
            "proposal_count": len(proposals),
            "proposals": proposals,
            "human_approval_required": True,
            "auto_activation_blocked": True,
        }
