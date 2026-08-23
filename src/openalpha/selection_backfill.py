"""Historical outcome backfill engine — Phase 2A.

Reads selection snapshots from the Phase 1 ledger, fetches forward
OHLCV data for each formal candidate, and computes:
  - Forward returns (5d, 21d)
  - True path-based MFE/MAE from OHLCV bars
  - Range trading outcome evaluation

Writes immutable outcome records to a SEPARATE directory so the
Phase 1 ledger is never modified.

Safety: read-only consumer of the ledger.  No imports from scoring,
engine, broker, risk, order, or safety modules.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any

import numpy as np

from src.openalpha.selection_ledger import (
    SelectionRecord,
    load_selection_history,
)
from src.utils.market_calendar import (
    is_us_market_trading_day,
    next_us_market_trading_day,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

EVALUATION_VERSION = "backfill.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
OUTCOMES_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "selection_outcomes"


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class OutcomeRecord:
    """One row per backfilled candidate.

    Identity fields match SelectionRecord for joinability.
    Computed forward-looking fields are None until populated.
    """

    # ── Identity (matches SelectionRecord for JOIN) ──
    selection_run_id: str
    selection_date: str          # ISO date "2026-08-05"
    symbol: str
    formal_rank: int

    # ── Entry reference ──
    entry_reference_price: float
    entry_price_type: str = "selection_reference"
    range_low: float = 0.0
    range_high: float = 0.0

    # ── Forward prices ──
    price_5d: float | None = None
    price_21d: float | None = None

    # ── Forward returns ──
    return_5d_pct: float | None = None
    return_21d_pct: float | None = None

    # ── Path-based MFE/MAE over 21-trading-day window ──
    mfe_21d_pct: float | None = None      # (max_high - entry) / entry * 100
    mae_21d_pct: float | None = None      # (min_low - entry) / entry * 100

    # ── Bar counts ──
    bars_available: int = 0
    bars_required_5d: int = 5
    bars_required_21d: int = 21

    # ── Range evaluation ──
    range_touch_support: bool | None = None     # any bar.low <= range_low
    range_touch_resistance: bool | None = None  # any bar.high >= range_high
    range_breakdown: bool | None = None         # any bar.close < range_low
    range_breakout: bool | None = None          # any bar.close > range_high
    range_outcome: str | None = None            # SUCCESS | PARTIAL | FAILED | UNKNOWN

    # ── Status ──
    status: str = "SUCCESS"                     # SUCCESS | FAILED_DATA | INSUFFICIENT_HISTORY
    status_detail: str = ""

    # ── Data provenance ──
    data_provider: str = ""                     # "yfinance" | "chart_api" | "none"
    evaluation_version: str = EVALUATION_VERSION
    backfilled_at: str = ""                     # ISO timestamp
    content_hash: str = ""                      # SHA-256 for idempotency

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutcomeRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: d[k] for k in known if k in d})


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
    """Atomic write: tmp → os.replace.  Never leaves partial files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def _trading_days_after(start: date, n: int) -> date:
    """Return the date *n* US market trading days after *start*."""
    d = start
    for _ in range(n):
        d = next_us_market_trading_day(d)
    return d


