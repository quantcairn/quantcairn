#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.candidate_validation import CandidateTransitionError
from src.candidate_validation.backtest_admission_runner import admit_candidate_to_backtest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manually admit one DATA_VALID candidate to PENDING_BACKTEST.")
    parser.add_argument("--candidate-id", required=True, help="Exact candidate id to admit")
    parser.add_argument(
        "--candidate-store",
        required=True,
        help="Path to the candidate store root or candidates.jsonl",
    )
    parser.add_argument("--operator", required=True, help="Human operator approving the backtest admission")
    parser.add_argument("--reason", required=True, help="Reason for the admission")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only; never mutate candidate state.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the transition to PENDING_BACKTEST.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.apply and args.dry_run:
        payload = {"ok": False, "error": "dry_run_and_apply_are_mutually_exclusive", "applied": False}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1
    dry_run = not bool(args.apply)
    if args.dry_run:
        dry_run = True

    try:
        result = admit_candidate_to_backtest(
            candidate_id=args.candidate_id,
            candidate_store=Path(args.candidate_store),
            dry_run=dry_run,
            operator=args.operator,
            reason=args.reason,
        )
    except CandidateTransitionError as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "candidate_id": args.candidate_id,
            "operator": args.operator,
            "reason": args.reason,
            "dry_run": dry_run,
            "applied": False,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1
    except Exception as exc:  # pragma: no cover - CLI safety
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "candidate_id": args.candidate_id,
            "operator": args.operator,
            "reason": args.reason,
            "dry_run": dry_run,
            "applied": False,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
