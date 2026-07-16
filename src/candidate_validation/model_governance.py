from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = PROJECT_DIR / "config" / "candidate_models"


class CandidateModelStatus(str, Enum):
    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    WALK_FORWARD_VALIDATED = "WALK_FORWARD_VALIDATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in {float("inf"), float("-inf")}:
        return default
    return number


def _atomic_write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return path


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return path


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_weights(weights: dict[str, Any] | None) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for key, value in dict(weights or {}).items():
        number = _safe_float(value)
        if number is None:
            continue
        normalized[str(key)] = round(number, 4)
    total = sum(normalized.values())
    if total > 0:
        normalized = {key: round(value / total, 4) for key, value in normalized.items()}
    return normalized


@dataclass(slots=True)
class CandidateModelManifest:
    model_version: str
    parent_version: str | None = None
    created_at: str = field(default_factory=_utc_now_iso)
    training_period: dict[str, str | None] = field(default_factory=lambda: {"start": None, "end": None})
    sample_count: int = 0
    feature_names: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    target_definition: str = ""
    validation_metrics: dict[str, Any] = field(default_factory=dict)
    approval_status: str = CandidateModelStatus.DRAFT.value
    parent_status: str = ""
    approved_by_human: bool = False
    approval_reason: str = ""
    notes: str = ""
    status: str = CandidateModelStatus.DRAFT.value
    model_class: str = "candidate_score"
    calibration: dict[str, Any] = field(default_factory=dict)
    champion: bool = False
    challenger: bool = False
    rejected_reason: str = ""

    def __post_init__(self) -> None:
        self.model_version = _safe_text(self.model_version)
        self.parent_version = _safe_text(self.parent_version) or None
        self.created_at = _safe_text(self.created_at) or _utc_now_iso()
        self.training_period = {
            "start": _safe_text((self.training_period or {}).get("start")) or None,
            "end": _safe_text((self.training_period or {}).get("end")) or None,
        }
        self.sample_count = int(self.sample_count or 0)
        self.feature_names = [str(item) for item in (self.feature_names or []) if _safe_text(item)]
        self.weights = _normalize_weights(self.weights)
        self.target_definition = _safe_text(self.target_definition)
        self.validation_metrics = dict(self.validation_metrics or {})
        self.approval_status = _safe_text(self.approval_status).upper() or CandidateModelStatus.DRAFT.value
        self.parent_status = _safe_text(self.parent_status)
        self.approved_by_human = bool(self.approved_by_human)
        self.approval_reason = _safe_text(self.approval_reason)
        self.notes = _safe_text(self.notes)
        self.status = _safe_text(self.status).upper() or self.approval_status
        self.model_class = _safe_text(self.model_class) or "candidate_score"
        self.calibration = dict(self.calibration or {})
        self.champion = bool(self.champion)
        self.challenger = bool(self.challenger)
        self.rejected_reason = _safe_text(self.rejected_reason)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateModelManifest":
        return cls(
            model_version=payload.get("model_version") or "",
            parent_version=payload.get("parent_version"),
            created_at=payload.get("created_at") or _utc_now_iso(),
            training_period=payload.get("training_period") or {},
            sample_count=payload.get("sample_count") or 0,
            feature_names=list(payload.get("feature_names") or []),
            weights=payload.get("weights") or {},
            target_definition=payload.get("target_definition") or "",
            validation_metrics=payload.get("validation_metrics") or {},
            approval_status=payload.get("approval_status") or payload.get("status") or CandidateModelStatus.DRAFT.value,
            parent_status=payload.get("parent_status") or "",
            approved_by_human=bool(payload.get("approved_by_human", False)),
            approval_reason=payload.get("approval_reason") or "",
            notes=payload.get("notes") or "",
            status=payload.get("status") or payload.get("approval_status") or CandidateModelStatus.DRAFT.value,
            model_class=payload.get("model_class") or "candidate_score",
            calibration=payload.get("calibration") or {},
            champion=bool(payload.get("champion", False)),
            challenger=bool(payload.get("challenger", False)),
            rejected_reason=payload.get("rejected_reason") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "parent_version": self.parent_version,
            "created_at": self.created_at,
            "training_period": dict(self.training_period),
            "sample_count": self.sample_count,
            "feature_names": list(self.feature_names),
            "weights": dict(self.weights),
            "target_definition": self.target_definition,
            "validation_metrics": dict(self.validation_metrics),
            "approval_status": self.approval_status,
            "parent_status": self.parent_status,
            "approved_by_human": self.approved_by_human,
            "approval_reason": self.approval_reason,
            "notes": self.notes,
            "status": self.status,
            "model_class": self.model_class,
            "calibration": dict(self.calibration),
            "champion": self.champion,
            "challenger": self.challenger,
            "rejected_reason": self.rejected_reason,
        }

    def can_transition(self, new_status: CandidateModelStatus, *, approved_by_human: bool = False) -> bool:
        current = CandidateModelStatus(self.approval_status) if self.approval_status in CandidateModelStatus._value2member_map_ else CandidateModelStatus.DRAFT
        new_status = CandidateModelStatus(new_status)
        transitions = {
            CandidateModelStatus.DRAFT: {CandidateModelStatus.BACKTESTED, CandidateModelStatus.REJECTED},
            CandidateModelStatus.BACKTESTED: {CandidateModelStatus.WALK_FORWARD_VALIDATED, CandidateModelStatus.REVIEW_REQUIRED, CandidateModelStatus.REJECTED},
            CandidateModelStatus.WALK_FORWARD_VALIDATED: {CandidateModelStatus.REVIEW_REQUIRED, CandidateModelStatus.REJECTED},
            CandidateModelStatus.REVIEW_REQUIRED: {CandidateModelStatus.APPROVED, CandidateModelStatus.REJECTED},
            CandidateModelStatus.APPROVED: {CandidateModelStatus.ACTIVE, CandidateModelStatus.REJECTED},
            CandidateModelStatus.ACTIVE: {CandidateModelStatus.RETIRED, CandidateModelStatus.REJECTED},
            CandidateModelStatus.RETIRED: set(),
            CandidateModelStatus.REJECTED: set(),
        }
        if new_status == CandidateModelStatus.ACTIVE and not approved_by_human:
            return False
        return new_status in transitions.get(current, set())

    def transition(self, new_status: CandidateModelStatus, *, approved_by_human: bool = False, reason: str = "") -> "CandidateModelManifest":
        if not self.can_transition(new_status, approved_by_human=approved_by_human):
            raise ValueError(f"invalid_model_transition:{self.approval_status}->{new_status.value}")
        payload = self.to_dict()
        payload["approval_status"] = new_status.value
        payload["status"] = new_status.value
        if approved_by_human:
            payload["approved_by_human"] = True
        if reason:
            payload["approval_reason"] = reason
        return CandidateModelManifest.from_dict(payload)


