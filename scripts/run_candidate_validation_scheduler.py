#!/usr/bin/env python3
"""Run the candidate validation orchestrator.

Automatically advances research candidates through the safe early
validation stages (AI_CANDIDATE → DATA_VALID).  Never auto-assigns
PAPER_ELIGIBLE or LIVE_ELIGIBLE.

Usage:
    python scripts/run_candidate_validation_scheduler.py          # dry-run
    python scripts/run_candidate_validation_scheduler.py --apply  # apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.candidate_validation.orchestrator import CandidateValidationOrchestrator


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the candidate validation orchestrator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply transitions (default: dry-run only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output JSON to stdout",
    )
    parser.add_argument(
        "--selection-run-id",
        default=None,
        help="Only process candidates from a specific selection run",
    )
    args = parser.parse_args()

    dry_run = not args.apply

    orchestrator = CandidateValidationOrchestrator()
    result = orchestrator.run(dry_run=dry_run)

    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    print()
    print("=" * 60)
    print("  Candidate Validation Scheduler")
    print("=" * 60)
    print(f"  Mode:     {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"  Run ID:   {result.get('run_id', '?')}")
    print(f"  Status:   {result.get('status', 'UNKNOWN')}")
    print(f"  Applied:  {result.get('applied', False)}")
    print()

    print(f"  Processed:   {result.get('candidates_processed', 0)}")
    print(f"  Advanced:    {result.get('candidates_advanced', 0)}")
    print(f"  Skipped:     {result.get('candidates_skipped', 0)}")
    print(f"  Failed:      {result.get('candidates_failed', 0)}")
    print()

    if result.get("phases_executed"):
        print(f"  Phases:      {', '.join(result['phases_executed'])}")
        print()

    if result.get("candidate_results"):
        print("  Candidates:")
        for cr in result["candidate_results"]:
            symbol = cr.get("symbol", "?")
            final = cr.get("final_status", "?")
            adv = "✓" if cr.get("advanced") else "—"
            skipped = cr.get("skipped", False)
            skip_reason = cr.get("skip_reason", "")
            note = cr.get("note", "")
            status_line = f"  {adv} {symbol:5s}  → {final:30s}"
            if skipped and skip_reason:
                status_line += f"  [{skip_reason}]"
            if note:
                status_line += f"  ({note})"
            print(status_line)
        print()

    if result.get("errors"):
        print("  Errors:")
        for err in result["errors"]:
            print(f"    - {err}")
        print()

    # Safety attestation
    print("-" * 60)
    print("  Safety attestation")
    print("  -----------------------------------")
    print(f"  trade_api_used        = {result.get('trade_api_used')}")
    print(f"  broker_used           = {result.get('broker_used')}")
    print(f"  paper_eligible_auto   = {result.get('paper_eligible_auto')}")
    print(f"  live_eligible_auto    = {result.get('live_eligible_auto')}")
    print("=" * 60)
    print()

    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
