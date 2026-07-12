#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.candidate_validation.dataset_validation_runner import validate_candidate_dataset
from src.candidate_validation import CandidateTransitionError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manually validate a single AI candidate dataset.")
    parser.add_argument("--candidate-id", required=True, help="Exact candidate id to validate")
    parser.add_argument(
        "--candidate-store",
        required=True,
        help="Path to the candidate store root or candidates.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for validation artifacts; a candidate-id subdirectory will be created inside it",
    )
    parser.add_argument(
        "--download-missing",
        action="store_true",
        help="Allow Quote API downloads for missing local data when explicitly confirmed with --apply",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only; never mutate candidate state or download missing data.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Allow updating the candidate state and downloading missing Quote API data.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    dry_run = True
    if args.apply:
        dry_run = False
    if args.dry_run:
        dry_run = True

    try:
        result = validate_candidate_dataset(
            candidate_id=args.candidate_id,
            candidate_store=Path(args.candidate_store),
            output_dir=Path(args.output_dir),
            download_missing=bool(args.download_missing),
            dry_run=dry_run,
        )
    except CandidateTransitionError as exc:
        payload = {"ok": False, "error": str(exc), "dry_run": dry_run}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1
    except Exception as exc:  # pragma: no cover - CLI safety
        payload = {"ok": False, "error": f"{type(exc).__name__}:{exc}", "dry_run": dry_run}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
