"""Regime Performance Analysis — Phase 6B.

Analyzes selection and paper trading performance broken down by
market regime (bull / bear / sideways).  Reads pre-computed JSON
directly — NEVER imports research modules.

Reads from:
  - artifacts/learning/research_history/regime_tags.json
  - artifacts/learning/analytics/
  - artifacts/learning/paper_analytics/

Writes to:
  - artifacts/learning/regime_analysis/
    ├── regime_summary.json
    └── regime_selection_bias.json

Safety: research-only.  No imports from scoring, engine, broker,
risk, order, or safety modules.  No live trading.  No ML training.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

REGIME_VERSION = "regime_analysis.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning"
REGIME_ROOT = LEARNING_ROOT / "regime_analysis"

VALID_REGIMES = {"bull", "bear", "sideways"}


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
# Data source readers (direct file I/O — NO module imports)
# ═══════════════════════════════════════════════════════════════════════════════

def _read_regime_tags() -> list[dict[str, Any]] | None:
    """Read Phase 4A regime tags."""
    path = LEARNING_ROOT / "research_history" / "regime_tags.json"
    data = _read_json(path)
    if not data:
        return None
    tags = data.get("tags")
    return list(tags) if isinstance(tags, list) else None


def _read_selection_analytics() -> dict[str, Any] | None:
    """Read Phase 2C selection analytics."""
    path = LEARNING_ROOT / "analytics" / "performance_summary.json"
    return _read_json(path)


def _read_selection_sectors() -> dict[str, Any] | None:
    """Read Phase 2C sector analysis."""
    path = LEARNING_ROOT / "analytics" / "sector_analysis.json"
    return _read_json(path)


def _read_paper_summary() -> dict[str, Any] | None:
    """Read Phase 5C paper analytics summary."""
    path = LEARNING_ROOT / "paper_analytics" / "summary.json"
    return _read_json(path)


def _read_paper_sectors() -> dict[str, Any] | None:
    """Read Phase 5C paper sector analysis."""
    path = LEARNING_ROOT / "paper_analytics" / "sector_analysis.json"
    return _read_json(path)


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def run_regime_analysis() -> dict[str, Any]:
    """Run regime-aware performance analysis.

    Joins regime tags with selection and paper analytics data to
    produce per-regime performance breakdowns.
    """
    generated_at = _utc_now_iso()

    tags = _read_regime_tags()
    sel_analytics = _read_selection_analytics()
    sel_sectors = _read_selection_sectors()
    paper_summary = _read_paper_summary()
    paper_sectors = _read_paper_sectors()

    # ── Tag availability ──
    if not tags:
        summary = {
            "analysis_version": REGIME_VERSION,
            "generated_at": generated_at,
            "available": False,
            "reason": "no regime tags data",
            "regimes": {},
            "best_regime": None,
            "worst_regime": None,
            "regime_robustness": "UNKNOWN",
        }
        _write_atomic(
            REGIME_ROOT / "regime_summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2),
        )
        return summary

    # ── Build per-regime stats ──
    regimes: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "tag_count": 0,
        "selection_samples": 0,
        "selection_win_rate": None,
        "selection_avg_return_21d": None,
        "paper_trades": 0,
        "paper_win_rate": None,
        "paper_avg_return": None,
        "paper_avg_holding_days": None,
        "range_success_rate": None,
        "breakdown_rate": None,
        "breakout_rate": None,
        "top_sectors": [],
    })

    # Count regime tags
    regime_counts: dict[str, int] = defaultdict(int)
    for t in tags:
        r = str(t.get("regime") or "").lower()
        if r in VALID_REGIMES:
            regime_counts[r] += 1

    for regime in sorted(regime_counts):
        regimes[regime]["tag_count"] = regime_counts[regime]

    # ── Add selection analytics (global — not regime-partitioned) ──
    if sel_analytics:
        perf = sel_analytics.get("performance") or {}
        totals = sel_analytics.get("totals") or {}
        rng = sel_analytics.get("range_analysis") or {}

        # Assign global selection metrics to ALL regimes
        # (selection analytics are global; regime breakdown comes from tags only)
        for regime in regime_counts:
            regimes[regime]["selection_samples"] = _safe_int(
                sel_analytics.get("total_selections")
                or totals.get("total_trades"))
            regimes[regime]["selection_win_rate"] = _safe_float(perf.get("win_rate"))
            regimes[regime]["selection_avg_return_21d"] = _safe_float(
                perf.get("avg_return_21d_pct")
                or perf.get("avg_return_pct"))
            regimes[regime]["range_success_rate"] = _safe_float(
                rng.get("range_success_rate")
                or perf.get("range_success_rate"))

    # ── Add paper analytics ──
    if paper_summary:
        perf = paper_summary.get("performance") or {}
        totals = paper_summary.get("totals") or {}
        rng = paper_summary.get("range_analysis") or {}
        for regime in regime_counts:
            regimes[regime]["paper_trades"] = _safe_int(totals.get("total_trades"))
            regimes[regime]["paper_win_rate"] = _safe_float(perf.get("win_rate"))
            regimes[regime]["paper_avg_return"] = _safe_float(perf.get("avg_return_pct"))
            regimes[regime]["paper_avg_holding_days"] = _safe_float(
                perf.get("avg_holding_days"))
            regimes[regime]["breakdown_rate"] = _safe_float(rng.get("breakdown_rate"))
            regimes[regime]["breakout_rate"] = _safe_float(rng.get("breakout_rate"))

    # ── Top sectors per regime (from selection sector data) ──
    if sel_sectors:
        raw_sectors = sel_sectors.get("sector_distribution") or sel_sectors.get("sectors") or {}
        if isinstance(raw_sectors, dict):
            sorted_secs = sorted(raw_sectors.items(),
                                 key=lambda kv: _safe_int(kv[1] if isinstance(kv[1], (int, float)) else (kv[1].get("samples", 0) if isinstance(kv[1], dict) else 0)),
                                 reverse=True)
            top = [s[0] for s in sorted_secs[:3]]
            for regime in regime_counts:
                regimes[regime]["top_sectors"] = top

    # ── Determine best/worst regime ──
    valid_regimes_for_ranking = {
        r: d for r, d in regimes.items()
        if d.get("selection_win_rate") is not None
    }
    if valid_regimes_for_ranking:
        sorted_regimes = sorted(valid_regimes_for_ranking.items(),
                                key=lambda kv: _safe_float(kv[1].get("selection_win_rate")),
                                reverse=True)
        best_regime = sorted_regimes[0][0]
        worst_regime = sorted_regimes[-1][0]
    else:
        best_regime = None
        worst_regime = None

    # ── Regime robustness ──
    if not valid_regimes_for_ranking or len(valid_regimes_for_ranking) < 2:
        regime_robustness = "UNKNOWN"
    else:
        win_rates = [_safe_float(d.get("selection_win_rate"))
                     for d in valid_regimes_for_ranking.values()]
        spread = max(win_rates) - min(win_rates)
        if spread < 0.15:
            regime_robustness = "STRONG"
        elif spread < 0.35:
            regime_robustness = "MODERATE"
        else:
            regime_robustness = "WEAK"

    # ── Write summary ──
    summary = {
        "analysis_version": REGIME_VERSION,
        "generated_at": generated_at,
        "available": True,
        "regimes": {
            r: {
                "tag_count": d["tag_count"],
                "selection_win_rate": d["selection_win_rate"],
                "selection_avg_return_21d": d["selection_avg_return_21d"],
                "paper_trades": d["paper_trades"],
                "paper_win_rate": d["paper_win_rate"],
                "paper_avg_return": d["paper_avg_return"],
                "paper_avg_holding_days": d["paper_avg_holding_days"],
                "range_success_rate": d["range_success_rate"],
                "breakdown_rate": d["breakdown_rate"],
                "breakout_rate": d["breakout_rate"],
                "top_sectors": d["top_sectors"],
            }
            for r, d in sorted(regimes.items())
        },
        "best_regime": best_regime,
        "worst_regime": worst_regime,
        "regime_robustness": regime_robustness,
    }

    _write_atomic(
        REGIME_ROOT / "regime_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2),
    )

    # ── Selection bias report ──
    bias = _compute_selection_bias(sel_sectors, paper_sectors, regime_counts, generated_at)
    _write_atomic(
        REGIME_ROOT / "regime_selection_bias.json",
        json.dumps(bias, ensure_ascii=False, indent=2),
    )

    return summary


def _compute_selection_bias(
    sel_sectors: dict[str, Any] | None,
    paper_sectors: dict[str, Any] | None,
    regime_counts: dict[str, int],
    generated_at: str,
) -> dict[str, Any]:
    """Analyze whether the selector favors certain sectors across regimes."""
    sel_sec_raw: dict[str, int] = {}
    if sel_sectors:
        raw = sel_sectors.get("sector_distribution") or sel_sectors.get("sectors") or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    sel_sec_raw[k] = _safe_int(v.get("samples") or v.get("trades"))
                elif isinstance(v, (int, float)):
                    sel_sec_raw[k] = _safe_int(v)

    paper_sec_raw: dict[str, int] = {}
    if paper_sectors:
        raw = paper_sectors.get("sectors") or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    paper_sec_raw[k] = _safe_int(v.get("trades"))

    return {
        "analysis_version": REGIME_VERSION,
        "generated_at": generated_at,
        "selection_sector_distribution": sel_sec_raw,
        "paper_sector_distribution": paper_sec_raw,
        "regime_distribution": regime_counts,
    }


def load_regime_analysis() -> dict[str, Any] | None:
    """Load the latest regime analysis summary."""
    path = REGIME_ROOT / "regime_summary.json"
    return _read_json(path)
