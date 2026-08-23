"""Selection Performance Ledger — research-only evaluation layer.

Writes immutable JSON snapshots for every formal candidate produced by
the selection pipeline.  Written *after* selection completes and never
modifies trading behaviour.

Schema aligns overlapping fields with the outcome collector
(src/outcome/collector.py) so the two datasets can be joined on
(selection_run_id, symbol) when paper-trade outcomes are available.

Conventions:
  - Fire-and-forget — ledger failure never blocks selection.
  - Atomic writes — tmp file → fsync → os.replace.
  - Immutable — records are never overwritten after initial write.
  - Append-only index — index.json is replaced atomically on each write.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any

from quantcairn import __version__ as _selector_version

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

SCHEMA_VERSION = "selection_ledger.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
LEDGER_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "selection_ledger"
RUNS_DIR = LEDGER_ROOT / "runs"
INDEX_PATH = LEDGER_ROOT / "ledger_index.json"

# Structural identities — recorded for provenance but never change logic.
SCORING_LOGIC_VERSION = "scorer.v1:multi-factor-0.30-0.20-0.20-0.15-0.10"
COMPOSITION_LOGIC_VERSION = "selector.v1:greedy-diversified-sector-correlation"
UNIVERSE_VERSION = "managed-snapshot.v1"


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class SelectionRecord:
    """One row per formal candidate per selection run.

    Field names that overlap with outcome collector's _OUTCOME_COLUMNS_V3
    use identical names so future JOINs require no renaming.
    """

    # ── Identity ──
    selection_run_id: str
    selection_date: str          # ISO date "2026-08-05"
    symbol: str
    formal_rank: int             # 1-based position in TOP-K

    # ── Selection snapshot ──
    score: float
    base_score: float
    sector: str
    recommended_strategy: str
    data_source: str             # "live" | "eod_validated" | "fallback"
    run_mode: str                # "FULL" | "AFTER_MARKET" | "DEGRADED" | "EOD_ONLY"
    execution_mode: str          # "RESEARCH" | "PAPER" | "LIVE"
    candidate_type: str          # "RESEARCH_ONLY" | "PAPER_ELIGIBLE" | "LIVE_TRADABLE"

    # ── Entry reference ──
    entry_reference_price: float
    range_low: float
    range_high: float
    range_width_pct: float
    atr_pct: float
    gap_rate: float

    # ── Scoring factors (names aligned with outcome collector) ──
    feature_volatility_score: float
    feature_volume_score: float
    feature_trend_score: float            # trend_fit_score in scorer
    feature_repeatability_score: float
    feature_drawdown_safety_score: float
    feature_liquidity_score: float        # derived from avg_dollar_volume

    # ── Version provenance ──
    selector_version: str
    scoring_logic_version: str
    composition_logic_version: str
    universe_version: str

    # ── Forward-looking (None until Phase 2 backfill) ──
    price_5d: float | None = None
    price_21d: float | None = None
    return_5d_pct: float | None = None
    return_21d_pct: float | None = None
    mfe_21d_pct: float | None = None
    mae_21d_pct: float | None = None
    range_success: bool | None = None
    range_touch_support: bool | None = None
    range_touch_resistance: bool | None = None
    range_breakdown: bool | None = None
    range_breakout: bool | None = None

    # ── Meta ──
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Convert None values cleanly
        return {k: v for k, v in d.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SelectionRecord":
        # Filter to known fields to handle schema evolution
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: d[k] for k in known if k in d})


# ═══════════════════════════════════════════════════════════════════════════════
# Write
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
    """Atomic write: tmp → fsync → os.replace.  Never leaves partial files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_selection_snapshot(
    *,
    run_id: str,
    date: str,
    topk: list[dict[str, Any]],
    formal_candidates: list[str],
    run_mode: str = "",
    execution_mode: str = "",
    candidate_type: str = "",
) -> Path | None:
    """Write a SelectionRecord for each formal candidate in *topk*.

    Called from AIStrategySelector.run_selection() after the selection
    result is finalised.  Failure is silent — never blocks selection.

    Returns the written Path, or None if there are no formal candidates.
    """
    formal_set = {str(c).strip().upper() for c in (formal_candidates or [])}
    if not formal_set:
        return None

    now_iso = _utc_now_iso()
    records: list[dict[str, Any]] = []

    for rank, item in enumerate(topk or [], start=1):
        ticker = str(item.get("ticker") or "").strip().upper()
        if ticker not in formal_set:
            continue
        metrics = item.get("metrics") or {}
        risk = item.get("risk") or {}

        records.append(
            SelectionRecord(
                selection_run_id=str(run_id),
                selection_date=str(date),
                symbol=ticker,
                formal_rank=rank,
                # Selection snapshot
                score=_safe_float(item.get("score")),
                base_score=_safe_float(item.get("base_score")),
                sector=str(item.get("sector") or ""),
                recommended_strategy=str(item.get("recommended_strategy") or ""),
                data_source=str(item.get("data_source") or ""),
                run_mode=str(run_mode),
                execution_mode=str(execution_mode),
                candidate_type=str(candidate_type or item.get("candidate_type") or ""),
                # Entry reference
                entry_reference_price=_safe_float(metrics.get("price_midpoint")
                                                  or metrics.get("last_close")),
                range_low=_safe_float(item.get("range_low")),
                range_high=_safe_float(item.get("range_high")),
                range_width_pct=_safe_float(metrics.get("range_width_pct")),
                atr_pct=_safe_float(metrics.get("atr_pct")),
                gap_rate=_safe_float(metrics.get("gap_rate")),
                # Scoring factors
                feature_volatility_score=_safe_float(item.get("volatility_score")),
                feature_volume_score=_safe_float(item.get("volume_score")),
                feature_trend_score=_safe_float(item.get("trend_fit_score")),
                feature_repeatability_score=_safe_float(item.get("repeatability_score")),
                feature_drawdown_safety_score=_safe_float(item.get("drawdown_safety_score")),
                feature_liquidity_score=_safe_float(item.get("liquidity_score")),
                # Version provenance
                selector_version=str(_selector_version),
                scoring_logic_version=SCORING_LOGIC_VERSION,
                composition_logic_version=COMPOSITION_LOGIC_VERSION,
                universe_version=UNIVERSE_VERSION,
                # Meta
                recorded_at=now_iso,
            ).to_dict()
        )

    if not records:
        return None

    run_path = RUNS_DIR / str(date) / f"{str(run_id)}.json"
    _write_atomic(run_path, json.dumps(records, ensure_ascii=False, indent=2))

    _update_index(str(date), str(run_id))
    return run_path


