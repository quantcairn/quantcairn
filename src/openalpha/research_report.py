"""Research Report Generator — Phase 6C.

Generates a unified research report by synthesizing data from
Phase 6A (benchmark), Phase 6B (regime), Phase 3B (walk-forward),
Phase 2C (analytics), and Phase 5C (paper analytics).

Reads pre-computed JSON files directly — NEVER imports research
modules to prevent accidental recalculation.

Reads from:
  - artifacts/learning/research_benchmark/benchmark_summary.json
  - artifacts/learning/regime_analysis/regime_summary.json
  - artifacts/learning/walk_forward/walk_forward_summary.json
  - artifacts/learning/analytics/performance_summary.json
  - artifacts/learning/paper_analytics/summary.json

Writes to:
  - artifacts/learning/research_report/research_report.json

Safety: research-only.  No imports from scoring, engine, broker,
risk, order, or safety modules.  No live trading.  No ML training.
No trading recommendations.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

REPORT_VERSION = "research_report.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_ROOT = PROJECT_DIR / "artifacts" / "learning"
REPORT_ROOT = LEARNING_ROOT / "research_report"

# ── Research status thresholds ──
MIN_SOURCES_FOR_READY = 3
MIN_SOURCES_FOR_DEVELOPING = 1


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskItem:
    type: str
    severity: str      # "HIGH" | "MEDIUM" | "LOW"
    description: str
    source: str = ""   # which data source triggered this risk


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


# ═══════════════════════════════════════════════════════════════════════════════
# Data source readers
# ═══════════════════════════════════════════════════════════════════════════════

def _read_benchmark() -> dict[str, Any] | None:
    path = LEARNING_ROOT / "research_benchmark" / "benchmark_summary.json"
    return _read_json(path)


def _read_regime() -> dict[str, Any] | None:
    path = LEARNING_ROOT / "regime_analysis" / "regime_summary.json"
    return _read_json(path)


def _read_walk_forward() -> dict[str, Any] | None:
    path = LEARNING_ROOT / "walk_forward" / "walk_forward_summary.json"
    return _read_json(path)


def _read_selection_analytics() -> dict[str, Any] | None:
    path = LEARNING_ROOT / "analytics" / "performance_summary.json"
    return _read_json(path)


def _read_paper_summary() -> dict[str, Any] | None:
    path = LEARNING_ROOT / "paper_analytics" / "summary.json"
    return _read_json(path)


# ═══════════════════════════════════════════════════════════════════════════════
# Research status
# ═══════════════════════════════════════════════════════════════════════════════

def _determine_research_status(sources_available: int, benchmark: dict | None) -> str:
    """Determine the overall research readiness status."""
    if sources_available >= MIN_SOURCES_FOR_READY:
        return "READY"
    elif sources_available >= MIN_SOURCES_FOR_DEVELOPING:
        return "DEVELOPING"
    else:
        return "INSUFFICIENT_DATA"


# ═══════════════════════════════════════════════════════════════════════════════
# Risk generation
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_risks(
    benchmark: dict | None,
    regime: dict | None,
    walk_forward: dict | None,
    selection: dict | None,
    paper: dict | None,
) -> list[dict[str, str]]:
    """Generate risk observations from available data. Research only."""
    risks: list[dict[str, str]] = []

    # ── LOW_SAMPLE_SIZE ──
    if benchmark:
        snap = benchmark.get("latest_snapshot") or {}
        total = _safe_int(snap.get("selection_total_samples"))
        if total > 0 and total < 50:
            risks.append({
                "type": "LOW_SAMPLE_SIZE",
                "severity": "MEDIUM",
                "description": f"Only {total} selection samples — analysis may not be statistically reliable",
                "source": "benchmark",
            })

    # ── REGIME_DEPENDENCY ──
    if regime:
        robustness = str(regime.get("regime_robustness") or "")
        if robustness == "WEAK":
            risks.append({
                "type": "REGIME_DEPENDENCY",
                "severity": "HIGH",
                "description": "Selection performance varies significantly by market regime — strategy is regime-dependent",
                "source": "regime_analysis",
            })
        elif robustness == "MODERATE":
            risks.append({
                "type": "REGIME_DEPENDENCY",
                "severity": "MEDIUM",
                "description": "Selection performance shows moderate regime sensitivity",
                "source": "regime_analysis",
            })

    # ── PERFORMANCE_DEGRADATION ──
    if walk_forward:
        flags = walk_forward.get("flags") or []
        degradation_flags = [f for f in flags if isinstance(f, dict)
                             and f.get("type") == "PERIOD_DEGRADATION"]
        if degradation_flags:
            risks.append({
                "type": "PERFORMANCE_DEGRADATION",
                "severity": "MEDIUM",
                "description": f"Walk-forward analysis detected {len(degradation_flags)} period(s) with performance degradation",
                "source": "walk_forward",
            })

    # ── INSUFFICIENT_PAPER_HISTORY ──
    if paper:
        totals = paper.get("totals") or {}
        trades = _safe_int(totals.get("total_trades"))
        if trades > 0 and trades < 10:
            risks.append({
                "type": "INSUFFICIENT_PAPER_HISTORY",
                "severity": "LOW",
                "description": f"Only {trades} paper trades recorded — paper analytics not reliable",
                "source": "paper_analytics",
            })

    return risks


# ═══════════════════════════════════════════════════════════════════════════════
# Recommendations
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_recommendations(
    risks: list[dict[str, str]],
    status: str,
) -> list[str]:
    """Generate research recommendations based on risk observations."""
    recs: list[str] = []

    if status == "INSUFFICIENT_DATA":
        recs.append(
            "Accumulate more selection data — run the selection pipeline daily "
            "and ensure Phase 1-2 backfill is operational."
        )
        return recs

    if status == "DEVELOPING":
        recs.append(
            "Continue accumulating research data across multiple market regimes "
            "to improve statistical significance."
        )

    risk_types = {r["type"] for r in risks}

    if "LOW_SAMPLE_SIZE" in risk_types:
        recs.append(
            "Increase sample size by running the pipeline consistently. "
            "At least 50 selections are recommended for reliable analysis."
        )
    if "REGIME_DEPENDENCY" in risk_types:
        recs.append(
            "Monitor regime-dependent performance. Consider regime-aware "
            "selection filters when multiple regimes are well-represented."
        )
    if "PERFORMANCE_DEGRADATION" in risk_types:
        recs.append(
            "Investigate the periods flagged for performance degradation "
            "in the walk-forward analysis to identify root causes."
        )
    if "INSUFFICIENT_PAPER_HISTORY" in risk_types:
        recs.append(
            "Run the Phase 5 paper research pipeline to build paper "
            "trading history for more complete evaluation."
        )

    if not recs:
        recs.append(
            "Research pipeline is healthy. Continue accumulating data "
            "and monitoring for regime shifts."
        )

    return recs


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def run_research_report() -> dict[str, Any]:
    """Generate a unified research report from all available data sources.

    Synthesizes benchmark, regime, walk-forward, selection analytics,
    and paper analytics into a single structured report.  All metrics
    are read from pre-computed JSON — no recalculation.
    """
    generated_at = _utc_now_iso()

    # ── Read all available sources ──
    benchmark = _read_benchmark()
    regime = _read_regime()
    walk_forward = _read_walk_forward()
    selection = _read_selection_analytics()
    paper = _read_paper_summary()

    # Count available sources
    sources_available = sum(1 for s in [benchmark, regime, walk_forward,
                                         selection, paper] if s is not None)

    status = _determine_research_status(sources_available, benchmark)

    # ── Extract executive summary ──
    latest_snapshot = (benchmark.get("latest_snapshot") or {}) if benchmark else {}
    grade = str(latest_snapshot.get("composite_grade") or status)

    key_findings: list[str] = []

    if benchmark and latest_snapshot:
        wr = _safe_float(latest_snapshot.get("selection_win_rate"))
        if wr > 0:
            key_findings.append(
                f"Selection strategy win rate: {wr:.1%} "
                f"({_safe_int(latest_snapshot.get('selection_total_samples'))} samples)"
            )
        cs = latest_snapshot.get("composite_score")
        if cs is not None:
            key_findings.append(
                f"Composite research grade: {grade} (score: {_safe_float(cs):.2f})"
            )

    if regime and regime.get("available"):
        key_findings.append(
            f"Best performing regime: {regime.get('best_regime', 'unknown')} "
            f"(robustness: {regime.get('regime_robustness', 'UNKNOWN')})"
        )

    if walk_forward:
        st = _safe_float(walk_forward.get("overall_stability"))
        if st > 0:
            key_findings.append(
                f"Walk-forward stability: {st:.2f} "
                f"({_safe_int(walk_forward.get('total_periods'))} periods)"
            )

    if paper:
        perf = paper.get("performance") or {}
        pwr = _safe_float(perf.get("win_rate"))
        if pwr > 0:
            key_findings.append(f"Paper research win rate: {pwr:.1%}")

    # ── Generate risks ──
    risks = _generate_risks(benchmark, regime, walk_forward, selection, paper)

    # ── Generate recommendations ──
    recommendations = _generate_recommendations(risks, status)

    # ── Build report ──
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at": generated_at,

        "executive_summary": {
            "research_status": status,
            "composite_grade": grade,
            "sources_available": sources_available,
            "key_findings": key_findings,
        },

        "benchmark": latest_snapshot if benchmark else {},

        "validation": {
            "walk_forward": {
                "available": walk_forward is not None,
                "stability_score": _safe_float(walk_forward.get("overall_stability")) if walk_forward else None,
                "total_periods": _safe_int(walk_forward.get("total_periods")) if walk_forward else 0,
            } if walk_forward else {"available": False},
        },

        "regime_analysis": {
            "available": bool(regime and regime.get("available")),
            "best_regime": regime.get("best_regime") if regime else None,
            "worst_regime": regime.get("worst_regime") if regime else None,
            "robustness": regime.get("regime_robustness") if regime else "UNKNOWN",
        } if regime else {"available": False},

        "paper_research": {
            "available": paper is not None,
            "win_rate": _safe_float((paper.get("performance") or {}).get("win_rate")) if paper else None,
            "avg_return": _safe_float((paper.get("performance") or {}).get("avg_return_pct")) if paper else None,
        } if paper else {"available": False},

        "risks": risks,
        "recommendations": recommendations,
    }

    # ── Write report ──
    _write_atomic(
        REPORT_ROOT / "research_report.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )

    return report


def load_research_report() -> dict[str, Any] | None:
    """Load the latest research report."""
    path = REPORT_ROOT / "research_report.json"
    return _read_json(path)
