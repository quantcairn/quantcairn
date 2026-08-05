"""Research Analytics — Phase 2C.

Reads the ML-ready dataset produced by Phase 2B and produces
analytical reports on historical selection performance.

Outputs go to artifacts/learning/analytics/:
  - performance_summary.json  — overall win rate, avg returns, MFE/MAE
  - feature_analysis.json     — per-feature stats + return correlation
  - sector_analysis.json      — per-sector performance breakdown

Safety: read-only consumer. No model training, no parameter changes,
no imports from scoring, engine, broker, risk, order, or safety.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.openalpha.learning_dataset import (
    DATASET_ROOT,
    FEATURE_KEYS,
    LABEL_KEYS,
    NUMERIC_FEATURE_KEYS,
    load_dataset,
    load_summary,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

ANALYTICS_VERSION = "research_analytics.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYTICS_ROOT = PROJECT_DIR / "artifacts" / "learning" / "analytics"


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


def _pearson_correlation(xs: list[float], ys: list[float]) -> float:
    """Compute Pearson correlation coefficient. Returns 0.0 if degenerate."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0
    mean_x = _mean(xs)
    mean_y = _mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    std_x = (sum((x - mean_x) ** 2 for x in xs) / n) ** 0.5
    std_y = (sum((y - mean_y) ** 2 for y in ys) / n) ** 0.5
    if std_x == 0.0 or std_y == 0.0:
        return 0.0
    r = cov / (n * std_x * std_y)
    return round(max(-1.0, min(1.0, r)), 4)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Overall performance summary
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_performance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Overall selection performance metrics."""
    n = len(rows)
    if n == 0:
        return _empty_section("no rows in dataset")

    returns_5d = [r["return_5d_pct"] for r in rows if r.get("return_5d_pct") is not None]
    returns_21d = [r["return_21d_pct"] for r in rows if r.get("return_21d_pct") is not None]
    mfes = [r["mfe_21d_pct"] for r in rows if r.get("mfe_21d_pct") is not None]
    maes = [r["mae_21d_pct"] for r in rows if r.get("mae_21d_pct") is not None]

    wins = [r for r in rows if r.get("return_21d_pct", 0) > 0]
    range_successes = [r for r in rows if r.get("range_success") is True]

    return {
        "total_selections": n,
        "completed_outcomes": len(returns_21d),
        "win_rate": round(len(wins) / len(returns_21d), 4) if returns_21d else 0.0,
        "average_return_5d_pct": round(_mean(returns_5d), 4),
        "average_return_21d_pct": round(_mean(returns_21d), 4),
        "average_mfe_21d_pct": round(_mean(mfes), 4),
        "average_mae_21d_pct": round(_mean(maes), 4),
        "range_success_rate": round(len(range_successes) / n, 4) if n else 0.0,
        "return_21d_positive_count": len(wins),
        "return_21d_negative_count": len(returns_21d) - len(wins),
        "range_success_count": len(range_successes),
        "range_failure_count": n - len(range_successes),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Feature correlation with return_21d
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_feature_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-feature statistics and correlation with return_21d."""
    if not rows:
        return _empty_section("no rows in dataset")

    # Split into successful (return_21d > 0) and failed (return_21d <= 0)
    wins = [r for r in rows if _safe_float(r.get("return_21d_pct")) > 0]
    losses = [r for r in rows if _safe_float(r.get("return_21d_pct")) <= 0]

    # Collect return_21d for correlation
    all_returns = [_safe_float(r.get("return_21d_pct")) for r in rows]

    features: dict[str, dict[str, Any]] = {}
    for key in NUMERIC_FEATURE_KEYS:
        win_vals = [_safe_float(r.get(key)) for r in wins if r.get(key) is not None]
        loss_vals = [_safe_float(r.get(key)) for r in losses if r.get(key) is not None]
        all_vals = [_safe_float(r.get(key)) for r in rows if r.get(key) is not None]

        if not all_vals:
            features[key] = _empty_feature_section()
            continue

        corr = _pearson_correlation(all_vals, all_returns)

        features[key] = {
            "mean_all": round(_mean(all_vals), 4),
            "mean_successful": round(_mean(win_vals), 4) if win_vals else None,
            "mean_failed": round(_mean(loss_vals), 4) if loss_vals else None,
            "min": round(min(all_vals), 4),
            "max": round(max(all_vals), 4),
            "correlation_with_return_21d": corr,
            "correlation_abs": round(abs(corr), 4),
            "correlation_direction": "positive" if corr > 0 else ("negative" if corr < 0 else "none"),
            "samples": len(all_vals),
        }

    # Sort by absolute correlation descending
    sorted_features = dict(
        sorted(features.items(),
               key=lambda item: item[1].get("correlation_abs", 0),
               reverse=True)
    )

    return {
        "features": sorted_features,
        "num_wins": len(wins),
        "num_losses": len(losses),
    }


