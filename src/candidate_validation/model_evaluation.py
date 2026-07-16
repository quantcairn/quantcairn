from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .calibration import CandidateScoreCalibrator
from .model_governance import CandidateModelManifest, CandidateModelRegistry, CandidateModelStatus
from .outcome_dataset import CandidateOutcomeDatasetBuilder
from .weight_optimizer import CandidateWeightProposal, OfflineCandidateWeightOptimizer, DEFAULT_BASELINE_WEIGHTS

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_EVALUATION_ROOT = PROJECT_DIR / "artifacts" / "candidate_models" / "evaluation"


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return path


def _compare_metrics(baseline: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    def _score(metric: dict[str, Any]) -> float:
        score = 0.0
        for key, weight in (("precision_at_3", 1.5), ("precision_at_5", 1.0), ("backtest_pass_rate", 1.5), ("walk_forward_pass_rate", 2.0), ("score_rank_correlation", 1.0)):
            value = _safe_float(metric.get(key))
            if value is not None:
                score += value * weight
        return round(score, 4)

    baseline_score = _score(baseline)
    challenger_score = _score(challenger)
    improvement = round(challenger_score - baseline_score, 4)
    return {
        "baseline_score": baseline_score,
        "challenger_score": challenger_score,
        "improvement": improvement,
        "better_than_baseline": improvement > 0.0,
    }


@dataclass(slots=True)
class CandidateModelEvaluationService:
    candidate_root: Path = PROJECT_DIR / "artifacts" / "candidates"
    backtest_root: Path = PROJECT_DIR / "artifacts" / "backtests"
    model_root: Path = PROJECT_DIR / "config" / "candidate_models"
    evaluation_root: Path = DEFAULT_EVALUATION_ROOT

    def __post_init__(self) -> None:
        self.candidate_root = Path(self.candidate_root)
        self.backtest_root = Path(self.backtest_root)
        self.model_root = Path(self.model_root)
        self.evaluation_root = Path(self.evaluation_root)

    def _build_dataset(self):
        return CandidateOutcomeDatasetBuilder(
            candidate_root=self.candidate_root,
            backtest_root=self.backtest_root,
            dataset_root=self.evaluation_root / "datasets",
        ).build()

    def _load_baseline(self) -> CandidateModelManifest:
        registry = CandidateModelRegistry(self.model_root)
        try:
            return registry.load_baseline("baseline_v1")
        except Exception:
            return CandidateModelManifest(
                model_version="baseline_v1",
                status=CandidateModelStatus.ACTIVE.value,
                approval_status=CandidateModelStatus.ACTIVE.value,
                weights=dict(DEFAULT_BASELINE_WEIGHTS),
                feature_names=list(DEFAULT_BASELINE_WEIGHTS.keys()),
                target_definition="baseline_manual_weights",
            )

    def evaluate(self) -> dict[str, Any]:
        dataset = self._build_dataset()
        baseline = self._load_baseline()
        optimizer = OfflineCandidateWeightOptimizer(baseline_weights=baseline.weights or DEFAULT_BASELINE_WEIGHTS)
        proposal_version = f"proposal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        proposal: CandidateWeightProposal = optimizer.fit(dataset.samples, model_version=proposal_version, parent_version=baseline.model_version)
        calibrator = CandidateScoreCalibrator()
        calibration = calibrator.fit(dataset.samples, model_version=f"{proposal_version}_calibration")

        registry = CandidateModelRegistry(self.model_root)
        baseline_status = baseline.status or baseline.approval_status or CandidateModelStatus.ACTIVE.value
        challenger_status = registry.recommend_status(
            CandidateModelManifest(
                model_version=proposal.model_version,
                parent_version=proposal.parent_version,
                created_at=proposal.generated_at,
                training_period=proposal.training_period,
                sample_count=proposal.sample_count,
                feature_names=list(proposal.proposed_weights.keys()),
                weights=proposal.proposed_weights,
                target_definition=proposal.target_definition,
                validation_metrics=proposal.proposed_metrics,
                approval_status=CandidateModelStatus.DRAFT.value,
                parent_status=baseline_status,
                notes="offline proposal",
                calibration=calibration.to_dict(),
            ),
            sample_count_threshold=40,
            walk_forward_outperformed_baseline=_compare_metrics(proposal.baseline_metrics, proposal.proposed_metrics)["better_than_baseline"],
            max_drawdown_worsened_too_much=bool(
                _safe_float(proposal.proposed_metrics.get("max_drawdown")) is not None
                and _safe_float(proposal.baseline_metrics.get("max_drawdown")) is not None
                and float(proposal.proposed_metrics["max_drawdown"]) > float(proposal.baseline_metrics["max_drawdown"]) * 1.15
            ),
            stable_windows=bool(_safe_float(proposal.proposed_metrics.get("candidate_turnover"), 0.0) is not None and (proposal.proposed_metrics.get("candidate_turnover") or 0.0) <= 0.65),
            no_data_leakage=not any("leakage" in warning for warning in dataset.warnings),
            safety_constraints_ok=not proposal.overfitting_warning,
        )

        challenger_metrics = dict(proposal.proposed_metrics)
        challenger_metrics.update(
            {
                "calibration_error": calibration.calibration_error,
                "calibration_curve": [bucket.to_dict() for bucket in calibration.buckets],
                "training_sample_count": proposal.sample_count,
                "sample_size_warning": proposal.sample_size_warning,
                "overfitting_warning": proposal.overfitting_warning,
            }
        )
        baseline_metrics = dict(proposal.baseline_metrics)
        baseline_metrics.update(
            {
                "training_sample_count": proposal.sample_count,
                "sample_size_warning": proposal.sample_size_warning,
            }
        )
        comparison = _compare_metrics(baseline_metrics, challenger_metrics)
        recommended_action = "manual_review" if challenger_status == CandidateModelStatus.REVIEW_REQUIRED else "keep_baseline"
        approval_status = challenger_status.value
        active_model_version = baseline.model_version if baseline.approval_status == CandidateModelStatus.ACTIVE.value or baseline.status == CandidateModelStatus.ACTIVE.value else baseline.model_version
        challenger_version = proposal.model_version if proposal.sample_count else None
        report = {
            "title": "Candidate Model Evaluation",
            "generated_at": _utc_now_iso(),
            "dataset": {
                "sample_count": dataset.sample_count,
                "training_period": dataset.training_period,
                "target_definition": dataset.target_definition,
                "warnings": list(dataset.warnings),
                "source_paths": dict(dataset.source_paths),
            },
            "active_model_version": active_model_version,
            "challenger_version": challenger_version,
            "training_sample_count": proposal.sample_count,
            "training_period": proposal.training_period,
            "baseline_version": baseline.model_version,
            "baseline_status": baseline_status,
            "challenger_status": approval_status,
            "approval_status": approval_status,
            "recommended_action": recommended_action,
            "baseline_metrics": baseline_metrics,
            "challenger_metrics": challenger_metrics,
            "baseline_weights": dict(baseline.weights or DEFAULT_BASELINE_WEIGHTS),
            "proposed_weights": dict(proposal.proposed_weights),
            "feature_importance": dict(proposal.feature_importance),
            "confidence_interval": dict(proposal.confidence_interval),
            "calibration_curve": [bucket.to_dict() for bucket in calibration.buckets],
            "calibration_error": calibration.calibration_error,
            "sample_size_warning": proposal.sample_size_warning,
            "overfitting_warning": proposal.overfitting_warning,
            "proxy_target_used": proposal.proxy_target_used,
            "warnings": list(dataset.warnings) + list(proposal.warnings) + list(calibration.warnings),
            "comparison": comparison,
            "model_governance": {
                "baseline_model": baseline.to_dict(),
                "challenger_model": {
                    "model_version": proposal.model_version,
                    "parent_version": proposal.parent_version,
                    "created_at": proposal.generated_at,
                    "training_period": proposal.training_period,
                    "sample_count": proposal.sample_count,
                    "feature_names": list(proposal.proposed_weights.keys()),
                    "weights": dict(proposal.proposed_weights),
                    "target_definition": proposal.target_definition,
                    "validation_metrics": dict(challenger_metrics),
                    "approval_status": approval_status,
                    "parent_status": baseline_status,
                    "approved_by_human": False,
                    "approval_reason": "",
                    "notes": "offline challenger proposal",
                    "status": approval_status,
                    "model_class": "candidate_score",
                    "calibration": {
                        "model_version": calibration.model_version,
                        "source_metric": calibration.source_metric,
                        "sample_count": calibration.sample_count,
                        "calibration_error": calibration.calibration_error,
                        "raw_score_min": calibration.raw_score_min,
                        "raw_score_max": calibration.raw_score_max,
                        "buckets": [bucket.to_dict() for bucket in calibration.buckets],
                        "generated_at": calibration.generated_at,
                        "warnings": list(calibration.warnings),
                    },
                    "champion": False,
                    "challenger": True,
                    "rejected_reason": "",
                },
                "status_summary": {
                    "active": active_model_version,
                    "challenger": challenger_version,
                    "requires_review": approval_status == CandidateModelStatus.REVIEW_REQUIRED.value,
                },
            },
            "active_model": baseline.to_dict(),
            "challenger_model": {
                "model_version": proposal.model_version,
                "parent_version": proposal.parent_version,
                "created_at": proposal.generated_at,
                "training_period": proposal.training_period,
                "sample_count": proposal.sample_count,
                "feature_names": list(proposal.proposed_weights.keys()),
                "weights": dict(proposal.proposed_weights),
                "target_definition": proposal.target_definition,
                "validation_metrics": dict(challenger_metrics),
                "approval_status": approval_status,
                "parent_status": baseline_status,
                "notes": "offline challenger proposal",
            },
        }
        return report

    def write(self, *, output_dir: Path | None = None) -> dict[str, Any]:
        report = self.evaluate()
        target_root = Path(output_dir or self.evaluation_root)
        target_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target_root / "candidate_model_evaluation.json", report)
        return report


