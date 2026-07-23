"""Append-only, idempotent paper trade outcome dataset builder.

Primary data source: PaperPortfolioState (processed_fill_ids, fill_sequence).
Supplementary: runtime audit JSONL for prices, timestamps, P&L.
Selection context: bound to BUY event, inherited by SELL outcome.

Never calls a broker, never triggers orders, never mutates trading state.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from src.broker.paper_portfolio_state import read_paper_portfolio_state
from src.ai_selector.selection_bundle import load_committed_selection_bundle
from src.ai_selector.selection_state import load_selection_state

PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_DIR = PROJECT_DIR / "artifacts" / "learning"
OUTCOME_CSV_PATH = LEARNING_DIR / "outcome_dataset.csv"
OUTCOME_STATE_PATH = LEARNING_DIR / ".outcome_collector_state.json"
OUTCOME_LOCK_PATH = LEARNING_DIR / ".outcome_collector.lock"
OUTCOME_SUMMARY_PATH = LEARNING_DIR / "outcome_summary.json"
AUDIT_LOG_DIR = PROJECT_DIR / "logs"
SCHEMA_VERSION = "outcome_collector.v2"
ALLOWED_EXECUTION_MODES = {"paper", "sandbox"}
SAFE_LOOKBACK_DAYS = 3
MAX_CSV_ROWS = 50_000


# ── Utilities ──────────────────────────────────────────────────────────

_OUTCOME_CSV_COLUMNS_V2 = [
    "outcome_id",
    "account_id",
    "symbol",
    "buy_fill_id",
    "sell_fill_id",
    "selection_run_id",
    "selection_date",
    "selection_bundle_hash",
    "formal_rank",
    "formal_candidate_score",
    "strategy_fit_score",
    "market_regime",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
    "pnl_pct",
    "realized_pnl",
    "hold_duration_seconds",
    "exit_reason",
    "execution_mode",
    "selection_context_status",
    "training_eligible",
    "training_ineligible_reasons",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return default
    return round(result, 6)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


# ── Outcome ID ─────────────────────────────────────────────────────────

def _make_outcome_id(
    account_id: str,
    symbol: str,
    buy_fill_id: str,
    sell_fill_id: str,
    quantity: int,
    entry_time: str,
    exit_time: str,
    entry_price: float,
    exit_price: float,
) -> str:
    seed = "|".join([
        _normalize_symbol(account_id),
        _normalize_symbol(symbol),
        str(buy_fill_id or "").strip(),
        str(sell_fill_id or "").strip(),
        str(max(1, _safe_int(quantity))),
        str(entry_time or "").strip(),
        str(exit_time or "").strip(),
        str(round(_safe_float(entry_price), 4)),
        str(round(_safe_float(exit_price), 4)),
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


# ── Fill event ─────────────────────────────────────────────────────────

@dataclass(slots=True)
class FillEvent:
    symbol: str
    side: str
    fill_id: str
    order_id: str
    quantity: int
    price: float
    timestamp: str
    pnl: float | None = None
    reason: str = ""
    mode: str = "paper"
    account_id: str = ""
    selection_run_id: str = ""
    selection_date: str = ""
    selection_bundle_hash: str = ""
    event_id: str = ""

    @property
    def is_valid(self) -> bool:
        return (
            self.price > 0
            and self.quantity > 0
            and bool(self.fill_id.strip())
            and self.side in {"BUY", "SELL"}
            and self.mode in ALLOWED_EXECUTION_MODES
        )

    @classmethod
    def from_audit_line(cls, line: dict[str, Any]) -> "FillEvent | None":
        mode = str(line.get("execution_mode") or line.get("mode") or "").strip().lower()
        if mode not in ALLOWED_EXECUTION_MODES:
            return None

        phase = str(line.get("phase") or "").strip()
        # Non-trade phases never produce fill events
        _non_trade_phases = {
            "heartbeat", "startup_safety_check", "live_arming_check",
            "sell_lock_acquired", "sell_lock_released", "sell_lock_blocked",
            "exit_fence_set", "ai_selector_status", "ai_selector_cache_hit",
            "ai_selector_decision", "ai_selector_inactive",
            "position_sync_fence_set", "position_sync_fence_cleared",
            "portfolio_risk_blocked", "orphan_position_scan",
            "orphan_assignment_change", "orphan_exit_skipped",
            "orphan_identity_mismatch", "orphan_confirmed",
            "sandbox_first_run_confirmed",
        }
        if phase in _non_trade_phases:
            return None

        event_pnl = line.get("pnl")
        response = line.get("response") or {}
        response_status = str(response.get("status") or "").strip().lower()
        order = line.get("order")
        side_str = ""

        if isinstance(order, dict):
            side_str = str(order.get("side") or "").strip().upper()
            # Must be a confirmed fill, not just submitted
            if response_status and response_status not in {"filled", "partially_filled"}:
                if phase in {"risk_decision", "risk_exit_trigger", "orphan_risk_exit"}:
                    final_action = str(line.get("final_action") or "").strip().lower()
                    if final_action != "buy_candidate" and response_status != "filled":
                        return None
                else:
                    return None

        if side_str not in {"BUY", "SELL"}:
            return None

        symbol = _normalize_symbol(line.get("ticker") or line.get("symbol") or "")
        if not symbol:
            return None

        fill_qty = 0
        if isinstance(order, dict):
            fill_qty = _safe_int(order.get("qty", order.get("quantity")))
        if fill_qty <= 0:
            return None

        fill_price = _safe_float(
            order.get("price") if isinstance(order, dict) else 0.0
        ) or _safe_float(line.get("current_price"))

        if fill_price <= 0:
            return None

        fill_id = str(
            line.get("fill_id")
            or line.get("order_id")
            or (order.get("order_id") if isinstance(order, dict) else "")
            or line.get("event_id")
            or ""
        ).strip()
        if not fill_id:
            return None
        # Reject known non-fill identifiers
        _non_fill_ids = {"PENDING", "BP_CHECK", "STATE_ERROR", "NONE"}
        if fill_id.upper() in _non_fill_ids:
            return None
        # Reject test patterns
        fill_id_lower = fill_id.lower()
        if fill_id_lower.startswith("test-") or fill_id_lower.startswith("fake-"):
            return None
        # Reject pure numeric or generated SELL-N patterns (not real order IDs)
        if fill_id_lower.startswith("sell-"):
            try:
                int(fill_id_lower[5:])
                return None  # SELL-N where N is numeric = test/fake
            except ValueError:
                pass  # sell- with non-numeric suffix may be real

        pnl = _safe_float(event_pnl) if event_pnl is not None else None

        return cls(
            symbol=symbol,
            side=side_str,
            fill_id=fill_id,
            order_id=str(line.get("order_id") or "").strip(),
            quantity=fill_qty,
            price=fill_price,
            timestamp=str(line.get("timestamp") or ""),
            pnl=pnl,
            reason=str(line.get("reason") or line.get("final_action") or ""),
            mode=mode,
            account_id=str(line.get("account_id") or "paper-default"),
            selection_run_id=str(line.get("selection_run_id") or ""),
            selection_date=str(line.get("selection_date") or ""),
            selection_bundle_hash=str(line.get("selection_bundle_hash") or ""),
            event_id=str(line.get("event_id") or line.get("notification_key") or ""),
        )


# ── Audit log parsing ──────────────────────────────────────────────────

def _parse_audit_fills(
    log_dir: Path,
    since_timestamp: str | None = None,
    known_fill_ids: set[str] | None = None,
) -> list[FillEvent]:
    """Scan runtime audit JSONL files for confirmed paper fill events.

    Returns only fills with valid prices, quantities, and execution modes.
    Skips live, orphan, test, and fake events.
    """
    fills: list[FillEvent] = []
    known = known_fill_ids or set()
    if not log_dir.exists():
        return fills

    lookback = datetime.now(timezone.utc) - timedelta(days=SAFE_LOOKBACK_DAYS)
    lookback_ts = lookback.isoformat(timespec="seconds")

    for log_path in sorted(log_dir.glob("trades-*.jsonl")):
        # Skip files older than lookback window entirely
        try:
            file_stem = log_path.stem  # trades-20260723
            file_date_str = file_stem.replace("trades-", "")
            file_date = datetime.strptime(file_date_str, "%Y%m%d").date()
            if file_date < lookback.date():
                continue
        except ValueError:
            pass

        try:
            text = log_path.read_text(encoding="utf-8")
        except Exception:
            continue

        for line_text in text.strip().split("\n"):
            line_text = line_text.strip()
            if not line_text:
                continue
            try:
                line = json.loads(line_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(line, dict):
                continue

            # Time-based skip (performance only, not correctness)
            ts = str(line.get("timestamp") or "")
            if since_timestamp and ts and ts < since_timestamp:
                continue

            fill = FillEvent.from_audit_line(line)
            if fill is None:
                continue
            if not fill.is_valid:
                continue
            # Dedup by fill_id within a single parse run
            if fill.fill_id in known:
                continue
            known.add(fill.fill_id)
            fills.append(fill)

    return fills


# ── Lot-based FIFO matching ────────────────────────────────────────────

@dataclass(slots=True)
class BuyLot:
    symbol: str
    fill_id: str
    quantity: int
    filled: int
    price: float
    timestamp: str
    mode: str
    selection_run_id: str
    selection_date: str
    selection_bundle_hash: str


def _match_closed_trades_lots(fills: list[FillEvent]) -> list[dict[str, Any]]:
    """Match BUY/SELL pairs into closed trades using per-symbol FIFO buy lots.

    Supports partial fills: BUY 10 → SELL 4 → SELL 6 produces 2 outcomes.
    Supports multiple buy lots: BUY 5@100 → BUY 5@110 → SELL 7 →
       outcome1: 5@100→sell, outcome2: 2@110→sell, remaining lot: 3@110.
    """
    buys: dict[str, deque[BuyLot]] = {}
    closed: list[dict[str, Any]] = []

    for fill in sorted(fills, key=lambda f: f.timestamp):
        if fill.side == "BUY":
            lot = BuyLot(
                symbol=_normalize_symbol(fill.symbol),
                fill_id=fill.fill_id,
                quantity=fill.quantity,
                filled=0,
                price=fill.price,
                timestamp=fill.timestamp,
                mode=fill.mode,
                selection_run_id=fill.selection_run_id or "",
                selection_date=fill.selection_date or "",
                selection_bundle_hash=fill.selection_bundle_hash or "",
            )
            buys.setdefault(lot.symbol, deque()).append(lot)

        elif fill.side == "SELL":
            symbol = _normalize_symbol(fill.symbol)
            symbol_buys = buys.get(symbol)
            if not symbol_buys:
                continue

            remaining_sell = fill.quantity
            sell_price = fill.price
            sell_ts = fill.timestamp

            while remaining_sell > 0 and symbol_buys:
                buy_lot = symbol_buys[0]
                available = buy_lot.quantity - buy_lot.filled
                if available <= 0:
                    symbol_buys.popleft()
                    continue

                matched_qty = min(available, remaining_sell)
                buy_lot.filled += matched_qty
                remaining_sell -= matched_qty

                entry_ts = buy_lot.timestamp or sell_ts
                exit_ts = sell_ts or _utc_now_iso()

                try:
                    et = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                    xt = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
                    hold_seconds = max(0, int((xt - et).total_seconds()))
                except (ValueError, TypeError):
                    hold_seconds = 0

                # P&L: fill.pnl is total for the fill; allocate proportionally
                if fill.pnl is not None:
                    allocated_pnl = (fill.pnl * matched_qty / max(1, fill.quantity)) if fill.quantity > 0 else 0.0
                else:
                    allocated_pnl = (sell_price - buy_lot.price) * matched_qty

                pnl_pct = 0.0
                if buy_lot.price > 0:
                    pnl_pct = ((sell_price - buy_lot.price) / buy_lot.price) * 100.0

                outcome_id = _make_outcome_id(
                    account_id=fill.account_id or "paper-default",
                    symbol=symbol,
                    buy_fill_id=buy_lot.fill_id,
                    sell_fill_id=fill.fill_id,
                    quantity=matched_qty,
                    entry_time=entry_ts,
                    exit_time=exit_ts,
                    entry_price=buy_lot.price,
                    exit_price=sell_price,
                )

                closed.append({
                    "outcome_id": outcome_id,
                    "account_id": fill.account_id or "paper-default",
                    "symbol": symbol,
                    "buy_fill_id": buy_lot.fill_id,
                    "sell_fill_id": fill.fill_id,
                    "entry_time": entry_ts,
                    "exit_time": exit_ts,
                    "entry_price": buy_lot.price,
                    "exit_price": sell_price,
                    "quantity": matched_qty,
                    "realized_pnl": round(allocated_pnl, 4),
                    "pnl_pct": round(pnl_pct, 4),
                    "hold_duration_seconds": hold_seconds,
                    "exit_reason": fill.reason or "",
                    "execution_mode": fill.mode or buy_lot.mode,
                    # Entry-time selection context from the BUY lot
                    "selection_run_id": buy_lot.selection_run_id,
                    "selection_date": buy_lot.selection_date,
                    "selection_bundle_hash": buy_lot.selection_bundle_hash,
                })

                # Remove fully depleted lots
                if buy_lot.filled >= buy_lot.quantity:
                    symbol_buys.popleft()

    return closed


# ── Selection context enrichment ───────────────────────────────────────

def _load_selection_context_for_date(
    selection_date: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load per-symbol selection metadata from the committed bundle.

    When selection_date is provided, only return context if the bundle
    date matches — preventing exit-time bundles from overwriting entry
    context.
    """
    ctx: dict[str, dict[str, Any]] = {}
    bundle = load_committed_selection_bundle(PROJECT_DIR)
    if not isinstance(bundle, dict):
        return ctx

    report = bundle.get("report") or {}
    manifest = bundle.get("manifest") or {}

    bundle_date = str(manifest.get("selection_date") or report.get("selection_date") or "")
    bundle_run_id = str(manifest.get("selection_run_id") or report.get("selection_run_id") or "")
    bundle_hash = str(manifest.get("selection_bundle_hash") or report.get("selection_bundle_hash") or "")

    # Only use this bundle for enrichment if it matches the requested date
    # or no specific date was requested (first run with existing outcomes)
    if selection_date and bundle_date and bundle_date != selection_date:
        pass  # Don't use a different date's bundle

    base = {
        "selection_run_id": bundle_run_id,
        "selection_date": bundle_date,
        "selection_bundle_hash": bundle_hash,
    }

    top_items = []
    for key in ("top3", "top5", "top10", "tradable_top_candidates"):
        items = report.get(key)
        if isinstance(items, list):
            top_items.extend(items)

    seen: set[str] = set()
    for idx, item in enumerate(top_items):
        if not isinstance(item, dict):
            continue
        symbol = _normalize_symbol(item.get("ticker") or item.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        ctx[symbol] = {
            **base,
            "formal_rank": _safe_int(item.get("rank") or item.get("formal_rank"), 0),
            "formal_candidate_score": _safe_float(
                item.get("final_score") or item.get("score") or item.get("candidate_score")
            ),
            "strategy_fit_score": _safe_float(
                item.get("strategy_fit_score") or item.get("range_score")
            ),
            "market_regime": str(item.get("market_regime") or item.get("regime") or "NORMAL").upper(),
        }

    return ctx


def _enrich_outcomes(
    trades: list[dict[str, Any]],
    selection_ctx: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add selection metadata to each closed trade.

    If the trade already carries selection_run_id from the BUY lot,
    that entry-time context is preserved and NOT overwritten by the
    current/latest bundle.
    """
    enriched: list[dict[str, Any]] = []
    for trade in trades:
        symbol = str(trade.get("symbol") or "").upper()
        ct = selection_ctx.get(symbol, {})

        row = dict(trade)
        # If the trade does NOT have entry-time context, mark it
        has_context = bool(
            str(trade.get("selection_run_id") or "").strip()
            or str(trade.get("selection_bundle_hash") or "").strip()
        )

        if not has_context:
            # Try to enrich from bundle, but mark as potentially mismatched
            if ct:
                row["selection_run_id"] = ct.get("selection_run_id", "")
                row["selection_date"] = ct.get("selection_date", "")
                row["selection_bundle_hash"] = ct.get("selection_bundle_hash", "")
                row["formal_rank"] = ct.get("formal_rank", 0)
                row["formal_candidate_score"] = ct.get("formal_candidate_score", 0.0)
                row["strategy_fit_score"] = ct.get("strategy_fit_score", 0.0)
                row["market_regime"] = ct.get("market_regime", "NORMAL")
                row["selection_context_status"] = "INFERRED_FROM_LATEST_BUNDLE"
            else:
                row["selection_context_status"] = "MISSING_SELECTION_CONTEXT"
        else:
            # Already has entry-time context from the BUY lot
            # Fill in any missing supporting fields
            row.setdefault("formal_rank", ct.get("formal_rank", 0))
            row.setdefault("formal_candidate_score", ct.get("formal_candidate_score", 0.0))
            row.setdefault("strategy_fit_score", ct.get("strategy_fit_score", 0.0))
            row.setdefault("market_regime", ct.get("market_regime", "NORMAL"))
            row["selection_context_status"] = "BOUND_AT_ENTRY"

        # Training eligibility
        ineligible_reasons = []
        if row["selection_context_status"] == "MISSING_SELECTION_CONTEXT":
            ineligible_reasons.append("missing_selection_context")
        if row["selection_context_status"] == "INFERRED_FROM_LATEST_BUNDLE":
            ineligible_reasons.append("inferred_selection_context")
        if _safe_float(row.get("entry_price")) <= 0:
            ineligible_reasons.append("invalid_entry_price")
        if _safe_float(row.get("exit_price")) <= 0:
            ineligible_reasons.append("invalid_exit_price")
        if str(row.get("execution_mode") or "").strip().lower() not in ALLOWED_EXECUTION_MODES:
            ineligible_reasons.append("invalid_execution_mode")

        row["training_eligible"] = not bool(ineligible_reasons)
        row["training_ineligible_reasons"] = "; ".join(ineligible_reasons) if ineligible_reasons else ""
        enriched.append(row)

    return enriched


# ── State management ───────────────────────────────────────────────────

def _load_known_ids() -> set[str]:
    if not OUTCOME_STATE_PATH.exists():
        return _ids_from_csv()
    try:
        data = json.loads(OUTCOME_STATE_PATH.read_text(encoding="utf-8"))
        schema = str(data.get("schema_version") or "")
        if schema and schema != SCHEMA_VERSION:
            # Schema changed: rebuild from CSV
            return _ids_from_csv()
        ids = data.get("known_outcome_ids", [])
        if ids:
            return set(str(item) for item in ids)
    except (json.JSONDecodeError, OSError, ValueError):
        # Corrupt state: rebuild from CSV, fail closed
        return _ids_from_csv()
    return _ids_from_csv()


def _ids_from_csv() -> set[str]:
    ids: set[str] = set()
    if not OUTCOME_CSV_PATH.exists():
        return ids
    try:
        with OUTCOME_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                oid = str(row.get("outcome_id") or "").strip()
                if oid:
                    ids.add(oid)
    except Exception:
        pass
    return ids


def _load_known_fill_ids() -> set[str]:
    """Return processed_fill_ids from the current PaperPortfolioState."""
    try:
        state = read_paper_portfolio_state()
        if isinstance(state, dict):
            fill_ids = state.get("processed_fill_ids") or []
            return set(str(fid) for fid in fill_ids if str(fid).strip())
    except Exception:
        pass
    return set()


def _save_state(known_ids: set[str]) -> None:
    OUTCOME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "known_outcome_ids": sorted(known_ids),
        "updated_at": _utc_now_iso(),
    }
    fd, tmp = tempfile.mkstemp(
        prefix=".outcome_state.", suffix=".tmp", dir=str(OUTCOME_STATE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, OUTCOME_STATE_PATH)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── CSV persistence ────────────────────────────────────────────────────

def _acquire_lock() -> bool:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(OUTCOME_LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Check if stale (> 1 hour)
        try:
            if (time.time() - OUTCOME_LOCK_PATH.stat().st_mtime) > 3600:
                OUTCOME_LOCK_PATH.unlink(missing_ok=True)
                fd = os.open(str(OUTCOME_LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            else:
                return False
        except (OSError, ValueError):
            return False
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "acquired_at": time.time()}).encode())
    finally:
        os.close(fd)
    return True


def _release_lock() -> None:
    try:
        OUTCOME_LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _append_csv(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not OUTCOME_CSV_PATH.exists()

    fd, tmp = tempfile.mkstemp(
        prefix=".outcome_csv.", suffix=".tmp", dir=str(LEARNING_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_OUTCOME_CSV_COLUMNS_V2, extrasaction="ignore")
            writer.writeheader()
            if not write_header:
                # Copy existing rows
                try:
                    if OUTCOME_CSV_PATH.exists():
                        with OUTCOME_CSV_PATH.open("r", encoding="utf-8", newline="") as src:
                            reader = csv.DictReader(src)
                            count = 0
                            for row in reader:
                                writer.writerow(row)
                                count += 1
                                if count > MAX_CSV_ROWS:
                                    break
                except Exception:
                    pass
            for row in rows:
                writer.writerow(row)
        os.replace(tmp, OUTCOME_CSV_PATH)
        return len(rows)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        return 0


# ── Daily summary ──────────────────────────────────────────────────────

def compute_summary() -> dict[str, Any]:
    summary: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "schema_version": SCHEMA_VERSION,
        "closed_trade_count": 0,
        "training_eligible_count": 0,
        "win_rate": 0.0,
        "avg_pnl_pct": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "total_realized_pnl": 0.0,
        "avg_hold_duration_hours": 0.0,
        "wins": 0,
        "losses": 0,
        "top_symbols": [],
    }

    if not OUTCOME_CSV_PATH.exists():
        return summary

    try:
        with OUTCOME_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception:
        return summary

    if not rows:
        return summary

    wins = 0
    losses = 0
    pnl_pcts: list[float] = []
    hold_hours: list[float] = []
    symbol_stats: dict[str, dict[str, float]] = {}
    training_eligible = 0

    for row in rows:
        pnl_pct = _safe_float(row.get("pnl_pct"))
        realized = _safe_float(row.get("realized_pnl"))
        hold_s = _safe_float(row.get("hold_duration_seconds"))
        symbol = _normalize_symbol(row.get("symbol") or "")
        if str(row.get("training_eligible") or "").strip().lower() == "true":
            training_eligible += 1

        pnl_pcts.append(pnl_pct)
        if hold_s > 0:
            hold_hours.append(hold_s / 3600.0)

        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1

        if symbol:
            ss = symbol_stats.setdefault(symbol, {"count": 0, "total_pnl": 0.0})
            ss["count"] += 1
            ss["total_pnl"] += realized

    total = wins + losses
    summary["closed_trade_count"] = total
    summary["training_eligible_count"] = training_eligible
    summary["win_rate"] = round((wins / total * 100.0) if total > 0 else 0.0, 2)
    summary["avg_pnl_pct"] = round(sum(pnl_pcts) / len(pnl_pcts), 4) if pnl_pcts else 0.0
    summary["best_trade"] = round(max(pnl_pcts), 4) if pnl_pcts else 0.0
    summary["worst_trade"] = round(min(pnl_pcts), 4) if pnl_pcts else 0.0
    summary["total_realized_pnl"] = round(
        sum(_safe_float(row.get("realized_pnl")) for row in rows), 4
    )
    summary["avg_hold_duration_hours"] = (
        round(sum(hold_hours) / len(hold_hours), 2) if hold_hours else 0.0
    )
    summary["wins"] = wins
    summary["losses"] = losses
    summary["top_symbols"] = sorted(
        [
            {"symbol": s, "count": int(v["count"]), "total_pnl": round(v["total_pnl"], 4)}
            for s, v in symbol_stats.items()
        ],
        key=lambda x: -x["total_pnl"],
    )[:10]

    return summary


def write_summary(summary: dict[str, Any] | None = None) -> dict[str, Any]:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    data = summary or compute_summary()
    fd, tmp = tempfile.mkstemp(
        prefix=".outcome_summary.", suffix=".tmp", dir=str(LEARNING_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, OUTCOME_SUMMARY_PATH)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
    return data


# ── Main entry point ───────────────────────────────────────────────────

def collect_outcomes(
    *,
    log_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    audit_dir = Path(log_dir) if log_dir else AUDIT_LOG_DIR
    known_ids = _load_known_ids()
    known_fill_ids = _load_known_fill_ids()

    if not _acquire_lock() and not dry_run:
        return {
            "status": "LOCKED",
            "existing_count": len(known_ids),
            "new_count": 0,
            "total_count": len(known_ids),
            "skipped_count": 0,
            "summary": {},
        }

    try:
        fills = _parse_audit_fills(audit_dir, known_fill_ids=set(known_fill_ids))
        if not fills:
            return {
                "status": "NO_NEW_DATA",
                "existing_count": len(known_ids),
                "new_count": 0,
                "total_count": len(known_ids),
                "skipped_count": 0,
                "summary": write_summary() if not dry_run else compute_summary(),
            }

        closed = _match_closed_trades_lots(fills)
        selection_ctx = _load_selection_context_for_date()
        enriched = _enrich_outcomes(closed, selection_ctx)

        new_rows = [
            row for row in enriched
            if str(row.get("outcome_id") or "").strip() not in known_ids
        ]
        skipped = len(enriched) - len(new_rows)

        if dry_run:
            return {
                "status": "DRY_RUN",
                "existing_count": len(known_ids),
                "new_count": len(new_rows),
                "total_count": len(known_ids) + len(new_rows),
                "skipped_count": skipped,
                "new_trades": [
                    {k: v for k, v in row.items()
                     if k in {"symbol", "entry_time", "exit_time", "realized_pnl", "pnl_pct",
                              "quantity", "selection_context_status", "training_eligible"}}
                    for row in new_rows
                ],
                "summary": compute_summary(),
            }

        # Write new rows atomically
        written = _append_csv(new_rows) if new_rows else 0
        if written > 0:
            new_ids = known_ids | {str(row["outcome_id"]) for row in new_rows}
            _save_state(new_ids)
        else:
            new_ids = known_ids

        summary = write_summary()
        return {
            "status": "OK",
            "existing_count": len(known_ids),
            "new_count": written,
            "total_count": len(new_ids),
            "skipped_count": skipped,
            "summary": summary,
        }
    finally:
        _release_lock()
