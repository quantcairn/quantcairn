"""Walk Forward Evaluation Framework — Phase 3B.

Evaluates whether QuantCairn's selection strategy remains stable
across different market periods by analyzing the Phase 2B dataset
through rolling time windows.

This is NOT strategy optimization, NOT ML, NOT parameter tuning.
It produces research observations only — never modifies production
configuration or trading behavior.

Reads from: artifacts/learning/dataset/records.jsonl
Writes to:  artifacts/learning/walk_forward/

Safety: read-only consumer.  No imports from scoring, engine,
broker, risk, order, or safety modules.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from itertools import accumulate
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any

from src.openalpha.learning_dataset import (
    DATASET_ROOT,
    NUMERIC_FEATURE_KEYS,
    load_dataset,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

WALK_FORWARD_VERSION = "walk_forward.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
WF_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "walk_forward"

# ── Default thresholds for robustness flags ──
DEFAULT_MIN_SAMPLES = 10
DEFAULT_DEGRADATION_THRESHOLD = 0.20         # 20% relative drop in win rate
DEFAULT_FEATURE_DRIFT_SIGMA = 2.0            # mean diff > 2× pooled std


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class WalkForwardPeriod:
    """One time-window in the walk-forward analysis."""
    period_id: str                # "P1", "P2", ...
    train_start: str              # ISO date
    train_end: str
    validation_start: str
    validation_end: str
    forward_start: str
    forward_end: str | None = None  # None if forward extends to end of data


@dataclass
class WalkForwardResult:
    """Computed metrics for one walk-forward period."""
    period_id: str
    train_samples: int = 0
    validation_samples: int = 0
    forward_samples: int = 0
    validation_win_rate: float | None = None
    validation_avg_return_21d: float | None = None
    validation_avg_mfe: float | None = None
    validation_avg_mae: float | None = None
    validation_range_success_rate: float | None = None
    forward_win_rate: float | None = None
    forward_avg_return_21d: float | None = None
    forward_range_success_rate: float | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = _mean(values)
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def _add_months(d: date, months: int) -> date:
    """Add calendar months to a date (end-of-month safe)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    # Clamp to last day of month if the target month is shorter
    import calendar
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))


# ═══════════════════════════════════════════════════════════════════════════════
# Period generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_periods(
    dates: list[str],
    *,
    train_months: int = 3,
    validation_months: int = 1,
    forward_months: int = 1,
    step_months: int = 1,
) -> list[WalkForwardPeriod]:
    """Generate rolling walk-forward periods from a sorted list of ISO dates.

    Parameters:
        dates: sorted list of ISO date strings "YYYY-MM-DD"
        train_months: calendar months in the training window
        validation_months: calendar months in the validation window
        forward_months: calendar months in the forward (out-of-sample) window
        step_months: how many months to advance the window start each period

    Returns a list of WalkForwardPeriod objects.
    """
    if not dates:
        return []

    sorted_dates = sorted(dates)
    first_date = date.fromisoformat(sorted_dates[0])
    last_date = date.fromisoformat(sorted_dates[-1])

    periods: list[WalkForwardPeriod] = []
    period_idx = 1
    window_start = first_date

    while True:
        train_end = _add_months(window_start, train_months - 1)
        # Use last day of train_end month
        import calendar
        train_end = date(train_end.year, train_end.month,
                         calendar.monthrange(train_end.year, train_end.month)[1])

        val_start = train_end + timedelta(days=1)
        val_end = _add_months(val_start, validation_months - 1)
        val_end = date(val_end.year, val_end.month,
                       calendar.monthrange(val_end.year, val_end.month)[1])

        fwd_start = val_end + timedelta(days=1)
        fwd_end = _add_months(fwd_start, forward_months - 1)
        fwd_end = date(fwd_end.year, fwd_end.month,
                       calendar.monthrange(fwd_end.year, fwd_end.month)[1])

        # Stop if forward start is beyond the last date
        if fwd_start > last_date:
            break

        # Stop if we can't even fill the validation window
        if val_start > last_date:
            break

        periods.append(WalkForwardPeriod(
            period_id=f"P{period_idx}",
            train_start=window_start.isoformat(),
            train_end=train_end.isoformat(),
            validation_start=val_start.isoformat(),
            validation_end=val_end.isoformat(),
            forward_start=fwd_start.isoformat(),
            forward_end=fwd_end.isoformat() if fwd_end <= last_date else None,
        ))

        period_idx += 1
        window_start = _add_months(window_start, step_months)

    return periods


# ═══════════════════════════════════════════════════════════════════════════════
# Per-period computation
# ═══════════════════════════════════════════════════════════════════════════════

