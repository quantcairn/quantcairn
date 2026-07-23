"""Weight Advisor v2 — reads v3 outcome datasets, proposes weight adjustments.

Data source:   artifacts/learning/outcome_dataset.parquet (→ CSV fallback)
Output:        artifacts/learning/suggested_weights.json
                artifacts/learning/strategy_performance.json
Governance:    creates LearningProposal (PENDING_HUMAN_APPROVAL)

Advisory only — NEVER modifies active weights, Selector, Broker, or
TradingEngine.  All activation requires human approval via Governance.
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_DIR = PROJECT_DIR / "artifacts" / "learning"
OUTCOME_CSV_PATH = LEARNING_DIR / "outcome_dataset.csv"
OUTCOME_PARQUET_PATH = LEARNING_DIR / "outcome_dataset.parquet"
WEIGHTS_PATH = LEARNING_DIR / "suggested_weights.json"
STRATEGY_PERF_PATH = LEARNING_DIR / "strategy_performance.json"
MIN_SAMPLE_SIZE = 20
MODEL_VERSION = "weight_advisor.v2"
MAX_DELTA_PER_FACTOR = 0.05


BASELINE_WEIGHTS: dict[str, float] = {
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

# V3 parquet → selector dimension mapping
FEATURE_COLUMN_MAP = {
    "volatility_score": "feature_volatility_score",
    "volume_score": "feature_volume_score",
    "trend_fit_score": "feature_trend_score",
    "repeatability_score": "feature_repeatability_score",
    "drawdown_safety_score": "feature_drawdown_safety_score",
    "correlation_bonus": "feature_risk_score",  # proxy: risk ↔ correlation
}

CHINESE_LABELS: dict[str, str] = {
    "volatility_score": "波动率得分",
    "volume_score": "成交量得分",
    "trend_fit_score": "趋势拟合得分",
    "repeatability_score": "可重复性得分",
    "drawdown_safety_score": "回撤安全得分",
    "correlation_bonus": "相关性红利",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(result) or math.isinf(result):
        return float(default)
    return round(result, 6)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading (Parquet → CSV fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_outcomes_parquet() -> list[dict[str, Any]] | None:
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(str(OUTCOME_PARQUET_PATH))
        return table.to_pylist()
    except Exception:
        return None


def _load_outcomes_csv() -> list[dict[str, Any]]:
    if not OUTCOME_CSV_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with OUTCOME_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("training_eligible") or "").strip().lower() != "true":
                    continue
                rows.append(dict(row))
    except Exception:
        return []
    return rows


def _load_outcomes() -> list[dict[str, Any]]:
    parquet_rows = _load_outcomes_parquet()
    if parquet_rows:
        return [r for r in parquet_rows
                if str(r.get("training_eligible") or "").strip().lower() == "true"]
    return _load_outcomes_csv()


# ═══════════════════════════════════════════════════════════════════════════════
# Factor performance analysis
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class FactorPerformance:
    factor: str
    label: str
    win_avg: float = 0.0
    loss_avg: float = 0.0
    even_avg: float = 0.0
    diff: float = 0.0          # win_avg - loss_avg
    direction: str = ""         # "positive" | "negative"
    win_count: int = 0
    loss_count: int = 0
    even_count: int = 0


def _compute_factor_performance(outcomes: list[dict[str, Any]]) -> list[FactorPerformance]:
    """Compute WIN/LOSS/EVEN averages for each feature column."""
    rng = max(1, len(outcomes))
    # Group outcomes by WIN/LOSS/EVEN
    groups: dict[str, list[dict[str, Any]]] = {"WIN": [], "LOSS": [], "EVEN": []}
    for r in outcomes:
        oc = str(r.get("outcome") or "").strip().upper()
        groups.setdefault(oc, []).append(r)

    results: list[FactorPerformance] = []
    for dim in FEATURE_NAMES:
        col = FEATURE_COLUMN_MAP.get(dim, f"feature_{dim}")
        label = CHINESE_LABELS.get(dim, dim)
        wins = [_safe_float(r.get(col)) for r in groups.get("WIN", [])]
        losses = [_safe_float(r.get(col)) for r in groups.get("LOSS", [])]
        evens = [_safe_float(r.get(col)) for r in groups.get("EVEN", [])]
        wa = round(sum(wins) / max(1, len(wins)), 4) if wins else 0.0
        la = round(sum(losses) / max(1, len(losses)), 4) if losses else 0.0
        ea = round(sum(evens) / max(1, len(evens)), 4) if evens else 0.0
        diff = round(wa - la, 4)
        results.append(FactorPerformance(
            factor=dim, label=label,
            win_avg=wa, loss_avg=la, even_avg=ea,
            diff=diff,
            direction="positive" if diff > 0 else "negative",
            win_count=len(wins), loss_count=len(losses), even_count=len(evens),
        ))
    results.sort(key=lambda x: -x.diff)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Weight suggestion with delta protection
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class WeightSuggestion:
    factor: str
    label: str
    current: float
    suggested: float
    delta: float
    confidence: float = 0.0


def _compute_weight_suggestions(
    factors: list[FactorPerformance],
    sample_size: int,
) -> list[WeightSuggestion]:
    """Propose per-factor weight adjustments with ±0.05 cap.

    Confidence = f(sample_size, win_rate_differential, factor direction consistency).
    """
    results: list[WeightSuggestion] = []
    total_outcomes = sum(f.win_count + f.loss_count + f.even_count for f in factors) / max(1, len(factors))
    win_rate = sum(f.win_count for f in factors) / max(1, total_outcomes)

    # Factor consistency: how many factors point in the same direction as overall win rate
    positive_factors = sum(1 for f in factors if f.diff > 0)

    for factor in factors:
        base = BASELINE_WEIGHTS.get(factor.factor, 0.20)
        # Scale: positive diff → increase weight, negative diff → decrease
        # Magnitude: proportional to |diff| normalized against max |diff|
        max_abs_diff = max(abs(f.diff) for f in factors) if factors else 1.0
        if max_abs_diff > 0:
            scale = min(1.0, abs(factor.diff) / max_abs_diff)
        else:
            scale = 0.0

        raw_delta = scale * MAX_DELTA_PER_FACTOR
        if factor.diff > 0:
            raw_delta = raw_delta
        else:
            raw_delta = -raw_delta

        # ── Confidence ───────────────────────────────────────────────────
        # 1. Sample size confidence (logistic: 0 at 0, ~0.9 at 60+)
        sample_conf = 1.0 / (1.0 + math.exp(-0.08 * (sample_size - 30)))
        sample_conf = max(0.0, min(1.0, sample_conf))

        # 2. Factor direction agreement: how well does this factor separate WIN/LOSS
        total_factor_samples = factor.win_count + factor.loss_count + factor.even_count
        agreement = 0.5
        if total_factor_samples > 0:
            # If diff is large relative to the averages, confidence is higher
            avg_val = (factor.win_avg + factor.loss_avg) / 2.0 if (factor.win_avg + factor.loss_avg) > 0 else 1.0
            agreement = min(1.0, abs(factor.diff) / max(1.0, avg_val) * 2.0)

        # 3. Win-rate differential: how different is WIN from LOSS overall
        wr_diff = abs(factor.win_avg - factor.loss_avg) / max(0.01, factor.win_avg + factor.loss_avg)
        wr_conf = min(1.0, wr_diff * 3.0)

        confidence = round(0.35 * sample_conf + 0.35 * wr_conf + 0.30 * agreement, 4)

        # Apply delta cap and clamp
        capped_delta = max(-MAX_DELTA_PER_FACTOR, min(MAX_DELTA_PER_FACTOR, raw_delta))
        suggested = round(base + capped_delta, 4)

        results.append(WeightSuggestion(
            factor=factor.factor,
            label=factor.label,
            current=base,
            suggested=suggested,
            delta=capped_delta,
            confidence=confidence,
        ))

    # ── Normalize to sum to 1.0 ──────────────────────────────────────────
    total = sum(r.suggested for r in results) or 1.0
    for r in results:
        r.suggested = round(r.suggested / total, 4)
        r.delta = round(r.suggested - r.current, 4)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy performance analysis
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_strategy_performance(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-strategy trade statistics."""
    strategies: dict[str, dict[str, Any]] = {}
    for r in outcomes:
        strat = str(r.get("strategy") or r.get("strategy_family") or "unknown")
        outcome = str(r.get("outcome") or "").strip().upper()
        pnl = _safe_float(r.get("return_pct") or r.get("pnl_pct"))
        realized = _safe_float(r.get("realized_pnl"))
        s = strategies.setdefault(strat, {
            "strategy": strat, "trade_count": 0,
            "wins": 0, "losses": 0, "evens": 0,
            "avg_return": 0.0, "total_return": 0.0,
            "total_realized_pnl": 0.0,
        })
        s["trade_count"] += 1
        s["total_return"] += pnl
        s["total_realized_pnl"] += realized
        if outcome == "WIN":
            s["wins"] += 1
        elif outcome == "LOSS":
            s["losses"] += 1
        else:
            s["evens"] += 1

    items = list(strategies.values())
    for s in items:
        tc = max(1, s["trade_count"])
        s["win_rate"] = round(s["wins"] / tc * 100.0, 2)
        s["avg_return"] = round(s["total_return"] / tc, 4)
        s["total_return"] = round(s["total_return"], 4)
        s["total_realized_pnl"] = round(s["total_realized_pnl"], 4)

    items.sort(key=lambda x: -x["total_realized_pnl"])
    best = items[0]["strategy"] if items else ""
    worst = items[-1]["strategy"] if items else ""

    return {
        "generated_at": _utc_now_iso(),
        "strategies": items,
        "best_strategy": best,
        "worst_strategy": worst,
        "strategy_count": len(items),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Governance integration
# ═══════════════════════════════════════════════════════════════════════════════

def _register_governance_proposal(report: dict[str, Any]) -> None:
    """Register the weight proposal with the governance system."""
    try:
        from src.outcome.governance import LearningGovernance
        gov = LearningGovernance()
        gov.register_weight_proposal(dry_run=False)
    except Exception:
        pass  # governance registration is best-effort


# ═══════════════════════════════════════════════════════════════════════════════
# Main advisor
# ═══════════════════════════════════════════════════════════════════════════════

def run_weight_advisor(
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read outcome data, compute weight suggestions, write reports.

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
            "weight_suggestions": [],
            "factor_performance": [],
            "top_positive_factors": [],
            "top_negative_factors": [],
            "strategy_performance": {},
            "evaluation_metrics": {},
            "explanation": (
                f"Insufficient training data: {sample_size} eligible outcomes, "
                f"minimum {MIN_SAMPLE_SIZE} required.  Baseline weights retained."
            ),
        })
    else:
        # 1. Factor performance analysis
        factors = _compute_factor_performance(outcomes)
        top_positive = [{"factor": f.factor, "label": f.label, "win_avg": f.win_avg,
                          "loss_avg": f.loss_avg, "diff": f.diff}
                         for f in factors if f.diff > 0][:3]
        top_negative = [{"factor": f.factor, "label": f.label, "win_avg": f.win_avg,
                          "loss_avg": f.loss_avg, "diff": f.diff}
                         for f in factors if f.diff < 0][-3:]
        top_negative.reverse()

        # 2. Weight suggestions with delta protection
        suggestions = _compute_weight_suggestions(factors, sample_size)
        proposed_weights: dict[str, float] = {}
        weight_details: list[dict[str, Any]] = []
        for s in suggestions:
            proposed_weights[s.factor] = s.suggested
            weight_details.append({
                "factor": s.factor,
                "label": s.label,
                "current": s.current,
                "suggested": s.suggested,
                "delta": s.delta,
                "confidence": s.confidence,
            })

        # 3. Strategy performance
        strategy_perf = _compute_strategy_performance(outcomes)

        # 4. Confidence summary (average of all individual factor confidences)
        avg_confidence = (
            round(sum(s.confidence for s in suggestions) / max(1, len(suggestions)), 4)
            if suggestions else 0.0
        )

        report.update({
            "status": "COMPLETED",
            "proposed_weights": proposed_weights,
            "weight_suggestions": weight_details,
            "factor_performance": [
                {"factor": f.factor, "label": f.label, "win_avg": f.win_avg,
                 "loss_avg": f.loss_avg, "even_avg": f.even_avg, "diff": f.diff,
                 "direction": f.direction}
                for f in factors
            ],
            "top_positive_factors": top_positive,
            "top_negative_factors": top_negative,
            "strategy_performance": strategy_perf,
            "avg_confidence": avg_confidence,
            "evaluation_metrics": {
                "max_delta_per_factor": MAX_DELTA_PER_FACTOR,
                "total_weight": round(sum(proposed_weights.values()), 4),
                "sample_size": sample_size,
                "weight_count": len(proposed_weights),
            },
            "explanation": (
                f"Factor performance analysis on {sample_size} closed trades. "
                f"Confidence={avg_confidence:.2%}. "
                f"Weight adjustments capped at ±{MAX_DELTA_PER_FACTOR} per factor. "
                f"Human approval required via governance."
            ),
        })

    if not dry_run:
        _write_report(report)
        _write_strategy_performance(report.get("strategy_performance") or {})
        if report.get("status") == "COMPLETED":
            _register_governance_proposal(report)

    return report


def _write_report(report: dict[str, Any]) -> Path:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".suggested_weights.", suffix=".tmp", dir=str(LEARNING_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        os.replace(tmp, WEIGHTS_PATH)
        return WEIGHTS_PATH
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _write_strategy_performance(data: dict[str, Any]) -> Path:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".strategy_perf.", suffix=".tmp", dir=str(LEARNING_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        os.replace(tmp, STRATEGY_PERF_PATH)
        return STRATEGY_PERF_PATH
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        raise
