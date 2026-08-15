"""Paper Analytics Research Layer — Phase 5C.

Computes aggregate analytics from Phase 5A paper trades
and Phase 5B position snapshots.  Pure read-only consumer —
never modifies source data.

Reads from:
  - artifacts/learning/paper_trading/
  - artifacts/learning/paper_tracking/

Writes to:
  - artifacts/learning/paper_analytics/
    ├── summary.json
    ├── performance.json
    ├── factor_analysis.json
    └── sector_analysis.json

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

from src.openalpha.paper_research import (
    load_paper_trades,
    PaperTradeRecord,
)
from src.openalpha.paper_tracker import (
    load_snapshots,
    PositionSnapshot,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

ANALYTICS_VERSION = "paper_analytics.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYTICS_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "paper_analytics"


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


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def _pearson_correlation(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient. Returns 0.0 if degenerate."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0
    mx = _mean(xs)
    my = _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    if sx == 0.0 or sy == 0.0:
        return 0.0
    return round(max(-1.0, min(1.0, cov / (n * sx * sy))), 4)


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def run_paper_analytics() -> dict[str, Any]:
    """Run all paper analytics and write 4 JSON reports.

    Returns a summary dict with paths of written files.
    """
    generated_at = _utc_now_iso()

    # ── Load Phase 5A + 5B data ──
    trades = load_paper_trades()
    snapshots = load_snapshots()

    # Build lookup: paper_trade_id → most recent snapshot
    snapshot_by_trade: dict[str, PositionSnapshot] = {}
    for s in snapshots:
        existing = snapshot_by_trade.get(s.paper_trade_id)
        if existing is None or s.snapshot_date > existing.snapshot_date:
            snapshot_by_trade[s.paper_trade_id] = s

    # ── 1. Summary ──
    summary = _compute_summary(trades, snapshot_by_trade, generated_at)
    _write_atomic(
        ANALYTICS_ROOT / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2),
    )

    # ── 2. Performance ──
    performance = _compute_performance(trades, snapshot_by_trade, generated_at)
    _write_atomic(
        ANALYTICS_ROOT / "performance.json",
        json.dumps(performance, ensure_ascii=False, indent=2),
    )

    # ── 3. Factor analysis ──
    factor = _compute_factor_analysis(trades, snapshot_by_trade, generated_at)
    _write_atomic(
        ANALYTICS_ROOT / "factor_analysis.json",
        json.dumps(factor, ensure_ascii=False, indent=2),
    )

    # ── 4. Sector analysis ──
    sector = _compute_sector_analysis(trades, snapshot_by_trade, generated_at)
    _write_atomic(
        ANALYTICS_ROOT / "sector_analysis.json",
        json.dumps(sector, ensure_ascii=False, indent=2),
    )

    return {
        "analytics_version": ANALYTICS_VERSION,
        "generated_at": generated_at,
        "total_trades": len(trades),
        "trades_with_snapshots": len(snapshot_by_trade),
        "files": {
            "summary": str(ANALYTICS_ROOT / "summary.json"),
            "performance": str(ANALYTICS_ROOT / "performance.json"),
            "factor_analysis": str(ANALYTICS_ROOT / "factor_analysis.json"),
            "sector_analysis": str(ANALYTICS_ROOT / "sector_analysis.json"),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Summary
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_summary(
    trades: list[PaperTradeRecord],
    snapshot_by_trade: dict[str, PositionSnapshot],
    generated_at: str,
) -> dict[str, Any]:
    """Compute overall paper trading summary."""
    total = len(trades)
    open_count = sum(1 for t in trades if t.status == "OPEN")
    closed_count = sum(1 for t in trades if t.status == "CLOSED")
    failed_count = sum(1 for t in trades if t.status == "FAILED")
    with_snaps = len(snapshot_by_trade)

    # Metrics from trades that HAVE snapshots
    returns: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []
    holding_days_list: list[int] = []
    mfe_captures: list[float] = []

    wins = 0
    losses = 0
    breakdowns = 0
    breakouts = 0
    support_touches = 0
    resistance_touches = 0
    review_exits = 0

    for trade in trades:
        snap = snapshot_by_trade.get(trade.paper_trade_id)
        if snap is None:
            continue

        if snap.unrealized_return_pct is not None:
            returns.append(snap.unrealized_return_pct)
            if snap.unrealized_return_pct > 0:
                wins += 1
            else:
                losses += 1

            # MFE capture rate: how much of the favorable excursion was captured
            if snap.mfe_since_entry_pct is not None and snap.mfe_since_entry_pct > 0:
                mfe_captures.append(snap.unrealized_return_pct / snap.mfe_since_entry_pct)

        if snap.mfe_since_entry_pct is not None:
            mfes.append(snap.mfe_since_entry_pct)
        if snap.mae_since_entry_pct is not None:
            maes.append(snap.mae_since_entry_pct)
        if snap.holding_days > 0:
            holding_days_list.append(snap.holding_days)

        if snap.range_breakdown:
            breakdowns += 1
        if snap.range_breakout:
            breakouts += 1
        if snap.range_touch_support:
            support_touches += 1
        if snap.range_touch_resistance:
            resistance_touches += 1
        if snap.exit_recommendation == "REVIEW_EXIT":
            review_exits += 1

    completed = wins + losses
    win_rate = round(wins / completed, 4) if completed > 0 else 0.0

    return {
        "analytics_version": ANALYTICS_VERSION,
        "generated_at": generated_at,
        "totals": {
            "total_trades": total,
            "open_trades": open_count,
            "closed_trades": closed_count,
            "failed_trades": failed_count,
            "trades_with_snapshots": with_snaps,
        },
        "performance": {
            "win_rate": win_rate,
            "wins": wins,
            "losses": losses,
            "avg_return_pct": round(_mean(returns), 4),
            "avg_holding_days": round(_mean([float(d) for d in holding_days_list]), 1),
            "avg_mfe_pct": round(_mean(mfes), 4),
            "avg_mae_pct": round(_mean(maes), 4),
            "mfe_capture_rate": round(_mean(mfe_captures), 4),
        },
        "range_analysis": {
            "breakdown_count": breakdowns,
            "breakout_count": breakouts,
            "support_touch_count": support_touches,
            "resistance_touch_count": resistance_touches,
            "review_exit_count": review_exits,
            "breakdown_rate": round(breakdowns / with_snaps, 4) if with_snaps else 0.0,
            "breakout_rate": round(breakouts / with_snaps, 4) if with_snaps else 0.0,
            "review_exit_rate": round(review_exits / with_snaps, 4) if with_snaps else 0.0,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Performance
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_performance(
    trades: list[PaperTradeRecord],
    snapshot_by_trade: dict[str, PositionSnapshot],
    generated_at: str,
) -> dict[str, Any]:
    """Compute per-trade performance records."""
    trade_rows: list[dict[str, Any]] = []
    returns: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []

    for trade in trades:
        snap = snapshot_by_trade.get(trade.paper_trade_id)
        row: dict[str, Any] = {
            "paper_trade_id": trade.paper_trade_id,
            "symbol": trade.symbol,
            "sector": trade.sector,
            "status": trade.status,
            "entry_price": trade.entry_price,
            "entry_date": trade.entry_date,
            "range_low": trade.range_low,
            "range_high": trade.range_high,
            "current_price": None,
            "unrealized_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            "holding_days": 0,
            "exit_signal": None,
            "exit_recommendation": "HOLD",
            "has_snapshot": False,
        }

        if snap is not None:
            row["current_price"] = snap.current_price
            row["unrealized_return_pct"] = snap.unrealized_return_pct
            row["mfe_pct"] = snap.mfe_since_entry_pct
            row["mae_pct"] = snap.mae_since_entry_pct
            row["holding_days"] = snap.holding_days
            row["exit_signal"] = snap.exit_signal
            row["exit_recommendation"] = snap.exit_recommendation
            row["has_snapshot"] = True

            if snap.unrealized_return_pct is not None:
                returns.append(snap.unrealized_return_pct)
            if snap.mfe_since_entry_pct is not None:
                mfes.append(snap.mfe_since_entry_pct)
            if snap.mae_since_entry_pct is not None:
                maes.append(snap.mae_since_entry_pct)

        trade_rows.append(row)

    # Return distribution
    if returns:
        returns_sorted = sorted(returns)
        n = len(returns_sorted)
        dist = {
            "min": round(returns_sorted[0], 4),
            "p25": round(returns_sorted[max(0, n // 4)], 4),
            "p50": round(returns_sorted[n // 2], 4),
            "p75": round(returns_sorted[min(n - 1, 3 * n // 4)], 4),
            "max": round(returns_sorted[-1], 4),
        }
    else:
        dist = {"min": None, "p25": None, "p50": None, "p75": None, "max": None}

    return {
        "analytics_version": ANALYTICS_VERSION,
        "generated_at": generated_at,
        "trades": trade_rows,
        "aggregates": {
            "total": len(trade_rows),
            "with_snapshots": len(returns),
            "positive_return_count": sum(1 for r in returns if r > 0),
            "negative_return_count": sum(1 for r in returns if r <= 0),
            "return_distribution": dist,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Factor analysis
# ═══════════════════════════════════════════════════════════════════════════════

# ── Available selection factors on PaperTradeRecord ──
FACTOR_KEYS = [
    "score",
]


def _compute_factor_analysis(
    trades: list[PaperTradeRecord],
    snapshot_by_trade: dict[str, PositionSnapshot],
    generated_at: str,
) -> dict[str, Any]:
    """Compute correlation between selection factors and outcomes."""
    # Build arrays: factor values → returns
    factor_data: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"winning": [], "losing": [], "all": []})

    all_returns: list[float] = []

    for trade in trades:
        snap = snapshot_by_trade.get(trade.paper_trade_id)
        if snap is None or snap.unrealized_return_pct is None:
            continue

        ret = snap.unrealized_return_pct
        all_returns.append(ret)

        for key in FACTOR_KEYS:
            val = getattr(trade, key, 0.0)
            fd = factor_data[key]
            fd["all"].append(val)
            if ret > 0:
                fd["winning"].append(val)
            else:
                fd["losing"].append(val)

    factors: dict[str, dict[str, Any]] = {}
    for key in FACTOR_KEYS:
        fd = factor_data[key]
        factors[key] = {
            "mean_winning": round(_mean(fd["winning"]), 4) if fd["winning"] else None,
            "mean_losing": round(_mean(fd["losing"]), 4) if fd["losing"] else None,
            "mean_all": round(_mean(fd["all"]), 4) if fd["all"] else None,
            "correlation_with_return": _pearson_correlation(fd["all"], all_returns),
            "samples": len(fd["all"]),
        }

    return {
        "analytics_version": ANALYTICS_VERSION,
        "generated_at": generated_at,
        "factors": factors,
        "feature_columns": FACTOR_KEYS,
        "samples": len(all_returns),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Sector analysis
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_sector_analysis(
    trades: list[PaperTradeRecord],
    snapshot_by_trade: dict[str, PositionSnapshot],
    generated_at: str,
) -> dict[str, Any]:
    """Compute per-sector performance breakdown."""
    sectors: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "trades": 0,
        "open": 0,
        "wins": 0,
        "losses": 0,
        "returns": [],
        "mfes": [],
        "maes": [],
        "holding_days_list": [],
        "breakdown_count": 0,
        "breakout_count": 0,
    })

    for trade in trades:
        sec = trade.sector or "Unknown"
        sd = sectors[sec]
        sd["trades"] += 1

        if trade.status == "OPEN":
            sd["open"] += 1

        snap = snapshot_by_trade.get(trade.paper_trade_id)
        if snap is None:
            continue

        if snap.unrealized_return_pct is not None:
            sd["returns"].append(snap.unrealized_return_pct)
            if snap.unrealized_return_pct > 0:
                sd["wins"] += 1
            else:
                sd["losses"] += 1

        if snap.mfe_since_entry_pct is not None:
            sd["mfes"].append(snap.mfe_since_entry_pct)
        if snap.mae_since_entry_pct is not None:
            sd["maes"].append(snap.mae_since_entry_pct)
        if snap.holding_days > 0:
            sd["holding_days_list"].append(snap.holding_days)
        if snap.range_breakdown:
            sd["breakdown_count"] += 1
        if snap.range_breakout:
            sd["breakout_count"] += 1

    result: dict[str, dict[str, Any]] = {}
    for sec, sd in sorted(sectors.items()):
        completed = sd["wins"] + sd["losses"]
        result[sec] = {
            "trades": sd["trades"],
            "open": sd["open"],
            "win_rate": round(sd["wins"] / completed, 4) if completed > 0 else 0.0,
            "avg_return_pct": round(_mean(sd["returns"]), 4) if sd["returns"] else None,
            "avg_mfe_pct": round(_mean(sd["mfes"]), 4) if sd["mfes"] else None,
            "avg_mae_pct": round(_mean(sd["maes"]), 4) if sd["maes"] else None,
            "avg_holding_days": round(_mean([float(d) for d in sd["holding_days_list"]]), 1),
            "breakdown_count": sd["breakdown_count"],
            "breakout_count": sd["breakout_count"],
        }

    return {
        "analytics_version": ANALYTICS_VERSION,
        "generated_at": generated_at,
        "sectors": result,
        "unique_sectors": len(result),
    }