class CandidateModelRegistry:
    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = Path(root_dir or DEFAULT_MODEL_ROOT)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def manifest_path(self, model_version: str) -> Path:
        return self.root_dir / f"{_safe_text(model_version)}.yaml"

    def load_manifest(self, model_version: str) -> CandidateModelManifest | None:
        payload = _load_yaml(self.manifest_path(model_version))
        if not payload:
            return None
        return CandidateModelManifest.from_dict(payload)

    def save_manifest(self, manifest: CandidateModelManifest, *, overwrite: bool = True) -> Path:
        path = self.manifest_path(manifest.model_version)
        if path.exists() and not overwrite:
            raise FileExistsError(path)
        return _atomic_write_yaml(path, manifest.to_dict())

    def list_manifests(self) -> list[CandidateModelManifest]:
        manifests: list[CandidateModelManifest] = []
        for path in sorted(self.root_dir.glob("*.yaml")):
            payload = _load_yaml(path)
            if payload:
                manifests.append(CandidateModelManifest.from_dict(payload))
        manifests.sort(key=lambda item: (item.status != CandidateModelStatus.ACTIVE.value, item.created_at, item.model_version))
        return manifests

    def active_model(self) -> CandidateModelManifest | None:
        for manifest in self.list_manifests():
            if manifest.status == CandidateModelStatus.ACTIVE.value or manifest.approval_status == CandidateModelStatus.ACTIVE.value:
                return manifest
        return None

    def challenger_model(self) -> CandidateModelManifest | None:
        manifests = self.list_manifests()
        review_required = [manifest for manifest in manifests if manifest.status == CandidateModelStatus.REVIEW_REQUIRED.value or manifest.approval_status == CandidateModelStatus.REVIEW_REQUIRED.value]
        if review_required:
            review_required.sort(key=lambda item: (item.sample_count, item.created_at), reverse=True)
            return review_required[0]
        backtested = [manifest for manifest in manifests if manifest.status == CandidateModelStatus.BACKTESTED.value or manifest.approval_status == CandidateModelStatus.BACKTESTED.value]
        if backtested:
            backtested.sort(key=lambda item: (item.sample_count, item.created_at), reverse=True)
            return backtested[0]
        return None

    def load_baseline(self, version: str = "baseline_v1") -> CandidateModelManifest:
        manifest = self.load_manifest(version)
        if manifest is None:
            raise FileNotFoundError(f"candidate_model_manifest_missing:{version}")
        return manifest

    def recommend_status(
        self,
        manifest: CandidateModelManifest,
        *,
        sample_count_threshold: int = 40,
        walk_forward_outperformed_baseline: bool = False,
        max_drawdown_worsened_too_much: bool = False,
        stable_windows: bool = False,
        no_data_leakage: bool = True,
        safety_constraints_ok: bool = True,
    ) -> CandidateModelStatus:
        if manifest.status == CandidateModelStatus.REJECTED.value:
            return CandidateModelStatus.REJECTED
        if manifest.sample_count < sample_count_threshold:
            return CandidateModelStatus.DRAFT
        if not no_data_leakage or not safety_constraints_ok:
            return CandidateModelStatus.REJECTED
        if not walk_forward_outperformed_baseline or max_drawdown_worsened_too_much or not stable_windows:
            return CandidateModelStatus.REVIEW_REQUIRED
        if manifest.approved_by_human:
            return CandidateModelStatus.APPROVED
        return CandidateModelStatus.REVIEW_REQUIRED

    def active_champion_challenger_snapshot(self) -> dict[str, Any]:
        active = self.active_model()
        challenger = self.challenger_model()
        return {
            "active_model_version": active.model_version if active else None,
            "challenger_version": challenger.model_version if challenger else None,
            "active_model": active.to_dict() if active else None,
            "challenger_model": challenger.to_dict() if challenger else None,
        }
