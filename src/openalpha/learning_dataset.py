"""Learning Dataset Builder — Phase 2B.

Joins completed selection snapshots (Phase 1) with backfilled forward
outcomes (Phase 2A) to produce an ML-ready JSONL dataset.

Reads from:
  - artifacts/learning/selection_ledger/runs/{date}/{run_id}.json
  - artifacts/learning/selection_outcomes/{date}/{run_id}.json

Writes to:
  - artifacts/learning/dataset/records.jsonl
  - artifacts/learning/dataset/dataset_summary.json

Safety: read-only consumer.  No imports from scoring, engine, broker,
risk, order, or safety modules.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

DATASET_VERSION = "learning_dataset.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
LEDGER_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "selection_ledger"
OUTCOMES_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "selection_outcomes"
DATASET_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "dataset"

# ── Feature names (from SelectionRecord) ──
FEATURE_KEYS = [
    "volatility_score",
    "volume_score",
    "trend_score",
    "repeatability_score",
    "drawdown_score",
    "liquidity_score",
    "atr_pct",
    "gap_rate",
    "sector",
    "range_width_pct",
]

# ── Label names (from OutcomeRecord) ──
LABEL_KEYS = [
    "return_5d_pct",
    "return_21d_pct",
    "mfe_21d_pct",
    "mae_21d_pct",
    "range_success",
    "range_breakout",
    "range_breakdown",
]

# ── Numeric feature keys (excludes sector, which is categorical) ──
NUMERIC_FEATURE_KEYS = [k for k in FEATURE_KEYS if k != "sector"]


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class DatasetRow:
    """One row in the ML-ready dataset.

    Features come from SelectionRecord (Phase 1).
    Labels come from OutcomeRecord (Phase 2A).
    """

    # ── Identity (for traceability, not features) ──
    selection_run_id: str
    selection_date: str
    symbol: str

    # ── Features ──
    volatility_score: float
    volume_score: float
    trend_score: float
    repeatability_score: float
    drawdown_score: float
    liquidity_score: float
    atr_pct: float
    gap_rate: float
    sector: str
    range_width_pct: float

    # ── Labels ──
    return_5d_pct: float | None = None
    return_21d_pct: float | None = None
    mfe_21d_pct: float | None = None
    mae_21d_pct: float | None = None
    range_success: bool | None = None
    range_breakout: bool | None = None
    range_breakdown: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}


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


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def _read_json(path: Path) -> list[dict[str, Any]] | None:
    """Read a JSON file, returning None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Dataset construction
# ═══════════════════════════════════════════════════════════════════════════════

