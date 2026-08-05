"""Research History Registry — Phase 4A.

Accumulates long-term research data across selection runs.
Reads existing artifacts (bundles, ledger, outcomes, dataset)
and writes aggregated registry snapshots.

All output is research observation only — never modifies
production configuration, trading behavior, or model parameters.

Reads from:
  - artifacts/learning/ (ledger, outcomes, dataset, analytics)
  - state/selection_bundles/ (historical selection data)
  - artifacts/selection/preflight.json (market state)

Writes to:
  - artifacts/learning/research_history/
    ├── run_index.json
    ├── dataset_tracker.json
    ├── regime_tags.json
    ├── research_quality_report.json
    └── ml_readiness.json

Safety: read-only consumer.  No imports from scoring, engine,
broker, risk, order, or safety modules.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

REGISTRY_VERSION = "research_registry.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
HISTORY_ROOT = PROJECT_DIR / "artifacts" / "learning" / "research_history"
LEARNING_ROOT = PROJECT_DIR / "artifacts" / "learning"
BUNDLES_ROOT = PROJECT_DIR / "state" / "selection_bundles"

# ── ML readiness thresholds (configurable, not hardcoded) ──
DEFAULT_ML_READINESS_THRESHOLDS: dict[str, int | float] = {
    "min_selections": 500,
    "min_outcomes": 300,
    "min_regimes": 3,
    "min_sectors": 5,
    "min_date_range_days": 90,
    "max_missing_outcome_ratio": 0.3,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RunRecord:
    """One entry per selection run. Written by fire-and-forget hook."""
    selection_run_id: str
    selection_date: str              # ISO date
    recorded_at: str                 # ISO timestamp

    # ── Run metadata ──
    execution_mode: str              # "RESEARCH" | "PAPER" | "LIVE"
    run_mode: str                    # "FULL" | "AFTER_MARKET" | "EOD_ONLY" | "DEGRADED"
    market_state: str = ""           # from preflight
    universe_size: int = 0
    max_symbols: int = 0

    # ── Pipeline outcome ──
    formal_candidates: list[str] | None = None
    formal_count: int = 0
    preview_count: int = 0
    quality_fallback_active: bool = False
    selection_stage: str = ""

    # ── Data quality ──
    data_mode: str = ""
    fallback_used: bool = False

    # ── Scoring ──
    scoring_eligible_count: int = 0

    # ── Meta ──
    research_registry_version: str = REGISTRY_VERSION


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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
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


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _read_json_list(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError):
        return None


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Run Record
# ═══════════════════════════════════════════════════════════════════════════════

def record_selection_run(
    *,
    run_id: str,
    date_str: str,
    execution_mode: str = "",
    run_mode: str = "",
    market_state: str = "",
    universe_size: int = 0,
    max_symbols: int = 0,
    formal_candidates: list[str] | None = None,
    formal_count: int = 0,
    preview_count: int = 0,
    quality_fallback_active: bool = False,
    selection_stage: str = "",
    data_mode: str = "",
    fallback_used: bool = False,
    scoring_eligible_count: int = 0,
) -> Path | None:
    """Record one selection run in the run_index.json registry.

    Called from AIStrategySelector.run_selection() as a fire-and-forget
    hook.  Failure is silent — never blocks the pipeline.
    """
    try:
        record = RunRecord(
            selection_run_id=str(run_id),
            selection_date=str(date_str),
            recorded_at=_utc_now_iso(),
            execution_mode=str(execution_mode),
            run_mode=str(run_mode),
            market_state=str(market_state),
            universe_size=_safe_int(universe_size),
            max_symbols=_safe_int(max_symbols),
            formal_candidates=list(formal_candidates) if formal_candidates else None,
            formal_count=_safe_int(formal_count),
            preview_count=_safe_int(preview_count),
            quality_fallback_active=bool(quality_fallback_active),
            selection_stage=str(selection_stage),
            data_mode=str(data_mode),
            fallback_used=bool(fallback_used),
            scoring_eligible_count=_safe_int(scoring_eligible_count),
        )
    except Exception:
        return None

    # Load existing index, append, deduplicate
    index_path = HISTORY_ROOT / "run_index.json"
    existing = _read_json_list(index_path) or []
    seen = {(r.get("selection_run_id"), r.get("selection_date"))
            for r in existing if r.get("selection_run_id")}
    key = (record.selection_run_id, record.selection_date)
    if key in seen:
        return None  # already recorded

    existing.append(asdict(record))
    _write_atomic(index_path, json.dumps(existing, ensure_ascii=False, indent=2))
    return index_path


def load_run_index() -> list[dict[str, Any]]:
    """Load the full run index."""
    return _read_json_list(HISTORY_ROOT / "run_index.json") or []


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Dataset Tracker
# ═══════════════════════════════════════════════════════════════════════════════

def build_dataset_tracker() -> dict[str, Any]:
    """Scan artifacts/learning/ directories and build a dataset growth snapshot."""
    snapshot_date = _utc_now_iso()

    # ── Selection ledger ──
    ledger_runs_dir = LEARNING_ROOT / "selection_ledger" / "runs"
    ledger_runs = 0
    ledger_candidates = 0
    if ledger_runs_dir.exists():
        for date_dir in ledger_runs_dir.iterdir():
            if date_dir.is_dir():
                for run_file in date_dir.iterdir():
                    if run_file.suffix == ".json":
                        ledger_runs += 1
                        data = _read_json_list(run_file)
                        if data:
                            ledger_candidates += len(data)

    # ── Outcomes ──
    outcomes_dir = LEARNING_ROOT / "selection_outcomes"
    outcome_runs = 0
    outcome_candidates = 0
    outcome_success = 0
    outcome_failed = 0
    outcome_insufficient = 0
    if outcomes_dir.exists():
        for date_dir in outcomes_dir.iterdir():
            if date_dir.is_dir():
                for run_file in date_dir.iterdir():
                    if run_file.suffix == ".json":
                        outcome_runs += 1
                        data = _read_json_list(run_file)
                        if data:
                            outcome_candidates += len(data)
                            for r in data:
                                status = str(r.get("status") or "")
                                if status == "SUCCESS":
                                    outcome_success += 1
                                elif status == "FAILED_DATA":
                                    outcome_failed += 1
                                elif status == "INSUFFICIENT_HISTORY":
                                    outcome_insufficient += 1

    # ── Dataset ──
    dataset_path = LEARNING_ROOT / "dataset" / "records.jsonl"
    dataset_rows = 0
    dataset_sectors: dict[str, int] = {}
    dataset_date_earliest: str | None = None
    dataset_date_latest: str | None = None
    if dataset_path.exists():
        try:
            for line in dataset_path.read_text(encoding="utf-8").strip().split("\n"):
                if line.strip():
                    r = json.loads(line)
                    dataset_rows += 1
                    sec = str(r.get("sector") or "Unknown")
                    dataset_sectors[sec] = dataset_sectors.get(sec, 0) + 1
                    d = str(r.get("selection_date") or "")
                    if d:
                        if dataset_date_earliest is None or d < dataset_date_earliest:
                            dataset_date_earliest = d
                        if dataset_date_latest is None or d > dataset_date_latest:
                            dataset_date_latest = d
        except (json.JSONDecodeError, OSError):
            pass

    total_attempted = outcome_success + outcome_failed + outcome_insufficient
    success_rate = (outcome_success / total_attempted) if total_attempted > 0 else 0.0

    tracker = {
        "snapshot_date": snapshot_date,
        "registry_version": REGISTRY_VERSION,
        "ledger": {
            "runs": ledger_runs,
            "candidates": ledger_candidates,
        },
        "outcomes": {
            "runs": outcome_runs,
            "candidates": outcome_candidates,
            "success": outcome_success,
            "failed": outcome_failed,
            "insufficient": outcome_insufficient,
            "success_rate": round(success_rate, 4),
        },
        "dataset": {
            "rows": dataset_rows,
            "sectors": dict(sorted(dataset_sectors.items())),
            "date_earliest": dataset_date_earliest,
            "date_latest": dataset_date_latest,
            "date_range_days": (
                (date.fromisoformat(dataset_date_latest)
                 - date.fromisoformat(dataset_date_earliest)).days
                if dataset_date_earliest and dataset_date_latest
                else 0
            ),
        },
    }

    _write_atomic(
        HISTORY_ROOT / "dataset_tracker.json",
        json.dumps(tracker, ensure_ascii=False, indent=2),
    )
    return tracker


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Regime Tagging
# ═══════════════════════════════════════════════════════════════════════════════

def build_regime_tags() -> dict[str, Any]:
    """Build market regime labels from bundle benchmark data and preflight snapshots.

    Simple v1 heuristic:
      - SPY 21d return > 10%  → "bull"
      - SPY 21d return < -10% → "bear"
      - SPY 21d return in [-10%, 10%] → "sideways"
      - SPY 21d volatility > 30% → "high_volatility" (overrides)
      - SPY 21d volatility < 10% → "low_volatility" (overrides)
    """
    generated_at = _utc_now_iso()
    tags: list[dict[str, Any]] = []

    if not BUNDLES_ROOT.exists():
        result = {
            "registry_version": REGISTRY_VERSION,
            "generated_at": generated_at,
            "tags": [],
            "count": 0,
            "source": "none",
        }
        _write_atomic(
            HISTORY_ROOT / "regime_tags.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    for run_id in sorted(os.listdir(BUNDLES_ROOT)):
        bundle_dir = BUNDLES_ROOT / run_id / "selection_bundle_v1"
        state_path = bundle_dir / "ai_selection_state.json"
        meta_path = bundle_dir / "bundle_metadata.json"

        state_data = _read_json(state_path)
        if not state_data:
            continue
        sel_date = str(state_data.get("et_date") or state_data.get("selection_date") or "")
        if not sel_date:
            continue

        # Try to extract benchmark data
        meta_data = _read_json(meta_path)
        benchmark_return_21d: float | None = None
        volatility_21d: float | None = None

        if meta_data:
            layers = meta_data.get("candidate_layers", {})
            for layer_cands in layers.values():
                if isinstance(layer_cands, list) and layer_cands:
                    c = layer_cands[0]
                    bm_changes = c.get("benchmark_change_pct", {})
                    if isinstance(bm_changes, dict):
                        spy_return = bm_changes.get("SPY.US") or bm_changes.get("SPY")
                        if spy_return is not None:
                            benchmark_return_21d = _safe_float(spy_return)
                    break

        # Determine regime
        confidence = "low"
        regime = "unknown"

        if benchmark_return_21d is not None:
            confidence = "medium"
            if benchmark_return_21d > 10.0:
                regime = "bull"
            elif benchmark_return_21d < -10.0:
                regime = "bear"
            else:
                regime = "sideways"

        tags.append({
            "date": sel_date,
            "regime": regime,
            "source": "bundle_benchmark",
            "confidence": confidence,
            "benchmark_return_21d": benchmark_return_21d,
            "volatility_21d": volatility_21d,
            "max_drawdown_21d": None,
        })

    result = {
        "registry_version": REGISTRY_VERSION,
        "generated_at": generated_at,
        "tags": tags,
        "count": len(tags),
        "source": "selection_bundles",
    }

    _write_atomic(
        HISTORY_ROOT / "regime_tags.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Research Quality Report
# ═══════════════════════════════════════════════════════════════════════════════

def build_quality_report() -> dict[str, Any]:
    """Build a research quality report from the registry, tracker, and regime data."""
    generated_at = _utc_now_iso()

    runs = load_run_index()
    tracker_data = _read_json(HISTORY_ROOT / "dataset_tracker.json") or {}
    regime_data = _read_json(HISTORY_ROOT / "regime_tags.json") or {}

    total_runs = len(runs)
    dataset_rows = tracker_data.get("dataset", {}).get("rows", 0)

    # Missing outcome ratio
    outcome_runs = tracker_data.get("outcomes", {}).get("runs", 0)
    missing_outcome_ratio = (
        round(1.0 - (outcome_runs / total_runs), 4) if total_runs > 0 else 1.0
    )

    # Average sample age
    today = date.today()
    ages: list[float] = []
    for r in runs:
        d_str = str(r.get("selection_date") or "")
        if d_str:
            try:
                ages.append((today - date.fromisoformat(d_str)).days)
            except ValueError:
                pass
    avg_age = round(_mean(ages), 1) if ages else 0.0

    # Sector coverage
    sectors = tracker_data.get("dataset", {}).get("sectors", {})

    # Feature availability (from dataset)
    feature_keys = [
        "volatility_score", "volume_score", "trend_score",
        "repeatability_score", "drawdown_score", "liquidity_score",
        "atr_pct", "gap_rate", "range_width_pct",
    ]
    feature_availability: dict[str, float] = {}
    dataset_path = LEARNING_ROOT / "dataset" / "records.jsonl"
    if dataset_path.exists() and dataset_rows > 0:
        non_null_counts: dict[str, int] = {k: 0 for k in feature_keys}
        try:
            for line in dataset_path.read_text(encoding="utf-8").strip().split("\n"):
                if line.strip():
                    r = json.loads(line)
                    for k in feature_keys:
                        if r.get(k) is not None and (not isinstance(r.get(k), float) or r.get(k) == r.get(k)):
                            non_null_counts[k] += 1
            for k in feature_keys:
                feature_availability[k] = round(non_null_counts[k] / dataset_rows, 4)
        except (json.JSONDecodeError, OSError):
            pass
    else:
        for k in feature_keys:
            feature_availability[k] = 0.0

    # Date coverage
    date_coverage_start = tracker_data.get("dataset", {}).get("date_earliest")
    date_coverage_end = tracker_data.get("dataset", {}).get("date_latest")

    # Regimes represented
    regimes = set()
    for t in (regime_data.get("tags") or []):
        r = t.get("regime")
        if r and r != "unknown":
            regimes.add(r)

    report = {
        "registry_version": REGISTRY_VERSION,
        "generated_at": generated_at,
        "total_runs_recorded": total_runs,
        "total_dataset_rows": dataset_rows,
        "missing_outcome_ratio": missing_outcome_ratio,
        "average_sample_age_days": avg_age,
        "sector_coverage": dict(sorted(sectors.items())),
        "unique_sectors": len(sectors),
        "feature_availability": feature_availability,
        "date_coverage_start": date_coverage_start,
        "date_coverage_end": date_coverage_end,
        "regimes_represented": sorted(regimes),
    }

    _write_atomic(
        HISTORY_ROOT / "research_quality_report.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# 5. ML Readiness
# ═══════════════════════════════════════════════════════════════════════════════

def build_ml_readiness(
    thresholds: dict[str, int | float] | None = None,
) -> dict[str, Any]:
    """Evaluate whether the dataset meets minimum thresholds for ML research.

    Returns a report.  NEVER triggers automatic training.
    """
    generated_at = _utc_now_iso()
    thresholds = thresholds or DEFAULT_ML_READINESS_THRESHOLDS

    quality = _read_json(HISTORY_ROOT / "research_quality_report.json") or {}
    tracker = _read_json(HISTORY_ROOT / "dataset_tracker.json") or {}
    regime_data = _read_json(HISTORY_ROOT / "regime_tags.json") or {}

    total_selections = quality.get("total_runs_recorded", 0)
    total_outcomes = tracker.get("outcomes", {}).get("success", 0)
    regime_count = len(quality.get("regimes_represented", []))
    sector_count = quality.get("unique_sectors", 0)
    date_range_days = tracker.get("dataset", {}).get("date_range_days", 0)
    missing_ratio = quality.get("missing_outcome_ratio", 1.0)

    # Check each threshold
    checks: dict[str, dict[str, Any]] = {}
    all_pass = True

    for key, req in thresholds.items():
        if key == "min_selections":
            actual: int | float = total_selections
        elif key == "min_outcomes":
            actual = total_outcomes
        elif key == "min_regimes":
            actual = regime_count
        elif key == "min_sectors":
            actual = sector_count
        elif key == "min_date_range_days":
            actual = date_range_days
        elif key == "max_missing_outcome_ratio":
            actual = missing_ratio
            # For this check: pass if actual <= max
            passed = actual <= float(req)
            checks[key] = {
                "required": req,
                "actual": round(actual, 4),
                "passed": passed,
            }
            if not passed:
                all_pass = False
            continue
        else:
            continue

        passed = actual >= int(req)
        checks[key] = {
            "required": req,
            "actual": actual,
            "passed": passed,
        }
        if not passed:
            all_pass = False

    # Determine status
    if all_pass:
        status = "READY"
        recommendation = "Dataset meets minimum thresholds for ML research."
    else:
        failed_count = sum(1 for c in checks.values() if not c["passed"])
        if failed_count <= 2:
            status = "BORDERLINE"
            recommendation = (
                f"Close to readiness — {failed_count} threshold(s) not met. "
                "Continue accumulating data."
            )
        else:
            status = "NOT_READY"
            recommendation = (
                f"{failed_count} threshold(s) not met. "
                "More data needed before ML research is viable."
            )

    reasons = []
    for key, check in checks.items():
        if not check["passed"]:
            reasons.append(
                f"{key}: required={check['required']}, actual={check['actual']}"
            )

    report = {
        "registry_version": REGISTRY_VERSION,
        "generated_at": generated_at,
        "status": status,
        "reasons": reasons,
        "thresholds": checks,
        "recommendation": recommendation,
    }

    _write_atomic(
        HISTORY_ROOT / "ml_readiness.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Convenience — update all
# ═══════════════════════════════════════════════════════════════════════════════

def update_all() -> dict[str, Any]:
    """Run all registry builders and return a summary."""
    tracker = build_dataset_tracker()
    regime = build_regime_tags()
    quality = build_quality_report()
    readiness = build_ml_readiness()

    return {
        "registry_version": REGISTRY_VERSION,
        "generated_at": _utc_now_iso(),
        "runs_recorded": len(load_run_index()),
        "tracker": {
            "ledger_runs": tracker.get("ledger", {}).get("runs", 0),
            "outcome_runs": tracker.get("outcomes", {}).get("runs", 0),
            "dataset_rows": tracker.get("dataset", {}).get("rows", 0),
        },
        "regime_tags": regime.get("count", 0),
        "quality": {
            "missing_outcome_ratio": quality.get("missing_outcome_ratio", 1.0),
            "unique_sectors": quality.get("unique_sectors", 0),
        },
        "ml_readiness": readiness.get("status", "NOT_READY"),
    }