def _compute_content_hash(record: OutcomeRecord) -> str:
    """SHA-256 of computed fields only (identity + forward metrics).
    Excludes meta fields (backfilled_at, content_hash itself)
    so the same OHLCV data always produces the same hash.
    """
    payload: dict[str, Any] = {
        "selection_run_id": record.selection_run_id,
        "selection_date": record.selection_date,
        "symbol": record.symbol,
        "formal_rank": record.formal_rank,
        "entry_reference_price": record.entry_reference_price,
        "entry_price_type": record.entry_price_type,
        "range_low": record.range_low,
        "range_high": record.range_high,
        "price_5d": record.price_5d,
        "price_21d": record.price_21d,
        "return_5d_pct": record.return_5d_pct,
        "return_21d_pct": record.return_21d_pct,
        "mfe_21d_pct": record.mfe_21d_pct,
        "mae_21d_pct": record.mae_21d_pct,
        "bars_available": record.bars_available,
        "range_touch_support": record.range_touch_support,
        "range_touch_resistance": record.range_touch_resistance,
        "range_breakdown": record.range_breakdown,
        "range_breakout": record.range_breakout,
        "range_outcome": record.range_outcome,
        "status": record.status,
        "status_detail": record.status_detail,
        "data_provider": record.data_provider,
        "evaluation_version": record.evaluation_version,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ═══════════════════════════════════════════════════════════════════════════════
# OHLCV fetching — uses existing QuantCairn data boundary
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_forward_bars(
    symbol: str,
    from_date: str,
    calendar_days_forward: int = 60,
) -> tuple[list[dict[str, float]], str]:
    """Fetch daily OHLCV bars from *from_date* forward.

    Tries the existing PriceFetcher → chart API → yfinance.download
    chain, mirroring the scorer's data access pattern.

    Returns (bars, provider) where bars is a list of dicts with
    keys: date, open, high, low, close, volume.
    """
    from datetime import datetime as dt

    start_dt = dt.fromisoformat(from_date)
    end_dt = start_dt + timedelta(days=calendar_days_forward)

    # ── Path 1: yfinance (primary — same as scorer at module level) ──
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_dt, end=end_dt, interval="1d")
        if df is not None and not df.empty:
            bars: list[dict[str, float]] = []
            for idx, row in df.iterrows():
                try:
                    bars.append({
                        "date": str(idx.date()),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row.get("Volume", 0)),
                    })
                except (ValueError, KeyError):
                    continue
            if bars:
                return bars, "yfinance"
    except Exception:
        pass

    # ── Path 2: PriceFetcher (existing data boundary) ──
    try:
        from src.data.fetcher import PriceFetcher

        fetcher = PriceFetcher(symbol, poll_interval=0)
        try:
            candles = fetcher.get_ohlcv(period="3mo", interval="1d")
            if candles:
                bars = []
                for c in candles:
                    bar_dt = getattr(c, "timestamp", None)
                    bar_date = bar_dt.date() if hasattr(bar_dt, "date") else str(bar_dt)[:10]
                    if isinstance(bar_date, date):
                        bar_date = bar_date.isoformat()
                    bar_date = str(bar_date)[:10]
                    if bar_date < from_date:
                        continue
                    if bar_date > end_dt.date().isoformat():
                        continue
                    bars.append({
                        "date": bar_date,
                        "open": float(getattr(c, "open", 0)),
                        "high": float(getattr(c, "high", 0)),
                        "low": float(getattr(c, "low", 0)),
                        "close": float(getattr(c, "close", 0)),
                        "volume": float(getattr(c, "volume", 0) or 0),
                    })
                if bars:
                    return bars, "price_fetcher"
        finally:
            fetcher.close()
    except Exception:
        pass

    # ── Path 3: yfinance.download (last resort) ──
    try:
        import yfinance as yf

        df = yf.download(symbol, start=start_dt, end=end_dt, interval="1d", progress=False)
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex if "pd" in dir() else ()):
                import pandas as pd
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
            bars = []
            for idx, row in df.iterrows():
                try:
                    bars.append({
                        "date": str(idx.date()) if hasattr(idx, "date") else str(idx)[:10],
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row.get("Volume", 0)),
                    })
                except (ValueError, KeyError):
                    continue
            if bars:
                return bars, "yfinance_download"
    except Exception:
        pass

    return [], "none"


