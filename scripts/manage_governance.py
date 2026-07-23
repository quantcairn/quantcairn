#!/usr/bin/env python3
"""Manage learning governance proposals.

Usage:
    python scripts/manage_governance.py                     # list proposals
    python scripts/manage_governance.py --register          # register new proposal from Weight Advisor
    python scripts/manage_governance.py --accept PROPOSAL   # accept for review
    python scripts/manage_governance.py --approve PROPOSAL --reason "text"  # human approves
    python scripts/manage_governance.py --reject PROPOSAL --reason "text"   # reject
    python scripts/manage_governance.py --snapshot          # full governance snapshot

All state-changing actions require explicit flags.
ACTIVE activation requires --reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.outcome.governance import LearningGovernance


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage learning governance")
    parser.add_argument("--register", action="store_true", help="Register a new proposal from Weight Advisor")
    parser.add_argument("--accept", type=str, metavar="PROPOSAL_ID", help="Accept a proposal for human review")
    parser.add_argument("--approve", type=str, metavar="PROPOSAL_ID", help="Human-approve a proposal (requires --reason)")
    parser.add_argument("--reject", type=str, metavar="PROPOSAL_ID", help="Reject a proposal (requires --reason)")
    parser.add_argument("--reason", type=str, default="", help="Approval/rejection reason")
    parser.add_argument("--snapshot", action="store_true", help="Output full governance snapshot")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON")
    args = parser.parse_args()

    gov = LearningGovernance()
    dry_run = not args.apply

    if args.register:
        result = gov.register_weight_proposal(dry_run=dry_run)
        if result is None:
            print("No proposal registered: insufficient data or weight advisor incomplete")
            return 1
        if args.json_output:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
            return 0
        print(f"Proposal registered: {result.proposal_id}")
        print(f"  Type:        {result.proposal_type}")
        print(f"  Version:     {result.model_version}")
        print(f"  Samples:     {result.sample_count}")
        print(f"  Improved:    {result.improved}")
        print(f"  Status:      {result.approval_status}")
        print(f"  Next step:   run --accept {result.proposal_id} to accept for review")
        return 0

    if args.accept:
        result = gov.accept_proposal(args.accept, reason=args.reason, dry_run=dry_run)
        if args.json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            return 0
        print(f"Accept: {result.get('status')}")
        if result.get("status") == "OK":
            print(f"  Next step: human must --approve {args.accept} --reason 'why'")
        else:
            print(f"  Error: {result.get('error', '')}")
            return 1
        return 0

    if args.approve:
        if not args.reason.strip():
            print("ERROR: --approve requires --reason (human approval requires justification)")
            return 1
        result = gov.approve_proposal(args.approve, reason=args.reason, dry_run=dry_run)
        if args.json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            return 0
        print(f"Approve: {result.get('status')}")
        if result.get("status") == "OK":
            print(f"  New champion activated: {result.get('champion_activated', False)}")
        else:
            print(f"  Error: {result.get('error', '')}")
            return 1
        return 0

    if args.reject:
        if not args.reason.strip():
            print("ERROR: --reject requires --reason")
            return 1
        result = gov.reject_proposal(args.reject, reason=args.reason, dry_run=dry_run)
        if args.json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            return 0
        print(f"Reject: {result.get('status')}")
        if result.get("status") != "OK":
            print(f"  Error: {result.get('error', '')}")
            return 1
        return 0

    if args.snapshot:
        snapshot = gov.governance_snapshot()
        if args.json_output:
            print(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str))
            return 0
        print("=== Governance Snapshot ===")
        print(f"  Champion:  {snapshot['champion_version']} ({snapshot['champion_status']})")
        print(f"  Challenger: {snapshot['challenger_version'] or 'none'}")
        print(f"  Approval:  {snapshot['approval_status']}")
        print(f"  Human approved: {snapshot['approved_by_human']}")
        print(f"  Proposals: {snapshot['proposal_count']}")
        print(f"  Auto-activation blocked: {snapshot['auto_activation_blocked']}")
        return 0

    # Default: list proposals
    proposals = gov.list_proposals()
    active = gov.active_champion()
    challenger = gov.latest_challenger()

    if args.json_output:
        print(json.dumps({
            "active": active,
            "challenger": challenger,
            "proposals": proposals,
        }, indent=2, ensure_ascii=False, default=str))
        return 0

    print("=== Learning Governance ===")
    print(f"  🏆 Active Champion: {active['model_version'] if active else 'baseline_v1'}")
    if challenger:
        print(f"  ⚡ Challenger:      {challenger.get('model_version') or challenger.get('proposal_id')} ({challenger.get('approval_status')})")
    else:
        print("  ⚡ Challenger:      none")
    print(f"  📋 Proposals:       {len(proposals)}")
    print()

    if proposals:
        print(f"  {'ID':<16s} {'Version':<28s} {'Status':<28s} {'Samples':>8s} {'Human':>6s}")
        print(f"  {'-'*16} {'-'*28} {'-'*28} {'-'*8} {'-'*6}")
        for p in proposals[-10:]:  # last 10
            print(f"  {p['proposal_id']:<16s} {p['model_version']:<28s} {p['approval_status']:<28s} {p['sample_count']:>8d} {'✓' if p['approved_by_human'] else '✗':>6s}")
        print()
        print("   Actions: --accept ID | --approve ID --reason 'why' | --reject ID --reason 'why'")
    else:
        print("  No proposals yet. Run --register to create one from Weight Advisor.")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
