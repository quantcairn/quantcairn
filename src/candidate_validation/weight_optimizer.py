from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .outcome_dataset import CandidateOutcomeSample

FACTOR_NAMES = ("liquidity_score", "trend_score", "volatility_score", "risk_score", "strategy_fit_score")
FACTOR_BOUNDS: dict[str, tuple[float, float]] = {
    "liquidity_score": (0.10, 0.40),
    "trend_score": (0.10, 0.35),
    "volatility_score": (0.05, 0.25),
    "risk_score": (0.05, 0.25),
    "strategy_fit_score": (0.10, 0.35),
}
DEFAULT_BASELINE_WEIGHTS: dict[str, float] = {
    "liquidity_score": 0.30,
    "trend_score": 0.25,
    "volatility_score": 0.15,
    "risk_score": 0.15,
    "strategy_fit_score": 0.15,
}


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


def _median(values: list[float], default: float = 0.5) -> float:
    if not values:
        return default
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def _gaussian_elimination(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(matrix)
    aug = [list(row) + [vector[idx]] for idx, row in enumerate(matrix)]
    for col in range(size):
        pivot_row = max(range(col, size), key=lambda row: abs(aug[row][col]))
        pivot = aug[pivot_row][col]
        if abs(pivot) < 1e-12:
            continue
        if pivot_row != col:
            aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        for idx in range(col, size + 1):
            aug[col][idx] /= pivot
        for row in range(size):
            if row == col:
                continue
            factor = aug[row][col]
            if abs(factor) < 1e-12:
                continue
            for idx in range(col, size + 1):
                aug[row][idx] -= factor * aug[col][idx]
    return [aug[idx][size] for idx in range(size)]


def _ridge_regression(features: list[list[float]], target: list[float], alpha: float = 0.35) -> list[float]:
    if not features:
        return [0.0] * len(FACTOR_NAMES)
    feature_count = len(features[0])
    xtx = [[0.0 for _ in range(feature_count)] for _ in range(feature_count)]
    xty = [0.0 for _ in range(feature_count)]
    for row, y_value in zip(features, target):
        for i in range(feature_count):
            xty[i] += row[i] * y_value
            for j in range(feature_count):
                xtx[i][j] += row[i] * row[j]
    for idx in range(feature_count):
        xtx[idx][idx] += alpha
    try:
        return _gaussian_elimination(xtx, xty)
    except Exception:
        return [0.0] * feature_count


def _weights_to_dict(weights: list[float]) -> dict[str, float]:
    values = {name: max(0.0, float(weights[idx] if idx < len(weights) else 0.0)) for idx, name in enumerate(FACTOR_NAMES)}
    total = sum(values.values())
    if total <= 0:
        return dict(DEFAULT_BASELINE_WEIGHTS)
    return {name: round(value / total, 4) for name, value in values.items()}


def _apply_bounds(weights: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    warnings: list[str] = []
    bounded = dict(weights)
    for name, value in list(bounded.items()):
        lower, upper = FACTOR_BOUNDS[name]
        if value < lower:
            bounded[name] = lower
            warnings.append(f"{name}:raised_to_minimum")
        elif value > upper:
            bounded[name] = upper
            warnings.append(f"{name}:clipped_to_maximum")
    total = sum(bounded.values())
    if total <= 0:
        return dict(DEFAULT_BASELINE_WEIGHTS), warnings + ["weights_invalid_using_baseline"]
    normalized = {name: value / total for name, value in bounded.items()}
    for _ in range(10):
        clipped = False
        overflow = 0.0
        underflow = 0.0
        for name, value in list(normalized.items()):
            lower, upper = FACTOR_BOUNDS[name]
            if value > upper:
                overflow += value - upper
                normalized[name] = upper
                clipped = True
            elif value < lower:
                underflow += lower - value
                normalized[name] = lower
                clipped = True
        if not clipped:
            break
        available = [name for name, value in normalized.items() if FACTOR_BOUNDS[name][0] < value < FACTOR_BOUNDS[name][1]]
        if not available:
            break
        available_total = sum(normalized[name] for name in available)
        adjustment = overflow - underflow
        if available_total <= 0:
            break
        for name in available:
            ratio = normalized[name] / available_total
            normalized[name] = _clamp(normalized[name] + adjustment * ratio, FACTOR_BOUNDS[name][0], FACTOR_BOUNDS[name][1])
    total = sum(normalized.values())
    if total > 0:
        normalized = {name: round(value / total, 4) for name, value in normalized.items()}
    return normalized, warnings


def _rank_correlations(samples: list[tuple[float, float]]) -> float | None:
    if len(samples) < 2:
        return None

    def rank(values: list[float]) -> list[float]:
        ordered = sorted(enumerate(values), key=lambda item: item[1])
        ranks = [0.0] * len(values)
        idx = 0
        while idx < len(ordered):
            j = idx
            while j < len(ordered) and ordered[j][1] == ordered[idx][1]:
                j += 1
            avg = (idx + 1 + j) / 2.0
            for k in range(idx, j):
                ranks[ordered[k][0]] = avg
            idx = j
        return ranks

    x_values = [pair[0] for pair in samples]
    y_values = [pair[1] for pair in samples]
    x_rank = rank(x_values)
    y_rank = rank(y_values)
    mean_x = sum(x_rank) / len(x_rank)
    mean_y = sum(y_rank) / len(y_rank)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_rank, y_rank))
    var_x = sum((x - mean_x) ** 2 for x in x_rank)
    var_y = sum((y - mean_y) ** 2 for y in y_rank)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(cov / math.sqrt(var_x * var_y), 4)


