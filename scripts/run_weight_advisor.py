#!/usr/bin/env python3
"""Run the weight advisor on closed trade outcomes.

Reads artifacts/learning/outcome_dataset.csv.  If >= 20 training-eligible
outcomes exist, computes time-split correlation importance and proposes
blended selector-scoring weights.  Below threshold, reports
INSUFFICIENT_DATA.

Usage:
    python scripts/run_weight_advisor.py           # dry-run
    python scripts/run_weight_advisor.py --apply   # write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.outcome.weight_advisor import run_weight_advisor, MIN_SAMPLE_SIZE


def main() -> int:
    parser = argparse.ArgumentParser(description="Weight advisor for selector scoring.")
    parser.add_argument("--apply", action="store_true", help="Write suggested_weights.json (default: dry-run)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON to stdout")
    args = parser.parse_args()

    dry_run = not args.apply
    report = run_weight_advisor(dry_run=dry_run)

    if args.json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    print("=== Weight Advisor ===")
    print(f"  Mode:           {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"  Status:         {report['status']}")
    print(f"  Sample size:    {report['sample_size']}/{MIN_SAMPLE_SIZE}")
    print(f"  Approval:       {report['approval_status']}")
    print()

    if report["status"] == "INSUFFICIENT_DATA":
        print(f"  {report['explanation']}")
        print()
        print("  Baseline weights (unchanged):")
        for k, v in report["baseline_weights"].items():
            print(f"    {k:30s}  {v:.4f}")
    else:
        print("  Feature importances:")
        for k, v in (report.get("feature_importances") or {}).items():
            print(f"    {k:35s}  {v:.4f}")
        print()
        print("  Proposed weights (50/50 blended with baseline):")
        baseline = report["baseline_weights"]
        proposed = report["proposed_weights"]
        for k in sorted(baseline.keys()):
            b = baseline[k]
            p = proposed.get(k, b)
            arrow = "↑" if p > b else ("↓" if p < b else "→")
            print(f"    {k:30s}  {b:.4f} {arrow} {p:.4f}")
        print()
        ev = report.get("evaluation_metrics", {})
        bl = ev.get("baseline", {})
        pr = ev.get("proposed", {})
        print("  Test-set evaluation:")
        print(f"              Baseline   Proposed")
        print(f"    R²:       {bl.get('r2', 0):.4f}      {pr.get('r2', 0):.4f}")
        print(f"    Hit Rate: {bl.get('hit_rate', 0):.4f}    {pr.get('hit_rate', 0):.4f}")
        print(f"    MDA:      {bl.get('mean_direction_accuracy', 0):.4f}    {pr.get('mean_direction_accuracy', 0):.4f}")
        print(f"    Improved: {ev.get('improved', False)}")

    print()
    print(f"  {report['explanation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