def build_learning_dataset(
    *,
    since_date: str | None = None,
    until_date: str | None = None,
) -> dict[str, Any]:
    """Join SelectionRecords with OutcomeRecords and produce a JSONL dataset.

    Only includes records where:
      - OutcomeRecord.status == "SUCCESS"
      - OutcomeRecord.return_21d_pct is not None

    Returns a summary dict.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()  # (run_id, symbol) for dedup
    skipped_missing_outcome = 0
    skipped_not_success = 0
    skipped_no_21d = 0
    skipped_duplicate = 0

    # ── Discover available dates ──
    available_dates = _discover_dates(since_date, until_date)
    if not available_dates:
        return _empty_summary("no ledger or outcome data found")

    # ── Iterate over dates and runs ──
    for date_str in sorted(available_dates):
        # Get runs for this date from either ledger or outcomes
        run_ids = _discover_runs(date_str)
        for run_id in run_ids:
            ledger_path = LEDGER_ROOT / "runs" / date_str / f"{run_id}.json"
            outcome_path = OUTCOMES_ROOT / date_str / f"{run_id}.json"

            selections = _read_json(ledger_path)
            outcomes = _read_json(outcome_path)

            if selections is None:
                continue
            if outcomes is None:
                skipped_missing_outcome += len(selections)
                continue

            # Build outcome lookup by symbol
            outcome_by_symbol: dict[str, dict[str, Any]] = {}
            for o in outcomes:
                sym = str(o.get("symbol") or "").strip().upper()
                if sym:
                    outcome_by_symbol[sym] = o

            for sel in selections:
                sym = str(sel.get("symbol") or "").strip().upper()
                if not sym:
                    continue

                key = (run_id, sym)
                if key in seen:
                    skipped_duplicate += 1
                    continue
                seen.add(key)

                outcome = outcome_by_symbol.get(sym)
                if outcome is None:
                    skipped_missing_outcome += 1
                    continue

                status = str(outcome.get("status") or "")
                if status != "SUCCESS":
                    skipped_not_success += 1
                    continue

                return_21d = outcome.get("return_21d_pct")
                if return_21d is None:
                    skipped_no_21d += 1
                    continue

                # ── Build the row ──
                row = DatasetRow(
                    selection_run_id=str(sel.get("selection_run_id") or run_id),
                    selection_date=str(sel.get("selection_date") or date_str),
                    symbol=sym,
                    # Features
                    volatility_score=_safe_float(sel.get("feature_volatility_score")
                                                or sel.get("volatility_score")),
                    volume_score=_safe_float(sel.get("feature_volume_score")
                                            or sel.get("volume_score")),
                    trend_score=_safe_float(sel.get("feature_trend_score")
                                           or sel.get("trend_fit_score")),
                    repeatability_score=_safe_float(sel.get("feature_repeatability_score")
                                                   or sel.get("repeatability_score")),
                    drawdown_score=_safe_float(sel.get("feature_drawdown_safety_score")
                                              or sel.get("drawdown_safety_score")),
                    liquidity_score=_safe_float(sel.get("feature_liquidity_score")
                                               or sel.get("liquidity_score")),
                    atr_pct=_safe_float(sel.get("atr_pct")),
                    gap_rate=_safe_float(sel.get("gap_rate")),
                    sector=str(sel.get("sector") or ""),
                    range_width_pct=_safe_float(sel.get("range_width_pct")),
                    # Labels
                    return_5d_pct=(_safe_float(outcome.get("return_5d_pct"))
                                   if outcome.get("return_5d_pct") is not None else None),
                    return_21d_pct=_safe_float(return_21d),
                    mfe_21d_pct=(_safe_float(outcome.get("mfe_21d_pct"))
                                if outcome.get("mfe_21d_pct") is not None else None),
                    mae_21d_pct=(_safe_float(outcome.get("mae_21d_pct"))
                                if outcome.get("mae_21d_pct") is not None else None),
                    range_success=(outcome.get("range_outcome") == "SUCCESS"
                                   if outcome.get("range_outcome") is not None else None),
                    range_breakout=_safe_bool(outcome.get("range_breakout")),
                    range_breakdown=_safe_bool(outcome.get("range_breakdown")),
                )
                rows.append(row.to_dict())

    # ── Write dataset ──
    if rows:
        records_path = DATASET_ROOT / "records.jsonl"
        _write_atomic(
            records_path,
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        )

    # ── Compute summary statistics ──
    summary = _compute_summary(rows)
    summary["dataset_version"] = DATASET_VERSION
    summary["total_samples"] = len(rows)
    summary["skipped_missing_outcome"] = skipped_missing_outcome
    summary["skipped_not_success"] = skipped_not_success
    summary["skipped_no_21d"] = skipped_no_21d
    summary["skipped_duplicate"] = skipped_duplicate
    summary["generated_at"] = _utc_now_iso()
    summary["date_range"] = {
        "earliest": min(r["selection_date"] for r in rows) if rows else None,
        "latest": max(r["selection_date"] for r in rows) if rows else None,
    }

    summary_path = DATASET_ROOT / "dataset_summary.json"
    _write_atomic(summary_path, json.dumps(summary, ensure_ascii=False, indent=2))

    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _discover_dates(
    since_date: str | None = None,
    until_date: str | None = None,
) -> set[str]:
    """Discover dates that exist in the ledger directory.

    Includes ALL ledger dates so mismatches (ledger has data but
    outcomes don't) are properly counted as skipped_missing_outcome.
    """
    available: set[str] = set()
    ledger_runs = LEDGER_ROOT / "runs"
    if ledger_runs.exists():
        for d in ledger_runs.iterdir():
            if d.is_dir():
                available.add(d.name)

    # Also include outcome-only dates (shouldn't happen but be safe)
    if OUTCOMES_ROOT.exists():
        for d in OUTCOMES_ROOT.iterdir():
            if d.is_dir():
                available.add(d.name)

    if since_date:
        available = {d for d in available if d >= since_date}
    if until_date:
        available = {d for d in available if d <= until_date}

    return available


def _discover_runs(date_str: str) -> set[str]:
    """Discover run IDs for a given date from BOTH ledger and outcomes."""
    run_ids: set[str] = set()
    ledger_date_dir = LEDGER_ROOT / "runs" / date_str
    if ledger_date_dir.exists():
        for f in ledger_date_dir.iterdir():
            if f.suffix == ".json":
                run_ids.add(f.stem)
    outcome_date_dir = OUTCOMES_ROOT / date_str
    if outcome_date_dir.exists():
        for f in outcome_date_dir.iterdir():
            if f.suffix == ".json":
                run_ids.add(f.stem)
    return run_ids


def _empty_summary(reason: str) -> dict[str, Any]:
    """Build a summary dict for empty datasets."""
    return {
        "dataset_version": DATASET_VERSION,
        "total_samples": 0,
        "success_rate": 0.0,
        "average_return_5d": 0.0,
        "average_return_21d": 0.0,
        "average_mfe_21d": 0.0,
        "average_mae_21d": 0.0,
        "sector_distribution": {},
        "feature_statistics": {},
        "label_statistics": {},
        "skipped_missing_outcome": 0,
        "skipped_not_success": 0,
        "skipped_no_21d": 0,
        "skipped_duplicate": 0,
        "generated_at": _utc_now_iso(),
        "date_range": {"earliest": None, "latest": None},
        "error": reason,
    }


def _compute_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate statistics from a list of dataset rows."""
    if not rows:
        return {
            "total_samples": 0,
            "success_rate": 0.0,
            "average_return_5d": 0.0,
            "average_return_21d": 0.0,
            "average_mfe_21d": 0.0,
            "average_mae_21d": 0.0,
            "sector_distribution": {},
            "feature_statistics": {},
        }

    n = len(rows)

    returns_5d = [r["return_5d_pct"] for r in rows if r.get("return_5d_pct") is not None]
    returns_21d = [r["return_21d_pct"] for r in rows if r.get("return_21d_pct") is not None]
    mfes = [r["mfe_21d_pct"] for r in rows if r.get("mfe_21d_pct") is not None]
    maes = [r["mae_21d_pct"] for r in rows if r.get("mae_21d_pct") is not None]
    successes = [r["range_success"] for r in rows if r.get("range_success") is not None]

    # Sector distribution
    sector_counts: Counter[str] = Counter()
    for r in rows:
        sector_counts[r.get("sector", "Unknown")] += 1

    # Feature statistics (mean, std, min, max for each numeric feature)
    feature_stats: dict[str, dict[str, float]] = {}
    for key in NUMERIC_FEATURE_KEYS:
        vals = [_safe_float(r.get(key)) for r in rows if r.get(key) is not None]
        if vals:
            feature_stats[key] = {
                "mean": round(sum(vals) / len(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
            }

    # Label statistics
    label_stats: dict[str, dict[str, float]] = {}
    for key in ["return_5d_pct", "return_21d_pct", "mfe_21d_pct", "mae_21d_pct"]:
        vals = [_safe_float(r.get(key)) for r in rows if r.get(key) is not None]
        if vals:
            label_stats[key] = {
                "mean": round(sum(vals) / len(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "std": round(
                    (sum((v - sum(vals) / len(vals)) ** 2 for v in vals) / len(vals)) ** 0.5, 4
                ),
            }

    return {
        "total_samples": n,
        "success_rate": round(sum(successes) / len(successes), 4) if successes else 0.0,
        "average_return_5d": round(sum(returns_5d) / len(returns_5d), 4) if returns_5d else 0.0,
        "average_return_21d": round(sum(returns_21d) / len(returns_21d), 4) if returns_21d else 0.0,
        "average_mfe_21d": round(sum(mfes) / len(mfes), 4) if mfes else 0.0,
        "average_mae_21d": round(sum(maes) / len(maes), 4) if maes else 0.0,
        "sector_distribution": dict(sorted(sector_counts.items())),
        "feature_statistics": feature_stats,
        "label_statistics": label_stats,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════════

def load_dataset() -> list[dict[str, Any]]:
    """Load the dataset from records.jsonl, returning a list of dicts."""
    path = DATASET_ROOT / "records.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_summary() -> dict[str, Any] | None:
    """Load the dataset summary."""
    path = DATASET_ROOT / "dataset_summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
