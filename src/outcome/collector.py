"""Append-only, idempotent paper trade outcome dataset builder.

Reads PaperPortfolioState, runtime trade audit logs, and SelectionBundle
to derive closed-trade outcomes with selection context.  Never calls a
broker, never triggers orders, never mutates PaperPortfolioState or any
trading configuration.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.broker.paper_portfolio_state import read_paper_portfolio_state
from src.ai_selector.selection_bundle import load_committed_selection_bundle
from src.ai_selector.selection_state import load_selection_state

PROJECT_DIR = Path(__file__).resolve().parents[2]
LEARNING_DIR = PROJECT_DIR / "artifacts" / "learning"
OUTCOME_CSV_PATH = LEARNING_DIR / "outcome_dataset.csv"
OUTCOME_STATE_PATH = LEARNING_DIR / ".outcome_collector_state.json"
OUTCOME_SUMMARY_PATH = LEARNING_DIR / "outcome_summary.json"
AUDIT_LOG_DIR = PROJECT_DIR / "logs"

OUTCOME_CSV_COLUMNS = [
    "outcome_id",
    "symbol",
    "selection_run_id",
    "selection_date",
    "formal_rank",
    "formal_candidate_score",
    "strategy_fit_score",
    "market_regime",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "pnl_pct",
    "realized_pnl",
    "hold_duration_seconds",
    "exit_reason",
    "execution_mode",
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


def _make_outcome_id(
    symbol: str,
    entry_time: str,
    exit_time: str,
    entry_price: float,
    exit_price: float,
) -> str:
    seed = f"{symbol}|{entry_time}|{exit_time}|{entry_price}|{exit_price}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


# ── Audit log parsing ────────────────────────────────────────────────


@dataclass(slots=True)
class FillEvent:
    symbol: str
    side: str  # BUY | SELL
    fill_id: str
    quantity: int
    price: float
    timestamp: str
    pnl: float | None = None
    reason: str = ""
    mode: str = "paper"

    @classmethod
    def from_audit_line(cls, line: dict[str, Any]) -> "FillEvent | None":
        phase = str(line.get("phase") or "").strip()
        if phase not in {"risk_decision", "risk_exit_trigger", "orphan_risk_exit"}:
            # Look for fill confirmation events in the order field
            order = line.get("order")
            if isinstance(order, dict) and str(order.get("side") or "").upper() in {"BUY", "SELL"}:
                pass
            else:
                return None

        order = line.get("order")
        if not isinstance(order, dict):
            return None

        side = str(order.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            return None

        qty = _safe_int(order.get("qty", order.get("quantity")))
        if qty <= 0:
            # Check response for final action
            response = line.get("response") or {}
            status = str(response.get("status") or "").lower()
            if status not in {"filled", "partially_filled"}:
                return None

        symbol = _normalize_symbol(line.get("ticker") or line.get("symbol") or "")
        if not symbol:
            return None

        fill_id = str(line.get("order_id") or line.get("fill_id") or "")
        price = _safe_float(line.get("current_price") or line.get("execution_price") or order.get("price"))
        if price <= 0:
            return None

        return cls(
            symbol=symbol,
            side=side,
            fill_id=fill_id,
            quantity=qty,
            price=price,
            timestamp=str(line.get("timestamp") or line.get("executed_at") or ""),
            pnl=_safe_float(line.get("pnl")) if line.get("pnl") is not None else None,
            reason=str(line.get("reason") or line.get("final_action") or ""),
            mode=str(line.get("execution_mode") or line.get("mode") or "paper"),
        )


def _parse_audit_fills(log_dir: Path, since_timestamp: str | None = None) -> list[FillEvent]:
    """Scan runtime audit JSONL files for BUY/SELL fill events."""
    fills: list[FillEvent] = []
    if not log_dir.exists():
        return fills

    for log_path in sorted(log_dir.glob("trades-*.jsonl")):
        try:
            for line_text in log_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line_text.strip():
                    continue
                try:
                    line = json.loads(line_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(line, dict):
                    continue

                ts = str(line.get("timestamp") or "")
                if since_timestamp and ts and ts <= since_timestamp:
                    continue

                fill = FillEvent.from_audit_line(line)
                if fill is not None:
                    fills.append(fill)
        except Exception:
            continue

    return fills


# ── Trade matching ────────────────────────────────────────────────────


def _match_closed_trades(fills: list[FillEvent]) -> list[dict[str, Any]]:
    """Match BUY/SELL pairs into closed trades, FIFO per symbol."""
    from collections import deque
    buys: dict[str, deque[FillEvent]] = {}
    closed: list[dict[str, Any]] = []

    for fill in sorted(fills, key=lambda f: f.timestamp):
        if fill.side == "BUY":
            buys.setdefault(fill.symbol, deque()).append(fill)
        elif fill.side == "SELL":
            symbol = fill.symbol
            symbol_buys = buys.get(symbol)
            if not symbol_buys:
                # Sell without matching buy — skip
                continue

            # Match against oldest buy (FIFO)
            matched_buy = symbol_buys.popleft()
            entry_ts_str = matched_buy.timestamp or fill.timestamp
            exit_ts_str = fill.timestamp or _utc_now_iso()

            try:
                entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
                exit_ts = datetime.fromisoformat(exit_ts_str.replace("Z", "+00:00"))
                hold_seconds = max(0, int((exit_ts - entry_ts).total_seconds()))
            except (ValueError, TypeError):
                hold_seconds = 0

            pnl = fill.pnl
            if pnl is None and matched_buy.price > 0:
                pnl = (fill.price - matched_buy.price) * min(matched_buy.quantity, fill.quantity)

            pnl_pct = 0.0
            if matched_buy.price > 0:
                pnl_pct = ((fill.price - matched_buy.price) / matched_buy.price) * 100.0

            outcome_id = _make_outcome_id(
                symbol, entry_ts_str, exit_ts_str, matched_buy.price, fill.price
            )

            closed.append({
                "outcome_id": outcome_id,
                "symbol": symbol,
                "entry_time": entry_ts_str,
                "exit_time": exit_ts_str,
                "entry_price": matched_buy.price,
                "exit_price": fill.price,
                "realized_pnl": round(pnl or 0.0, 4),
                "pnl_pct": round(pnl_pct, 4),
                "hold_duration_seconds": hold_seconds,
                "exit_reason": fill.reason or "",
                "buy_fill_id": matched_buy.fill_id,
                "sell_fill_id": fill.fill_id,
                "execution_mode": fill.mode or matched_buy.mode,
            })

    return closed


# ── Selection context enrichment ──────────────────────────────────────


def _load_selection_context() -> dict[str, dict[str, Any]]:
    """Load per-symbol selection metadata from the latest committed bundle."""
    ctx: dict[str, dict[str, Any]] = {}
    bundle = load_committed_selection_bundle(PROJECT_DIR)
    if not isinstance(bundle, dict):
        return ctx

    report = bundle.get("report") or {}
    manifest = bundle.get("manifest") or {}

    # Selection-level metadata
    base = {
        "selection_run_id": str(manifest.get("selection_run_id") or report.get("selection_run_id") or ""),
        "selection_date": str(manifest.get("selection_date") or report.get("selection_date") or ""),
    }

    # Per-candidate enrichment from top_items
    top_items = []
    for key in ("top3", "top5", "top10", "tradable_top_candidates", "research_top_candidates"):
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
            "formal_candidate_score": _safe_float(item.get("final_score") or item.get("score") or item.get("candidate_score")),
            "strategy_fit_score": _safe_float(item.get("strategy_fit_score") or item.get("range_score")),
            "market_regime": str(item.get("market_regime") or item.get("regime") or "NORMAL").upper(),
        }

    # Fallback: try to enrich from selection state
    state = load_selection_state()
    if isinstance(state, dict):
        state_run_id = str(state.get("selection_run_id") or "")
        state_date = str(state.get("et_date") or "")
        for symbol in list(ctx.keys()):
            if not ctx[symbol].get("selection_run_id"):
                ctx[symbol]["selection_run_id"] = state_run_id
            if not ctx[symbol].get("selection_date"):
                ctx[symbol]["selection_date"] = state_date

    return ctx


def _enrich_outcomes(
    trades: list[dict[str, Any]],
    selection_ctx: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add selection metadata to each closed trade."""
    enriched: list[dict[str, Any]] = []
    for trade in trades:
        symbol = str(trade.get("symbol") or "").upper()
        ctx = selection_ctx.get(symbol, {})

        row = dict(trade)
        row.setdefault("formal_rank", ctx.get("formal_rank", 0))
        row.setdefault("formal_candidate_score", ctx.get("formal_candidate_score", 0.0))
        row.setdefault("strategy_fit_score", ctx.get("strategy_fit_score", 0.0))
        row.setdefault("market_regime", ctx.get("market_regime", "NORMAL"))
        row.setdefault("selection_run_id", ctx.get("selection_run_id", ""))
        row.setdefault("selection_date", ctx.get("selection_date", ""))
        enriched.append(row)
    return enriched


