"""Research Benchmark Framework — Phase 6A.

Synthesizes data from four existing research pipelines into a
unified benchmark snapshot with a composite performance grade.

Reads pre-computed JSON files directly — NEVER imports research
modules to avoid accidental recalculation.

Reads from:
  - artifacts/learning/analytics/
  - artifacts/learning/walk_forward/
  - artifacts/learning/paper_analytics/
  - artifacts/learning/research_history/

Writes to:
  - artifacts/learning/research_benchmark/
    ├── benchmark_summary.json
    └── snapshots/benchmark_{date}.json

Safety: research-only.  No imports from scoring, engine, broker,
risk, order, or safety modules.  No live trading.  No ML training.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

BENCHMARK_VERSION = "research_benchmark.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_ROOT = PROJECT_DIR / "artifacts" / "learning"
BENCHMARK_ROOT = LEARNING_ROOT / "research_benchmark"

# ── Composite score weights ──
WEIGHT_SELECTION = 0.35
WEIGHT_WALK_FORWARD = 0.25
WEIGHT_PAPER = 0.25
WEIGHT_REGISTRY = 0.15

# ── Grade thresholds ──
GRADE_A = 0.70
GRADE_B = 0.55
GRADE_C = 0.40
GRADE_D = 0.25


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BenchmarkSnapshot:
    """One unified research benchmark snapshot at a point in time."""

    benchmark_date: str = ""
    benchmark_version: str = BENCHMARK_VERSION

    # ── Source availability ──
    selection_analytics_available: bool = False
    walk_forward_available: bool = False
    paper_analytics_available: bool = False
    registry_available: bool = False
    coverage_score: float = 0.0

    # ── Selection performance (Phase 2C) ──
    selection_win_rate: float | None = None
    selection_avg_return_21d: float | None = None
    selection_range_success_rate: float | None = None
    selection_total_samples: int = 0
    selection_sector_distribution: dict[str, int] | None = None

    # ── Walk-forward stability (Phase 3B) ──
    stability_score: float | None = None
    stability_periods: int = 0
    stability_win_rate_range: dict[str, float] | None = None

    # ── Paper trading (Phase 5C) ──
    paper_win_rate: float | None = None
    paper_avg_return: float | None = None
    paper_avg_holding_days: float | None = None
    paper_mfe_capture_rate: float | None = None
    paper_total_trades: int = 0
    paper_breakdown_rate: float | None = None
    paper_breakout_rate: float | None = None
    paper_sector_distribution: dict[str, int] | None = None

    # ── Data quality (Phase 4A) ──
    dataset_total_rows: int = 0
    dataset_missing_outcome_ratio: float | None = None
    dataset_unique_sectors: int = 0
    dataset_regimes_represented: list[str] | None = None
    ml_readiness_status: str = ""

    # ── Composite ──
    composite_score: float | None = None
    composite_grade: str = "INSUFFICIENT_DATA"

    # ── Meta ──
    generated_at: str = ""

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


def _deep_get(d: dict[str, Any] | None, *keys: str) -> Any:
    """Safely navigate nested dicts.  Returns None if any key is missing."""
    if d is None:
        return None
    current: Any = d
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return None
        if current is None:
            return None
    return current


# ═══════════════════════════════════════════════════════════════════════════════
# Data source readers (direct file I/O — NO module imports)
# ═══════════════════════════════════════════════════════════════════════════════

def _read_selection_analytics() -> dict[str, Any] | None:
    """Read Phase 2C selection analytics summary."""
    path = LEARNING_ROOT / "analytics" / "performance_summary.json"
    return _read_json(path)


def _read_walk_forward() -> dict[str, Any] | None:
    """Read Phase 3B walk-forward summary."""
    path = LEARNING_ROOT / "walk_forward" / "walk_forward_summary.json"
    return _read_json(path)


def _read_paper_analytics() -> dict[str, Any] | None:
    """Read Phase 5C paper analytics summary."""
    path = LEARNING_ROOT / "paper_analytics" / "summary.json"
    return _read_json(path)


def _read_registry() -> dict[str, Any] | None:
    """Read Phase 4A registry quality report."""
    path = LEARNING_ROOT / "research_history" / "research_quality_report.json"
    return _read_json(path)


def _read_regime_tags() -> dict[str, Any] | None:
    """Read Phase 4A regime tags."""
    path = LEARNING_ROOT / "research_history" / "regime_tags.json"
    return _read_json(path)


def _read_dataset_tracker() -> dict[str, Any] | None:
    """Read Phase 4A dataset tracker."""
    path = LEARNING_ROOT / "research_history" / "dataset_tracker.json"
    return _read_json(path)


# ═══════════════════════════════════════════════════════════════════════════════
# Sector distribution extractors
# ═══════════════════════════════════════════════════════════════════════════════

def _read_selection_sectors() -> dict[str, int] | None:
    """Extract sector distribution from Phase 2C analytics."""
    path = LEARNING_ROOT / "analytics" / "sector_analysis.json"
    data = _read_json(path)
    if not data:
        return None
    sectors: dict[str, int] = {}
    raw = data.get("sector_distribution") or data.get("sectors") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                sectors[k] = _safe_int(v.get("samples") or v.get("trades"))
            elif isinstance(v, (int, float)):
                sectors[k] = _safe_int(v)
    return sectors or None


def _read_paper_sectors() -> dict[str, int] | None:
    """Extract sector distribution from Phase 5C paper analytics."""
    path = LEARNING_ROOT / "paper_analytics" / "sector_analysis.json"
    data = _read_json(path)
    if not data:
        return None
    sectors: dict[str, int] = {}
    raw = data.get("sectors") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                sectors[k] = _safe_int(v.get("trades"))
    return sectors or None


# ═══════════════════════════════════════════════════════════════════════════════
# Composite score
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_composite(snap: BenchmarkSnapshot) -> float:
    """Compute weighted composite score from available sources.

    Normalizes weights so missing sources are excluded from the average.
    Returns 0.0 if fewer than 2 sources are available.
    """
    total = 0.0
    weights = 0.0

    if snap.selection_analytics_available and snap.selection_win_rate is not None:
        total += snap.selection_win_rate * WEIGHT_SELECTION
        weights += WEIGHT_SELECTION

    if snap.walk_forward_available and snap.stability_score is not None:
        total += snap.stability_score * WEIGHT_WALK_FORWARD
        weights += WEIGHT_WALK_FORWARD

    if snap.paper_analytics_available and snap.paper_win_rate is not None:
        total += snap.paper_win_rate * WEIGHT_PAPER
        weights += WEIGHT_PAPER

    if snap.registry_available and snap.dataset_missing_outcome_ratio is not None:
        quality = max(0.0, 1.0 - snap.dataset_missing_outcome_ratio)
        total += quality * WEIGHT_REGISTRY
        weights += WEIGHT_REGISTRY

    if weights < 2 * min(WEIGHT_SELECTION, WEIGHT_WALK_FORWARD, WEIGHT_PAPER, WEIGHT_REGISTRY):
        # Fewer than 2 sources contributed
        return 0.0

    return round(total / weights, 4) if weights > 0 else 0.0


def _grade(score: float) -> str:
    """Map composite score to letter grade."""
    if score >= GRADE_A:
        return "A"
    elif score >= GRADE_B:
        return "B"
    elif score >= GRADE_C:
        return "C"
    elif score >= GRADE_D:
        return "D"
    else:
        return "F"


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def run_benchmark() -> dict[str, Any]:
    """Read all research artifacts and produce a unified benchmark snapshot.

    Reads from four data sources (Phase 2C, 3B, 5C, 4A) independently.
    Computes a composite score only when ≥2 sources are available.
    Writes a dated snapshot and updates the historical benchmark summary.
    """
    today_str = date.today().isoformat()
    now_iso = _utc_now_iso()

    # ── 1. Read each source independently ──
    sel_data = _read_selection_analytics()
    wf_data = _read_walk_forward()
    paper_data = _read_paper_analytics()
    reg_data = _read_registry()

    # ── 2. Build snapshot ──
    snap = BenchmarkSnapshot(
        benchmark_date=today_str,
        benchmark_version=BENCHMARK_VERSION,
        generated_at=now_iso,
    )

    # ── Source availability ──
    sel_avail = sel_data is not None and "error" not in sel_data
    wf_avail = wf_data is not None
    paper_avail = paper_data is not None
    reg_avail = reg_data is not None

    snap.selection_analytics_available = sel_avail
    snap.walk_forward_available = wf_avail
    snap.paper_analytics_available = paper_avail
    snap.registry_available = reg_avail

    available_count = int(sel_avail) + int(wf_avail) + int(paper_avail) + int(reg_avail)
    snap.coverage_score = round(available_count / 4.0, 2)

    # ── Selection metrics ──
    if sel_avail and sel_data:
        perf = sel_data.get("performance") or {}
        snap.selection_win_rate = _safe_float(perf.get("win_rate"))
        snap.selection_avg_return_21d = _safe_float(perf.get("avg_return_21d_pct"))
        snap.selection_range_success_rate = _safe_float(perf.get("range_success_rate"))
        snap.selection_total_samples = _safe_int(sel_data.get("total_selections")
                                                 or _deep_get(sel_data, "totals", "total_trades"))
        snap.selection_sector_distribution = _read_selection_sectors()

    # ── Walk-forward stability ──
    if wf_avail and wf_data:
        snap.stability_score = _safe_float(wf_data.get("overall_stability"))
        snap.stability_periods = _safe_int(wf_data.get("total_periods"))
        fwd_range = wf_data.get("forward_win_rate_range") or {}
        if isinstance(fwd_range, dict):
            snap.stability_win_rate_range = {
                "min": _safe_float(fwd_range.get("min")),
                "max": _safe_float(fwd_range.get("max")),
                "mean": _safe_float(fwd_range.get("mean")),
            }

    # ── Paper trading ──
    if paper_avail and paper_data:
        perf = paper_data.get("performance") or {}
        totals = paper_data.get("totals") or {}
        rng = paper_data.get("range_analysis") or {}
        snap.paper_win_rate = _safe_float(perf.get("win_rate"))
        snap.paper_avg_return = _safe_float(perf.get("avg_return_pct"))
        snap.paper_avg_holding_days = _safe_float(perf.get("avg_holding_days"))
        snap.paper_mfe_capture_rate = _safe_float(perf.get("mfe_capture_rate"))
        snap.paper_total_trades = _safe_int(totals.get("total_trades"))
        snap.paper_breakdown_rate = _safe_float(rng.get("breakdown_rate"))
        snap.paper_breakout_rate = _safe_float(rng.get("breakout_rate"))
        snap.paper_sector_distribution = _read_paper_sectors()

    # ── Data quality ──
    if reg_avail and reg_data:
        snap.dataset_total_rows = _safe_int(reg_data.get("total_dataset_rows"))
        snap.dataset_missing_outcome_ratio = _safe_float(reg_data.get("missing_outcome_ratio"))
        snap.dataset_unique_sectors = _safe_int(reg_data.get("unique_sectors"))
        snap.dataset_regimes_represented = list(reg_data.get("regimes_represented") or [])
        snap.ml_readiness_status = str(reg_data.get("ml_readiness_status") or "")

        # Fallback: try ml_readiness.json directly
        if not snap.ml_readiness_status:
            ml_path = LEARNING_ROOT / "research_history" / "ml_readiness.json"
            ml_data = _read_json(ml_path)
            if ml_data:
                snap.ml_readiness_status = str(ml_data.get("status") or "")

    # ── Composite ──
    if available_count >= 2:
        snap.composite_score = _compute_composite(snap)
        snap.composite_grade = _grade(snap.composite_score)
    else:
        snap.composite_score = None
        snap.composite_grade = "INSUFFICIENT_DATA"

    # ── 3. Write snapshot ──
    snapshot_path = BENCHMARK_ROOT / "snapshots" / f"benchmark_{today_str}.json"
    _write_atomic(snapshot_path, json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))

    # ── 4. Update summary ──
    _update_summary(snap.to_dict())

    return snap.to_dict()


# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

def _update_summary(snapshot_dict: dict[str, Any]) -> None:
    """Update the benchmark_summary.json with latest snapshot + history."""
    summary_path = BENCHMARK_ROOT / "benchmark_summary.json"

    existing: dict[str, Any] = {}
    if summary_path.exists():
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    history: list[dict[str, Any]] = existing.get("history") or []
    if isinstance(history, list):
        # Avoid duplicate dates
        existing_dates = {h.get("benchmark_date") for h in history if h.get("benchmark_date")}
        if snapshot_dict.get("benchmark_date") not in existing_dates:
            # Keep only summary fields in history (lightweight)
            history.append({
                "benchmark_date": snapshot_dict.get("benchmark_date"),
                "coverage_score": snapshot_dict.get("coverage_score"),
                "composite_score": snapshot_dict.get("composite_score"),
                "composite_grade": snapshot_dict.get("composite_grade"),
                "selection_win_rate": snapshot_dict.get("selection_win_rate"),
                "stability_score": snapshot_dict.get("stability_score"),
                "paper_win_rate": snapshot_dict.get("paper_win_rate"),
                "generated_at": snapshot_dict.get("generated_at"),
            })

    summary = {
        "benchmark_version": BENCHMARK_VERSION,
        "latest_snapshot": snapshot_dict,
        "history": history[-90:],  # keep last 90 days
        "updated_at": _utc_now_iso(),
    }

    _write_atomic(summary_path, json.dumps(summary, ensure_ascii=False, indent=2))


def load_benchmark() -> dict[str, Any] | None:
    """Load the latest benchmark summary."""
    path = BENCHMARK_ROOT / "benchmark_summary.json"
    return _read_json(path)