def load_candidate_model_evaluation_snapshot(
    *,
    candidate_root: Path | None = None,
    backtest_root: Path | None = None,
    model_root: Path | None = None,
) -> dict[str, Any]:
    try:
        return CandidateModelEvaluationService(
            candidate_root=candidate_root or PROJECT_DIR / "artifacts" / "candidates",
            backtest_root=backtest_root or PROJECT_DIR / "artifacts" / "backtests",
            model_root=model_root or PROJECT_DIR / "config" / "candidate_models",
        ).evaluate()
    except Exception as exc:
        return {
            "title": "Candidate Model Evaluation",
            "generated_at": _utc_now_iso(),
            "active_model_version": None,
            "challenger_version": None,
            "training_sample_count": 0,
            "training_period": {"start": None, "end": None},
            "baseline_version": "baseline_v1",
            "baseline_status": CandidateModelStatus.ACTIVE.value,
            "challenger_status": CandidateModelStatus.DRAFT.value,
            "approval_status": CandidateModelStatus.DRAFT.value,
            "recommended_action": "collect_more_samples",
            "baseline_metrics": {},
            "challenger_metrics": {},
            "baseline_weights": dict(DEFAULT_BASELINE_WEIGHTS),
            "proposed_weights": dict(DEFAULT_BASELINE_WEIGHTS),
            "feature_importance": {name: 0.0 for name in DEFAULT_BASELINE_WEIGHTS},
            "confidence_interval": {name: [DEFAULT_BASELINE_WEIGHTS[name], DEFAULT_BASELINE_WEIGHTS[name]] for name in DEFAULT_BASELINE_WEIGHTS},
            "calibration_curve": [],
            "calibration_error": None,
            "sample_size_warning": True,
            "overfitting_warning": True,
            "proxy_target_used": True,
            "warnings": [str(exc)],
            "comparison": {},
            "dataset": {"sample_count": 0, "training_period": {"start": None, "end": None}, "target_definition": "unavailable", "warnings": [str(exc)], "source_paths": {}},
            "model_governance": {},
            "active_model": None,
            "challenger_model": None,
        }
