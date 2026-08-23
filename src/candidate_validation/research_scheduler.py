from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.candidate_validation.performance_tracker import CandidatePerformanceTracker
from src.candidate_validation.research_report import CandidateDailyResearchReportGenerator
from src.candidate_validation.orchestrator import _selection_bundle_identity
from src.candidate_validation.store import CandidateValidationStore
from src.openalpha.selection_bundle import load_committed_selection_bundle
from src.config.runtime_paths import resolve_artifacts_dir
from src.utils.market_calendar import (
    US_EASTERN,
    is_us_market_holiday,
    is_us_market_trading_day,
    market_session_context,
)

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_RESEARCH_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "research" / "daily"
DEFAULT_CANDIDATE_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "candidates"


def _utc_now_iso() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = _safe_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


def _relative_path(path: Path, project_dir: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_dir.resolve()))
    except Exception:
        return path.name


def market_calendar_check(
    report_date: date | None = None,
    *,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    requested_date = report_date
    if now_et is None:
        if requested_date is not None:
            now_et = datetime.combine(requested_date, time(12, 0), tzinfo=US_EASTERN)
        else:
            now_et = datetime.now(US_EASTERN)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=US_EASTERN)
    else:
        now_et = now_et.astimezone(US_EASTERN)

    session = market_session_context(now_et)
    resolved_date = requested_date or session.current_session
    resolved_trading_day = is_us_market_trading_day(resolved_date)
    resolved_holiday = is_us_market_holiday(resolved_date)
    is_weekend = resolved_date.weekday() >= 5
    should_run = bool(resolved_trading_day)
    if not should_run:
        reason = "market_holiday" if resolved_holiday else "weekend" if is_weekend else "market_closed"
    else:
        reason = "trading_day"
    return {
        "requested_date": resolved_date.isoformat(),
        "current_session": session.current_session.isoformat(),
        "previous_completed_session": session.previous_completed_session.isoformat(),
        "next_session": session.next_session.isoformat(),
        "is_market_holiday": resolved_holiday or session.is_market_holiday,
        "is_weekend": is_weekend,
        "is_trading_day": resolved_trading_day,
        "is_premarket": session.is_premarket,
        "is_regular_session": session.is_regular_session,
        "is_after_hours": session.is_after_hours,
        "session_label": session.session_label,
        "session_status": session.current_session_status,
        "session_reason": session.current_session_reason,
        "should_run": should_run,
        "reason": reason,
        "now_et": now_et.isoformat(),
    }


def _default_selector_runner(project_dir: Path) -> dict[str, Any]:
    script = project_dir / "scripts" / "run_ai_selector.py"
    if not script.exists():
        raise FileNotFoundError("run_ai_selector_script_missing")
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"run_ai_selector_failed:{result.returncode}")
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _refresh_candidate_performance(candidate_root: Path) -> dict[str, Any]:
    store = CandidateValidationStore(candidate_root)
    tracker = CandidatePerformanceTracker(candidate_root)
    candidates = store.load_latest_candidates()
    tracker.sync(candidates)
    return tracker.analyze(candidates)


def _research_day_dir(research_root: Path, report_date: date) -> Path:
    return research_root / report_date.isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _latest_completed_research_dir(research_root: Path) -> Path | None:
    if not research_root.exists():
        return None
    candidates = []
    for path in research_root.iterdir():
        if not path.is_dir():
            continue
        audit_path = path / "research_run_audit.json"
        if audit_path.exists():
            candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.name)
    return candidates[-1]


def latest_research_status(
    *,
    project_dir: Path | None = None,
    research_root: Path | None = None,
) -> dict[str, Any]:
    project_root = Path(project_dir or PROJECT_DIR)
    root = Path(research_root or (project_root / "artifacts" / "research" / "daily"))
    latest_dir = _latest_completed_research_dir(root)
    if latest_dir is None:
        return {
            "available": False,
            "state": "STALE",
            "status_label": "unavailable",
            "detail": "research run unavailable",
            "last_research_run": None,
            "research_date": None,
            "candidate_count": 0,
            "report_status": "unavailable",
            "output_dir": _relative_path(root, project_root),
            "audit_path": None,
            "report_path": None,
        }

    audit_path = latest_dir / "research_run_audit.json"
    audit = _load_json(audit_path) or {}
    report_path = latest_dir / "daily_candidate_report.json"
    report = _load_json(report_path) or {}
    status = _safe_text(audit.get("status") or audit.get("report_status") or "").lower()
    report_status = _safe_text(audit.get("report_status") or status or "unavailable")
    candidate_count = report.get("candidate_count")
    if candidate_count is None:
        candidate_count = audit.get("candidate_count", 0)
    state = "SAFE" if status in {"completed", "already_completed"} and report else "STALE"
    status_label = "SAFE" if state == "SAFE" else _safe_text(report_status or "STALE").upper() if report_status not in {"unavailable", ""} else "unavailable"
    detail = "daily research report ready" if state == "SAFE" else _safe_text(audit.get("detail") or "research run stale")
    return {
        "available": bool(report),
        "state": state,
        "status_label": status_label,
        "detail": detail,
        "last_research_run": audit.get("completed_at") or audit.get("generated_at") or report.get("generated_at"),
        "research_date": audit.get("research_date") or latest_dir.name,
        "candidate_count": int(candidate_count or 0),
        "report_status": report_status,
        "output_dir": _relative_path(latest_dir, project_root),
        "audit_path": _relative_path(audit_path, project_root),
        "report_path": _relative_path(report_path, project_root),
        "generated_at": report.get("generated_at") or audit.get("generated_at"),
        "created_at": audit.get("created_at") or audit.get("generated_at"),
    }