# ═══════════════════════════════════════════════════════════════════════════════
# Metric computation
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_forward_metrics(
    bars: list[dict[str, float]],
    entry_price: float,
    selection_date_str: str,
    range_low: float,
    range_high: float,
    provider: str,
) -> OutcomeRecord:
    """Compute all forward metrics from OHLCV bars.

    *bars* must be sorted chronologically and cover the window
    from *selection_date* through at least 21 trading days forward
    (or as many as are available).
    """
    # ── Filter bars after selection date ──
    future_bars = [b for b in bars if b["date"] > selection_date_str]
    if not future_bars:
        return _failed_record(
            selection_date_str, entry_price, range_low, range_high,
            provider, "FAILED_DATA", "no bars after selection date",
        )

    # ── Map trading days to bar indices ──
    sel_date = date.fromisoformat(selection_date_str)
    target_5d = _trading_days_after(sel_date, 5)
    target_21d = _trading_days_after(sel_date, 21)

    # Build a date → bar map
    bar_by_date: dict[str, dict[str, float]] = {}
    trading_days_in_window: list[dict[str, float]] = []
    for b in future_bars:
        bar_by_date[b["date"]] = b
        bd = date.fromisoformat(b["date"])
        if is_us_market_trading_day(bd):
            trading_days_in_window.append(b)

    # ── Price at N trading days ──
    price_5d = None
    price_21d = None

    target_5d_str = target_5d.isoformat()
    target_21d_str = target_21d.isoformat()

    # Use the bar on or just after the target date
    for td in sorted(bar_by_date.keys()):
        if td >= target_5d_str and price_5d is None:
            price_5d = bar_by_date[td]["close"]
        if td >= target_21d_str and price_21d is None:
            price_21d = bar_by_date[td]["close"]

    bars_available = len(trading_days_in_window)

    # ── Insufficient history check ──
    if bars_available < 5:
        rec = _failed_record(
            selection_date_str, entry_price, range_low, range_high,
            provider, "INSUFFICIENT_HISTORY",
            f"only {bars_available} trading days available (need ≥5)",
        )
        rec.bars_available = bars_available
        return rec

    # ── Forward returns ──
    return_5d_pct = None
    return_21d_pct = None
    if price_5d is not None and entry_price > 0:
        return_5d_pct = round((price_5d - entry_price) / entry_price * 100.0, 4)
    if price_21d is not None and entry_price > 0:
        return_21d_pct = round((price_21d - entry_price) / entry_price * 100.0, 4)

    # ── True path-based MFE/MAE ──
    # Use ALL bars in the window (not just trading days) for high/low extremes
    highs = [b["high"] for b in future_bars]
    lows = [b["low"] for b in future_bars]
    max_high = max(highs)
    min_low = min(lows)

    mfe_21d_pct = round(max(0.0, (max_high - entry_price) / entry_price * 100.0), 4)
    mae_21d_pct = round(min(0.0, (min_low - entry_price) / entry_price * 100.0), 4)

    # ── Range evaluation ──
    touch_support = None
    touch_resistance = None
    breakdown = None
    breakout = None

    if range_low > 0 and range_high > 0 and range_high > range_low:
        touch_support = any(b["low"] <= range_low for b in future_bars)
        touch_resistance = any(b["high"] >= range_high for b in future_bars)
        breakdown = any(b["close"] < range_low for b in future_bars)
        breakout = any(b["close"] > range_high for b in future_bars)

        # ── Range outcome ──
        # SUCCESS: support touched first, then resistance touched later
        # PARTIAL: one side touched only
        # FAILED: breakdown/breakout without successful range
        range_outcome = _determine_range_outcome(
            future_bars, range_low, range_high,
            touch_support, touch_resistance, breakdown, breakout,
        )
    else:
        range_outcome = "UNKNOWN"

    status = "SUCCESS"
    # Partial: couldn't get both 5d and 21d
    if price_5d is None and price_21d is None:
        status = "INSUFFICIENT_HISTORY"
    elif price_21d is None:
        status_detail = "partial: 21d not available"
        status = "SUCCESS"  # still useful data

    rec = OutcomeRecord(
        selection_run_id="",   # filled by caller
        selection_date=selection_date_str,
        symbol="",             # filled by caller
        formal_rank=0,         # filled by caller
        entry_reference_price=entry_price,
        entry_price_type="selection_reference",
        range_low=range_low,
        range_high=range_high,
        price_5d=price_5d,
        price_21d=price_21d,
        return_5d_pct=return_5d_pct,
        return_21d_pct=return_21d_pct,
        mfe_21d_pct=mfe_21d_pct,
        mae_21d_pct=mae_21d_pct,
        bars_available=bars_available,
        bars_required_5d=5,
        bars_required_21d=21,
        range_touch_support=touch_support,
        range_touch_resistance=touch_resistance,
        range_breakdown=breakdown,
        range_breakout=breakout,
        range_outcome=range_outcome,
        status=status,
        status_detail="",
        data_provider=provider,
        evaluation_version=EVALUATION_VERSION,
        backfilled_at="",
        content_hash="",
    )
    return rec


def _determine_range_outcome(
    bars: list[dict[str, float]],
    range_low: float,
    range_high: float,
    touch_support: bool,
    touch_resistance: bool,
    breakdown: bool,
    breakout: bool,
) -> str:
    """Determine the range outcome from bar-by-bar events.

    SUCCESS: support touched first, then resistance touched later
             (the ideal range-trading scenario).
    PARTIAL: one side touched only.
    FAILED: breakdown or breakout without both touches.
    """
    if not bars:
        return "UNKNOWN"

    # Find first support touch and first resistance touch chronologically
    first_support_idx: int | None = None
    first_resistance_idx: int | None = None

    for i, b in enumerate(bars):
        if first_support_idx is None and b["low"] <= range_low:
            first_support_idx = i
        if first_resistance_idx is None and b["high"] >= range_high:
            first_resistance_idx = i

    support_first = (
        first_support_idx is not None
        and first_resistance_idx is not None
        and first_support_idx < first_resistance_idx
    )
    resistance_first = (
        first_support_idx is not None
        and first_resistance_idx is not None
        and first_resistance_idx < first_support_idx
    )

    if support_first:
        # Support touched, then resistance — ideal range trade
        return "SUCCESS"
    elif breakdown or breakout:
        # Close crossed a boundary — failed range
        return "FAILED"
    elif resistance_first:
        # Resistance touched first — wrong direction for a support entry
        return "FAILED"
    elif touch_support and not touch_resistance:
        return "PARTIAL"
    elif touch_resistance and not touch_support:
        return "PARTIAL"
    else:
        return "PARTIAL"