def _precision_at_k(predictions: list[tuple[float, float, datetime | None, str]], k: int) -> float | None:
    if not predictions:
        return None
    ordered = sorted(predictions, key=lambda item: (-item[0], item[2] or datetime.min.replace(tzinfo=timezone.utc), item[3]))
    subset = ordered[: min(k, len(ordered))]
    if not subset:
        return None
    return round(sum(1 for _, label, _, _ in subset if label >= 0.5) / len(subset), 4)


def _candidate_turnover(predictions: list[tuple[float, float, datetime | None, str]], window_size: int = 5) -> float | None:
    ordered = sorted(predictions, key=lambda item: (item[2] or datetime.min.replace(tzinfo=timezone.utc), item[3]))
    if len(ordered) <= window_size:
        return None
    top_sets: list[set[str]] = []
    for idx in range(window_size, len(ordered) + 1, window_size):
        chunk = ordered[:idx]
        top = {candidate_id for _, _, _, candidate_id in sorted(chunk, key=lambda item: -item[0])[: min(3, len(chunk))]}
        if top:
            top_sets.append(top)
    if len(top_sets) < 2:
        return None
    changes: list[float] = []
    for previous, current in zip(top_sets, top_sets[1:]):
        union = previous | current
        if not union:
            continue
        changes.append(1.0 - (len(previous & current) / len(union)))
    if not changes:
        return None
    return round(sum(changes) / len(changes), 4)


def _training_window(samples: list[CandidateOutcomeSample]) -> dict[str, str | None]:
    dates = [sample.selection_date for sample in samples if sample.selection_date]
    if not dates:
        return {"start": None, "end": None}
    return {"start": min(dates), "end": max(dates)}


def _sample_label(sample: CandidateOutcomeSample) -> float:
    composite = [
        sample.label_data_valid,
        sample.label_backtest_pass,
        sample.label_walk_forward_pass,
        sample.label_positive_return,
        sample.label_max_drawdown_ok,
    ]
    values = [1.0 if value else 0.0 for value in composite if value is not None]
    if values:
        return round(sum(values) / len(values), 4)
    if sample.candidate_score is not None:
        return round(_clamp(sample.candidate_score / 100.0), 4)
    if sample.data_quality_score is not None:
        return round(_clamp(sample.data_quality_score / 100.0), 4)
    return 0.0


def _feature_vector(sample: CandidateOutcomeSample, medians: dict[str, float]) -> list[float]:
    values: list[float] = []
    for name in FACTOR_NAMES:
        raw = getattr(sample, name)
        value = _safe_float(raw)
        if value is None:
            value = medians.get(name, 50.0)
        values.append(_clamp(value / 100.0))
    return values