def _rows_in_window(
    rows: list[dict[str, Any]],
    start: str,
    end: str | None,
) -> list[dict[str, Any]]:
    """Filter rows whose selection_date falls in [start, end]."""
    result: list[dict[str, Any]] = []
    for r in rows:
        d = str(r.get("selection_date") or "")
        if d < start:
            continue
        if end is not None and d > end:
            continue
        result.append(r)
    return result


def _compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate metrics for a set of dataset rows."""
    n = len(rows)
    if n == 0:
        return {
            "samples": 0,
            "win_rate": None,
            "avg_return_21d": None,
            "avg_mfe": None,
            "avg_mae": None,
            "range_success_rate": None,
        }

    returns_21d = [_safe_float(r.get("return_21d_pct"))
                   for r in rows if r.get("return_21d_pct") is not None]
    mfes = [_safe_float(r.get("mfe_21d_pct"))
            for r in rows if r.get("mfe_21d_pct") is not None]
    maes = [_safe_float(r.get("mae_21d_pct"))
            for r in rows if r.get("mae_21d_pct") is not None]

    wins = sum(1 for v in returns_21d if v > 0)
    range_successes = sum(1 for r in rows if r.get("range_success") is True)

    return {
        "samples": n,
        "win_rate": round(wins / len(returns_21d), 4) if returns_21d else None,
        "avg_return_21d": round(_mean(returns_21d), 4) if returns_21d else None,
        "avg_mfe": round(_mean(mfes), 4) if mfes else None,
        "avg_mae": round(_mean(maes), 4) if maes else None,
        "range_success_rate": round(range_successes / n, 4) if n else None,
    }


def _compute_period_result(
    rows: list[dict[str, Any]],
    period: WalkForwardPeriod,
) -> WalkForwardResult:
    """Compute WalkForwardResult for one period."""
    train_rows = _rows_in_window(rows, period.train_start, period.train_end)
    val_rows = _rows_in_window(rows, period.validation_start, period.validation_end)
    fwd_rows = _rows_in_window(rows, period.forward_start, period.forward_end)

    val_m = _compute_metrics(val_rows)
    fwd_m = _compute_metrics(fwd_rows)

    return WalkForwardResult(
        period_id=period.period_id,
        train_samples=len(train_rows),
        validation_samples=val_m["samples"],
        forward_samples=fwd_m["samples"],
        validation_win_rate=val_m["win_rate"],
        validation_avg_return_21d=val_m["avg_return_21d"],
        validation_avg_mfe=val_m["avg_mfe"],
        validation_avg_mae=val_m["avg_mae"],
        validation_range_success_rate=val_m["range_success_rate"],
        forward_win_rate=fwd_m["win_rate"],
        forward_avg_return_21d=fwd_m["avg_return_21d"],
        forward_range_success_rate=fwd_m["range_success_rate"],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Feature stability
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_feature_stability(
    rows: list[dict[str, Any]],
    periods: list[WalkForwardPeriod],
) -> dict[str, Any]:
    """Compute per-feature per-period statistics and detect drift."""
    if not rows or not periods:
        return {"features": {}, "warnings": []}

    # Collect feature values per period (using forward window as the evaluation set)
    period_features: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))

    for period in periods:
        fwd_rows = _rows_in_window(rows, period.forward_start, period.forward_end)
        for r in fwd_rows:
            for key in NUMERIC_FEATURE_KEYS:
                val = _safe_float(r.get(key))
                period_features[period.period_id][key].append(val)

    # Compute per-feature stats
    features: dict[str, dict[str, Any]] = {}
    drift_warnings: list[dict[str, Any]] = []

    for key in NUMERIC_FEATURE_KEYS:
        per_period: dict[str, dict[str, float]] = {}
        all_means: list[float] = []

        for period in periods:
            vals = period_features[period.period_id].get(key, [])
            if vals:
                m = _mean(vals)
                s = _std(vals)
                per_period[period.period_id] = {
                    "mean": round(m, 4),
                    "std": round(s, 4),
                    "samples": len(vals),
                }
                all_means.append(m)

        if not all_means:
            features[key] = {"periods": per_period, "stability": None,
                             "mean_of_means": 0.0, "drift_detected": False}
            continue

        # Stability: 1 - CV of means across periods
        mean_of_means = _mean(all_means)
        std_of_means = _std(all_means)
        cv = abs(std_of_means / mean_of_means) if mean_of_means != 0 else 0.0
        stability = round(max(0.0, 1.0 - cv), 4)

        # Drift detection: if any period mean differs from pooled mean by > 2 sigma
        drift_detected = False
        all_vals: list[float] = []
        for period in periods:
            all_vals.extend(period_features[period.period_id].get(key, []))
        pooled_std = _std(all_vals)

        for period in periods:
            pm = per_period.get(period.period_id, {}).get("mean")
            if pm is not None and pooled_std > 0:
                diff = abs(pm - mean_of_means)
                if diff > DEFAULT_FEATURE_DRIFT_SIGMA * pooled_std:
                    drift_detected = True
                    drift_warnings.append({
                        "type": "FEATURE_DRIFT",
                        "feature": key,
                        "period": period.period_id,
                        "period_mean": round(pm, 4),
                        "pooled_mean": round(mean_of_means, 4),
                        "diff": round(diff, 4),
                        "sigma_multiple": round(diff / pooled_std, 2),
                    })

        features[key] = {
            "periods": per_period,
            "stability": stability,
            "mean_of_means": round(mean_of_means, 4),
            "drift_detected": drift_detected,
        }

    return {"features": features, "warnings": drift_warnings}


# ═══════════════════════════════════════════════════════════════════════════════
# Sector stability
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_sector_stability(
    rows: list[dict[str, Any]],
    periods: list[WalkForwardPeriod],
) -> dict[str, Any]:
    """Per-sector metrics across periods."""
    if not rows or not periods:
        return {"sectors": {}, "periods": []}

    sectors: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total_samples": 0,
        "by_period": {},
    })

    for period in periods:
        fwd_rows = _rows_in_window(rows, period.forward_start, period.forward_end)
        sec_counts: Counter[str] = Counter()
        sec_wins: Counter[str] = Counter()
        sec_returns: dict[str, list[float]] = defaultdict(list)

        for r in fwd_rows:
            sec = str(r.get("sector") or "Unknown")
            sec_counts[sec] += 1
            ret = _safe_float(r.get("return_21d_pct"))
            if r.get("return_21d_pct") is not None:
                sec_returns[sec].append(ret)
                if ret > 0:
                    sec_wins[sec] += 1

        for sec in sec_counts:
            n = sec_counts[sec]
            w = sec_wins.get(sec, 0)
            rets = sec_returns.get(sec, [])
            sectors[sec]["total_samples"] += n
            sectors[sec]["by_period"][period.period_id] = {
                "samples": n,
                "success_rate": round(w / n, 4) if n else None,
                "avg_return_21d": round(_mean(rets), 4) if rets else None,
            }

    return {
        "sectors": {
            sec: {
                "total_samples": info["total_samples"],
                "by_period": info["by_period"],
            }
            for sec, info in sorted(sectors.items())
        },
        "periods": [p.period_id for p in periods],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Robustness flags
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_robustness_flags(
    results: list[WalkForwardResult],
    feature_stability: dict[str, Any],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    degradation_threshold: float = DEFAULT_DEGRADATION_THRESHOLD,
) -> list[dict[str, Any]]:
    """Generate research observations.  Never blocks trading."""
    flags: list[dict[str, Any]] = []

    # ── LOW_SAMPLE_SIZE ──
    for r in results:
        for window_name, count in [
            ("train", r.train_samples),
            ("validation", r.validation_samples),
            ("forward", r.forward_samples),
        ]:
            if count < min_samples:
                flags.append({
                    "type": "LOW_SAMPLE_SIZE",
                    "period": r.period_id,
                    "window": window_name,
                    "samples": count,
                    "threshold": min_samples,
                    "message": (
                        f"Period {r.period_id} {window_name} window has "
                        f"{count} samples (threshold: {min_samples})"
                    ),
                })

    # ── PERIOD_DEGRADATION ──
    for i in range(1, len(results)):
        prev = results[i - 1]
        curr = results[i]
        if (prev.forward_win_rate is not None
                and curr.forward_win_rate is not None
                and prev.forward_win_rate > 0):
            relative_drop = (prev.forward_win_rate - curr.forward_win_rate) / prev.forward_win_rate
            if relative_drop > degradation_threshold:
                flags.append({
                    "type": "PERIOD_DEGRADATION",
                    "period": curr.period_id,
                    "previous_period": prev.period_id,
                    "previous_win_rate": prev.forward_win_rate,
                    "current_win_rate": curr.forward_win_rate,
                    "relative_drop": round(relative_drop, 4),
                    "message": (
                        f"Forward win rate declined from {prev.forward_win_rate:.1%} "
                        f"to {curr.forward_win_rate:.1%} "
                        f"(drop: {relative_drop:.1%})"
                    ),
                })

    # ── FEATURE_DRIFT (from feature stability) ──
    for w in feature_stability.get("warnings", []):
        flags.append(w)

    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def run_walk_forward(
    *,
    train_months: int = 3,
    validation_months: int = 1,
    forward_months: int = 1,
    step_months: int = 1,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    degradation_threshold: float = DEFAULT_DEGRADATION_THRESHOLD,
) -> dict[str, Any]:
    """Run the complete walk-forward evaluation.

    Returns a summary dict with paths of written files.
    """
    rows = load_dataset()
    generated_at = _utc_now_iso()

    if not rows:
        summary: dict[str, Any] = {
            "walk_forward_version": WALK_FORWARD_VERSION,
            "generated_at": generated_at,
            "total_periods": 0,
            "total_rows": 0,
            "flags": [{
                "type": "NO_DATA",
                "message": "dataset is empty — no walk-forward evaluation possible",
            }],
        }
        _write_atomic(
            WF_ROOT / "walk_forward_summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2),
        )
        return summary

    # ── Generate periods ──
    dates = sorted({str(r.get("selection_date") or "")
                    for r in rows if r.get("selection_date")})
    periods = generate_periods(
        dates,
        train_months=train_months,
        validation_months=validation_months,
        forward_months=forward_months,
        step_months=step_months,
    )

    # ── Per-period results ──
    results = [_compute_period_result(rows, p) for p in periods]

    # ── Feature stability ──
    feature_stability = _compute_feature_stability(rows, periods)

    # ── Sector stability ──
    sector_stability = _compute_sector_stability(rows, periods)

    # ── Robustness flags ──
    flags = _generate_robustness_flags(
        results, feature_stability,
        min_samples=min_samples,
        degradation_threshold=degradation_threshold,
    )

    # ── Aggregate stability score ──
    # High stability = low variance in forward win rates across periods
    fwd_rates = [r.forward_win_rate for r in results
                 if r.forward_win_rate is not None]
    if len(fwd_rates) >= 2:
        fwd_mean = _mean(fwd_rates)
        fwd_std = _std(fwd_rates)
        cv_forward = abs(fwd_std / fwd_mean) if fwd_mean > 0 else 1.0
        overall_stability = round(max(0.0, 1.0 - cv_forward), 4)
    else:
        overall_stability = None
        fwd_mean = _mean(fwd_rates) if fwd_rates else 0.0
        fwd_std = 0.0

    # ── Write outputs ──

    # 1. Summary
    summary = {
        "walk_forward_version": WALK_FORWARD_VERSION,
        "generated_at": generated_at,
        "total_periods": len(periods),
        "total_rows": len(rows),
        "config": {
            "train_months": train_months,
            "validation_months": validation_months,
            "forward_months": forward_months,
            "step_months": step_months,
        },
        "overall_stability": overall_stability,
        "forward_win_rate_range": {
            "min": round(min(fwd_rates), 4) if fwd_rates else None,
            "max": round(max(fwd_rates), 4) if fwd_rates else None,
            "mean": round(fwd_mean, 4),
            "std": round(fwd_std, 4),
        },
        "flags_count": len(flags),
        "flags": flags[:20],  # top-level summary only shows first 20
    }
    _write_atomic(
        WF_ROOT / "walk_forward_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2),
    )

    # 2. Period results
    period_payload = {
        "walk_forward_version": WALK_FORWARD_VERSION,
        "generated_at": generated_at,
        "periods": [asdict(r) for r in results],
    }
    _write_atomic(
        WF_ROOT / "period_results.json",
        json.dumps(period_payload, ensure_ascii=False, indent=2),
    )

    # 3. Feature stability
    feat_payload = {
        "walk_forward_version": WALK_FORWARD_VERSION,
        "generated_at": generated_at,
        "features": feature_stability["features"],
        "warnings": feature_stability["warnings"],
    }
    _write_atomic(
        WF_ROOT / "feature_stability.json",
        json.dumps(feat_payload, ensure_ascii=False, indent=2),
    )

    # 4. Sector stability
    sect_payload = {
        "walk_forward_version": WALK_FORWARD_VERSION,
        "generated_at": generated_at,
        "sectors": sector_stability["sectors"],
        "periods": sector_stability["periods"],
    }
    _write_atomic(
        WF_ROOT / "sector_stability.json",
        json.dumps(sect_payload, ensure_ascii=False, indent=2),
    )

    return summary


def load_walk_forward() -> dict[str, dict[str, Any] | None]:
    """Load all walk-forward reports."""
    result: dict[str, dict[str, Any] | None] = {}
    for name in ("walk_forward_summary", "period_results",
                 "feature_stability", "sector_stability"):
        path = WF_ROOT / f"{name}.json"
        if path.exists():
            try:
                result[name] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result[name] = None
        else:
            result[name] = None
    return result
