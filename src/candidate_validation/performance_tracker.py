from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any, Iterable

from .models import CandidateRecord, ValidationStatus

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))
CANDIDATE_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "candidates"

SCORE_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (90.0, 100.0, "90-100"),
    (80.0, 90.0, "80-90"),
    (70.0, 80.0, "70-80"),
    (60.0, 70.0, "60-70"),
)
HIGH_SCORE_THRESHOLD = 80.0
_DATA_VALID_AND_LATER = {
    ValidationStatus.DATA_VALID.value,
    ValidationStatus.PENDING_BACKTEST.value,
    ValidationStatus.BACKTEST_COMPLETE.value,
    ValidationStatus.BACKTEST_FAILED.value,
    ValidationStatus.PENDING_WALK_FORWARD.value,
    ValidationStatus.WALK_FORWARD_COMPLETE.value,
    ValidationStatus.WALK_FORWARD_FAILED.value,
    ValidationStatus.PENDING_SHADOW.value,
    ValidationStatus.SHADOW_OBSERVING.value,
    ValidationStatus.SHADOW_COMPLETE.value,
    ValidationStatus.PAPER_ELIGIBLE.value,
    ValidationStatus.PAPER_INELIGIBLE.value,
    ValidationStatus.LIVE_ELIGIBLE.value,
    ValidationStatus.LIVE_INELIGIBLE.value,
}
_BACKTEST_SUCCESS_AND_LATER = {
    ValidationStatus.BACKTEST_COMPLETE.value,
    ValidationStatus.PENDING_WALK_FORWARD.value,
    ValidationStatus.WALK_FORWARD_COMPLETE.value,
    ValidationStatus.WALK_FORWARD_FAILED.value,
    ValidationStatus.PENDING_SHADOW.value,
    ValidationStatus.SHADOW_OBSERVING.value,
    ValidationStatus.SHADOW_COMPLETE.value,
    ValidationStatus.PAPER_ELIGIBLE.value,
    ValidationStatus.PAPER_INELIGIBLE.value,
    ValidationStatus.LIVE_ELIGIBLE.value,
    ValidationStatus.LIVE_INELIGIBLE.value,
}
_WALK_FORWARD_SUCCESS_AND_LATER = {
    ValidationStatus.WALK_FORWARD_COMPLETE.value,
    ValidationStatus.PENDING_SHADOW.value,
    ValidationStatus.SHADOW_OBSERVING.value,
    ValidationStatus.SHADOW_COMPLETE.value,
    ValidationStatus.PAPER_ELIGIBLE.value,
    ValidationStatus.PAPER_INELIGIBLE.value,
    ValidationStatus.LIVE_ELIGIBLE.value,
    ValidationStatus.LIVE_INELIGIBLE.value,
}


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
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


def _bucket_label(score: float | None) -> str:
    value = _safe_float(score)
    if value is None:
        return "unbucketed"
    for lower, upper, label in SCORE_BUCKETS:
        if lower <= value <= upper or (label != "90-100" and lower <= value < upper):
            return label
    return "unbucketed"


def _status_rank(status: str) -> int:
    order = [
        ValidationStatus.AI_CANDIDATE.value,
        ValidationStatus.CLASSIFIED.value,
        ValidationStatus.BENCHMARK_ASSIGNED.value,
        ValidationStatus.STRATEGY_ASSIGNED.value,
        ValidationStatus.PENDING_DATA_VALIDATION.value,
        ValidationStatus.DATA_VALID.value,
        ValidationStatus.DATA_INVALID.value,
        ValidationStatus.PENDING_BACKTEST.value,
        ValidationStatus.BACKTEST_COMPLETE.value,
        ValidationStatus.BACKTEST_FAILED.value,
        ValidationStatus.PENDING_WALK_FORWARD.value,
        ValidationStatus.WALK_FORWARD_COMPLETE.value,
        ValidationStatus.WALK_FORWARD_FAILED.value,
        ValidationStatus.PENDING_SHADOW.value,
        ValidationStatus.SHADOW_OBSERVING.value,
        ValidationStatus.SHADOW_COMPLETE.value,
        ValidationStatus.PAPER_ELIGIBLE.value,
        ValidationStatus.PAPER_INELIGIBLE.value,
        ValidationStatus.LIVE_ELIGIBLE.value,
        ValidationStatus.LIVE_INELIGIBLE.value,
        ValidationStatus.REJECTED.value,
    ]
    normalized = _safe_text(status).upper()
    try:
        return order.index(normalized)
    except ValueError:
        return -1