def _empty_feature_section() -> dict[str, Any]:
    return {
        "mean_all": 0.0,
        "mean_successful": None,
        "mean_failed": None,
        "min": 0.0,
        "max": 0.0,
        "correlation_with_return_21d": 0.0,
        "correlation_abs": 0.0,
        "correlation_direction": "none",
        "samples": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Sector analysis
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_sector_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-sector performance breakdown."""
    if not rows:
        return _empty_section("no rows in dataset")

    sectors: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "samples": 0,
        "successes": 0,
        "returns_21d": [],
        "mfes": [],
        "maes": [],
        "scores": [],
    })

    for r in rows:
        sec = str(r.get("sector") or "Unknown")
        sd = sectors[sec]
        sd["samples"] += 1
        if _safe_float(r.get("return_21d_pct")) > 0:
            sd["successes"] += 1
        ret = r.get("return_21d_pct")
        if ret is not None:
            sd["returns_21d"].append(_safe_float(ret))
        mfe = r.get("mfe_21d_pct")
        if mfe is not None:
            sd["mfes"].append(_safe_float(mfe))
        mae = r.get("mae_21d_pct")
        if mae is not None:
            sd["maes"].append(_safe_float(mae))
        sd["scores"].append(_safe_float(r.get("volatility_score")))

    result: dict[str, dict[str, Any]] = {}
    for sec, sd in sorted(sectors.items()):
        n = sd["samples"]
        result[sec] = {
            "samples": n,
            "success_rate": round(sd["successes"] / n, 4) if n else 0.0,
            "avg_return_21d_pct": round(_mean(sd["returns_21d"]), 4),
            "avg_mfe_21d_pct": round(_mean(sd["mfes"]), 4),
            "avg_mae_21d_pct": round(_mean(sd["maes"]), 4),
        }

    return {"sectors": result, "unique_sectors": len(result)}


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def _empty_section(reason: str) -> dict[str, Any]:
    return {"error": reason}


def run_analytics() -> dict[str, Any]:
    """Run all analytics and write reports to artifacts/learning/analytics/.

    Returns a summary dict with paths of written files.
    """
    rows = load_dataset()

    generated_at = _utc_now_iso()
    summary = load_summary() or {}

    # ── Performance summary ──
    perf = _compute_performance_summary(rows)
    perf["analytics_version"] = ANALYTICS_VERSION
    perf["generated_at"] = generated_at
    perf["dataset_summary"] = {
        "total_samples": summary.get("total_samples", 0),
        "dataset_version": summary.get("dataset_version", ""),
        "date_range": summary.get("date_range", {}),
    }

    perf_path = ANALYTICS_ROOT / "performance_summary.json"
    _write_atomic(perf_path, json.dumps(perf, ensure_ascii=False, indent=2))

    # ── Feature analysis ──
    feat = _compute_feature_analysis(rows)
    feat["analytics_version"] = ANALYTICS_VERSION
    feat["generated_at"] = generated_at

    feat_path = ANALYTICS_ROOT / "feature_analysis.json"
    _write_atomic(feat_path, json.dumps(feat, ensure_ascii=False, indent=2))

    # ── Sector analysis ──
    sect = _compute_sector_analysis(rows)
    sect["analytics_version"] = ANALYTICS_VERSION
    sect["generated_at"] = generated_at

    sect_path = ANALYTICS_ROOT / "sector_analysis.json"
    _write_atomic(sect_path, json.dumps(sect, ensure_ascii=False, indent=2))

    return {
        "analytics_version": ANALYTICS_VERSION,
        "rows_analyzed": len(rows),
        "generated_at": generated_at,
        "files": {
            "performance_summary": str(perf_path),
            "feature_analysis": str(feat_path),
            "sector_analysis": str(sect_path),
        },
    }


def load_analytics() -> dict[str, dict[str, Any] | None]:
    """Load all analytics reports. Returns dict keyed by report type."""
    result: dict[str, dict[str, Any] | None] = {}
    for name in ("performance_summary", "feature_analysis", "sector_analysis"):
        path = ANALYTICS_ROOT / f"{name}.json"
        if path.exists():
            try:
                result[name] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result[name] = None
        else:
            result[name] = None
    return result
