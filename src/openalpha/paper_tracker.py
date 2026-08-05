"""Paper Position Tracker — Phase 5B.

Computes current state snapshots for paper research trades
created by Phase 5A.  Reads OPEN trades from paper_trading/,
fetches current OHLCV data, and writes PositionSnapshot records.

CRITICAL SAFETY BOUNDARY:
  - NEVER modifies Phase 5A paper trade status.
  - exit_signal, exit_recommendation, and trade_status_observation
    are RESEARCH OBSERVATIONS only.
  - Phase 5A ledger/paper_trading data remains IMMUTABLE.

Reads from:
  - artifacts/learning/paper_trading/

Writes to:
  - artifacts/learning/paper_tracking/

Safety: research-only.  No imports from scoring, engine, broker,
risk, order, or safety modules.  No live trading.  No order creation.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.openalpha.paper_research import (
    load_paper_trades,
    PaperTradeRecord,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

PAPER_TRACKER_VERSION = "paper_tracker.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
PAPER_TRADING_ROOT = PROJECT_DIR / "artifacts" / "learning" / "paper_trading"
TRACKING_ROOT = PROJECT_DIR / "artifacts" / "learning" / "paper_tracking"


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionSnapshot:
    """One snapshot of a paper trade's current market state.

    Written for each OPEN trade on each tracking date.
    Immutable once written.  NEVER modifies the Phase 5A trade record.
    """

    # ── Identity (joins to Phase 5A PaperTradeRecord) ──
    paper_trade_id: str
    symbol: str
    selection_date: str
    entry_date: str
    entry_price: float
    range_low: float
    range_high: float

    # ── Snapshot date ──
    snapshot_date: str               # ISO date this snapshot was taken

    # ── Current market state ──
    current_price: float | None = None
    price_source: str = ""           # "yfinance" | "price_fetcher" | "none"

    # ── Performance since entry ──
    unrealized_return_pct: float | None = None
    holding_days: int = 0

    # ── Path-based risk (since entry) ──
    mfe_since_entry_pct: float | None = None
    mae_since_entry_pct: float | None = None

    # ── Range evaluation (since entry) ──
    range_touch_support: bool | None = None
    range_touch_resistance: bool | None = None
    range_breakdown: bool | None = None
    range_breakout: bool | None = None

    # ── RESEARCH OBSERVATIONS (never modify trade state) ──
    exit_signal: str | None = None           # None | "breakdown" | "breakout"
    exit_recommendation: str = "HOLD"        # "HOLD" | "REVIEW_EXIT"
    trade_status_observation: str = "OPEN"   # "OPEN" | "SHOULD_CLOSE"

    # ── Meta ──
    paper_tracker_version: str = PAPER_TRACKER_VERSION
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PositionSnapshot":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: d[k] for k in known if k in d})


@dataclass
class DailySummary:
    """Aggregate tracking summary for one date."""

    snapshot_date: str
    total_open_trades: int = 0
    avg_unrealized_return_pct: float | None = None
    avg_holding_days: float = 0.0
    avg_mfe_pct: float | None = None
    avg_mae_pct: float | None = None
    breakdown_count: int = 0
    breakout_count: int = 0
    support_touch_count: int = 0
    resistance_touch_count: int = 0
    review_exit_count: int = 0
    sector_breakdown: dict[str, int] | None = None
    paper_tracker_version: str = PAPER_TRACKER_VERSION
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


def _read_json_list(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# OHLCV fetching — follows existing QuantCairn data boundary
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_position_bars(
    symbol: str,
    from_date: str,
) -> tuple[list[dict[str, float]], str]:
    """Fetch daily OHLCV bars from *from_date* to today."""
    end_dt = datetime.now(timezone.utc) + timedelta(days=1)

    # Path 1: yfinance Ticker
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        from datetime import datetime as dt
        start_dt = dt.fromisoformat(from_date)
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

    # Path 2: PriceFetcher
    try:
        from src.data.fetcher import PriceFetcher
        fetcher = PriceFetcher(symbol, poll_interval=0)
        try:
            candles = fetcher.get_ohlcv(period="3mo", interval="1d")
            if candles:
                from datetime import datetime as dt
                start_d = dt.fromisoformat(from_date).date()
                end_d = end_dt.date() if isinstance(end_dt, datetime) else end_dt.date()
                bars = []
                for c in candles:
                    ts = getattr(c, "timestamp", None)
                    bar_d = ts.date() if hasattr(ts, "date") else None
                    if bar_d is None:
                        continue
                    if bar_d < start_d or bar_d > end_d:
                        continue
                    bars.append({
                        "date": bar_d.isoformat(),
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

    # Path 3: yfinance.download (last resort)
    try:
        import yfinance as yf
        from datetime import datetime as dt
        start_dt = dt.fromisoformat(from_date)
        df = yf.download(symbol, start=start_dt, end=end_dt, interval="1d", progress=False)
        if df is not None and not df.empty:
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
# Snapshot computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_snapshot(trade: PaperTradeRecord) -> PositionSnapshot:
    """Compute a PositionSnapshot for a single paper trade.

    Fetches OHLCV data, computes all metrics.
    NEVER modifies the input trade record.
    """
    today_str = date.today().isoformat()
    now_iso = _utc_now_iso()

    snap = PositionSnapshot(
        paper_trade_id=trade.paper_trade_id,
        symbol=trade.symbol,
        selection_date=trade.selection_date,
        entry_date=trade.entry_date,
        entry_price=trade.entry_price,
        range_low=trade.range_low,
        range_high=trade.range_high,
        snapshot_date=today_str,
        recorded_at=now_iso,
    )

    # ── Fetch bars ──
    bars, provider = _fetch_position_bars(trade.symbol, trade.entry_date)

    if not bars:
        snap.price_source = provider or "none"
        snap.exit_recommendation = "HOLD"
        snap.trade_status_observation = "OPEN"
        return snap

    snap.price_source = provider

    # ── Holding days ──
    try:
        entry_d = date.fromisoformat(trade.entry_date)
        snap.holding_days = (date.today() - entry_d).days
    except ValueError:
        snap.holding_days = 0

    # ── Current price ──
    if bars:
        snap.current_price = bars[-1]["close"]

    # ── Unrealized return ──
    if snap.current_price and trade.entry_price > 0:
        snap.unrealized_return_pct = round(
            (snap.current_price - trade.entry_price) / trade.entry_price * 100.0, 4
        )

    # ── Path-based MFE/MAE ──
    if bars and trade.entry_price > 0:
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        snap.mfe_since_entry_pct = round(
            max(0.0, (max(highs) - trade.entry_price) / trade.entry_price * 100.0), 4
        )
        snap.mae_since_entry_pct = round(
            min(0.0, (min(lows) - trade.entry_price) / trade.entry_price * 100.0), 4
        )

    # ── Range evaluation ──
    if bars and trade.range_low > 0 and trade.range_high > trade.range_low:
        snap.range_touch_support = any(b["low"] <= trade.range_low for b in bars)
        snap.range_touch_resistance = any(b["high"] >= trade.range_high for b in bars)
        snap.range_breakdown = any(b["close"] < trade.range_low for b in bars)
        snap.range_breakout = any(b["close"] > trade.range_high for b in bars)

    # ── RESEARCH OBSERVATIONS (never modify trade state) ──
    if snap.range_breakdown:
        snap.exit_signal = "breakdown"
        snap.exit_recommendation = "REVIEW_EXIT"
        snap.trade_status_observation = "SHOULD_CLOSE"
    elif snap.range_breakout:
        snap.exit_signal = "breakout"
        snap.exit_recommendation = "REVIEW_EXIT"
        snap.trade_status_observation = "SHOULD_CLOSE"
    else:
        snap.exit_signal = None
        snap.exit_recommendation = "HOLD"
        snap.trade_status_observation = "OPEN"

    return snap


# ═══════════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════════

def run_position_tracking(
    *,
    snapshot_date: str | None = None,
) -> dict[str, Any]:
    """Run position tracking for all OPEN paper trades.

    Computes PositionSnapshot for each OPEN trade and writes
    aggregate DailySummary.  NEVER modifies trade statuses.

    Returns a summary dict.
    """
    today_str = snapshot_date or date.today().isoformat()
    now_iso = _utc_now_iso()

    # ── Load OPEN trades ──
    trades = load_paper_trades(status="OPEN")
    if not trades:
        # Write empty summary
        summary = DailySummary(
            snapshot_date=today_str,
            total_open_trades=0,
            generated_at=now_iso,
        )
        _write_atomic(
            TRACKING_ROOT / "summary" / today_str / "summary.json",
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
        )
        return {
            "snapshot_date": today_str,
            "total_open_trades": 0,
            "snapshots": 0,
        }

    # ── Compute snapshots ──
    snapshots: list[PositionSnapshot] = []
    for trade in trades:
        try:
            snap = compute_snapshot(trade)
            snapshots.append(snap)
        except Exception:
            # Continue processing other trades; never abort on one failure
            pass

    # ── Dedup snapshots for this date ──
    positions_dir = TRACKING_ROOT / "positions" / today_str
    existing_path = positions_dir / "positions.json"
    existing = _read_json_list(existing_path) or []
    existing_ids = {s.get("paper_trade_id") for s in existing if s.get("paper_trade_id")}

    new_snapshots = [s for s in snapshots if s.paper_trade_id not in existing_ids]
    all_snapshots = existing + [s.to_dict() for s in new_snapshots]

    if new_snapshots:
        _write_atomic(
            existing_path,
            json.dumps(all_snapshots, ensure_ascii=False, indent=2),
        )

    # ── Daily summary ──
    all_for_summary = [PositionSnapshot.from_dict(s) if isinstance(s, dict) else s
                       for s in all_snapshots]
    # Convert any dicts still in the list
    resolved: list[PositionSnapshot] = []
    for s in all_for_summary:
        if isinstance(s, PositionSnapshot):
            resolved.append(s)
        elif isinstance(s, dict):
            try:
                resolved.append(PositionSnapshot.from_dict(s))
            except (TypeError, KeyError):
                pass

    summary = _compute_daily_summary(resolved, today_str, now_iso)

    _write_atomic(
        TRACKING_ROOT / "summary" / today_str / "summary.json",
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
    )

    # ── Update index ──
    _update_index(today_str)

    return {
        "snapshot_date": today_str,
        "total_open_trades": len(trades),
        "snapshots": len(new_snapshots),
        "skipped": len(existing_ids),
        "review_exit_count": summary.review_exit_count,
    }


def _compute_daily_summary(
    snapshots: list[PositionSnapshot],
    snapshot_date: str,
    now_iso: str,
) -> DailySummary:
    """Compute aggregate daily summary from a list of snapshots."""
    n = len(snapshots)

    returns = [s.unrealized_return_pct for s in snapshots
               if s.unrealized_return_pct is not None]
    mfes = [s.mfe_since_entry_pct for s in snapshots
            if s.mfe_since_entry_pct is not None]
    maes = [s.mae_since_entry_pct for s in snapshots
            if s.mae_since_entry_pct is not None]
    holding_days = [s.holding_days for s in snapshots]

    sectors: dict[str, int] = defaultdict(int)
    for t in load_paper_trades(status="OPEN"):
        sectors[t.sector] += 1

    return DailySummary(
        snapshot_date=snapshot_date,
        total_open_trades=n,
        avg_unrealized_return_pct=round(_mean(returns), 4) if returns else None,
        avg_holding_days=round(_mean([float(d) for d in holding_days]), 1) if holding_days else 0.0,
        avg_mfe_pct=round(_mean(mfes), 4) if mfes else None,
        avg_mae_pct=round(_mean(maes), 4) if maes else None,
        breakdown_count=sum(1 for s in snapshots if s.range_breakdown),
        breakout_count=sum(1 for s in snapshots if s.range_breakout),
        support_touch_count=sum(1 for s in snapshots if s.range_touch_support),
        resistance_touch_count=sum(1 for s in snapshots if s.range_touch_resistance),
        review_exit_count=sum(1 for s in snapshots if s.exit_recommendation == "REVIEW_EXIT"),
        sector_breakdown=dict(sorted(sectors.items())),
        generated_at=now_iso,
    )


def _update_index(date_str: str) -> None:
    """Update the tracking index with a new snapshot date."""
    index_path = TRACKING_ROOT / "index.json"

    existing: dict[str, Any] = {}
    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    dates: list[str] = existing.get("dates", [])
    if date_str not in dates:
        dates.append(date_str)
        dates.sort()

    existing["dates"] = dates
    existing["version"] = PAPER_TRACKER_VERSION
    existing["updated_at"] = _utc_now_iso()

    _write_atomic(index_path, json.dumps(existing, ensure_ascii=False, indent=2))


def load_snapshots(
    *,
    snapshot_date: str | None = None,
) -> list[PositionSnapshot]:
    """Load position snapshots, optionally filtered by date."""
    if not TRACKING_ROOT.exists():
        return []

    snapshots: list[PositionSnapshot] = []

    if snapshot_date:
        date_dirs = [TRACKING_ROOT / "positions" / snapshot_date]
    else:
        positions_dir = TRACKING_ROOT / "positions"
        if not positions_dir.exists():
            return []
        date_dirs = sorted(d for d in positions_dir.iterdir() if d.is_dir())

    for date_dir in date_dirs:
        path = date_dir / "positions.json"
        data = _read_json_list(path)
        if not data:
            continue
        for rec in data:
            try:
                snapshots.append(PositionSnapshot.from_dict(rec))
            except (TypeError, KeyError):
                continue

    return snapshots


def load_daily_summaries() -> list[DailySummary]:
    """Load all daily summaries."""
    summary_dir = TRACKING_ROOT / "summary"
    if not summary_dir.exists():
        return []

    summaries: list[DailySummary] = []
    for date_dir in sorted(summary_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        path = date_dir / "summary.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                summaries.append(DailySummary(
                    snapshot_date=str(data.get("snapshot_date") or ""),
                    total_open_trades=int(data.get("total_open_trades", 0)),
                    avg_unrealized_return_pct=data.get("avg_unrealized_return_pct"),
                    avg_holding_days=float(data.get("avg_holding_days", 0)),
                    avg_mfe_pct=data.get("avg_mfe_pct"),
                    avg_mae_pct=data.get("avg_mae_pct"),
                    breakdown_count=int(data.get("breakdown_count", 0)),
                    breakout_count=int(data.get("breakout_count", 0)),
                    support_touch_count=int(data.get("support_touch_count", 0)),
                    resistance_touch_count=int(data.get("resistance_touch_count", 0)),
                    review_exit_count=int(data.get("review_exit_count", 0)),
                    sector_breakdown=data.get("sector_breakdown"),
                    generated_at=str(data.get("generated_at", "")),
                ))
        except (json.JSONDecodeError, OSError, TypeError, KeyError):
            continue

    return summaries