def _metric_bundle(samples: list[CandidateOutcomeSample], weights: dict[str, float]) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "precision_at_3": None,
            "precision_at_5": None,
            "backtest_pass_rate": None,
            "walk_forward_pass_rate": None,
            "average_forward_return": None,
            "max_drawdown": None,
            "calibration_error": None,
            "candidate_turnover": None,
            "score_rank_correlation": None,
            "sample_warning": True,
        }
    medians = {
        name: _median([_safe_float(getattr(sample, name), 50.0) / 100.0 for sample in samples if _safe_float(getattr(sample, name)) is not None], default=0.5)
        for name in FACTOR_NAMES
    }
    scored: list[tuple[float, float, datetime | None, str]] = []
    score_pairs: list[tuple[float, float]] = []
    forward_returns: list[float] = []
    drawdowns: list[float] = []
    calibration_errors: list[float] = []
    backtest_flags: list[float] = []
    walk_forward_flags: list[float] = []
    for sample in samples:
        features = _feature_vector(sample, medians)
        prediction = sum(weights[name] * features[idx] for idx, name in enumerate(FACTOR_NAMES))
        label = _sample_label(sample)
        selection_dt = datetime.fromisoformat(sample.selection_date.replace("Z", "+00:00")) if sample.selection_date else None
        scored.append((prediction, label, selection_dt, sample.candidate_id))
        score_pairs.append((prediction, label))
        calibration_errors.append(abs(prediction - label))
        if sample.outcome_return is not None:
            forward_returns.append(float(sample.outcome_return))
        if sample.outcome_max_drawdown is not None:
            drawdowns.append(float(sample.outcome_max_drawdown))
        if sample.label_backtest_pass is not None:
            backtest_flags.append(1.0 if sample.label_backtest_pass else 0.0)
        if sample.label_walk_forward_pass is not None:
            walk_forward_flags.append(1.0 if sample.label_walk_forward_pass else 0.0)
    top3 = _precision_at_k(scored, 3)
    top5 = _precision_at_k(scored, 5)
    return {
        "sample_count": len(samples),
        "precision_at_3": top3,
        "precision_at_5": top5,
        "backtest_pass_rate": round(sum(backtest_flags) / len(backtest_flags), 4) if backtest_flags else None,
        "walk_forward_pass_rate": round(sum(walk_forward_flags) / len(walk_forward_flags), 4) if walk_forward_flags else None,
        "average_forward_return": round(sum(forward_returns) / len(forward_returns), 6) if forward_returns else None,
        "max_drawdown": round(max(drawdowns), 6) if drawdowns else None,
        "calibration_error": round(sum(calibration_errors) / len(calibration_errors), 4) if calibration_errors else None,
        "candidate_turnover": _candidate_turnover(scored),
        "score_rank_correlation": _rank_correlations(score_pairs),
        "sample_warning": len(samples) < 20,
    }


def _bootstrap_confidence_interval(samples: list[CandidateOutcomeSample], weights: dict[str, float], *, seed: int = 42, rounds: int = 120) -> dict[str, list[float]]:
    if not samples:
        return {name: [weights[name], weights[name]] for name in FACTOR_NAMES}
    rng = random.Random(seed)
    points: dict[str, list[float]] = {name: [] for name in FACTOR_NAMES}
    for _ in range(rounds):
        subset = [samples[rng.randrange(0, len(samples))] for _ in range(len(samples))]
        medians = {
            name: _median([_safe_float(getattr(sample, name), 50.0) / 100.0 for sample in subset if _safe_float(getattr(sample, name)) is not None], default=0.5)
            for name in FACTOR_NAMES
        }
        features = [_feature_vector(sample, medians) for sample in subset]
        target = [_sample_label(sample) for sample in subset]
        coeffs = _ridge_regression(features, target, alpha=0.35)
        normalized = _weights_to_dict(coeffs)
        bounded, _ = _apply_bounds(normalized)
        for name in FACTOR_NAMES:
            points[name].append(float(bounded[name]))
    intervals: dict[str, list[float]] = {}
    for name, values in points.items():
        ordered = sorted(values)
        low_index = max(0, int(round(len(ordered) * 0.05)) - 1)
        high_index = min(len(ordered) - 1, int(round(len(ordered) * 0.95)))
        intervals[name] = [round(ordered[low_index], 4), round(ordered[high_index], 4)]
    return intervals


@dataclass(slots=True)
class CandidateWeightProposal:
    model_version: str
    parent_version: str
    target_definition: str
    baseline_weights: dict[str, float]
    proposed_weights: dict[str, float]
    baseline_metrics: dict[str, Any]
    proposed_metrics: dict[str, Any]
    feature_importance: dict[str, float]
    confidence_interval: dict[str, list[float]]
    sample_size_warning: bool
    overfitting_warning: bool
    proxy_target_used: bool
    label_source_breakdown: dict[str, int]
    sample_count: int
    training_period: dict[str, str | None]
    generated_at: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "parent_version": self.parent_version,
            "target_definition": self.target_definition,
            "baseline_weights": dict(self.baseline_weights),
            "proposed_weights": dict(self.proposed_weights),
            "baseline_metrics": dict(self.baseline_metrics),
            "proposed_metrics": dict(self.proposed_metrics),
            "feature_importance": dict(self.feature_importance),
            "confidence_interval": dict(self.confidence_interval),
            "sample_size_warning": self.sample_size_warning,
            "overfitting_warning": self.overfitting_warning,
            "proxy_target_used": self.proxy_target_used,
            "label_source_breakdown": dict(self.label_source_breakdown),
            "sample_count": self.sample_count,
            "training_period": dict(self.training_period),
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
        }


