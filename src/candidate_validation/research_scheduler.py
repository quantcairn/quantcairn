from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.candidate_validation.performance_tracker import CandidatePerformanceTracker
from src.candidate_validation.research_report import CandidateDailyResearchReportGenerator
from src.candidate_validation.store import CandidateValidationStore
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
        )
        return generator.write()

    def _load_existing_audit(self, report_date: date) -> dict[str, Any] | None:
        audit_path = _research_day_dir(self.research_root, report_date) / "research_run_audit.json"
        return _load_json(audit_path)

    def run(
        self,
        report_date: date | None = None,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
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

        existing_audit = self._load_existing_audit(effective_date)
        existing_status = _safe_text((existing_audit or {}).get("status") or (existing_audit or {}).get("report_status") or "").lower()
        if existing_audit and existing_status in {"completed", "already_completed"} and not force:
            return {
                "status": "already_completed",
                "report_status": existing_audit.get("report_status") or "completed",
                "applied": False,
                "dry_run": dry_run,
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
            }

        plan_steps = [
            {"name": "market_calendar_check", "status": "completed"},
            {"name": "run_ai_selector", "status": "planned"},
            {"name": "candidate_performance_refresh", "status": "planned"},
            {"name": "generate_daily_research_report", "status": "planned"},
        ]
        if dry_run:
            return {
                "status": "dry_run",
                "report_status": "planned",
                "applied": False,
                "dry_run": True,
                "force": force,
                "research_date": effective_date.isoformat(),
                "research_dir": _relative_path(day_dir, self.project_dir),
                "candidate_count": len(CandidateValidationStore(self.candidate_root).load_latest_candidates()) if self.candidate_root.exists() else 0,
                "last_research_run": existing_audit.get("completed_at") if existing_audit else None,
                "calendar": calendar,
                "steps": plan_steps,
                "output_paths": {
                    "json": _relative_path(report_json_path, self.project_dir),
                    "markdown": _relative_path(report_md_path, self.project_dir),
                    "audit": _relative_path(audit_path, self.project_dir),
                },
            }

        day_dir.mkdir(parents=True, exist_ok=True)
        started_at = _utc_now_iso()
        steps: list[dict[str, Any]] = [{"name": "market_calendar_check", "status": "completed"}]
        try:
            selector_result = self.selector_runner(self.project_dir) or {}
            steps.append({"name": "run_ai_selector", "status": "completed", "returncode": selector_result.get("returncode", 0)})
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
            _atomic_write_json(audit_path, audit)
            return {
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
            }
        except Exception as exc:
            failure = {
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
            try:
                _atomic_write_json(audit_path, failure)
            except Exception:
                pass
            return {
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
            }