@dataclass(slots=True)
class DailyResearchScheduler:
    project_dir: Path = PROJECT_DIR
    research_root: Path = DEFAULT_RESEARCH_ROOT
    candidate_root: Path = DEFAULT_CANDIDATE_ROOT
    selector_runner: Callable[[Path], dict[str, Any]] = _default_selector_runner
    performance_refresher: Callable[[Path], dict[str, Any]] = _refresh_candidate_performance
    report_generator: Callable[[date, Path, Path], dict[str, Any]] | None = None
    _selection_report_for_run: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _selection_metadata_for_run: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.research_root = Path(self.research_root)
        self.candidate_root = Path(self.candidate_root)
        if self.report_generator is None:
            self.report_generator = self._default_report_generator

    def _default_report_generator(self, report_date: date, project_dir: Path, output_dir: Path) -> dict[str, Any]:
        generator = CandidateDailyResearchReportGenerator(
            root_dir=output_dir,
            candidate_root=self.candidate_root,
            selection_report=self._selection_report_for_run,
            selection_metadata=self._selection_metadata_for_run,
        )
        return generator.write()

    @staticmethod
    def _new_research_run_id() -> str:
        return f"research_{datetime.now(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _bundle_identity(bundle: dict[str, Any] | None) -> dict[str, Any]:
        identity = _selection_bundle_identity(bundle, source_hint="committed_bundle")
        return {
            "selection_run_id": identity.get("selection_run_id"),
            "selection_date": identity.get("selection_date"),
            "selection_bundle_hash": identity.get("bundle_hash"),
            "bundle_source": identity.get("bundle_source"),
            "bundle_root": identity.get("bundle_root"),
            "bundle_manifest_path": identity.get("bundle_manifest_path"),
        }

    def _failure_result(
        self,
        *,
        status: str,
        reason: str,
        mode: str,
        dry_run: bool,
        force: bool,
        effective_date: date,
        calendar: dict[str, Any],
        day_dir: Path,
        audit_path: Path,
        identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = dict(identity or {})
        result = {
            "status": status,
            "report_status": "blocked" if status.startswith("blocked_") else "failed",
            "applied": False,
            "dry_run": dry_run,
            "force": force,
            "mode": mode,
            "execution_mode": mode,
            "research_run_id": self._new_research_run_id(),
            "selector_invoked": False,
            "research_date": effective_date.isoformat(),
            "research_dir": _relative_path(day_dir, self.project_dir),
            "candidate_count": 0,
            "calendar": calendar,
            "error": reason,
            "failure_reason": reason,
            "steps": [
                {"name": "market_calendar_check", "status": "completed"},
                {"name": "committed_selection_bundle", "status": "failed", "reason": reason},
            ],
            "audit_path": _relative_path(audit_path, self.project_dir),
        }
        result.update(identity)
        if not dry_run:
            day_dir.mkdir(parents=True, exist_ok=True)
            audit = dict(result)
            audit["started_at"] = _utc_now_iso()
            audit["completed_at"] = _utc_now_iso()
            audit["created_at"] = audit["started_at"]
            audit["all_trading_flags_false"] = True
            audit["trade_api_used"] = False
            audit["broker_used"] = False
            audit["trade_context_initialized"] = False
            _atomic_write_json(audit_path, audit)
            result["audit"] = audit
        return result

    def _load_existing_audit(self, report_date: date) -> dict[str, Any] | None:
        audit_path = _research_day_dir(self.research_root, report_date) / "research_run_audit.json"
        return _load_json(audit_path)

    def run(
        self,
        report_date: date | None = None,
        *,
        mode: str = "legacy",
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        mode = _safe_text(mode).lower() or "legacy"
        if mode not in {"legacy", "independent"}:
            raise ValueError(f"unsupported_research_mode:{mode}")
        calendar = market_calendar_check(report_date)
        effective_date = _safe_date(calendar["requested_date"]) or datetime.now(US_EASTERN).date()
        day_dir = _research_day_dir(self.research_root, effective_date)
        audit_path = day_dir / "research_run_audit.json"
        report_json_path = day_dir / "daily_candidate_report.json"
        report_md_path = day_dir / "daily_candidate_report.md"

        if not calendar["should_run"]:
            return {
                "status": "skipped_market_closed",
                "report_status": "skipped_market_closed",
                "applied": False,
                "dry_run": dry_run,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "candidate_count": 0,
                "last_research_run": None,
                "calendar": calendar,
                "steps": [
                    {"name": "market_calendar_check", "status": "blocked", "reason": calendar["reason"]},
                ],
            }

        committed_bundle: dict[str, Any] | None = None
        bundle_identity: dict[str, Any] = {}
        if mode == "independent":
            committed_bundle = load_committed_selection_bundle(self.project_dir)
            bundle_identity = self._bundle_identity(committed_bundle)
            if not committed_bundle:
                return self._failure_result(
                    status="blocked_missing_bundle" if dry_run else "failed",
                    reason="committed_selection_bundle_missing",
                    mode=mode,
                    dry_run=dry_run,
                    force=force,
                    effective_date=effective_date,
                    calendar=calendar,
                    day_dir=day_dir,
                    audit_path=audit_path,
                    identity=bundle_identity,
                )
            if not bundle_identity.get("selection_run_id") or not bundle_identity.get("selection_date"):
                return self._failure_result(
                    status="blocked_invalid_bundle" if dry_run else "failed",
                    reason="committed_selection_bundle_identity_incomplete",
                    mode=mode,
                    dry_run=dry_run,
                    force=force,
                    effective_date=effective_date,
                    calendar=calendar,
                    day_dir=day_dir,
                    audit_path=audit_path,
                    identity=bundle_identity,
                )

        existing_audit = self._load_existing_audit(effective_date)
        existing_status = _safe_text((existing_audit or {}).get("status") or (existing_audit or {}).get("report_status") or "").lower()
        existing_mode = _safe_text((existing_audit or {}).get("mode") or (existing_audit or {}).get("execution_mode") or "legacy").lower()
        same_independent_input = mode != "independent" or (
            existing_mode == "independent"
            and (existing_audit or {}).get("selection_run_id") == bundle_identity.get("selection_run_id")
            and (existing_audit or {}).get("selection_bundle_hash") == bundle_identity.get("selection_bundle_hash")
        )
        if existing_audit and existing_status in {"completed", "already_completed"} and not force and same_independent_input:
            return {
                "research_run_id": existing_audit.get("research_run_id"),
                "status": "already_completed",
                "report_status": existing_audit.get("report_status") or "completed",
                "applied": False,
                "dry_run": dry_run,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "candidate_count": int(existing_audit.get("candidate_count", 0) or 0),
                "last_research_run": existing_audit.get("completed_at") or existing_audit.get("generated_at"),
                "audit_path": _relative_path(audit_path, self.project_dir),
                "report_paths": {
                    "json": _relative_path(report_json_path, self.project_dir),
                    "markdown": _relative_path(report_md_path, self.project_dir),
                },
                "calendar": calendar,
                "steps": [
                    {"name": "market_calendar_check", "status": "completed"},
                    {"name": "selector", "status": "skipped", "reason": "already_completed"},
                    {"name": "performance_refresh", "status": "skipped", "reason": "already_completed"},
                    {"name": "generate_report", "status": "skipped", "reason": "already_completed"},
                ],
                "audit": existing_audit,
                **bundle_identity,
            }

        plan_steps = [
            {"name": "market_calendar_check", "status": "completed"},
            {
                "name": "run_ai_selector",
                "status": "skipped" if mode == "independent" else "planned",
                "reason": "independent_mode" if mode == "independent" else "",
            },
            {"name": "candidate_performance_refresh", "status": "planned"},
            {"name": "generate_daily_research_report", "status": "planned"},
        ]
        if dry_run:
            return {
                "status": "dry_run",
                "report_status": "planned",
                "applied": False,
                "dry_run": True,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "candidate_count": len(CandidateValidationStore(self.candidate_root).load_latest_candidates()) if self.candidate_root.exists() else 0,
                "last_research_run": existing_audit.get("completed_at") if existing_audit else None,
                "calendar": calendar,
                "steps": plan_steps,
                **bundle_identity,
                "output_paths": {
                    "json": _relative_path(report_json_path, self.project_dir),
                    "markdown": _relative_path(report_md_path, self.project_dir),
                    "audit": _relative_path(audit_path, self.project_dir),
                },
            }

        day_dir.mkdir(parents=True, exist_ok=True)
        started_at = _utc_now_iso()
        research_run_id = self._new_research_run_id()
        self._selection_report_for_run = (
            committed_bundle.get("report") if isinstance(committed_bundle, dict) else None
        )
        self._selection_metadata_for_run = dict(bundle_identity)
        steps: list[dict[str, Any]] = [{"name": "market_calendar_check", "status": "completed"}]
        try:
            selector_invoked = mode == "legacy"
            if selector_invoked:
                selector_result = self.selector_runner(self.project_dir) or {}
                steps.append({"name": "run_ai_selector", "status": "completed", "returncode": selector_result.get("returncode", 0)})
                committed_bundle = load_committed_selection_bundle(self.project_dir)
                bundle_identity = self._bundle_identity(committed_bundle)
                # Legacy keeps its existing latest-report input behavior; the
                # committed bundle read here is audit-only traceability.
                self._selection_report_for_run = None
                self._selection_metadata_for_run = {}
            else:
                steps.append({"name": "run_ai_selector", "status": "skipped", "reason": "independent_mode"})
            performance = self.performance_refresher(self.candidate_root) or {}
            steps.append(
                {
                    "name": "candidate_performance_refresh",
                    "status": "completed",
                    "candidate_count": int(performance.get("candidate_count", 0) or 0),
                }
            )
            report = self.report_generator(effective_date, self.project_dir, day_dir)  # type: ignore[misc]
            steps.append(
                {
                    "name": "generate_daily_research_report",
                    "status": "completed",
                    "candidate_count": int(report.get("candidate_count", 0) or 0),
                }
            )
            report_status = "completed"
            audit = {
                "research_run_id": research_run_id,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": selector_invoked,
                "status": "completed",
                "report_status": report_status,
                "applied": True,
                "dry_run": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "candidate_count": int(report.get("candidate_count", 0) or 0),
                "report_title": report.get("title"),
                "report_paths": {
                    "json": _relative_path(day_dir / "daily_candidate_report.json", self.project_dir),
                    "markdown": _relative_path(day_dir / "daily_candidate_report.md", self.project_dir),
                    "html": _relative_path(day_dir / "daily_candidate_report.html", self.project_dir),
                    "audit": _relative_path(audit_path, self.project_dir),
                },
                "calendar": calendar,
                "steps": steps,
                "market_calendar_check": calendar,
                "all_trading_flags_false": True,
                "trade_api_used": False,
                "broker_used": False,
                "trade_context_initialized": False,
                "selection_status": "AI_CANDIDATE",
                "candidate_store": _relative_path(self.candidate_root, self.project_dir),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "created_at": started_at,
            }
            audit.update(bundle_identity)
            _atomic_write_json(audit_path, audit)
            return {
                "research_run_id": research_run_id,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": selector_invoked,
                "status": "completed",
                "report_status": report_status,
                "applied": True,
                "dry_run": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "candidate_count": int(report.get("candidate_count", 0) or 0),
                "last_research_run": audit["completed_at"],
                "report_paths": audit["report_paths"],
                "audit_path": _relative_path(audit_path, self.project_dir),
                "calendar": calendar,
                "steps": steps,
                "report": report,
                "audit": audit,
                **bundle_identity,
            }
        except Exception as exc:
            failure = {
                "research_run_id": research_run_id,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": mode == "legacy",
                "status": "failed",
                "report_status": "failed",
                "applied": True,
                "dry_run": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "candidate_count": 0,
                "calendar": calendar,
                "steps": steps + [{"name": "failed", "status": "failed"}],
                "error": f"{type(exc).__name__}: {exc}",
                "failure_reason": str(exc),
                "all_trading_flags_false": True,
                "trade_api_used": False,
                "broker_used": False,
                "trade_context_initialized": False,
                "research_dir": _relative_path(day_dir, self.project_dir),
                "report_paths": {
                    "json": _relative_path(report_json_path, self.project_dir),
                    "markdown": _relative_path(report_md_path, self.project_dir),
                    "audit": _relative_path(audit_path, self.project_dir),
                },
                "created_at": started_at,
            }
            failure.update(bundle_identity)
            try:
                _atomic_write_json(audit_path, failure)
            except Exception:
                pass
            return {
                "research_run_id": research_run_id,
                "mode": mode,
                "execution_mode": mode,
                "selector_invoked": mode == "legacy",
                "status": "failed",
                "report_status": "failed",
                "applied": True,
                "dry_run": False,
                "force": force,
                "research_date": effective_date.isoformat(),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "candidate_count": 0,
                "last_research_run": failure["completed_at"],
                "audit_path": _relative_path(audit_path, self.project_dir),
                "calendar": calendar,
                "steps": failure["steps"],
                "error": failure["error"],
                "failure_reason": failure["failure_reason"],
                **bundle_identity,
            }
        finally:
            self._selection_report_for_run = None
            self._selection_metadata_for_run = None
