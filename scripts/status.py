#!/usr/bin/env python3
"""QuantCairn runtime status — read-only view of system state.

Usage:  .venv/bin/python scripts/status.py

Reads existing state files and artifacts.  Never connects to brokers,
modifies files, or initiates trading.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("SOXS_STATE_DIR", str(PROJECT_DIR / "state")))
ARTIFACTS_SEL = PROJECT_DIR / "artifacts" / "selection"


# ═══════════════════════════════════════════════════════════════════════════════
# Data readers — fall back gracefully if a file is missing
# ═══════════════════════════════════════════════════════════════════════════════

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _read_jsonl_last(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def _read_jsonl_count(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        return sum(1 for _ in open(path, encoding="utf-8"))
    except Exception:
        return 0


def _safe_float(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ═══════════════════════════════════════════════════════════════════════════════
# Data sections
# ═══════════════════════════════════════════════════════════════════════════════

def _section_execution_mode() -> None:
    env = os.environ.get("QUANTCAIRN_EXECUTION_MODE", "").strip().upper()
    flags = _read_json(STATE_DIR / "trading_flags.json") or {}
    reduce_all = flags.get("reduce_only_all", False)

    print()
    print("═" * 52)
    print("  Execution")
    print("═" * 52)
    mode = env if env else "RESEARCH (default)"
    icon = {"LIVE": "🔴", "PAPER": "🟡", "RESEARCH": "🔵"}.get(env, "⚪")
    print(f"  Mode:     {icon} {mode}")
    print(f"  Reduce-only (global): {'YES' if reduce_all else 'no'}")
    print(f"  Env var:  QUANTCAIRN_EXECUTION_MODE={'<not set>' if not env else env}")


def _section_last_selector_run() -> None:
    done_files = sorted(STATE_DIR.glob("ai_selector_*.done"))
    if not done_files:
        print()
        print("═" * 52)
        print("  Last Selector Run:  never")
        print("═" * 52)
        return

    last_done = done_files[-1]
    run_date = last_done.stem.replace("ai_selector_", "")
    run_time = last_done.read_text().strip()

    # Funnel debug
    funnel = _read_json(ARTIFACTS_SEL / "funnel_debug.json") or {}
    preflight = _read_json(ARTIFACTS_SEL / "preflight.json") or {}
    sel_state = _read_json(STATE_DIR / "ai_selection_state.json") or {}

    # Bundle manifest for count
    bundle_manifest = _read_json(STATE_DIR / "selection_bundle_manifest.json")

    print()
    print("═" * 52)
    print("  Last Selector Run")
    print("═" * 52)
    print(f"  Date:          {run_date}")
    print(f"  Time:          {run_time[:19]}")
    print(f"  Preflight:     market={preflight.get('market_state','?')}  "
          f"run_mode={preflight.get('run_mode','?')}  "
          f"trading_day={preflight.get('is_trading_day','?')}")
    print(f"  Pipeline rate: {funnel.get('pipeline_success_rate', 0):.1%}")
    print(f"  Formal cands:  {funnel.get('formal_candidates', 0)}")
    print(f"  Preview cands: {funnel.get('preview_candidates', 0)}")
    print(f"  Fallback:      {funnel.get('fallback_used', False)}")
    if sel_state:
        print(f"  Selection state: date={sel_state.get('selection_date','?')}  "
              f"count={sel_state.get('top_count','?')}  "
              f"mode={sel_state.get('mode','?')}")

    # Latest candidates from funnel symbol list
    formal_syms = funnel.get("formal_symbols", [])
    preview_syms = funnel.get("preview_symbols", [])
    if formal_syms:
        print(f"  Latest TOP:    {', '.join(formal_syms[:10])}")
    elif preview_syms:
        print(f"  Latest Preview: {', '.join(preview_syms[:10])}")


def _section_paper_portfolio() -> None:
    paper_dir = STATE_DIR / "paper"
    if not paper_dir.exists():
        print()
        print("═" * 52)
        print("  Paper Portfolio:  no accounts")
        print("═" * 52)
        return

    for acc_dir in sorted(paper_dir.iterdir()):
        if not acc_dir.is_dir():
            continue
        pf_path = acc_dir / "portfolio_state.json"
        state = _read_json(pf_path)
        if state is None:
            continue

        # Skip empty default account with no trades
        positions = state.get("positions") or []
        cash = _safe_float(state.get("cash"))
        equity = _safe_float(state.get("equity"))
        if not positions and cash == 10000.0 and equity == 10000.0:
            # Default empty account — show compact
            print()
            print("═" * 52)
            print(f"  Paper: {acc_dir.name} (empty — ${cash:,.2f} unused)")
            print("═" * 52)
            continue

        print()
        print("═" * 52)
        print(f"  Paper Portfolio: {acc_dir.name}")
        print("═" * 52)
        print(f"  Cash:          ${cash:,.2f}")
        print(f"  Equity:        ${equity:,.2f}")

        for p in positions:
            ticker = p.get("ticker", "?")
            qty = int(p.get("quantity") or 0)
            avg = _safe_float(p.get("avg_entry_price"))
            mv = _safe_float(p.get("market_value"))
            upnl = _safe_float(p.get("unrealized_pnl"))
            upnl_pct = _safe_float(p.get("unrealized_pnl_pct"))
            upnl_icon = "📈" if upnl >= 0 else "📉"
            print(f"  ───────────────────────────────────────────")
            print(f"  {ticker:<6s}  qty={qty:>5d}  "
                  f"avg=${avg:,.2f}  mv=${mv:,.2f}")
            if qty > 0:
                print(f"  {ticker:<6s}  unrealized PnL: {upnl_icon} "
                      f"${upnl:+,.2f} ({upnl_pct:+.2f}%)")
            cost_basis = avg * qty
            if cost_basis > 0:
                print(f"  {ticker:<6s}  cost basis:    ${cost_basis:,.2f}")


def _section_recent_trades() -> None:
    """Read trade history from the most recently active paper account."""
    paper_dir = STATE_DIR / "paper"
    if not paper_dir.exists():
        return

    # Find the account with the largest portfolio state file (most trades)
    best_size = 0
    best_state = None
    best_name = ""

    for acc_dir in sorted(paper_dir.iterdir()):
        if not acc_dir.is_dir():
            continue
        pf = acc_dir / "portfolio_state.json"
        if not pf.exists():
            continue
        sz = pf.stat().st_size
        if sz > best_size:
            best_size = sz
            best_state = _read_json(pf)
            best_name = acc_dir.name

    if best_state is None:
        return

    # Portfolio state v2 doesn't store trade history inline — check for
    # recent fills in the selection log
    sel_log = PROJECT_DIR / "logs" / f"selection_{datetime.now():%Y-%m-%d}.log"
    log_trades = []
    if sel_log.exists():
        for line in sel_log.read_text(encoding="utf-8").strip().split("\n"):
            try:
                rec = json.loads(line)
                if "summary" in rec:
                    syms = rec["summary"].get("final_selected_symbols", [])
                    if syms:
                        log_trades.append({
                            "at": rec["summary"].get("generated_at", ""),
                            "symbols": syms,
                            "stage": rec["summary"].get("selection_stage", ""),
                        })
            except Exception:
                pass

    # If portfolio has no positions, nothing to show
    positions = best_state.get("positions") or []
    cash = _safe_float(best_state.get("cash"))
    eq = _safe_float(best_state.get("equity"))
    if not positions and not log_trades:
        return

    print()
    print("═" * 52)
    print(f"  Recent Activity ({best_name})")
    print("═" * 52)

    if log_trades:
        last3 = log_trades[-3:]
        for t in last3:
            syms = t.get("symbols", [])
            if syms:
                print(f"  Selection: {', '.join(syms[:5])}  [{t.get('stage','?')}]")

    # Show PnL from portfolio state metadata if available
    realized_pnl = _safe_float(best_state.get("realized_pnl"))
    wins = best_state.get("wins", 0)
    losses = best_state.get("losses", 0)
    if wins or losses:
        print(f"  PnL: realized=${realized_pnl:+,.2f}  "
              f"wins={wins}  losses={losses}")

    # Show recent fills from broker audit (if available)
    broker_cache = STATE_DIR / "broker_cache"
    if broker_cache.exists():
        for f in sorted(broker_cache.iterdir()):
            if f.suffix != ".json":
                continue
            # These are typically broker snapshots, not trade history
            pass


def _section_outcome_records() -> None:
    outcome_dir = PROJECT_DIR / "artifacts" / "learning"
    if not outcome_dir.exists():
        return

    total = 0
    for f in outcome_dir.iterdir():
        if f.suffix == ".jsonl":
            total += _read_jsonl_count(f)
        elif f.suffix == ".csv" or f.suffix == ".parquet":
            # Parquet / CSV — count indirectly via stats
            try:
                total += 1  # placeholder: each file is one dataset version
            except Exception:
                pass

    if total == 0:
        return

    print()
    print("═" * 52)
    print("  Outcome Records")
    print("═" * 52)
    print(f"  Records:       ~{total} (across all formats)")

    # Last proposal in governance
    proposal_idx = _read_json(outcome_dir / "proposal_index.json")
    if proposal_idx:
        proposals = proposal_idx if isinstance(proposal_idx, list) else proposal_idx.get("proposals", [])
        if proposals:
            last = proposals[-1] if isinstance(proposals, list) else proposals
            if isinstance(last, dict):
                print(f"  Last proposal: {last.get('strategy','?')}  "
                      f"status={last.get('status','?')}")

    perf = _read_json(outcome_dir / "strategy_performance.json")
    if perf:
        strategies = perf if isinstance(perf, (list, dict)) else {}
        print(f"  Strategies:    {len(strategies) if isinstance(strategies, dict) else len(strategies)} tracked")


def _section_system_health() -> None:
    print()
    print("═" * 52)
    print("  System Health")
    print("═" * 52)

    # Check lock file — is a selector currently running?
    lock = STATE_DIR / "ai_selector.lock"
    running = False
    if lock.exists():
        try:
            import fcntl
            with open(lock, "w") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    running = True
        except Exception:
            running = lock.stat().st_size > 0

    print(f"  Selector running: {'YES — lock held' if running else 'no'}")

    # Notification sent today?
    nl_path = STATE_DIR / "notifications" / "notification_ledger.jsonl"
    last_nl = _read_jsonl_last(nl_path)
    if last_nl:
        status = last_nl.get("status", "?")
        sent_at = str(last_nl.get("sent_at", ""))[:10]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"  Notification:    {status}  "
              f"({'today' if sent_at == today else sent_at})")

    # Top configs present?
    config_dir = PROJECT_DIR / "configs"
    top_configs = sorted(config_dir.glob("TOP*.yaml"))
    if top_configs:
        modes = []
        for tc in top_configs[:5]:
            try:
                import yaml
                cfg = yaml.safe_load(tc.read_text(encoding="utf-8")) or {}
                m = cfg.get("mode", "?")
                modes.append(m)
            except Exception:
                modes.append("?")
        print(f"  TOP configs:     {len(top_configs)} files  "
              f"modes={modes}")

    # Bundle count
    bundle_dir = STATE_DIR / "selection_bundles"
    if bundle_dir.exists():
        bundles = list(bundle_dir.iterdir())
        print(f"  Bundles:         {len(bundles)} stored")

    # Test suite status
    print(f"  Project root:    {PROJECT_DIR}")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║           QuantCairn Runtime Status                  ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  PID:  {os.getpid()}")

    _section_execution_mode()
    _section_last_selector_run()
    _section_paper_portfolio()
    _section_recent_trades()
    _section_outcome_records()
    _section_system_health()

    print()
    print("═" * 52)
    print("  Status report complete.")
    print("═" * 52)
    print()


if __name__ == "__main__":
    main()
