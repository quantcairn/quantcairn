from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .outcome_dataset import CandidateOutcomeSample


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _composite_label(sample: CandidateOutcomeSample) -> float:
    values: list[float] = []
    for flag in (
        sample.label_data_valid,
        sample.label_backtest_pass,
        sample.label_walk_forward_pass,
        sample.label_positive_return,
        sample.label_max_drawdown_ok,
    ):
        if flag is not None:
            values.append(1.0 if flag else 0.0)
    if values:
        return round(sum(values) / len(values), 4)
    if sample.candidate_score is not None:
        return round(_clamp(sample.candidate_score / 100.0), 4)
    if sample.data_quality_score is not None:
        return round(_clamp(sample.data_quality_score / 100.0), 4)
    return 0.0


@dataclass(slots=True)
class CalibrationBucket:
    lower_bound: float
    upper_bound: float
    raw_score_mean: float
    probability: float
    sample_count: int
    positive_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "raw_score_mean": self.raw_score_mean,
            "probability": self.probability,
            "sample_count": self.sample_count,
            "positive_rate": self.positive_rate,
        }


@dataclass(slots=True)
class CandidateScoreCalibration:
    model_version: str
    source_metric: str
    sample_count: int
    calibration_error: float | None
    raw_score_min: float | None
    raw_score_max: float | None
    buckets: list[CalibrationBucket] = field(default_factory=list)
    generated_at: str = field(default_factory=_utc_now_iso)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "source_metric": self.source_metric,
            "sample_count": self.sample_count,
            "calibration_error": self.calibration_error,
            "raw_score_min": self.raw_score_min,
            "raw_score_max": self.raw_score_max,
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
            "buckets": [bucket.to_dict() for bucket in self.buckets],
        }

    def calibrate(self, raw_score: float | None) -> tuple[float | None, float | None]:
        value = _safe_float(raw_score)
        if value is None:
            return None, None
        if not self.buckets:
            probability = _clamp(value / 100.0)
            return probability, round(probability * 100.0, 2)

        ordered = sorted(self.buckets, key=lambda bucket: bucket.raw_score_mean)
        if value <= ordered[0].raw_score_mean:
            probability = ordered[0].probability
        elif value >= ordered[-1].raw_score_mean:
            probability = ordered[-1].probability
        else:
            probability = ordered[-1].probability
            for left, right in zip(ordered, ordered[1:]):
                if left.raw_score_mean <= value <= right.raw_score_mean:
                    if right.raw_score_mean <= left.raw_score_mean:
                        probability = right.probability
                    else:
                        span = right.raw_score_mean - left.raw_score_mean
                        ratio = (value - left.raw_score_mean) / span
                        probability = left.probability + ratio * (right.probability - left.probability)
                    break
        probability = _clamp(probability)
        return round(probability, 4), round(probability * 100.0, 2)


def _bucket_label(score: float) -> tuple[float, float]:
    lower = float(int(score // 10) * 10)
    upper = min(100.0, lower + 10.0)
    return lower, upper


class CandidateScoreCalibrator:
    def __init__(self, *, bucket_size: float = 10.0) -> None:
        self.bucket_size = float(bucket_size)

    def fit(
        self,
        samples: Iterable[CandidateOutcomeSample],
        *,
        model_version: str = "calibration_v1",
        source_metric: str = "composite_target",
    ) -> CandidateScoreCalibration:
        sample_list = list(samples)
        warnings: list[str] = []
        if not sample_list:
            return CandidateScoreCalibration(
                model_version=model_version,
                source_metric=source_metric,
                sample_count=0,
                calibration_error=None,
                raw_score_min=None,
                raw_score_max=None,
                buckets=[],
                warnings=["sample_set_empty"],
            )

        paired: list[tuple[float, float]] = []
        for sample in sample_list:
            score = _safe_float(sample.candidate_score)
            if score is None:
                continue
            paired.append((score, _composite_label(sample)))
        if not paired:
            warnings.append("candidate_score_missing")
            paired = [(float(idx), _composite_label(sample)) for idx, sample in enumerate(sample_list)]

        paired.sort(key=lambda item: item[0])
        bucket_map: dict[tuple[float, float], list[tuple[float, float]]] = {}
        for score, label in paired:
            bucket = _bucket_label(score)
            bucket_map.setdefault(bucket, []).append((score, label))

        buckets: list[CalibrationBucket] = []
        running_probability = 0.0
        for lower, upper in sorted(bucket_map.keys(), key=lambda item: item[0]):
            values = bucket_map[(lower, upper)]
            raw_score_mean = sum(score for score, _ in values) / len(values)
            positive_rate = sum(label for _, label in values) / len(values)
            probability = positive_rate
            if probability < running_probability:
                probability = running_probability
                warnings.append("isotonic_monotonicity_adjusted")
            running_probability = probability
            buckets.append(
                CalibrationBucket(
                    lower_bound=lower,
                    upper_bound=upper,
                    raw_score_mean=round(raw_score_mean, 4),
                    probability=round(_clamp(probability), 4),
                    sample_count=len(values),
                    positive_rate=round(positive_rate, 4),
                )
            )

        calibration_error = None
        if buckets:
            errors: list[float] = []
            for score, label in paired:
                probability, _ = CandidateScoreCalibration(
                    model_version=model_version,
                    source_metric=source_metric,
                    sample_count=len(sample_list),
                    calibration_error=None,
                    raw_score_min=min(score for score, _ in paired),
                    raw_score_max=max(score for score, _ in paired),
                    buckets=buckets,
                ).calibrate(score)
                if probability is not None:
                    errors.append(abs(probability - label))
            if errors:
                calibration_error = round(sum(errors) / len(errors), 4)

        return CandidateScoreCalibration(
            model_version=model_version,
            source_metric=source_metric,
            sample_count=len(sample_list),
            calibration_error=calibration_error,
            raw_score_min=min(score for score, _ in paired) if paired else None,
            raw_score_max=max(score for score, _ in paired) if paired else None,
            buckets=buckets,
            warnings=warnings,
        )