# ═══════════════════════════════════════════════════════════════════════════════
# Index
# ═══════════════════════════════════════════════════════════════════════════════

def _load_index() -> dict[str, Any]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"schema_version": SCHEMA_VERSION, "dates": [], "runs": {}}


def _update_index(date: str, run_id: str) -> None:
    idx = _load_index()
    idx["schema_version"] = SCHEMA_VERSION
    dates: list[str] = idx.setdefault("dates", [])
    runs: dict[str, list[str]] = idx.setdefault("runs", {})

    if date not in dates:
        dates.append(date)
        dates.sort()

    date_runs = runs.setdefault(date, [])
    if run_id not in date_runs:
        date_runs.append(run_id)

    idx["updated_at"] = _utc_now_iso()
    _write_atomic(INDEX_PATH, json.dumps(idx, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════════

def load_selection_history(
    *,
    since_date: str | None = None,
    until_date: str | None = None,
    symbols: list[str] | None = None,
    sectors: list[str] | None = None,
) -> list[SelectionRecord]:
    """Load all selection records in an optional date range.

    Returns a flat list of SelectionRecord objects sorted by date
    descending, then by formal_rank ascending.
    """
    idx = _load_index()
    all_dates: list[str] = idx.get("dates", [])

    results: list[SelectionRecord] = []
    for date in all_dates:
        if since_date and date < since_date:
            continue
        if until_date and date > until_date:
            continue
        date_runs = idx.get("runs", {}).get(date, [])
        for run_id in date_runs:
            run_path = RUNS_DIR / date / f"{run_id}.json"
            if not run_path.exists():
                continue
            try:
                raw = json.loads(run_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for item in raw:
                rec = SelectionRecord.from_dict(item)
                if symbols and rec.symbol not in set(symbols):
                    continue
                if sectors and rec.sector not in set(sectors):
                    continue
                results.append(rec)

    results.sort(key=lambda r: (-ord(r.selection_date[0]) if r.selection_date else "", r.formal_rank))
    return results


def load_latest_selection() -> list[SelectionRecord]:
    """Load records from the most recent selection date."""
    idx = _load_index()
    dates = idx.get("dates", [])
    if not dates:
        return []
    latest = dates[-1]
    return load_selection_history(since_date=latest, until_date=latest)
