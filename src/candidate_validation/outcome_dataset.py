from __future__ import annotations

import csv
import json
import math
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import CandidateRecord, ValidationStatus

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE_ROOT = PROJECT_DIR / "artifacts" / "candidates"
DEFAULT_BACKTEST_ROOT = PROJECT_DIR / "artifacts" / "backtests"
DEFAULT_DATASET_ROOT = PROJECT_DIR / "artifacts" / "candidate_models" / "datasets"


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


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = _safe_text(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _projected_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return path.name


def _clamp(value: float | None, lower: float = 0.0, upper: float = 100.0) -> float | None:
    if value is None:
        return None
    return max(lower, min(upper, float(value)))


def _bool_label(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _safe_text(value).lower()
    if text in {"1", "true", "yes", "y", "on", "pass", "passed", "ok", "valid", "complete", "completed"}:
        return True
    if text in {"0", "false", "no", "n", "off", "fail", "failed", "invalid", "blocked", "ineligible"}:
        return False
    return None


def _positive_float(value: Any) -> float | None:
    number = _safe_float(value)
    if number is None:
        return None
    return number if number >= 0 else None


def _normalise_target_component(value: bool | None) -> float | None:
    if value is None:
        return None
    return 1.0 if value else 0.0


def _composite_target(components: list[float | None], fallback: float | None = None) -> float:
    values = [value for value in components if value is not None]
    if values:
        return round(sum(values) / len(values), 4)
    if fallback is not None:
        return round(float(fallback), 4)
    return 0.0


def _data_quality_score(candidate: CandidateRecord) -> float:
    metadata = dict(candidate.metadata or {})
    score = 100.0
    if _safe_text(candidate.data_status).upper() != "VALID":
        score -= 25.0
    if not bool(candidate.scoring_eligible):
        score -= 15.0
    if bool(candidate.candidate_fallback):
        score -= 15.0
    if bool(candidate.mock_used):
        score -= 10.0
    if bool(candidate.degraded):
        score -= 10.0
    missing_fields = list(candidate.missing_fields or metadata.get("missing_fields") or [])
    score -= min(20.0, float(len(missing_fields)) * 4.0)
    freshness = _safe_text(candidate.data_freshness or metadata.get("freshness_status")).lower()
    if freshness in {"stale", "old"}:
        score -= 12.5
    if freshness == "invalid":
        score -= 20.0
    if _safe_text(metadata.get("stale_reason")):
        score -= 5.0
    return round(_clamp(score, 0.0, 100.0) or 0.0, 2)


def _selection_date(candidate: CandidateRecord) -> datetime | None:
    return _parse_iso_datetime(candidate.selected_at or candidate.created_at or candidate.updated_at)


def _artifact_outcomes(candidate: CandidateRecord, *, backtest_artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_dt = _selection_date(candidate)
    symbol = _safe_text(candidate.symbol).upper()
    if candidate_dt is None or not symbol:
        return {}
    selected: dict[str, Any] | None = None
    selected_dt: datetime | None = None
    for artifact in backtest_artifacts:
        if _safe_text(artifact.get("symbol")).upper() != symbol:
            continue
        artifact_end = _parse_iso_datetime(artifact.get("data_end") or artifact.get("generated_at"))
        if artifact_end is None:
            continue
        if artifact_end > candidate_dt:
            continue
        if selected is None or (selected_dt is not None and artifact_end > selected_dt) or selected_dt is None:
            selected = artifact
            selected_dt = artifact_end
    return selected or {}


def _extract_metrics(artifact: dict[str, Any]) -> dict[str, Any]:
    metrics = artifact.get("metrics")
    if not isinstance(metrics, list):
        return {}
    ranking = artifact.get("summary")
    best_version = ""
    if isinstance(ranking, dict):
        best_version = _safe_text(ranking.get("best_version") or ranking.get("best_version_all"))
    if not best_version and metrics:
        best_version = _safe_text((metrics[0] or {}).get("version"))
    for row in metrics:
        if _safe_text(row.get("version")) == best_version:
            return dict(row)
    return dict(metrics[0] or {})


def _artifact_target(artifact: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    metrics = _extract_metrics(artifact)
    labels: list[float | None] = []
    if _safe_text(metrics.get("reconciliation_status")).upper() == "OK":
        labels.append(1.0)
    else:
        labels.append(0.0)
    total_return = _safe_float(metrics.get("total_return"))
    if total_return is not None:
        labels.append(1.0 if total_return > 0 else 0.0)
    max_drawdown = _safe_float(metrics.get("max_drawdown"))
    if max_drawdown is not None:
        labels.append(1.0 if max_drawdown <= 0.20 else 0.0)
    backtest_pass = _safe_text(metrics.get("evidence_status")).upper() in {"ELIGIBLE", "INSUFFICIENT_EVIDENCE"}
    labels.append(1.0 if backtest_pass else 0.0)
    outcome = {
        "backtest_status": _safe_text(metrics.get("version") or metrics.get("strategy") or artifact.get("summary", {}).get("best_version") or "").upper() or "UNKNOWN",
        "walk_forward_status": _safe_text(artifact.get("summary", {}).get("ranking_status") or "").upper() or "UNKNOWN",
        "shadow_status": _safe_text(metrics.get("deployment_status") or "").upper() or "UNKNOWN",
        "paper_status": _safe_text(metrics.get("deployment_status") or "").upper() or "UNKNOWN",
        "outcome_return": total_return,
        "outcome_max_drawdown": max_drawdown,
        "outcome_window": f"{artifact.get('data_start') or 'unknown'} -> {artifact.get('data_end') or 'unknown'}",
    }
    return outcome, [f"artifact:{k}" for k in artifact.keys() if k in {"symbol", "data_start", "data_end", "generated_at"}]


@dataclass(slots=True)
class CandidateOutcomeSample:
    candidate_id: str
    symbol: str
    selection_date: str
    data_as_of: str
    scoring_version: str
    liquidity_score: float | None
    trend_score: float | None
    volatility_score: float | None
    risk_score: float | None
    strategy_fit_score: float | None
    candidate_score: float | None
    recommended_strategy: str
    data_quality_score: float | None
    validation_result: str
    backtest_result: str
    walk_forward_result: str
    shadow_result: str
    paper_result: str
    outcome_window: str
    label_data_valid: bool | None = None
    label_backtest_pass: bool | None = None
    label_walk_forward_pass: bool | None = None
    label_positive_return: bool | None = None
    label_max_drawdown_ok: bool | None = None
    outcome_return: float | None = None
    outcome_max_drawdown: float | None = None
    label_source: str = ""
    leakage_reason: str = ""
    source_path: str = ""
    artifact_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "selection_date": self.selection_date,
            "data_as_of": self.data_as_of,
            "scoring_version": self.scoring_version,
            "liquidity_score": self.liquidity_score,
            "trend_score": self.trend_score,
            "volatility_score": self.volatility_score,
            "risk_score": self.risk_score,
            "strategy_fit_score": self.strategy_fit_score,
            "candidate_score": self.candidate_score,
            "recommended_strategy": self.recommended_strategy,
            "data_quality_score": self.data_quality_score,
            "validation_result": self.validation_result,
            "backtest_result": self.backtest_result,
            "walk_forward_result": self.walk_forward_result,
            "shadow_result": self.shadow_result,
            "paper_result": self.paper_result,
            "outcome_window": self.outcome_window,
            "label_data_valid": self.label_data_valid,
            "label_backtest_pass": self.label_backtest_pass,
            "label_walk_forward_pass": self.label_walk_forward_pass,
            "label_positive_return": self.label_positive_return,
            "label_max_drawdown_ok": self.label_max_drawdown_ok,
            "outcome_return": self.outcome_return,
            "outcome_max_drawdown": self.outcome_max_drawdown,
            "label_source": self.label_source,
            "leakage_reason": self.leakage_reason,
            "source_path": self.source_path,
            "artifact_path": self.artifact_path,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(slots=True)
class CandidateOutcomeDataset:
    samples: list[CandidateOutcomeSample]
    generated_at: str
    training_period: dict[str, str | None]
    warnings: list[str]
    source_paths: dict[str, str]
    target_definition: str

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "sample_count": self.sample_count,
            "training_period": dict(self.training_period),
            "warnings": list(self.warnings),
            "source_paths": dict(self.source_paths),
            "target_definition": self.target_definition,
            "samples": [sample.to_dict() for sample in self.samples],
        }

    def write(self, output_dir: Path) -> dict[str, Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "candidate_outcome_dataset.json"
        csv_path = output_dir / "candidate_outcome_dataset.csv"
        json_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.samples[0].to_dict().keys()) if self.samples else list(CandidateOutcomeSample.__dataclass_fields__.keys()))
            writer.writeheader()
            for sample in self.samples:
                writer.writerow(sample.to_dict())
        return {"json": json_path, "csv": csv_path}


def _load_json_artifact(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _index_backtest_artifacts(backtest_root: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if not backtest_root.exists():
        return artifacts
    for path in sorted(backtest_root.rglob("comparison_summary.json")):
        payload = _load_json_artifact(path)
        if not payload:
            continue
        payload["_artifact_path"] = str(path)
        artifacts.append(payload)
    artifacts.sort(key=lambda item: (_safe_text(item.get("generated_at")), _safe_text(item.get("_artifact_path"))))
    return artifacts


class CandidateOutcomeDatasetBuilder:
    def __init__(
        self,
        *,
        candidate_root: Path | None = None,
        backtest_root: Path | None = None,
        dataset_root: Path | None = None,
    ) -> None:
        self.candidate_root = Path(candidate_root or DEFAULT_CANDIDATE_ROOT)
        self.backtest_root = Path(backtest_root or DEFAULT_BACKTEST_ROOT)
        self.dataset_root = Path(dataset_root or DEFAULT_DATASET_ROOT)

    def _label_from_candidate(self, candidate: CandidateRecord, artifact: dict[str, Any] | None) -> tuple[dict[str, Any], str, float | None, float | None, str]:
        metadata = dict(candidate.metadata or {})
        validation_status = _safe_text(candidate.validation_status).upper()
        backtest_result = _safe_text(metadata.get("backtest_result") or metadata.get("backtest_status") or validation_status or "UNKNOWN").upper()
        walk_forward_result = _safe_text(metadata.get("walk_forward_result") or metadata.get("walk_forward_status") or validation_status or "UNKNOWN").upper()
        shadow_result = _safe_text(metadata.get("shadow_result") or metadata.get("shadow_status") or validation_status or "UNKNOWN").upper()
        paper_result = _safe_text(metadata.get("paper_result") or metadata.get("paper_status") or "UNKNOWN").upper()

        label_data_valid = validation_status in {
            ValidationStatus.DATA_VALID.value,
            ValidationStatus.PENDING_BACKTEST.value,
            ValidationStatus.BACKTEST_COMPLETE.value,
            ValidationStatus.PENDING_WALK_FORWARD.value,
            ValidationStatus.WALK_FORWARD_COMPLETE.value,
            ValidationStatus.PENDING_SHADOW.value,
            ValidationStatus.SHADOW_OBSERVING.value,
            ValidationStatus.SHADOW_COMPLETE.value,
            ValidationStatus.PAPER_ELIGIBLE.value,
            ValidationStatus.LIVE_ELIGIBLE.value,
        } or _safe_text(candidate.data_status).upper() == "VALID"
        label_backtest_pass = validation_status in {
            ValidationStatus.BACKTEST_COMPLETE.value,
            ValidationStatus.PENDING_WALK_FORWARD.value,
            ValidationStatus.WALK_FORWARD_COMPLETE.value,
            ValidationStatus.PENDING_SHADOW.value,
            ValidationStatus.SHADOW_OBSERVING.value,
            ValidationStatus.SHADOW_COMPLETE.value,
            ValidationStatus.PAPER_ELIGIBLE.value,
            ValidationStatus.LIVE_ELIGIBLE.value,
        }
        label_walk_forward_pass = validation_status in {
            ValidationStatus.WALK_FORWARD_COMPLETE.value,
            ValidationStatus.PENDING_SHADOW.value,
            ValidationStatus.SHADOW_OBSERVING.value,
            ValidationStatus.SHADOW_COMPLETE.value,
            ValidationStatus.PAPER_ELIGIBLE.value,
            ValidationStatus.LIVE_ELIGIBLE.value,
        }
        label_positive_return = None
        label_max_drawdown_ok = None
        outcome_return = None
        outcome_drawdown = None
        label_source = "candidate_status"
        if artifact:
            outcome, _ = _artifact_target(artifact)
            outcome_return = outcome.get("outcome_return")
            outcome_drawdown = outcome.get("outcome_max_drawdown")
            backtest_result = _safe_text(outcome.get("backtest_result") or backtest_result).upper()
            walk_forward_result = _safe_text(outcome.get("walk_forward_result") or walk_forward_result).upper()
            shadow_result = _safe_text(outcome.get("shadow_result") or shadow_result).upper()
            paper_result = _safe_text(outcome.get("paper_result") or paper_result).upper()
            if outcome_return is not None:
                label_positive_return = outcome_return > 0
            if outcome_drawdown is not None:
                label_max_drawdown_ok = outcome_drawdown <= 0.20
            label_source = "artifact"

        if label_positive_return is None:
            positive_proxy = _safe_float(candidate.candidate_score)
            if positive_proxy is not None:
                label_positive_return = positive_proxy >= 75.0
                label_source = f"{label_source}+score_proxy"
        if label_max_drawdown_ok is None:
            proxy = _safe_float(candidate.risk_score)
            if proxy is not None:
                label_max_drawdown_ok = proxy >= 65.0
                label_source = f"{label_source}+risk_proxy"

        labels = {
            "label_data_valid": label_data_valid,
            "label_backtest_pass": label_backtest_pass,
            "label_walk_forward_pass": label_walk_forward_pass,
            "label_positive_return": label_positive_return,
            "label_max_drawdown_ok": label_max_drawdown_ok,
        }
        return labels, label_source, outcome_return, outcome_drawdown, f"{label_source}:{'artifact' if artifact else 'proxy'}"

    def build(self) -> CandidateOutcomeDataset:
        from .store import CandidateValidationStore

        store = CandidateValidationStore(self.candidate_root)
        candidates = store.load_latest_candidates()
        backtest_artifacts = _index_backtest_artifacts(self.backtest_root)
        warnings: list[str] = []
        if not candidates:
            warnings.append("candidate_store_empty")
        if not backtest_artifacts:
            warnings.append("backtest_artifacts_unavailable")

        samples: list[CandidateOutcomeSample] = []
        for candidate in candidates:
            selected_at = _selection_date(candidate)
            metadata = dict(candidate.metadata or {})
            selection_date = selected_at.isoformat() if selected_at else _safe_text(candidate.selected_at or candidate.updated_at or candidate.created_at)
            data_as_of_candidates = [
                _parse_iso_datetime(metadata.get("data_as_of")),
                _parse_iso_datetime(metadata.get("daily_data_as_of")),
                _parse_iso_datetime(metadata.get("benchmark_data_as_of")),
                _parse_iso_datetime(metadata.get("premarket_snapshot_at")),
                _parse_iso_datetime(candidate.selected_at),
            ]
            data_as_of_dt = max((value for value in data_as_of_candidates if value is not None), default=selected_at)
            data_as_of = data_as_of_dt.isoformat() if data_as_of_dt else ""
            leakage_reason = ""
            if selected_at is not None and data_as_of_dt is not None and data_as_of_dt > selected_at:
                leakage_reason = "data_as_of_after_selection_date"
            artifact = _artifact_outcomes(candidate, backtest_artifacts=backtest_artifacts)
            if artifact:
                artifact_end = _parse_iso_datetime(artifact.get("data_end") or artifact.get("generated_at"))
                if selected_at is not None and artifact_end is not None and artifact_end > selected_at:
                    leakage_reason = leakage_reason or "future_artifact_detected"
            labels, label_source, outcome_return, outcome_drawdown, source_flag = self._label_from_candidate(candidate, artifact)
            composite_components = [
                _normalise_target_component(labels["label_data_valid"]),
                _normalise_target_component(labels["label_backtest_pass"]),
                _normalise_target_component(labels["label_walk_forward_pass"]),
                _normalise_target_component(labels["label_positive_return"]),
                _normalise_target_component(labels["label_max_drawdown_ok"]),
            ]
            composite = _composite_target(composite_components, fallback=_safe_float(candidate.candidate_score, 0.0) / 100.0 if _safe_float(candidate.candidate_score) is not None else 0.0)
            sample = CandidateOutcomeSample(
                candidate_id=candidate.candidate_id,
                symbol=candidate.symbol,
                selection_date=selection_date,
                data_as_of=data_as_of,
                scoring_version=_safe_text(metadata.get("scoring_version") or metadata.get("selection_stage") or metadata.get("market_selection_stage") or "unknown") or "unknown",
                liquidity_score=candidate.liquidity_score,
                trend_score=candidate.trend_score,
                volatility_score=candidate.volatility_score,
                risk_score=candidate.risk_score,
                strategy_fit_score=candidate.strategy_fit_score,
                candidate_score=candidate.candidate_score,
                recommended_strategy=candidate.recommended_strategy,
                data_quality_score=_data_quality_score(candidate),
                validation_result=_safe_text(candidate.validation_status).upper(),
                backtest_result=_safe_text(artifact.get("backtest_result") or artifact.get("summary", {}).get("ranking_status") or labels.get("label_backtest_pass")).upper() if artifact else _safe_text(labels.get("label_backtest_pass")).upper(),
                walk_forward_result=_safe_text(artifact.get("walk_forward_result") or artifact.get("summary", {}).get("ranking_status") or labels.get("label_walk_forward_pass")).upper() if artifact else _safe_text(labels.get("label_walk_forward_pass")).upper(),
                shadow_result=_safe_text(artifact.get("shadow_result") or labels.get("label_walk_forward_pass")).upper(),
                paper_result=_safe_text(artifact.get("paper_result") or labels.get("label_walk_forward_pass")).upper(),
                outcome_window=_safe_text((artifact or {}).get("summary", {}).get("data_start") if artifact else "") + (" -> " + _safe_text((artifact or {}).get("summary", {}).get("data_end")) if artifact else ""),
                label_data_valid=labels["label_data_valid"],
                label_backtest_pass=labels["label_backtest_pass"],
                label_walk_forward_pass=labels["label_walk_forward_pass"],
                label_positive_return=labels["label_positive_return"],
                label_max_drawdown_ok=labels["label_max_drawdown_ok"],
                outcome_return=outcome_return,
                outcome_max_drawdown=outcome_drawdown,
                label_source=source_flag,
                leakage_reason=leakage_reason,
                source_path=_projected_path(self.candidate_root / "candidates.jsonl", PROJECT_DIR),
                artifact_path=_projected_path(Path(artifact["_artifact_path"]), PROJECT_DIR) if artifact and artifact.get("_artifact_path") else "",
                metadata=metadata,
            )
            if leakage_reason:
                warnings.append(f"{candidate.candidate_id}:{leakage_reason}")
                continue
            samples.append(sample)

        if not samples:
            warnings.append("no_leakage_safe_samples")

        sample_dates = [sample.selection_date for sample in samples if sample.selection_date]
        training_period = {
            "start": min(sample_dates) if sample_dates else None,
            "end": max(sample_dates) if sample_dates else None,
        }
        return CandidateOutcomeDataset(
            samples=samples,
            generated_at=_utc_now_iso(),
            training_period=training_period,
            warnings=warnings,
            source_paths={
                "candidate_store": _projected_path(self.candidate_root / "candidates.jsonl", PROJECT_DIR),
                "backtest_root": _projected_path(self.backtest_root, PROJECT_DIR),
            },
            target_definition="composite_of_data_valid_backtest_walk_forward_return_drawdown_with_proxy_fallback",
        )
