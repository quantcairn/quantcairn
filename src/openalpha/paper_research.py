"""Paper Trading Research Bridge — Phase 5A.

Converts selection records from the Phase 1 ledger into offline
simulated paper trade records for research analysis.

Reads from:
  - artifacts/learning/selection_ledger/

Writes to:
  - artifacts/learning/paper_trading/

Safety: research-only consumer.  No imports from scoring, engine,
broker, risk, order, or safety modules.  No live trading.  No
automatic parameter adjustment.  No ML training.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from src.config.runtime_paths import resolve_artifacts_dir
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

PAPER_RESEARCH_VERSION = "paper_research.v1"
PROJECT_DIR = Path(__file__).resolve().parents[2]
PAPER_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "paper_trading"
LEDGER_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "learning" / "selection_ledger"

VALID_STATUSES = {"OPEN", "CLOSED", "FAILED"}


# ═══════════════════════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PaperTradeRecord:
    """One simulated paper trade created from a selection record.

    This is a RESEARCH record — never consumed by the trading engine.
    It exists to evaluate what WOULD have happened if a position had
    been opened based on the selection.
    """

    # ── Identity ──
    paper_trade_id: str                      # UUID, generated on creation
    selection_run_id: str
    selection_date: str                      # ISO date of the selection run
    symbol: str

    # ── Entry ──
    entry_date: str                          # ISO date when trade was created
    entry_price: float
    range_low: float
    range_high: float
    sector: str
    score: float

    # ── Strategy ──
    recommended_strategy: str = ""

    # ── Status ──
    status: str = "OPEN"                     # OPEN | CLOSED | FAILED
    status_reason: str = ""

    # ── Exit (populated on close) ──
    exit_date: str | None = None
    exit_price: float | None = None
    return_pct: float | None = None
    holding_days: int | None = None
    range_outcome: str | None = None         # contained | breakdown | breakout | touch_both

    # ── Meta ──
    created_at: str = ""
    closed_at: str | None = None
    paper_research_version: str = PAPER_RESEARCH_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaperTradeRecord":
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
# 1. Create paper trades from selection records
# ═══════════════════════════════════════════════════════════════════════════════

def create_paper_trade(
    *,
    selection_run_id: str,
    selection_date: str,
    symbol: str,
    entry_price: float = 0.0,
    range_low: float = 0.0,
    range_high: float = 0.0,
    sector: str = "",
    score: float = 0.0,
    recommended_strategy: str = "",
) -> PaperTradeRecord:
    """Create a new OPEN paper trade from selection metadata.

    Returns a PaperTradeRecord.  Does NOT write to disk — call
    save_paper_trades() to persist.
    """
    now = _utc_now_iso()
    return PaperTradeRecord(
        paper_trade_id=uuid.uuid4().hex,
        selection_run_id=str(selection_run_id),
        selection_date=str(selection_date),
        symbol=str(symbol).strip().upper(),
        entry_date=str(selection_date),
        entry_price=_safe_float(entry_price),
        range_low=_safe_float(range_low),
        range_high=_safe_float(range_high),
        sector=str(sector),
        score=_safe_float(score),
        recommended_strategy=str(recommended_strategy),
        status="OPEN",
        created_at=now,
    )


def create_paper_trades_from_ledger(
    *,
    selection_date: str | None = None,
    run_id: str | None = None,
) -> list[PaperTradeRecord]:
    """Create PaperTradeRecords from existing selection ledger records.

    Reads the Phase 1 selection ledger and converts each selection
    record into an OPEN paper trade.  Filters by date or run_id
    if provided.

    Returns a list of PaperTradeRecord.  Does NOT write to disk.
    """
    trades: list[PaperTradeRecord] = []
    seen: set[tuple[str, str]] = set()

    runs_dir = LEDGER_ROOT / "runs"
    if not runs_dir.exists():
        return trades

    for date_dir in sorted(runs_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        if selection_date and date_dir.name != selection_date:
            continue

        for run_file in sorted(date_dir.iterdir()):
            if run_file.suffix != ".json":
                continue
            if run_id and run_file.stem != run_id:
                continue

            data = _read_json_list(run_file)
            if not data:
                continue

            for rec in data:
                sym = str(rec.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                rid = str(rec.get("selection_run_id") or run_file.stem)

                key = (rid, sym)
                if key in seen:
                    continue
                seen.add(key)

                trade = create_paper_trade(
                    selection_run_id=rid,
                    selection_date=str(rec.get("selection_date") or date_dir.name),
                    symbol=sym,
                    entry_price=_safe_float(rec.get("entry_reference_price")),
                    range_low=_safe_float(rec.get("range_low")),
                    range_high=_safe_float(rec.get("range_high")),
                    sector=str(rec.get("sector") or ""),
                    score=_safe_float(rec.get("score")),
                    recommended_strategy=str(rec.get("recommended_strategy") or ""),
                )
                trades.append(trade)

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Persist and load
# ═══════════════════════════════════════════════════════════════════════════════

def save_paper_trades(
    trades: list[PaperTradeRecord],
    *,
    date_str: str | None = None,
) -> Path | None:
    """Save a list of PaperTradeRecords to disk.

    Writes to artifacts/learning/paper_trading/{date}/trades.json.
    Appends to existing records if the file already exists.
    Deduplicates by paper_trade_id.
    """
    if not trades:
        return None

    now = _utc_now_iso()
    date_dir = str(date_str or now[:10])
    trades_path = PAPER_ROOT / date_dir / "trades.json"

    existing = _read_json_list(trades_path) or []
    existing_ids = {r.get("paper_trade_id") for r in existing if r.get("paper_trade_id")}

    for t in trades:
        if t.paper_trade_id not in existing_ids:
            existing.append(t.to_dict())
            existing_ids.add(t.paper_trade_id)

    _write_atomic(trades_path, json.dumps(existing, ensure_ascii=False, indent=2))
    return trades_path


def load_paper_trades(
    *,
    date_str: str | None = None,
    status: str | None = None,
    symbol: str | None = None,
) -> list[PaperTradeRecord]:
    """Load paper trades from disk, optionally filtered.

    If date_str is provided, loads only that date's trades.
    Otherwise loads ALL available dates.
    """
    if not PAPER_ROOT.exists():
        return []

    trades: list[PaperTradeRecord] = []

    if date_str:
        date_dirs = [PAPER_ROOT / date_str]
    else:
        date_dirs = sorted(d for d in PAPER_ROOT.iterdir() if d.is_dir())

    for date_dir in date_dirs:
        trades_path = date_dir / "trades.json"
        data = _read_json_list(trades_path)
        if not data:
            continue
        for rec in data:
            try:
                t = PaperTradeRecord.from_dict(rec)
            except (TypeError, KeyError):
                continue
            if status and t.status != status:
                continue
            if symbol and t.symbol != symbol.upper():
                continue
            trades.append(t)

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Update paper trades
# ═══════════════════════════════════════════════════════════════════════════════

def update_paper_trade(
    paper_trade_id: str,
    *,
    status: str | None = None,
    status_reason: str | None = None,
    exit_date: str | None = None,
    exit_price: float | None = None,
    return_pct: float | None = None,
    holding_days: int | None = None,
    range_outcome: str | None = None,
) -> PaperTradeRecord | None:
    """Update an existing paper trade by paper_trade_id.

    Returns the updated PaperTradeRecord, or None if not found.
    Saves the update atomically to disk.
    """
    now = _utc_now_iso()

    # Find the trade in all date directories
    if not PAPER_ROOT.exists():
        return None

    for date_dir in sorted(PAPER_ROOT.iterdir()):
        if not date_dir.is_dir():
            continue
        trades_path = date_dir / "trades.json"
        data = _read_json_list(trades_path)
        if not data:
            continue

        updated = False
        target: dict[str, Any] | None = None
        for rec in data:
            if rec.get("paper_trade_id") == paper_trade_id:
                target = rec
                break

        if target is None:
            continue

        if status is not None:
            if status not in VALID_STATUSES:
                return None
            target["status"] = status
            if status == "CLOSED":
                target["closed_at"] = now
        if status_reason is not None:
            target["status_reason"] = status_reason
        if exit_date is not None:
            target["exit_date"] = exit_date
        if exit_price is not None:
            target["exit_price"] = exit_price
        if return_pct is not None:
            target["return_pct"] = return_pct
        if holding_days is not None:
            target["holding_days"] = holding_days
        if range_outcome is not None:
            target["range_outcome"] = range_outcome

        _write_atomic(trades_path, json.dumps(data, ensure_ascii=False, indent=2))

        try:
            return PaperTradeRecord.from_dict(target)
        except (TypeError, KeyError):
            return None

    return None