def _failed_record(
    selection_date_str: str,
    entry_price: float,
    range_low: float,
    range_high: float,
    provider: str,
    status: str,
    detail: str,
) -> OutcomeRecord:
    """Build an OutcomeRecord for a failed/incomplete backfill."""
    return OutcomeRecord(
        selection_run_id="",
        selection_date=selection_date_str,
        symbol="",
        formal_rank=0,
        entry_reference_price=entry_price,
        entry_price_type="selection_reference",
        range_low=range_low,
        range_high=range_high,
        data_provider=provider,
        evaluation_version=EVALUATION_VERSION,
        status=status,
        status_detail=detail,
        range_outcome="UNKNOWN",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Backfill engine
# ═══════════════════════════════════════════════════════════════════════════════

def backfill_selection_outcomes(
    *,
    since_date: str | None = None,
    until_date: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill forward outcomes for all eligible SelectionRecords.

    Reads from the Phase 1 ledger, fetches forward OHLCV data,
    computes metrics, and writes to artifacts/learning/selection_outcomes/.

    Returns a summary dict with counts.
    """
    records = load_selection_history(since_date=since_date, until_date=until_date)
    if not records:
        return {"total": 0, "backfilled": 0, "skipped": 0, "failed": 0}

    backfilled = 0
    skipped = 0
    failed = 0
    now_iso = _utc_now_iso()

    # Group by (selection_date, selection_run_id) — one outcome file per run
    groups: dict[tuple[str, str], list[SelectionRecord]] = {}
    for rec in records:
        key = (rec.selection_date, rec.selection_run_id)
        groups.setdefault(key, []).append(rec)

    for (sel_date, run_id), group in groups.items():
        # ── Check if outcome file already exists ──
        outcome_path = OUTCOMES_ROOT / sel_date / f"{run_id}.json"
        if outcome_path.exists():
            try:
                existing = json.loads(outcome_path.read_text(encoding="utf-8"))
                # Check if ALL records in the existing file have content_hash set
                all_hashed = all(
                    r.get("content_hash") for r in existing
                    if isinstance(r, dict)
                )
                if all_hashed and len(existing) == len(group):
                    skipped += len(existing)
                    continue
            except (json.JSONDecodeError, OSError):
                pass  # corrupt file → regenerate

        # ── Backfill each candidate in the group ──
        outcome_records: list[dict[str, Any]] = []
        for rec in group:
            outcome = _backfill_one(rec, now_iso)
            if outcome.status in ("FAILED_DATA", "INSUFFICIENT_HISTORY"):
                failed += 1
            else:
                backfilled += 1
            outcome_records.append(outcome.to_dict())

        if not dry_run and outcome_records:
            _write_atomic(
                outcome_path,
                json.dumps(outcome_records, ensure_ascii=False, indent=2),
            )

    return {
        "total": len(records),
        "backfilled": backfilled,
        "skipped": skipped,
        "failed": failed,
    }


def _backfill_one(rec: SelectionRecord, now_iso: str) -> OutcomeRecord:
    """Backfill a single SelectionRecord.  Never raises."""
    try:
        bars, provider = _fetch_forward_bars(
            symbol=rec.symbol,
            from_date=rec.selection_date,
        )

        if not bars:
            outcome = _failed_record(
                rec.selection_date, rec.entry_reference_price,
                rec.range_low, rec.range_high,
                provider, "FAILED_DATA",
                "no OHLCV data returned for symbol",
            )
        else:
            outcome = _compute_forward_metrics(
                bars=bars,
                entry_price=rec.entry_reference_price,
                selection_date_str=rec.selection_date,
                range_low=rec.range_low,
                range_high=rec.range_high,
                provider=provider,
            )
    except Exception as exc:
        outcome = _failed_record(
            rec.selection_date, rec.entry_reference_price,
            rec.range_low, rec.range_high,
            "none", "FAILED_DATA",
            f"unexpected error: {exc}",
        )

    # Stamp identity fields from the source record
    outcome.selection_run_id = rec.selection_run_id
    outcome.symbol = rec.symbol
    outcome.formal_rank = rec.formal_rank
    outcome.backfilled_at = now_iso

    # Compute and stamp content hash
    outcome.content_hash = _compute_content_hash(outcome)

    return outcome