@dataclass(slots=True)
class CandidatePerformanceTracker:
    root_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.root_dir is None:
            self.root_dir = resolve_artifacts_dir(PROJECT_DIR) / "candidates"
        self.root_dir = Path(self.root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @property
    def performance_path(self) -> Path:
        return self.root_dir / "candidate_performance.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.root_dir / "candidate_performance_summary.csv"

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(key): CandidatePerformanceTracker._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [CandidatePerformanceTracker._jsonable(item) for item in value]
        return _safe_text(value)

    def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
            else:
                raise ValueError(f"invalid_jsonl_record:{path.name}")
        return rows

    def _write_jsonl(self, path: Path, rows: Iterable[dict[str, Any]]) -> Path:
        text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
        if text:
            text += "\n"
        return _atomic_write_text(path, text)

    def _build_record(self, candidate: CandidateRecord) -> dict[str, Any]:
        metadata = dict(candidate.metadata or {})
        candidate_score = candidate.candidate_score
        factor_scores = {
            "candidate_score": candidate.candidate_score,
            "liquidity_score": candidate.liquidity_score,
            "trend_score": candidate.trend_score,
            "volatility_score": candidate.volatility_score,
            "risk_score": candidate.risk_score,
            "strategy_fit_score": candidate.strategy_fit_score,
        }
        validation_status = _safe_text(candidate.validation_status).upper()
        backtest_status = _safe_text(metadata.get("backtest_status") or metadata.get("backtest_stage") or "").upper()
        walk_forward_status = _safe_text(metadata.get("walk_forward_status") or metadata.get("walk_forward_stage") or "").upper()
        shadow_status = _safe_text(metadata.get("shadow_status") or metadata.get("shadow_stage") or "").upper()
        if not backtest_status:
            backtest_status = validation_status if validation_status in {
                ValidationStatus.PENDING_BACKTEST.value,
                ValidationStatus.BACKTEST_COMPLETE.value,
                ValidationStatus.BACKTEST_FAILED.value,
            } else "NOT_RUN"
        if not walk_forward_status:
            walk_forward_status = validation_status if validation_status in {
                ValidationStatus.PENDING_WALK_FORWARD.value,
                ValidationStatus.WALK_FORWARD_COMPLETE.value,
                ValidationStatus.WALK_FORWARD_FAILED.value,
            } else "NOT_RUN"
        if not shadow_status:
            shadow_status = validation_status if validation_status in {
                ValidationStatus.PENDING_SHADOW.value,
                ValidationStatus.SHADOW_OBSERVING.value,
                ValidationStatus.SHADOW_COMPLETE.value,
            } else "NOT_RUN"

        return {
            "candidate_id": candidate.candidate_id,
            "symbol": candidate.symbol,
            "market": candidate.market,
            "candidate_score": candidate_score,
            "factor_scores": factor_scores,
            "recommended_strategy": candidate.recommended_strategy,
            "validation_status": validation_status,
            "backtest_status": backtest_status,
            "walk_forward_status": walk_forward_status,
            "shadow_status": shadow_status,
            "score_bucket": _bucket_label(candidate_score),
            "selection_stage": metadata.get("selection_stage") or metadata.get("market_selection_stage") or "",
            "created_at": candidate.created_at,
            "updated_at": candidate.updated_at,
        }

    def _bucket_rows(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for lower, upper, label in SCORE_BUCKETS:
            bucket = [row for row in records if row.get("score_bucket") == label]
            candidate_count = len(bucket)
            if candidate_count:
                data_valid_count = sum(1 for row in bucket if _status_rank(row.get("validation_status")) >= _status_rank(ValidationStatus.DATA_VALID.value))
                backtest_complete_count = sum(1 for row in bucket if row.get("backtest_status") in _BACKTEST_SUCCESS_AND_LATER)
                walk_forward_complete_count = sum(1 for row in bucket if row.get("walk_forward_status") in _WALK_FORWARD_SUCCESS_AND_LATER)
                average_score = round(sum(_safe_float(row.get("candidate_score"), 0.0) or 0.0 for row in bucket) / candidate_count, 2)
                high_score_success_rate = round((backtest_complete_count / candidate_count) * 100.0, 2)
            else:
                data_valid_count = 0
                backtest_complete_count = 0
                walk_forward_complete_count = 0
                average_score = None
                high_score_success_rate = None
            rows.append(
                {
                    "score_bucket": label,
                    "lower_bound": lower,
                    "upper_bound": upper,
                    "candidate_count": candidate_count,
                    "data_valid_rate": round((data_valid_count / candidate_count) * 100.0, 2) if candidate_count else None,
                    "backtest_complete_rate": round((backtest_complete_count / candidate_count) * 100.0, 2) if candidate_count else None,
                    "walk_forward_complete_rate": round((walk_forward_complete_count / candidate_count) * 100.0, 2) if candidate_count else None,
                    "average_score": average_score,
                    "high_score_success_rate": high_score_success_rate,
                }
            )
        return rows

    def analyze(self, records: Iterable[CandidateRecord] | None = None) -> dict[str, Any]:
        candidate_records = list(records or self.load_records())
        performance_rows = [self._build_record(record) for record in candidate_records]
        bucket_rows = self._bucket_rows(performance_rows)
        total_count = len(performance_rows)
        average_score = None
        if total_count:
            score_sum = sum(_safe_float(row.get("candidate_score"), 0.0) or 0.0 for row in performance_rows)
            average_score = round(score_sum / total_count, 2)
        high_score_rows = [row for row in performance_rows if (_safe_float(row.get("candidate_score")) or 0.0) >= HIGH_SCORE_THRESHOLD]
        high_score_success_rate = None
        if high_score_rows:
            success_count = sum(1 for row in high_score_rows if row.get("backtest_status") in _BACKTEST_SUCCESS_AND_LATER)
            high_score_success_rate = round((success_count / len(high_score_rows)) * 100.0, 2)
        return {
            "available": bool(performance_rows),
            "state": "SAFE" if performance_rows else "STALE",
            "status_label": "SAFE" if performance_rows else "STALE",
            "detail": "performance tracking ready" if performance_rows else "candidate performance unavailable",
            "title": "Candidate Ranking Performance",
            "candidate_count": total_count,
            "average_score": average_score,
            "high_score_threshold": HIGH_SCORE_THRESHOLD,
            "high_score_candidate_count": len(high_score_rows),
            "high_score_success_rate": high_score_success_rate,
            "score_bucket_distribution": bucket_rows,
            "performance_rows": performance_rows[:5],
            "last_updated": max((str(row.get("updated_at") or "") for row in performance_rows), default=""),
        }

    def load_records(self) -> list[dict[str, Any]]:
        try:
            return self._load_jsonl(self.performance_path)
        except Exception:
            return []

    def sync(self, records: Iterable[CandidateRecord]) -> dict[str, Any]:
        candidate_records = list(records)
        performance_rows = [self._build_record(record) for record in candidate_records]
        ordered = sorted(performance_rows, key=lambda item: (str(item.get("updated_at") or ""), str(item.get("candidate_id") or "")), reverse=True)
        self._write_jsonl(self.performance_path, ordered)
        summary_rows = self._bucket_rows(ordered)
        summary_lines = ["score_bucket,candidate_count,data_valid_rate,backtest_complete_rate,walk_forward_complete_rate,average_score,high_score_success_rate"]
        for row in summary_rows:
            summary_lines.append(
                ",".join(
                    _safe_text(row.get(column))
                    for column in (
                        "score_bucket",
                        "candidate_count",
                        "data_valid_rate",
                        "backtest_complete_rate",
                        "walk_forward_complete_rate",
                        "average_score",
                        "high_score_success_rate",
                    )
                )
            )
        _atomic_write_text(self.summary_path, "\n".join(summary_lines) + "\n")
        return self.analyze(candidate_records)