class OfflineCandidateWeightOptimizer:
    def __init__(self, *, baseline_weights: dict[str, float] | None = None) -> None:
        self.baseline_weights = dict(baseline_weights or DEFAULT_BASELINE_WEIGHTS)

    def fit(self, samples: Iterable[CandidateOutcomeSample], *, model_version: str = "", parent_version: str = "baseline_v1") -> CandidateWeightProposal:
        candidate_samples = list(samples)
        warnings: list[str] = []
        if not candidate_samples:
            warnings.append("sample_set_empty")
            baseline_metrics = _metric_bundle([], self.baseline_weights)
            return CandidateWeightProposal(
                model_version=model_version or "proposal_empty",
                parent_version=parent_version,
                target_definition="composite",
                baseline_weights=dict(self.baseline_weights),
                proposed_weights=dict(self.baseline_weights),
                baseline_metrics=baseline_metrics,
                proposed_metrics=baseline_metrics,
                feature_importance={name: 0.0 for name in FACTOR_NAMES},
                confidence_interval={name: [self.baseline_weights[name], self.baseline_weights[name]] for name in FACTOR_NAMES},
                sample_size_warning=True,
                overfitting_warning=True,
                proxy_target_used=True,
                label_source_breakdown={},
                sample_count=0,
                training_period={"start": None, "end": None},
                generated_at=_utc_now_iso(),
                warnings=warnings,
            )

        medians = {
            name: _median([_safe_float(getattr(sample, name), 50.0) / 100.0 for sample in candidate_samples if _safe_float(getattr(sample, name)) is not None], default=0.5)
            for name in FACTOR_NAMES
        }
        features = [_feature_vector(sample, medians) for sample in candidate_samples]
        target = [_sample_label(sample) for sample in candidate_samples]
        label_source_breakdown: dict[str, int] = {}
        for sample in candidate_samples:
            label_source_breakdown[sample.label_source or "unknown"] = label_source_breakdown.get(sample.label_source or "unknown", 0) + 1
        proxy_target_used = any("proxy" in (sample.label_source or "") for sample in candidate_samples)

        coefficients = _ridge_regression(features, target, alpha=0.35)
        raw_weights = _weights_to_dict(coefficients)
        clipped_weights, bound_warnings = _apply_bounds(raw_weights)
        warnings.extend(bound_warnings)
        if any(coeff < 0 for coeff in coefficients):
            warnings.append("negative_coefficients_clipped")
        if proxy_target_used:
            warnings.append("proxy_target_used")

        baseline_metrics = _metric_bundle(candidate_samples, self.baseline_weights)
        proposed_metrics = _metric_bundle(candidate_samples, clipped_weights)
        feature_importance = {name: round(abs(coefficients[idx]), 4) for idx, name in enumerate(FACTOR_NAMES)}
        total_importance = sum(feature_importance.values())
        if total_importance > 0:
            feature_importance = {name: round(value / total_importance, 4) for name, value in feature_importance.items()}
        confidence_interval = _bootstrap_confidence_interval(candidate_samples, clipped_weights)

        sample_size_warning = len(candidate_samples) < 20 or sum(1 for sample in candidate_samples if sample.label_source and "artifact" in sample.label_source) < 5
        overfitting_warning = False
        if baseline_metrics.get("score_rank_correlation") is not None and proposed_metrics.get("score_rank_correlation") is not None:
            gap = float(proposed_metrics["score_rank_correlation"]) - float(baseline_metrics["score_rank_correlation"])
            if gap > 0.15 and (proposed_metrics.get("precision_at_5") or 0) < (baseline_metrics.get("precision_at_5") or 0):
                overfitting_warning = True
        if sample_size_warning:
            warnings.append("sample_size_warning")
        if overfitting_warning:
            warnings.append("overfitting_warning")

        return CandidateWeightProposal(
            model_version=model_version or f"proposal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            parent_version=parent_version,
            target_definition="composite_of_data_valid_backtest_walk_forward_return_drawdown_with_proxy_fallback",
            baseline_weights=dict(self.baseline_weights),
            proposed_weights=clipped_weights,
            baseline_metrics=baseline_metrics,
            proposed_metrics=proposed_metrics,
            feature_importance=feature_importance,
            confidence_interval=confidence_interval,
            sample_size_warning=sample_size_warning,
            overfitting_warning=overfitting_warning,
            proxy_target_used=proxy_target_used,
            label_source_breakdown=label_source_breakdown,
            sample_count=len(candidate_samples),
            training_period=_training_window(candidate_samples),
            generated_at=_utc_now_iso(),
            warnings=warnings,
        )
