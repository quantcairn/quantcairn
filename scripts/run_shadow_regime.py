#!/usr/bin/env python3
"""Run shadow market regime evaluation against the current SelectionBundle.

Reads the committed SelectionBundle, computes Bull / Bear / Range scores
for every candidate, and writes a research-only comparison report.

Usage:
    python scripts/run_shadow_regime.py           # dry-run
    python scripts/run_shadow_regime.py --apply   # write report
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.shadow.regime_evaluator import evaluate_shadow_regime


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow market regime evaluation")
    parser.add_argument("--apply", action="store_true", help="Write report (default: dry-run)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON")
    args = parser.parse_args()

    dry_run = not args.apply
    result = evaluate_shadow_regime(dry_run=dry_run)

    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    print()
    print("=" * 60)
    print("  Shadow Market Regime Evaluation")
    print("=" * 60)
    print(f"  Mode:           {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"  Status:         {result.get('status')}")
    print(f"  Selection date: {result.get('selection_date', 'N/A')}")
    print(f"  Candidates:     {result.get('candidate_count', 0)} total")
    print(f"  Evaluated:      {result.get('evaluated_count', 0)}")
    print()

    dist = result.get("regime_distribution", {})
    if dist:
        print(f"  Regime Distribution:  🟢 Bull={dist.get('BULL', 0)}  "
              f"🔴 Bear={dist.get('BEAR', 0)}  🟡 Range={dist.get('RANGE', 0)}")

    agg = result.get("aggregate_scores", {})
    if agg:
        print(f"  Avg Scores:           Bull={agg.get('bull_avg', 0):.1f}  "
              f"Bear={agg.get('bear_avg', 0):.1f}  Range={agg.get('range_avg', 0):.1f}")
        print(f"  Dominant Regime:      {result.get('dominant_regime', 'N/A')} "
              f"({result.get('dominant_score', 0):.1f})")

    comp = result.get("comparison", {})
    if comp:
        print()
        print("  ── Comparison with Current Selector ──")
        print(f"  Current TOP3:    {comp.get('current', [])}")
        print(f"  Shadow TOP3:     {comp.get('shadow_top3', [])}")
        added = comp.get("added_by_shadow", [])
        removed = comp.get("removed_by_shadow", [])
        if comp.get("changed"):
            print(f"  ➕ Added:        {added if added else '(none)'}")
            print(f"  ➖ Removed:      {removed if removed else '(none)'}")
        else:
            print("  ✅ No difference from current selector")

    per = result.get("per_candidate") or []
    if per:
        print()
        print("  ── Per-Candidate Regime ──")
        for r in per[:15]:
            w = r["winner"]
            icon = {"BULL": "🟢", "BEAR": "🔴", "RANGE": "🟡"}.get(w, "⚪")
            print(f"  {icon} {r['symbol']:5s}  "
                  f"Bull={r['bull_score']:.0f} Bear={r['bear_score']:.0f} "
                  f"Range={r['range_score']:.0f}  → {w} ({r['confidence']})")

    print()
    print("  ⚠️  Shadow evaluation only — no trading impact")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
