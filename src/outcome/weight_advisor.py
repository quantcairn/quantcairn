"""Read-only weight advisor for selector scoring.

Reads outcome_dataset.csv, computes feature importance and weight
suggestions via time-split validation.  Never auto-modifies baseline
configuration, never auto-activates a Challenger, never affects the
live Selector or Paper/Live trading.

Minimum 20 closed trades required.  Below threshold, returns
INSUFFICIENT_DATA with baseline-only weights.
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_DIR = PROJECT_DIR / "artifacts" / "learning"
OUTCOME_CSV_PATH = LEARNING_DIR / "outcome_dataset.csv"
WEIGHTS_PATH = LEARNING_DIR / "suggested_weights.json"
MIN_SAMPLE_SIZE = 20
MODEL_VERSION = "weight_advisor.v1"

BASELINE_WEIGHTS = {
    "volatility_score": 0.30,
    "volume_score": 0.20,
    "trend_fit_score": 0.20,
    "repeatability_score": 0.15,
    "drawdown_safety_score": 0.10,
    "correlation_bonus": 0.05,
}

FEATURE_NAMES = [
    "volatility_score",
    "volume_score",
    "trend_fit_score",
    "repeatability_score",
    "drawdown_safety_score",
    "correlation_bonus",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return float(default)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


# ── Data loading ──────────────────────────────────────────────────────

def _load_outcomes() -> list[dict[str, Any]]:
    """Load training-eligible rows from outcome_dataset.csv."""
    if not OUTCOME_CSV_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with OUTCOME_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if str(row.get("training_eligible") or "").strip().lower() != "true":
                    continue
                rows.append(dict(row))
    except Exception:
        return []
    return rows


# ── Feature / target extraction ───────────────────────────────────────

def _extract_features(outcomes: list[dict[str, Any]]) -> tuple[list[dict[str, float]], list[float]]:
    """Build feature dicts and P&L targets from outcome rows.

    Feature set:
      - formal_rank (normalized 1/rank)
      - formal_candidate_score (0-100)
      - strategy_fit_score (0-100)
      - market_regime_encoded (one-hot: NORMAL, BEAR, BULL, RANGE, UNKNOWN)

    Target: pnl_pct
    """
    features: list[dict[str, float]] = []
    targets: list[float] = []

    for row in outcomes:
        rank = _safe_float(row.get("formal_rank"), default=3.0)
        feat = {
            "formal_rank_inverse": round(1.0 / max(1.0, rank), 4),
            "formal_candidate_score": _safe_float(row.get("formal_candidate_score"), default=50.0),
            "strategy_fit_score": _safe_float(row.get("strategy_fit_score"), default=50.0),
            "hold_duration_hours": _safe_float(row.get("hold_duration_seconds"), default=0.0) / 3600.0,
            "regime_NORMAL": 1.0 if str(row.get("market_regime") or "").strip().upper() == "NORMAL" else 0.0,
            "regime_BEAR": 1.0 if str(row.get("market_regime") or "").strip().upper() == "BEAR" else 0.0,
            "regime_BULL": 1.0 if str(row.get("market_regime") or "").strip().upper() == "BULL" else 0.0,
            "regime_RANGE": 1.0 if str(row.get("market_regime") or "").strip().upper() == "RANGE" else 0.0,
        }
        features.append(feat)
        targets.append(_safe_float(row.get("pnl_pct"), default=0.0))

    return features, targets


# ── Time-split validation ─────────────────────────────────────────────

def _time_split(
    features: list[dict[str, float]],
    targets: list[float],
    outcomes: list[dict[str, Any]],
) -> tuple[list[dict[str, float]], list[float], list[dict[str, float]], list[float]]:
    """Split into train (first 70%) and test (last 30%) by chronological order."""
    n = len(outcomes)
    split_idx = int(n * 0.70)
    return (
        features[:split_idx],
        targets[:split_idx],
        features[split_idx:],
        targets[split_idx:],
    )


# ── Simple feature importance via correlation ─────────────────────────

def _correlation_importance(
    features: list[dict[str, float]],
    targets: list[float],
) -> dict[str, float]:
    """Compute Pearson correlation between each feature and P&L target.

    Returns absolute correlation values, normalized to sum to 1.0.
    """
    n = len(features)
    if n < 3:
        return {name: 1.0 / len(FEATURE_NAMES) for name in FEATURE_NAMES}

    feature_keys = sorted(set().union(*(f.keys() for f in features)))
    correlations: dict[str, float] = {}

    for key in feature_keys:
        xs = [f.get(key, 0.0) for f in features]
        ys = list(targets)

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        std_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
        std_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))

        if std_x > 0 and std_y > 0:
            corr = cov / (std_x * std_y)
            correlations[key] = abs(max(-1.0, min(1.0, corr)))
        else:
            correlations[key] = 0.0

    total = sum(correlations.values())
    if total > 0:
        correlations = {k: round(v / total, 4) for k, v in correlations.items()}
    else:
        correlations = {k: 1.0 / len(feature_keys) for k in feature_keys}

    return correlations


# ── Weight proposal ────────────────────────────────────────────────────

def _map_to_selector_features(feature_importance: dict[str, float]) -> dict[str, float]:
    """Map the outcome-derived feature importance to selector scoring dimensions.

    The selector uses 6 dimensions: volatility_score, volume_score,
    trend_fit_score, repeatability_score, drawdown_safety_score,
    correlation_bonus.

    We map outcome features to these dimensions using a fixed mapping
    and blend with baseline (50/50) to stay conservative.
    """
    # Fixed mapping: outcome-derived signal to selector dimension
    mapping: dict[str, list[str]] = {
        "volatility_score": ["formal_candidate_score"],
        "volume_score": ["hold_duration_hours"],
        "trend_fit_score": ["strategy_fit_score", "regime_NORMAL", "regime_BULL"],
        "repeatability_score": ["formal_rank_inverse"],
        "drawdown_safety_score": ["regime_BEAR", "regime_RANGE"],
        "correlation_bonus": ["strategy_fit_score", "formal_candidate_score"],
    }

    proposed: dict[str, float] = {}
    for dim, sources in mapping.items():
        raw = sum(feature_importance.get(s, 0.0) for s in sources) / max(1, len(sources))
        proposed[dim] = raw

    total = sum(proposed.values()) or 1.0
    proposed = {k: round(v / total, 4) for k, v in proposed.items()}

    # Blend 50/50 with baseline
    blended: dict[str, float] = {}
    for dim in FEATURE_NAMES:
        base = BASELINE_WEIGHTS.get(dim, 0.0)
        prop = proposed.get(dim, base)
        blended[dim] = round(base * 0.50 + prop * 0.50, 4)

    total_blended = sum(blended.values()) or 1.0
    blended = {k: round(v / total_blended, 4) for k, v in blended.items()}

    return blended


# ── Evaluation ─────────────────────────────────────────────────────────

def _evaluate_on_test(
    features: list[dict[str, float]],
    targets: list[float],
    weights: dict[str, float],
) -> dict[str, Any]:
    """Compute test-set metrics: MSE, R², hit-rate, mean direction accuracy."""
    n = len(features)
    if n < 2:
        return {"mse": 0.0, "r2": 0.0, "hit_rate": 0.0, "mean_direction_accuracy": 0.0}

    # Simple linear model: predicted = sum(weight_i * feature_i)
    feature_keys = sorted(set().union(*(f.keys() for f in features)))
    predictions: list[float] = []
    for feat in features:
        pred = sum(weights.get(k, 0.0) * feat.get(k, 0.0) for k in feature_keys)
        predictions.append(pred)

    # MSE
    mse = sum((p - y) ** 2 for p, y in zip(predictions, targets)) / n

    # R²
    mean_y = sum(targets) / n
    ss_total = sum((y - mean_y) ** 2 for y in targets)
    ss_res = sum((p - y) ** 2 for p, y in zip(predictions, targets))
    r2 = 1.0 - (ss_res / ss_total) if ss_total > 0 else 0.0

    # Hit rate: % of predictions with same sign as actual
    hits = sum(1 for p, y in zip(predictions, targets) if (p > 0) == (y > 0))
    hit_rate = hits / n if n > 0 else 0.0

    # Direction accuracy on consecutive pairs
    dir_correct = 0
    for i in range(1, n):
        if (targets[i] - targets[i - 1] > 0) == (predictions[i] - predictions[i - 1] > 0):
            dir_correct += 1
    mda = dir_correct / (n - 1) if n > 1 else 0.0

    return {
        "mse": round(mse, 4),
        "r2": round(r2, 4),
        "hit_rate": round(hit_rate, 4),
        "mean_direction_accuracy": round(mda, 4),
    }


# ── Main advisor ───────────────────────────────────────────────────────

def run_weight_advisor(
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read outcome data, compute weight suggestions, write suggested_weights.json.

    Returns complete advisor report dict.
    """
    outcomes = _load_outcomes()
    sample_size = len(outcomes)

    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "run_date": _utc_now_iso(),
        "sample_size": sample_size,
        "baseline_weights": dict(BASELINE_WEIGHTS),
        "approval_status": "PENDING_HUMAN_APPROVAL",
    }

    if sample_size < MIN_SAMPLE_SIZE:
        report.update({
            "status": "INSUFFICIENT_DATA",
            "proposed_weights": dict(BASELINE_WEIGHTS),
            "feature_importances": {},
            "evaluation_metrics": {},
            "explanation": (
                f"Insufficient training data: {sample_size} eligible outcomes, "
                f"minimum {MIN_SAMPLE_SIZE} required.  Baseline weights retained. "
                f"Paper trading must accumulate more closed trades before "
                f"weight suggestion is meaningful."
            ),
            "test_set_size": 0,
        })
    else:
        features, targets = _extract_features(outcomes)
        train_f, train_t, test_f, test_t = _time_split(features, targets, outcomes)
        importance = _correlation_importance(train_f, train_t)
        proposed = _map_to_selector_features(importance)
        baseline_eval = _evaluate_on_test(test_f, test_t, BASELINE_WEIGHTS)
        proposed_eval = _evaluate_on_test(test_f, test_t, proposed)

        improved = (
            proposed_eval.get("r2", 0.0) >= baseline_eval.get("r2", 0.0)
            and proposed_eval.get("hit_rate", 0.0) >= baseline_eval.get("hit_rate", 0.0)
        )

        report.update({
            "status": "COMPLETED",
            "train_set_size": len(train_f),
            "test_set_size": len(test_f),
            "feature_importances": {
                k: round(v, 4) for k, v in sorted(importance.items(), key=lambda x: -x[1])
            },
            "proposed_weights": dict(proposed),
            "evaluation_metrics": {
                "baseline": baseline_eval,
                "proposed": proposed_eval,
                "improved": improved,
            },
            "explanation": (
                f"Time-split validation on {sample_size} closed trades "
                f"(train={len(train_f)}, test={len(test_f)}). "
                f"Proposed weights {'improve' if improved else 'do not improve'} "
                f"over baseline.  Feature importance derived from Pearson "
                f"correlation with realized P&L %."
            ),
        })

    if not dry_run:
        _write_report(report)

    return report


def _write_report(report: dict[str, Any]) -> Path:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".suggested_weights.", suffix=".tmp", dir=str(LEARNING_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, WEIGHTS_PATH)
        return WEIGHTS_PATH
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        raise