# ── State management ──────────────────────────────────────────────────


def _load_known_ids() -> set[str]:
    """Return the set of outcome_ids already present in the CSV."""
    if not OUTCOME_STATE_PATH.exists():
        return _ids_from_csv()
    try:
        data = json.loads(OUTCOME_STATE_PATH.read_text(encoding="utf-8"))
        ids = data.get("known_outcome_ids", [])
        if ids:
            return set(str(item) for item in ids)
    except Exception:
        pass
    return _ids_from_csv()


def _ids_from_csv() -> set[str]:
    """Scan the CSV header and extract existing outcome_ids."""
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


def _last_timestamp() -> str | None:
    """Return the latest timestamp from the state file, for incremental parsing."""
    if not OUTCOME_STATE_PATH.exists():
        return None
    try:
        data = json.loads(OUTCOME_STATE_PATH.read_text(encoding="utf-8"))
        ts = str(data.get("last_audit_timestamp") or "").strip()
        return ts if ts else None
    except Exception:
        return None


def _save_state(known_ids: set[str], last_ts: str | None = None) -> None:
    """Persist the known-outcome-id set and last audit timestamp."""
    OUTCOME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "known_outcome_ids": sorted(known_ids),
        "last_audit_timestamp": last_ts or _utc_now_iso(),
        "updated_at": _utc_now_iso(),
    }
    fd, tmp = tempfile.mkstemp(prefix=".outcome_state.", suffix=".tmp", dir=str(OUTCOME_STATE_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, OUTCOME_STATE_PATH)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def _append_csv(rows: list[dict[str, Any]]) -> int:
    """Append new rows to the CSV. Returns count of rows written."""
    if not rows:
        return 0
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not OUTCOME_CSV_PATH.exists()
    fd, tmp = tempfile.mkstemp(prefix=".outcome_csv.", suffix=".tmp", dir=str(LEARNING_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTCOME_CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            else:
                # Copy existing content
                try:
                    if OUTCOME_CSV_PATH.exists():
                        f.write(OUTCOME_CSV_PATH.read_text(encoding="utf-8"))
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


# ── Daily summary ─────────────────────────────────────────────────────


def compute_summary() -> dict[str, Any]:
    """Read outcome_dataset.csv and produce a daily summary."""
    summary: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "closed_trade_count": 0,
        "win_rate": 0.0,
        "avg_pnl_pct": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "total_realized_pnl": 0.0,
        "avg_hold_duration_hours": 0.0,
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

    for row in rows:
        pnl_pct = _safe_float(row.get("pnl_pct"))
        realized = _safe_float(row.get("realized_pnl"))
        hold_s = _safe_float(row.get("hold_duration_seconds"))
        symbol = _normalize_symbol(row.get("symbol") or "")

        pnl_pcts.append(pnl_pct)
        if hold_s > 0:
            hold_hours.append(hold_s / 3600.0)

        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1
        # realized == 0 is neither (breakeven)

        if symbol:
            ss = symbol_stats.setdefault(symbol, {"count": 0, "total_pnl": 0.0})
            ss["count"] += 1
            ss["total_pnl"] += realized

    total = wins + losses
    summary["closed_trade_count"] = total
    summary["win_rate"] = round((wins / total * 100.0) if total > 0 else 0.0, 2)
    summary["avg_pnl_pct"] = round(sum(pnl_pcts) / len(pnl_pcts), 4) if pnl_pcts else 0.0
    summary["best_trade"] = round(max(pnl_pcts), 4) if pnl_pcts else 0.0
    summary["worst_trade"] = round(min(pnl_pcts), 4) if pnl_pcts else 0.0
    summary["total_realized_pnl"] = round(sum(_safe_float(row.get("realized_pnl")) for row in rows), 4)
    summary["avg_hold_duration_hours"] = round(sum(hold_hours) / len(hold_hours), 2) if hold_hours else 0.0
    summary["wins"] = wins
    summary["losses"] = losses
    summary["top_symbols"] = sorted(
        [{"symbol": s, "count": int(v["count"]), "total_pnl": round(v["total_pnl"], 4)}
         for s, v in symbol_stats.items()],
        key=lambda x: -x["total_pnl"],
    )[:10]

    return summary


def write_summary(summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write (or update) the daily outcome summary JSON."""
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    data = summary or compute_summary()
    fd, tmp = tempfile.mkstemp(prefix=".outcome_summary.", suffix=".tmp", dir=str(LEARNING_DIR))
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


# ── Main entry point ──────────────────────────────────────────────────


def collect_outcomes(
    *,
    log_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read audit logs, match trades, enrich with selection context, append to CSV.

    Returns:
        dict with keys: status, existing_count, new_count, total_count,
                        skipped_count, summary
    """
    audit_dir = Path(log_dir) if log_dir else AUDIT_LOG_DIR
    known_ids = _load_known_ids()
    last_ts = _last_timestamp()

    # Parse fills since last timestamp
    fills = _parse_audit_fills(audit_dir, since_timestamp=last_ts)
    if not fills:
        return {
            "status": "NO_NEW_DATA",
            "existing_count": len(known_ids),
            "new_count": 0,
            "total_count": len(known_ids),
            "skipped_count": 0,
            "summary": {} if dry_run else write_summary(),
        }

    # Match closed trades
    closed = _match_closed_trades(fills)

    # Enrich with selection context
    selection_ctx = _load_selection_context()
    enriched = _enrich_outcomes(closed, selection_ctx)

    # Deduplicate: skip outcomes we already have
    new_rows = [row for row in enriched if str(row.get("outcome_id") or "").strip() not in known_ids]
    skipped = len(enriched) - len(new_rows)

    if dry_run:
        return {
            "status": "DRY_RUN",
            "existing_count": len(known_ids),
            "new_count": len(new_rows),
            "total_count": len(known_ids) + len(new_rows),
            "skipped_count": skipped,
            "new_trades": [
                {k: v for k, v in row.items() if k in {"symbol", "entry_time", "exit_time", "realized_pnl", "pnl_pct"}}
                for row in new_rows
            ],
            "summary": compute_summary(),
        }

    # Write new rows
    written = _append_csv(new_rows) if new_rows else 0
    if written > 0:
        new_ids = known_ids | {str(row["outcome_id"]) for row in new_rows}
        _save_state(new_ids, _utc_now_iso())
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
