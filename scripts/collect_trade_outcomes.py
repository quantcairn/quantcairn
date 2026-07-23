#!/usr/bin/env python3
"""Collect paper trade outcomes into a learning dataset.

Reads runtime audit logs, matches BUY/SELL pairs into closed trades,
enriches with SelectionBundle context, and writes (or updates)
artifacts/learning/outcome_dataset.csv.

Usage:
    python scripts/collect_trade_outcomes.py           # dry-run
    python scripts/collect_trade_outcomes.py --apply   # write
    python scripts/collect_trade_outcomes.py --summary # summary only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.outcome.collector import collect_outcomes, compute_summary, write_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect paper trade outcomes.")
    parser.add_argument("--apply", action="store_true", help="Write new outcomes to CSV (default: dry-run)")
    parser.add_argument("--summary", action="store_true", help="Only compute and print the daily summary")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON to stdout")
    args = parser.parse_args()

    if args.summary:
        summary = write_summary()
        if args.json_output:
            print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
            return 0
        print("=== Trade Outcome Summary ===")
        print(f"  Closed trades:  {summary['closed_trade_count']}")
        print(f"  Win rate:       {summary['win_rate']}%")
        print(f"  Avg P&L %:      {summary['avg_pnl_pct']}%")
        print(f"  Best trade:     {summary['best_trade']}%")
        print(f"  Worst trade:    {summary['worst_trade']}%")
        print(f"  Total P&L:      {summary['total_realized_pnl']}")
        print(f"  Avg hold (hrs): {summary['avg_hold_duration_hours']}")
        print(f"  Wins/Losses:    {summary.get('wins', 0)}/{summary.get('losses', 0)}")
        if summary.get("top_symbols"):
            print("  Top symbols:")
            for item in summary["top_symbols"][:5]:
                print(f"    {item['symbol']}: {item['count']} trades, P&L {item['total_pnl']}")
        return 0

    dry_run = not args.apply
    result = collect_outcomes(dry_run=dry_run)

    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    print("=== Outcome Collection ===")
    print(f"  Mode:       {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"  Status:     {result['status']}")
    print(f"  Existing:   {result['existing_count']}")
    print(f"  New:        {result['new_count']}")
    print(f"  Skipped:    {result['skipped_count']}")
    print(f"  Total:      {result['total_count']}")
    print()

    new_trades = result.get("new_trades") or []
    if new_trades:
        print(f"  New closed trades ({len(new_trades)}):")
        for t in new_trades[:20]:
            print(f"    {t['symbol']:5s}  P&L%: {t.get('pnl_pct', 0):.2f}%  P&L: {t.get('realized_pnl', 0):.4f}")
        if len(new_trades) > 20:
            print(f"    ... and {len(new_trades) - 20} more")

    summary = result.get("summary") or {}
    if summary:
        print()
        print("  Summary:")
        print(f"    closed_trades={summary.get('closed_trade_count', 0)}")
        print(f"    win_rate={summary.get('win_rate', 0)}%")
        print(f"    avg_pnl_pct={summary.get('avg_pnl_pct', 0)}%")
        print(f"    best_trade={summary.get('best_trade', 0)}")
        print(f"    worst_trade={summary.get('worst_trade', 0)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
