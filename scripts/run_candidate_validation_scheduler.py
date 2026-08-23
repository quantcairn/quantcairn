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
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.candidate_validation.orchestrator import CandidateValidationOrchestrator
from src.config.runtime_paths import resolve_artifacts_dir


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        try:
            return _jsonable(value.value)
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _append_jsonl(path: Path, row: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    line = json.dumps(_jsonable(row), ensure_ascii=False, default=str)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing)
            handle.write(line + "\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


def _audit_path() -> Path:
    return resolve_artifacts_dir(PROJECT_DIR) / "candidates" / "validation_scheduler_runs.jsonl"


def _write_run_audit(result: dict[str, Any], *, dry_run: bool) -> None:
    transition_events = [
        {
            "candidate_id": cr.get("candidate_id"),
            "symbol": cr.get("symbol"),
            "advanced": bool(cr.get("advanced")),
            "final_status": cr.get("final_status"),
            "phases_executed": cr.get("phases_executed", []),
            "skip_reason": cr.get("skip_reason", ""),
            "note": cr.get("note", ""),
            "data_validation_error": cr.get("data_validation_error", ""),
        }
        for cr in (result.get("candidate_results") or [])
        if isinstance(cr, dict)
    ]
    audit_row = {
        "run_id": result.get("run_id"),
        "validation_run_id": result.get("validation_run_id") or result.get("run_id"),
        "selection_run_id": result.get("selection_run_id"),
        "selection_date": result.get("selection_date"),
        "bundle_hash": result.get("bundle_hash"),
        "bundle_source": result.get("bundle_source"),
        "bundle_root": result.get("bundle_root"),
        "bundle_manifest_path": result.get("bundle_manifest_path"),
        "candidate_input_count": result.get("candidate_input_count", 0),
        "timestamp": _utc_now_iso(),
        "mode": "dry_run" if dry_run else "apply",
        "dry_run": dry_run,
        "candidates_scanned": len(result.get("candidate_results") or []),
        "candidates_advanced": result.get("candidates_advanced", 0),
        "transition_events": transition_events,
        "transitions": transition_events,
        "errors": result.get("errors", []),
        "status": result.get("status", "UNKNOWN"),
        "applied": bool(result.get("applied", False)),
    }
    _append_jsonl(_audit_path(), audit_row)


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
    try:
        _write_run_audit(result, dry_run=dry_run)
    except Exception as exc:
        result.setdefault("errors", []).append(
            f"validation_scheduler_audit_error:{type(exc).__name__}:{exc}"
        )

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
