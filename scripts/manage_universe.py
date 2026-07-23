#!/usr/bin/env python3
"""Manage the symbol universe — list, filter, snapshot, enable/disable.

Usage:
    python scripts/manage_universe.py                          # list enabled + disabled
    python scripts/manage_universe.py --snapshot               # run filters, write snapshot
    python scripts/manage_universe.py --enable SYMBOL          # enable a symbol
    python scripts/manage_universe.py --disable SYMBOL --reason "..."  # disable a symbol
    python scripts/manage_universe.py --reset                  # reset to default profiles
    python scripts/manage_universe.py --apply                  # persist changes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.universe.manager import UniverseManager
from src.universe.profiles import default_universe


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage symbol universe")
    parser.add_argument("--snapshot", action="store_true", help="Run filter pipeline and write snapshot")
    parser.add_argument("--enable", type=str, metavar="SYMBOL", help="Enable a symbol")
    parser.add_argument("--disable", type=str, metavar="SYMBOL", help="Disable a symbol")
    parser.add_argument("--reason", type=str, default="", help="Reason for disabling")
    parser.add_argument("--reset", action="store_true", help="Reset to default profiles")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry-run)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON")
    parser.add_argument("--full", action="store_true", help="Show all symbols including disabled")
    args = parser.parse_args()

    manager = UniverseManager()
    dry_run = not args.apply

    if args.reset:
        manager.save_symbols(default_universe())
        print("Reset to default universe: 48 symbols")
        return 0

    if args.enable:
        symbols = manager.load_symbols()
        target = args.enable.strip().upper()
        found = False
        for s in symbols:
            if s.symbol == target:
                s.enable()
                found = True
                print(f"Enabled: {s.symbol}")
                break
        if not found:
            print(f"Symbol not found: {target}")
            return 1
        if not dry_run:
            manager.save_symbols(symbols)
        return 0

    if args.disable:
        symbols = manager.load_symbols()
        target = args.disable.strip().upper()
        found = False
        for s in symbols:
            if s.symbol == target:
                s.disable(args.reason or "manually_disabled")
                found = True
                print(f"Disabled: {s.symbol} — {s.disabled_reason}")
                break
        if not found:
            print(f"Symbol not found: {target}")
            return 1
        if not dry_run:
            manager.save_symbols(symbols)
        return 0

    if args.snapshot:
        snap = manager.build_snapshot(dry_run=dry_run)
        if args.json_output:
            print(json.dumps(snap.to_dict(), indent=2, ensure_ascii=False, default=str))
            return 0
        print("=== Universe Snapshot ===")
        print(f"  Total:       {snap.total_symbols}")
        print(f"  Enabled:     {snap.enabled_symbols}")
        print(f"  Disabled:    {snap.total_symbols - snap.enabled_symbols}")
        print(f"  Max Lev/Inv: {snap.max_leveraged_inverse}")
        print()
        print("  Composition:")
        for k, v in sorted(snap.composition.items()):
            print(f"    {k:25s} {v}")
        print()
        fr = snap.filter_results
        print(f"  Filters applied:")
        print(f"    Liquidity dropped:     {fr.get('liquidity_dropped', 0)}")
        print(f"    Price dropped:         {fr.get('price_dropped', 0)}")
        print(f"    Risk dropped:          {fr.get('risk_dropped', 0)}")
        print(f"    Composition dropped:   {fr.get('composition_dropped', 0)}")
        if not dry_run:
            print()
            print(f"  Snapshot written: {manager.snapshot_path}")
        return 0

    # Default: list
    symbols = manager.load_symbols()
    enabled = [s for s in symbols if s.enabled]
    disabled = [s for s in symbols if not s.enabled]

    if args.json_output:
        print(json.dumps({
            "total": len(symbols), "enabled": len(enabled),
            "disabled": len(disabled),
            "enabled_symbols": [s.to_dict() for s in enabled],
            "disabled_symbols": [s.to_dict() for s in disabled],
        }, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"=== Managed Universe ({len(symbols)} total) ===")
    print(f"  🟢 Enabled:  {len(enabled)}")
    print(f"  🔴 Disabled: {len(disabled)}")
    print()

    print("  Enabled symbols:")
    for s in enabled:
        tags = f" [{', '.join(s.tags)}]" if s.tags else ""
        print(f"    {s.symbol:5s}  {s.asset_type:18s}  {s.sector:15s}  lev={s.leverage_type:3s}{tags}")
    if not enabled:
        print("    (none)")

    if args.full and disabled:
        print()
        print("  Disabled symbols:")
        for s in disabled:
            print(f"    {s.symbol:5s}  — {s.disabled_reason}")
    elif disabled:
        print(f"\n  ({len(disabled)} disabled — use --full to show)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
