"""Read existing QuantCairn evidence into an auditable daily snapshot.

This module is deliberately observational.  It does not import selector,
research runners, broker code, or launchd helpers, and only writes through the
explicit snapshot writer functions.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config.runtime_paths import RuntimePaths, runtime_paths
from src.utils.market_calendar import is_us_market_trading_day


SCHEMA_VERSION = "daily_runtime_snapshot.v1"
NOT_AVAILABLE = "NOT_AVAILABLE"
NOT_COLLECTED = "NOT_COLLECTED"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_LOG_LINES = 200
FRESHNESS_POLICY = {
    "selector": {"max_age_hours": 72, "cadence": "daily launchd selector windows"},
    "candidate_validation": {"max_age_hours": 72, "cadence": "daily launchd validation windows"},
    "research": {"max_age_hours": 96, "cadence": "daily launchd research window"},
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _evidence(path: Path, *, status: str, detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "timestamp": None, "status": status}
    try:
        result["timestamp"] = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        pass
    if detail:
        result["detail"] = detail
    return result


def _value(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # Selector currently writes datetime.now().isoformat(), which is a
        # local wall-clock timestamp without an offset.  The artifact contract
        # does not declare UTC, so preserve local-time semantics rather than
        # silently shifting it by treating it as UTC.
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except (TypeError, ValueError):
        return None


def _freshness(
    *,
    source_timestamp: Any,
    source_operational_date: Any,
    expected_date: date,
    as_of: datetime,
    policy_key: str,
) -> dict[str, Any]:
    timestamp = _parse_timestamp(source_timestamp)
    operational_date = _parse_date(source_operational_date)
    result: dict[str, Any] = {
        "status": "NOT_AVAILABLE" if timestamp is None and operational_date is None else "UNKNOWN",
        "age_seconds": None,
        "source_timestamp": source_timestamp,
        "source_operational_date": operational_date.isoformat() if operational_date else None,
        "reason": "missing_time_evidence" if timestamp is None and operational_date is None else "insufficient_time_evidence",
        "timestamp_timezone_source": None,
    }
    if source_timestamp:
        text = str(source_timestamp)
        result["timestamp_timezone_source"] = "host_local_for_naive" if "T" in text and not any(marker in text[10:] for marker in ("+", "-", "Z")) else "artifact_offset"
    if operational_date and operational_date < expected_date:
        result.update(status="STALE", reason="source_operational_date_older_than_snapshot_date")
    if timestamp is None:
        return result
    age = (as_of.astimezone(timezone.utc) - timestamp).total_seconds()
    result["age_seconds"] = max(0.0, age)
    if age < 0:
        result.update(status="UNKNOWN", reason="source_timestamp_in_future")
        return result
    max_age = FRESHNESS_POLICY[policy_key]["max_age_hours"] * 3600
    if age <= max_age and not (operational_date and operational_date < expected_date):
        result.update(status="FRESH", reason="within_daily_policy_window")
    elif result["status"] != "STALE":
        result.update(status="STALE", reason="exceeds_daily_policy_window")
    return result


def _expected_operational_date(snapshot_date: date) -> date:
    """Use the latest trading session for weekend/holiday snapshots."""
    expected = snapshot_date
    while not is_us_market_trading_day(expected):
        expected -= timedelta(days=1)
    return expected
def _stage_count(funnel: dict[str, Any], names: tuple[str, ...], field: str = "output_count") -> int | None:
    for stage in funnel.get("stages", []) if isinstance(funnel.get("stages"), list) else []:
        if not isinstance(stage, dict) or str(stage.get("stage", "")).upper() not in names:
            continue
        result = _count(stage.get(field))
        if result is not None:
            return result
        symbols = stage.get("output_symbols")
        if isinstance(symbols, list):
            return len(symbols)
    return None


def _symbol(row: Any) -> str | None:
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        value = row.get("ticker") or row.get("symbol")
        return str(value) if value else None
    return None


def _selector_snapshot(paths: RuntimePaths) -> tuple[dict[str, Any], dict[str, Any]]:
    path = paths.reports_dir / "ai_selection_latest.json"
    report = _read_json(path)
    provenance = _evidence(path, status="READ" if report else "MISSING")
    if not report:
        return {
            "status": NOT_AVAILABLE, "last_run_at": None, "report_timestamp": None,
            "universe_count": None, "filtered_count": None,
            "formal_eligible_count": None, "quality_checked_count": None,
            "quality_timeout_count": None, "final_candidate_count": None,
            "final_candidates": [], "evidence_status": NOT_AVAILABLE,
            "selection_run_id": None, "selection_date": None,
        }, provenance

    funnel = report.get("selection_funnel") if isinstance(report.get("selection_funnel"), dict) else {}
    final = _value(report, "final_selected_symbols", "selected_symbols")
    if not isinstance(final, list):
        final = [_symbol(item) for item in (_value(report, "top3", "top5", default=[]) or [])]
    final = [item for item in (_symbol(item) for item in final) if item]
    timeout = _value(report, "quality_timeout_count")
    if timeout is None:
        quality = report.get("quality_filter_report") if isinstance(report.get("quality_filter_report"), dict) else {}
        timeout = quality.get("timeout_count")
    status = str(_value(report, "execution_status", "status", default="UNKNOWN")).upper()
    if status == "COMPLETED":
        status = "COMPLETED"
    explicit_final_count = _count(_value(report, "final_candidate_count"))
    return {
        "status": status,
        "last_run_at": _value(report, "generated_at", "completed_at", "run_completed_at"),
        "report_timestamp": _value(report, "generated_at", "updated_at"),
        "universe_count": _count(_value(report, "universe_count")) or _stage_count(funnel, ("UNIVERSE",)),
        "filtered_count": _count(_value(report, "filtered_count", "universe_filtered_count")) or _stage_count(funnel, ("UNIVERSE_FILTER",)),
        "formal_eligible_count": _count(_value(report, "formal_eligible_count")) or _stage_count(funnel, ("FORMAL_ELIGIBILITY",)),
        "quality_checked_count": _count(_value(report, "quality_checked_count")) or _stage_count(funnel, ("DATA_QUALITY",), "input_count"),
        "quality_timeout_count": _count(timeout),
        "final_candidate_count": len(final) if final else (explicit_final_count if explicit_final_count is not None else 0),
        "final_candidates": final,
        "selection_run_id": _value(report, "selection_run_id", default=funnel.get("selection_run_id")) or None,
        "selection_date": _value(report, "selection_date", default=funnel.get("selection_date")) or None,
        "evidence_status": "AVAILABLE",
    }, provenance


def _jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        return [], _evidence(path, status="MISSING")
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return [], _evidence(path, status="UNREADABLE", detail="bounded_size_limit")
        lines = path.read_text(encoding="utf-8").splitlines()[-_MAX_LOG_LINES:]
    except OSError:
        return [], _evidence(path, status="UNREADABLE")
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows, _evidence(path, status="READ" if rows else "EMPTY")


def _validation_snapshot(paths: RuntimePaths, current_selection_run_id: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    path = paths.artifacts_dir / "candidates" / "validation_scheduler_runs.jsonl"
    rows, provenance = _jsonl_rows(path)
    row = next((candidate for candidate in reversed(rows) if current_selection_run_id and candidate.get("selection_run_id") == current_selection_run_id), None)
    relationship = "MATCHED_CURRENT_RUN" if row else "DIFFERENT_RUN" if rows and rows[-1].get("selection_run_id") else "UNKNOWN"
    if row is None and rows:
        row = rows[-1]
    if not row:
        return {"status": NOT_AVAILABLE, "total": None, "passed": None, "rejected": None, "validation_run_id": None, "selection_run_id": None, "selection_date": None, "timestamp": None, "relationship": "NOT_AVAILABLE"}, provenance
    total = _count(_value(row, "candidates_scanned", "candidate_input_count"))
    passed = _count(_value(row, "candidates_advanced", "passed"))
    failed = _count(_value(row, "candidates_failed", "failed"))
    skipped = _count(row.get("candidates_skipped"))
    return {
        "status": str(_value(row, "status", default="UNKNOWN")).upper(),
        "total": total, "passed": passed,
        "rejected": (failed or 0) + (skipped or 0) if failed is not None or skipped is not None else None,
        "validation_run_id": _value(row, "validation_run_id", "run_id") or None,
        "selection_run_id": row.get("selection_run_id") or None,
        "selection_date": row.get("selection_date") or None,
        "timestamp": _value(row, "timestamp", "completed_at", "generated_at") or None,
        "relationship": relationship,
    }, provenance


def _research_snapshot(paths: RuntimePaths, current_selection_run_id: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    root = paths.artifacts_dir / "research" / "daily"
    try:
        dirs = sorted((item for item in root.iterdir() if item.is_dir()), reverse=True)
    except OSError:
        dirs = []
    available: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for directory in dirs[:100]:
        audit_path = directory / "research_run_audit.json"
        audit = _read_json(audit_path)
        if not audit:
            continue
        available.append((directory, audit, {"audit_path": audit_path}))
    selected = next((item for item in available if current_selection_run_id and item[1].get("selection_run_id") == current_selection_run_id), None)
    relationship = "MATCHED_CURRENT_RUN" if selected else "DIFFERENT_RUN" if available and available[0][1].get("selection_run_id") else "UNKNOWN"
    chosen = selected or (available[0] if available else None)
    if chosen:
        directory, audit, _meta = chosen
        audit_path = directory / "research_run_audit.json"
        report_path = directory / "daily_candidate_report.json"
        report = _read_json(report_path) or {}
        return {
            "status": str(_value(audit, "status", "report_status", default="UNKNOWN")).upper(),
            "total": _count(_value(report, "candidate_count", default=audit.get("candidate_count"))),
            "completed": _count(_value(audit, "completed", default=1 if str(audit.get("status", "")).lower() in {"completed", "already_completed"} else 0)),
            "failed": _count(_value(audit, "failed", default=0)),
            "pending": _count(_value(audit, "pending", default=0)),
            "last_update_at": _value(audit, "completed_at", "generated_at", default=report.get("generated_at")),
            "research_run_id": _value(audit, "research_run_id", "run_id") or None,
            "selection_run_id": audit.get("selection_run_id") or None,
            "selection_date": _value(audit, "selection_date", "research_date", default=directory.name) or None,
            "relationship": relationship,
        }, _evidence(audit_path, status="READ")
    return {"status": NOT_AVAILABLE, "total": None, "completed": None, "failed": None, "pending": None, "last_update_at": None, "research_run_id": None, "selection_run_id": None, "selection_date": None, "relationship": "NOT_AVAILABLE"}, _evidence(root, status="MISSING")


def _simple_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        result: dict[str, Any] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if ":" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split(":", 1)
                value = value.strip().strip("'\"")
                result[key.strip()] = value
        except OSError:
            pass
        return result


def _snapshot_top_config_dir(project_dir: Path) -> Path:
    """Resolve TOP evidence without depending on uncommitted path changes."""
    explicit = str(os.environ.get("SOXS_TOP_CONFIG_DIR", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    config_root = str(os.environ.get("SOXS_CONFIG_DIR", "") or "").strip()
    if config_root:
        return (Path(config_root).expanduser().resolve() / "top_configs").resolve()
    return (project_dir / "configs").resolve()


def _top_snapshot(paths: RuntimePaths) -> tuple[dict[str, Any], dict[str, Any]]:
    state_root = paths.state_dir / "top_supervisor"
    status_path = state_root / "status"
    status: dict[str, str] = {}
    try:
        status = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in status_path.read_text(encoding="utf-8").splitlines() if "=" in line}
    except OSError:
        pass
    config_root = _snapshot_top_config_dir(paths.project_dir)
    slots: list[dict[str, Any]] = []
    for number, port in ((1, 8080), (2, 8081), (3, 8082)):
        path = config_root / f"TOP{number}.yaml"
        config = _simple_yaml(path) if path.exists() else {}
        enabled = str(config.get("enabled", "false")).lower() in {"true", "1", "yes"}
        selection = config.get("selection") if isinstance(config.get("selection"), dict) else {}
        slot_run_id = config.get("selection_run_id") or config.get("run_id") or selection.get("selection_run_id") or selection.get("run_id")
        slots.append({"slot": f"TOP{number}", "ticker": config.get("ticker"), "port": port, "http_status": None, "mode": config.get("mode"), "running": enabled and bool(status), "selection_run_id": slot_run_id})
    run_ids = []
    for slot in slots:
        if slot.get("selection_run_id"):
            run_ids.append(slot["selection_run_id"])
    return {
        "supervisor_status": status.get("state", NOT_AVAILABLE),
        "enabled_slots": [slot["slot"] for slot in slots if slot["running"]],
        "slots": slots,
        "top_selection_run_id": run_ids[0] if run_ids and len(set(run_ids)) == 1 else None,
    }, _evidence(status_path, status="READ" if status else "MISSING")


def _run_consistency(selector: dict[str, Any], validation: dict[str, Any], research: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    values = {
        "selection_run_id": selector.get("selection_run_id"),
        "validation_run_id": validation.get("validation_run_id"),
        "research_run_id": research.get("research_run_id"),
        "top_selection_run_id": top.get("top_selection_run_id"),
    }
    present = {key: value for key, value in values.items() if value}
    if len(present) < 2:
        status = "UNKNOWN" if present else "NOT_AVAILABLE"
        mismatches: list[str] = []
    else:
        # Validation/research run IDs identify their own execution. Compare
        # their selection_run_id separately so different execution IDs do not
        # create a false mismatch.
        selection_ids = {
            key: value for key, value in {
                "selection_run_id": selector.get("selection_run_id"),
                "validation_selection_run_id": validation.get("selection_run_id"),
                "research_selection_run_id": research.get("selection_run_id"),
                "top_selection_run_id": top.get("top_selection_run_id"),
            }.items() if value
        }
        distinct = set(selection_ids.values())
        mismatches = sorted(str(value) for value in distinct) if len(distinct) > 1 else []
        status = "MISMATCH" if mismatches else "CONSISTENT" if distinct else "UNKNOWN"
    return {"status": status, **values, "mismatches": mismatches}


def _overall_status(selector: dict[str, Any], validation: dict[str, Any], research: dict[str, Any], top: dict[str, Any], run_consistency: dict[str, Any]) -> str:
    if selector["status"] == NOT_AVAILABLE:
        return "BLOCKED"
    if str(selector["status"]).upper() not in {"COMPLETED", "SUCCESS", "PARTIAL"}:
        return "DEGRADED"
    freshness_states = [section.get("freshness", {}).get("status") for section in (selector, validation, research)]
    if selector.get("quality_timeout_count") or validation["status"] == NOT_AVAILABLE or research["status"] == NOT_AVAILABLE:
        return "DEGRADED"
    if any(state == "STALE" for state in freshness_states) or run_consistency["status"] == "MISMATCH":
        return "DEGRADED"
    if any(state in {"UNKNOWN", "NOT_AVAILABLE"} for state in freshness_states) or run_consistency["status"] in {"UNKNOWN", "NOT_AVAILABLE"}:
        return "UNKNOWN"
    if top["supervisor_status"] in {NOT_AVAILABLE, "FAILED"}:
        return "DEGRADED"
    return "HEALTHY"


def collect_daily_runtime_snapshot(*, snapshot_date: date, paths: RuntimePaths | None = None, generated_at: str | None = None, as_of: datetime | None = None) -> dict[str, Any]:
    runtime = paths or runtime_paths()
    selector, selector_evidence = _selector_snapshot(runtime)
    current_selection_run_id = selector.get("selection_run_id")
    validation, validation_evidence = _validation_snapshot(runtime, current_selection_run_id)
    research, research_evidence = _research_snapshot(runtime, current_selection_run_id)
    top, top_evidence = _top_snapshot(runtime)
    effective_as_of = as_of or _parse_timestamp(generated_at) or datetime.now(timezone.utc)
    if effective_as_of.tzinfo is None:
        effective_as_of = effective_as_of.replace(tzinfo=timezone.utc)
    expected_date = _expected_operational_date(snapshot_date)
    selector["freshness"] = _freshness(source_timestamp=selector.get("last_run_at"), source_operational_date=selector.get("selection_date"), expected_date=expected_date, as_of=effective_as_of, policy_key="selector")
    validation["freshness"] = _freshness(source_timestamp=validation.get("timestamp"), source_operational_date=validation.get("selection_date"), expected_date=expected_date, as_of=effective_as_of, policy_key="candidate_validation")
    research["freshness"] = _freshness(source_timestamp=research.get("last_update_at"), source_operational_date=research.get("selection_date"), expected_date=expected_date, as_of=effective_as_of, policy_key="research")
    consistency = _run_consistency(selector, validation, research, top)
    warnings: list[str] = []
    for name, section in (("candidate_validation", validation), ("research", research)):
        if section.get("status") == NOT_AVAILABLE:
            warnings.append(f"{name}_evidence_unavailable")
    if top["supervisor_status"] in {NOT_AVAILABLE, "FAILED"}:
        warnings.append("top_runtime_evidence_unavailable_or_failed")
    return {
        "schema_version": SCHEMA_VERSION,
        "date": snapshot_date.isoformat(),
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": str(os.environ.get("QUANTCAIRN_EXECUTION_MODE") or "UNKNOWN").upper(),
        "overall_status": _overall_status(selector, validation, research, top, consistency),
        "selector": selector,
        "candidate_validation": validation,
        "research": research,
        "top": top,
        "run_consistency": consistency,
        "freshness_policy": FRESHNESS_POLICY,
        "dashboard": {"status": NOT_COLLECTED, "http_status": None},
        "runtime": {"selector_status": selector.get("status"), "candidate_validation_status": validation.get("status"), "research_status": research.get("status"), "top_status": top.get("supervisor_status")},
        "warnings": warnings,
        "errors": [],
        "evidence": {"selection_report": selector_evidence, "validation_report": validation_evidence, "research_state": research_evidence, "top_state": top_evidence},
    }


def render_markdown(snapshot: dict[str, Any]) -> str:
    selector = snapshot["selector"]
    validation = snapshot["candidate_validation"]
    research = snapshot["research"]
    top = snapshot["top"]
    lines = [f"# QuantCairn PAPER Daily Runtime Report", "", f"Date: {snapshot['date']}", f"Overall Status: {snapshot['overall_status']}", "", "## Selection", f"Universe: {selector.get('universe_count')}", f"Filtered: {selector.get('filtered_count')}", f"Formal Eligible: {selector.get('formal_eligible_count')}", f"Quality Checked: {selector.get('quality_checked_count')}", f"Quality Timeout: {selector.get('quality_timeout_count')}", f"Final Candidates: {', '.join(selector.get('final_candidates') or []) or 'none'}", "", "## Candidate Validation", f"Status: {validation.get('status')}", f"Total: {validation.get('total')}", f"Passed: {validation.get('passed')}", f"Rejected: {validation.get('rejected')}", "", "## Research", f"Status: {research.get('status')}", f"Completed: {research.get('completed')}", f"Failed: {research.get('failed')}", f"Pending: {research.get('pending')}", "", "## TOP", f"Supervisor: {top.get('supervisor_status')}"]
    for slot in top.get("slots", []):
        lines.append(f"{slot['slot']}: {slot.get('ticker') or 'none'} ({slot.get('mode') or 'unknown'})")
    lines.extend(["", "## Evidence Freshness", f"Selector: {selector.get('freshness', {}).get('status')}", f"Validation: {validation.get('freshness', {}).get('status')}", f"Research: {research.get('freshness', {}).get('status')}", f"Run Consistency: {snapshot['run_consistency']['status']}", "", "## Runtime", f"Selector: {snapshot['runtime']['selector_status']}", f"Candidate Validation: {snapshot['runtime']['candidate_validation_status']}", f"Research: {snapshot['runtime']['research_status']}", f"Dashboard: {snapshot['dashboard']['status']}", "", "## Warnings"])
    lines.extend(f"- {item}" for item in snapshot.get("warnings", []))
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def write_daily_runtime_snapshot(snapshot: dict[str, Any], *, reports_dir: Path) -> tuple[Path, Path]:
    root = Path(reports_dir) / "daily_runtime"
    day = str(snapshot["date"])
    json_path = root / f"{day}.json"
    markdown_path = root / f"{day}.md"
    _atomic_write(json_path, json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _atomic_write(markdown_path, render_markdown(snapshot))
    return json_path, markdown_path
