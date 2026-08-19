#!/usr/bin/env python3
"""Read-only QuantCairn runtime health and pre-cutover diagnostics.

This module deliberately uses artifact/configuration reads only.  It never
starts, stops, reloads, or connects to an external service.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from scripts.runtime_identity import collect_identity, identity_findings
from src.config.runtime_paths import RuntimePaths, resolve_top_config_dir, runtime_paths

US_EASTERN = ZoneInfo("America/New_York")
SERVICES = {
    "com.quantcairn.ai-selector": "scripts/ai_selector_wrapper.py",
    "com.quantcairn.candidate-validation": "scripts/run_candidate_validation_scheduler.py",
    "com.quantcairn.combined": "scripts/start_combined.py",
    "com.quantcairn.research": "scripts/run_daily_research.py",
    "com.quantcairn.orphan-monitor": "scripts/start_orphan_monitor.py",
    "com.quantcairn.top-engines": "scripts/start_top_engines.sh",
}


def _paths(project_dir: Path | None = None) -> RuntimePaths:
    return runtime_paths(project_dir or PROJECT_DIR)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _read_jsonl_last(path: Path, n: int = 20) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except (ValueError, TypeError):
                    continue
    except OSError:
        return []
    return rows[-n:]


def _glob_latest(path: Path, pattern: str) -> Path | None:
    try:
        matches = sorted(path.glob(pattern), reverse=True)
    except OSError:
        return None
    return matches[0] if matches else None


def _status(value: str, *, detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": value}
    if detail:
        result["detail"] = detail
    return result


def _check_scheduler(project_dir: Path | None = None) -> dict:
    result: dict = {
        "active": False, "last_decision": None, "last_decision_reason": None,
        "last_decision_at": None, "today_skipped_count": 0, "today_runs": 0,
        "decisions_today": [],
    }
    log_path = _paths(project_dir).logs_dir / "ai_selector.err.log"
    if not log_path.exists():
        result["last_decision_reason"] = "no_log_file"
        result["status"] = "UNKNOWN"
        return result
    try:
        lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if "[SCHEDULER]" in line]
    except OSError:
        result["status"] = "UNKNOWN"
        return result
    if lines:
        result["active"] = True
        last = lines[-1]
        for token in last.split():
            if token.startswith("decision="):
                result["last_decision"] = token.split("=", 1)[1]
            elif token.startswith("reason="):
                result["last_decision_reason"] = token.split("=", 1)[1]
            elif token.startswith("et_date="):
                result["last_decision_at"] = token.split("=", 1)[1]
        today = date.today().isoformat()
        for line in lines:
            if today in line:
                result["decisions_today"].append(line.strip())
                if "decision=run" in line:
                    result["today_runs"] += 1
                elif "decision=skipped" in line:
                    result["today_skipped_count"] += 1
        result["status"] = "HEALTHY"
    else:
        result["status"] = "STALE"
    return result


def _check_ai_selector(project_dir: Path | None = None) -> dict:
    paths = _paths(project_dir)
    result: dict = {
        "last_run_date": None, "last_selection_date": None,
        "last_selection_symbols": [], "last_run_status": None, "run_markers": [],
    }
    try:
        for marker in sorted(paths.state_dir.glob("ai_selector_*.done"), reverse=True):
            marker_date = marker.stem.replace("ai_selector_", "")
            result["run_markers"].append(marker_date)
            if result["last_run_date"] is None:
                result["last_run_date"] = marker_date
    except OSError:
        pass
    latest_log = _glob_latest(paths.logs_dir, "selection_*.log")
    if latest_log:
        data = _read_json(latest_log) or {}
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        result["last_selection_date"] = latest_log.stem.replace("selection_", "")
        result["last_selection_symbols"] = summary.get("final_selected_symbols", []) or []
        result["last_run_status"] = "timed_out" if summary.get("timed_out") else "completed"
    manifest = _read_json(paths.state_dir / "selection_bundle_manifest.json")
    if manifest and result["last_selection_date"] is None:
        result["last_selection_date"] = manifest.get("selection_date")
    result["status"] = "HEALTHY" if result["last_selection_date"] else "STALE"
    return result


def _check_market() -> dict:
    try:
        from src.utils.market_calendar import market_session_context
        ctx = market_session_context()
        return {
            "status": "HEALTHY", "now_et": ctx.now_et.isoformat(),
            "session_label": ctx.session_label, "market_open": ctx.market_open,
            "is_market_holiday": ctx.is_market_holiday,
            "is_regular_session": ctx.is_regular_session,
            "current_session_date": ctx.current_session.isoformat(),
            "previous_completed_session": ctx.previous_completed_session.isoformat(),
            "session_reason": ctx.current_session_reason,
        }
    except Exception as exc:
        return {"status": "UNKNOWN", "error": str(exc)}


def _check_execution_mode(project_dir: Path | None = None) -> dict:
    root = _paths(project_dir).project_dir
    mode = (os.environ.get("QUANTCAIRN_EXECUTION_MODE") or os.environ.get("OPENALPHA_EXECUTION_MODE") or "UNKNOWN").strip().upper()
    allow_live = os.environ.get("OPENALPHA_ALLOW_LIVE_ORDER", "0")
    has_live_config = False
    config_dir = resolve_top_config_dir(root, required=False)
    for idx in range(1, 6):
        path = config_dir / f"TOP{idx}.yaml" if config_dir is not None else None
        text = path.read_text(encoding="utf-8") if path is not None and path.exists() else ""
        if "mode: live" in text.lower():
            has_live_config = True
            break
    effective = "DISABLED"
    if mode == "LIVE" or has_live_config:
        effective = "CONFIGURED_BUT_GATED"
        if allow_live == "1":
            effective = "ENABLED — VERIFY SAFETY"
    return {
        "status": "MISCONFIGURED" if mode == "LIVE" else "HEALTHY",
        "execution_mode": mode,
        "has_live_config": has_live_config,
        "live_order_gate": {"allow_live_order_env": allow_live, "top_allow_live_order_default": "false", "effective_live_trading": effective},
        "env_vars": {
            "YF_DISABLE_CURL_CFFI": os.environ.get("YF_DISABLE_CURL_CFFI", "not set"),
            "SOXS_TELEGRAM_BOT_TOKEN": "SET" if os.environ.get("SOXS_TELEGRAM_BOT_TOKEN") or os.environ.get("SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN") else "NOT_SET",
            "SOXS_TELEGRAM_CHAT_ID": "SET" if os.environ.get("SOXS_TELEGRAM_CHAT_ID") or os.environ.get("SOXS_OPENALPHA_TELEGRAM_CHAT_ID") else "NOT_SET",
            "QUANTCAIRN_ADMIN_CHAT_ID": "SET" if os.environ.get("QUANTCAIRN_ADMIN_CHAT_ID") or os.environ.get("SOXS_OPENALPHA_ADMIN_CHAT_ID") else "NOT_SET",
        },
    }


def _check_notifier(project_dir: Path | None = None) -> dict:
    path = _paths(project_dir).state_dir / "trade_notification_state.json"
    data = _read_json(path)
    if not data:
        return {"status": "STALE", "dedup_state": "missing", "records": 0, "paper_records": 0, "live_records": 0, "last_updated": None, "telegram_configured": bool(os.environ.get("SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN"))}
    sent_keys = data.get("sent_keys", []) if isinstance(data.get("sent_keys"), list) else []
    notifications = data.get("notifications", {}) if isinstance(data.get("notifications"), dict) else {}
    newest = None
    for key in sent_keys[-5:]:
        item = notifications.get(key, {}) if isinstance(notifications.get(key), dict) else {}
        newest = {"key": key, "ticker": item.get("ticker", ""), "side": item.get("side", ""), "created_at": item.get("created_at", "")}
    return {"status": "HEALTHY", "dedup_state": "active", "records": len(sent_keys), "paper_records": sum(str(k).startswith("paper:") for k in sent_keys), "live_records": sum(str(k).startswith("live:") for k in sent_keys), "last_updated": data.get("updated_at"), "newest_notification": newest, "telegram_configured": bool(os.environ.get("SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN"))}


def _check_paper_portfolio(project_dir: Path | None = None) -> dict:
    state = _paths(project_dir).state_dir
    primary = state / "paper" / "paper-default" / "portfolio_state.json"
    legacy = state / "paper_portfolio_state.json"
    primary_data = _read_json(primary)
    legacy_data = _read_json(legacy) if primary_data is None else None
    data = primary_data or legacy_data
    if not data:
        return {"status": "STALE", "state": "missing", "cash": None, "equity": None, "positions": 0}
    positions = data.get("positions", []) if isinstance(data.get("positions"), list) else []
    orders = data.get("orders", []) if isinstance(data.get("orders"), list) else []
    return {"status": "HEALTHY" if data.get("equity") is not None else "DEGRADED", "state": "valid" if data.get("equity") is not None else "invalid", "cash": data.get("cash"), "equity": data.get("equity"), "buying_power": data.get("buying_power"), "realized_pnl": data.get("realized_pnl"), "unrealized_pnl": data.get("unrealized_pnl"), "positions": len(positions), "position_symbols": [p.get("symbol") or p.get("ticker", "") for p in positions if isinstance(p, dict)], "open_orders": len(orders), "total_trades": data.get("total_trades", 0), "last_updated": data.get("updated_at") or data.get("last_update_time"), "source": "primary" if primary_data is not None else "legacy"}


def _check_processes() -> dict:
    result: dict = {"combined_dashboard": False, "processes": [], "port_8090_listening": False}
    try:
        out = subprocess.check_output(["pgrep", "-af", "start_combined|ai_selector_wrapper|start_orphan_monitor|start_top_engines"], text=True, stderr=subprocess.DEVNULL).strip()
        for line in out.splitlines():
            parts = line.split(" ", 1)
            cmd = parts[1] if len(parts) > 1 else line
            if "pgrep" not in cmd and "system_health" not in cmd:
                result["processes"].append(cmd)
                result["combined_dashboard"] |= "start_combined" in cmd
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        port_out = subprocess.check_output(["lsof", "-i", ":8090", "-P", "-n", "-sTCP:LISTEN", "-F", "p"], text=True, stderr=subprocess.DEVNULL).strip()
        result["port_8090_listening"] = bool(port_out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


def _plist(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            value = plistlib.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _check_launchd_drift(project_dir: Path | None = None) -> dict:
    root = _paths(project_dir).project_dir
    installed_root = Path.home() / "Library" / "LaunchAgents"
    services: dict[str, Any] = {}
    for label, expected_script in SERVICES.items():
        path = installed_root / f"{label}.plist"
        data = _plist(path)
        if data is None:
            services[label] = {"status": "UNKNOWN", "installed": path.exists(), "loaded": False, "script": expected_script, "issues": ["plist_missing_or_invalid"]}
            continue
        args = [str(item) for item in data.get("ProgramArguments", [])]
        env = data.get("EnvironmentVariables", {}) if isinstance(data.get("EnvironmentVariables"), dict) else {}
        joined = " ".join(args)
        issues: list[str] = []
        if expected_script not in joined:
            issues.append("entrypoint_drift")
        if env.get("SOXS_PROJECT_DIR") not in (None, str(root)):
            issues.append("project_root_drift")
        for key in ("SOXS_STATE_DIR", "SOXS_REPORTS_DIR", "SOXS_ARTIFACTS_DIR", "SOXS_LOGS_DIR"):
            if key not in env:
                issues.append(f"missing_{key}")
        if label == "com.quantcairn.research" and "--mode independent" not in joined:
            issues.append("research_mode_drift")
        if label == "com.quantcairn.orphan-monitor" and str(env.get("QUANTCAIRN_EXECUTION_MODE", "")).upper() != "PAPER":
            issues.append("orphan_execution_mode_not_paper")
        if label == "com.quantcairn.top-engines" and "start_top_engines.sh" not in joined:
            issues.append("top_supervisor_entrypoint_drift")
        services[label] = {"status": "MISCONFIGURED" if issues else "HEALTHY", "installed": True, "loaded": False, "program_arguments": args, "environment_keys": sorted(env), "script": expected_script, "issues": issues, "installed_plist": str(path)}
    return {"status": "MISCONFIGURED" if any(item.get("status") == "MISCONFIGURED" for item in services.values()) else "HEALTHY", "installed_root": str(installed_root), "services": services, "read_only": True}


def _key_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _check_top_runtime(project_dir: Path | None = None) -> dict:
    control = _paths(project_dir).state_dir / "top_supervisor"
    status = _key_values(control / "status")
    owner = _key_values(control / "owner")
    pid_file = _key_values(control / "supervisor.pid")
    pid = pid_file.get("pid") or status.get("supervisor_pid")
    alive = False
    command = None
    if pid and pid.isdigit():
        try:
            os.kill(int(pid), 0)
            alive = True
            command = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError, FileNotFoundError):
            pass
    owned = alive and bool(command and "start_top_engines.sh" in command)
    ports: dict[str, Any] = {}
    for slot, port in (("TOP1", 8080), ("TOP2", 8081), ("TOP3", 8082)):
        try:
            out = subprocess.check_output(["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"], text=True, stderr=subprocess.DEVNULL).strip()
            ports[slot] = {"port": port, "listening": bool(out), "owner_verified": False, "pids": out.split() if out else []}
        except (subprocess.CalledProcessError, FileNotFoundError):
            ports[slot] = {"port": port, "listening": False, "owner_verified": False, "pids": []}
    state = status.get("state") or ("RUNNING" if owned else "UNKNOWN")
    return {"status": "HEALTHY" if owned and state in {"RUNNING", "READY", "RESTART_CONFIRMED"} else "STALE" if not control.exists() else "DEGRADED", "control_root": str(control), "supervisor_pid": pid, "supervisor_alive": alive, "ownership_verified": owned, "command": command, "status_file": status, "owner_file": owner, "ports": ports, "restart_state": state, "configs": {slot: (_paths(project_dir).project_dir / "configs" / f"{slot}.yaml").is_file() for slot in ("TOP1", "TOP2", "TOP3")}}


def _check_selection_bundle(project_dir: Path | None = None) -> dict:
    state = _paths(project_dir).state_dir
    manifest = _read_json(state / "selection_bundle_manifest.json") or {}
    selection_state = _read_json(state / "ai_selection_state.json") or {}
    run_id = manifest.get("selection_run_id") or selection_state.get("selection_run_id")
    selection_date = manifest.get("selection_date") or selection_state.get("selection_date")
    bundle_root = state / "selection_bundles" / str(run_id) if run_id else None
    present = bool(manifest and run_id and bundle_root and bundle_root.exists())
    issues: list[str] = []
    if manifest and selection_state.get("selection_run_id") not in (None, run_id):
        issues.append("selection_run_id_mismatch")
    if manifest and selection_state.get("selection_date") not in (None, selection_date):
        issues.append("selection_date_mismatch")
    return {"status": "HEALTHY" if present and not issues else "DEGRADED" if manifest else "STALE", "manifest_present": bool(manifest), "bundle_present": present, "selection_run_id": run_id, "selection_date": selection_date, "bundle_hash": manifest.get("selection_bundle_hash") or manifest.get("bundle_hash"), "execution_status": selection_state.get("execution_status"), "selection_outcome": selection_state.get("selection_outcome"), "selected_top_n": selection_state.get("selected_top_n"), "selected_symbols": selection_state.get("selected_symbols", []), "issues": issues, "root": str(state)}


def _check_candidate_validation(project_dir: Path | None = None) -> dict:
    path = _paths(project_dir).artifacts_dir / "candidates" / "validation_scheduler_runs.jsonl"
    rows = _read_jsonl_last(path, 1)
    if not rows:
        return {"status": "STALE", "available": False, "audit_path": str(path), "errors": ["audit_missing_or_empty"]}
    row = rows[-1]
    forbidden = []
    for event in row.get("transitions", []) or []:
        if isinstance(event, dict) and str(event.get("from_status", "")) == "AI_CANDIDATE" and str(event.get("final_status", "")) in {"TRADABLE", "PAPER_ELIGIBLE", "LIVE_ELIGIBLE"}:
            forbidden.append(event)
    return {"status": "MISCONFIGURED" if forbidden else "HEALTHY", "available": True, "audit_path": str(path), "validation_run_id": row.get("validation_run_id") or row.get("run_id"), "selection_run_id": row.get("selection_run_id"), "selection_date": row.get("selection_date"), "bundle_hash": row.get("bundle_hash"), "bundle_source": row.get("bundle_source"), "mode": row.get("mode"), "dry_run": row.get("dry_run"), "applied": row.get("applied"), "timestamp": row.get("timestamp"), "status_value": row.get("status"), "candidates_scanned": row.get("candidates_scanned", 0), "candidates_advanced": row.get("candidates_advanced", 0), "transitions": row.get("transitions", []), "errors": row.get("errors", []), "forbidden_transitions": forbidden, "safety": {"trade_api_used": False, "broker_used": False, "paper_eligible_auto": False, "live_eligible_auto": False}}


def _check_research(project_dir: Path | None = None) -> dict:
    paths = _paths(project_dir)
    root = paths.artifacts_dir / "research" / "daily"
    dirs = sorted([p for p in root.iterdir() if p.is_dir() and (p / "research_run_audit.json").exists()], reverse=True) if root.exists() else []
    if not dirs:
        return {"status": "STALE", "available": False, "root": str(root), "issues": ["research_artifact_missing"]}
    audit_path = dirs[0] / "research_run_audit.json"
    audit = _read_json(audit_path) or {}
    report = _read_json(dirs[0] / "daily_candidate_report.json") or {}
    issues = []
    if str(audit.get("mode", "")).lower() == "independent" and audit.get("selector_invoked") is True:
        issues.append("independent_mode_invoked_selector")
    return {"status": "MISCONFIGURED" if issues else "HEALTHY" if report else "DEGRADED", "available": bool(report), "root": str(root), "research_run_id": audit.get("research_run_id"), "mode": audit.get("mode") or audit.get("execution_mode"), "status_value": audit.get("status"), "report_status": audit.get("report_status"), "research_date": audit.get("research_date"), "selection_run_id": audit.get("selection_run_id"), "selection_date": audit.get("selection_date"), "selection_bundle_hash": audit.get("selection_bundle_hash"), "bundle_source": audit.get("bundle_source"), "selector_invoked": audit.get("selector_invoked"), "generated_at": audit.get("generated_at") or audit.get("completed_at"), "freshness_status": audit.get("freshness_status") or report.get("freshness_status"), "audit_path": str(audit_path), "issues": issues}


def _check_preflight(project_dir: Path | None = None) -> dict:
    path = _paths(project_dir).artifacts_dir / "selection" / "preflight.json"
    data = _read_json(path)
    if not data:
        return {"status": "UNKNOWN", "available": False, "coverage_scope": "sample", "path": str(path), "issues": ["preflight_artifact_missing"]}
    timed_out = bool(data.get("scan_timed_out"))
    errors = data.get("scan_errors", []) or []
    qualified = not timed_out and not errors and float(data.get("quote_coverage_pct", 0) or 0) >= 90 and float(data.get("ohlcv_coverage_pct", 0) or 0) >= 90
    return {"status": "HEALTHY" if qualified else "DEGRADED", "available": True, "coverage_scope": "sample", "path": str(path), "market_state": data.get("market_state"), "run_mode": data.get("run_mode"), "data_mode": data.get("data_mode"), "quote_coverage_pct": data.get("quote_coverage_pct"), "ohlcv_coverage_pct": data.get("ohlcv_coverage_pct"), "scan_timed_out": timed_out, "scan_errors": errors, "symbols_checked": data.get("symbols_checked"), "qualified_full_live": qualified, "selection_run_id": data.get("selection_run_id"), "generated_at": data.get("generated_at")}


def _check_shadow(project_dir: Path | None = None) -> dict:
    paths = _paths(project_dir)
    candidates = list(paths.artifacts_dir.glob("shadow/**/*.json")) if paths.artifacts_dir.exists() else []
    latest = max(candidates, key=lambda p: p.stat().st_mtime, default=None)
    if latest is None:
        return {"status": "STALE", "available": False, "latest_artifact": None, "issues": ["shadow_artifact_missing"]}
    data = _read_json(latest) or {}
    age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - latest.stat().st_mtime)
    return {"status": "HEALTHY" if age_seconds < 86400 else "STALE", "available": bool(data), "latest_artifact": str(latest), "age_seconds": round(age_seconds, 1), "shadow_state": data.get("state") or data.get("status"), "last_updated": data.get("updated_at") or data.get("generated_at")}


def _check_history_cache(project_dir: Path | None = None) -> dict:
    path = _paths(project_dir).state_dir / "history_cache" / "ohlcv.sqlite3"
    if not path.exists():
        return {"status": "STALE", "exists": False, "path": str(path), "cache_mode": os.environ.get("OPENALPHA_HISTORY_CACHE_MODE", "unset")}
    result: dict[str, Any] = {"status": "HEALTHY", "exists": True, "path": str(path), "size_bytes": path.stat().st_size, "cache_mode": os.environ.get("OPENALPHA_HISTORY_CACHE_MODE", "unset")}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            tables = db.execute("select name from sqlite_master where type='table'").fetchall()
            result["tables"] = [row[0] for row in tables]
            result["readable"] = True
    except (sqlite3.Error, OSError) as exc:
        result.update(status="DEGRADED", readable=False, error=str(exc))
    return result


def _check_disk(project_dir: Path | None = None) -> dict:
    paths = _paths(project_dir)
    roots = {name: path for name, path in {"state": paths.state_dir, "reports": paths.reports_dir, "artifacts": paths.artifacts_dir, "logs": paths.logs_dir}.items()}
    sizes: dict[str, int] = {}
    for name, root in roots.items():
        total = 0
        if root.exists():
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    pass
        sizes[name] = total
    usage = shutil.disk_usage(paths.project_dir)
    return {"status": "HEALTHY", "free_bytes": usage.free, "total_bytes": usage.total, "root_sizes_bytes": sizes}


def _check_orphan_monitor(project_dir: Path | None = None) -> dict:
    root = _paths(project_dir)
    label = "com.quantcairn.orphan-monitor"
    launch_agent = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log_paths = [root.logs_dir / "orphan-monitor.log", root.logs_dir / "orphan-monitor.err.log"]
    latest = max((p for p in log_paths if p.exists()), key=lambda p: p.stat().st_mtime, default=None)
    loaded = False
    launchctl_status = None
    for command in (["launchctl", "print", f"gui/{os.getuid()}/{label}"], ["launchctl", "list", label]):
        try:
            output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
            if output:
                loaded, launchctl_status = True, output.splitlines()[0][:200]
                break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    processes = _check_processes()
    log_files = [{"path": str(path), "exists": path.exists(), "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat() if path.exists() else None} for path in log_paths]
    return {"status": "HEALTHY" if launch_agent.exists() and loaded else "UNKNOWN", "label": label, "installed": launch_agent.exists(), "installed_plist": str(launch_agent), "loaded": loaded, "running": any("start_orphan_monitor" in p for p in processes["processes"]), "launchctl_status": launchctl_status, "log_files": log_files, "logs_present": any(p.exists() for p in log_paths), "last_log_file": str(latest) if latest else None, "last_log_updated": datetime.fromtimestamp(latest.stat().st_mtime).isoformat() if latest else None, "last_log_excerpt": latest.read_text(encoding="utf-8").splitlines()[-1][:240] if latest and latest.read_text(encoding="utf-8").splitlines() else None}


def _aggregate(report: dict[str, Any]) -> dict[str, Any]:
    sections = ("runtime_identity", "launchd", "selection_bundle", "candidate_validation", "research", "preflight", "top_runtime", "paper_portfolio", "notifier", "shadow", "history_cache")
    statuses = {name: report.get(name, {}).get("status", "UNKNOWN") for name in sections}
    if any(status == "MISCONFIGURED" for status in statuses.values()):
        overall = "MISCONFIGURED"
    elif any(status in {"BLOCKED", "DEGRADED"} for status in statuses.values()):
        overall = "DEGRADED"
    elif any(status in {"STALE", "UNKNOWN"} for status in statuses.values()):
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"
    return {"status": overall, "subsystems": statuses, "read_only": True}


def generate_report(project_dir: Path | None = None) -> dict:
    paths = _paths(project_dir)
    identity = collect_identity(paths.project_dir)
    report = {
        "runtime_identity": {"status": identity_findings(identity)["status"], "identity": identity, "findings": identity_findings(identity)},
        "scheduler": _check_scheduler(paths.project_dir), "ai_selector": _check_ai_selector(paths.project_dir), "market": _check_market(),
        "execution_mode": _check_execution_mode(paths.project_dir), "notifier": _check_notifier(paths.project_dir), "paper_portfolio": _check_paper_portfolio(paths.project_dir),
        "orphan_monitor": _check_orphan_monitor(paths.project_dir), "processes": _check_processes(), "launchd": _check_launchd_drift(paths.project_dir),
        "top_runtime": _check_top_runtime(paths.project_dir), "selection_bundle": _check_selection_bundle(paths.project_dir), "candidate_validation": _check_candidate_validation(paths.project_dir),
        "research": _check_research(paths.project_dir), "preflight": _check_preflight(paths.project_dir), "shadow": _check_shadow(paths.project_dir), "history_cache": _check_history_cache(paths.project_dir), "disk": _check_disk(paths.project_dir),
    }
    report["overall"] = _aggregate(report)
    return report


def _icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _render_section(lines: list[str], title: str, section: dict[str, Any]) -> None:
    status = section.get("status", "UNKNOWN")
    lines.append(f"\n{title}: {status}")
    detail = section.get("detail") or section.get("error") or section.get("issues")
    if detail:
        lines.append(f"  detail: {detail}")


def render_text(report: dict) -> str:
    lines = ["QuantCairn Health Report", "=" * 60]
    scheduler = report.get("scheduler", {})
    lines.extend(["\nScheduler:", f"  {_icon(bool(scheduler.get('active')))} active: {scheduler.get('active', False)}", f"  last decision: {scheduler.get('last_decision') or 'unknown'}"])
    selector = report.get("ai_selector", {})
    lines.extend(["\nAI Selector:", f"  last run: {selector.get('last_run_date') or 'never'}", f"  last selection date: {selector.get('last_selection_date') or 'none'}", f"  last status: {selector.get('last_run_status') or 'unknown'}"])
    market = report.get("market", {})
    lines.extend(["\nMarket (US Eastern):", f"  session: {market.get('session_label', 'unknown')}", f"  market open: {market.get('market_open', 'unknown')}"])
    execution = report.get("execution_mode", {})
    gate = execution.get("live_order_gate", {})
    lines.extend(["\nExecution Mode:", f"  mode: {execution.get('execution_mode', 'UNKNOWN')}", f"  live trading: {gate.get('effective_live_trading', 'DISABLED')}"])
    notifier = report.get("notifier", {})
    lines.extend(["\nNotifier:", f"  dedup state: {notifier.get('dedup_state', 'missing')}", f"  records: {notifier.get('records', 0)}"])
    portfolio = report.get("paper_portfolio", {})
    lines.extend(["\nPaper Portfolio:", f"  state: {portfolio.get('state', 'missing')}", f"  positions: {portfolio.get('positions', 0)}"])
    for title, key in (("Launchd", "launchd"), ("Selection Bundle", "selection_bundle"), ("Candidate Validation", "candidate_validation"), ("Research", "research"), ("Preflight", "preflight"), ("TOP Runtime", "top_runtime"), ("Shadow", "shadow"), ("History Cache", "history_cache"), ("Disk", "disk")):
        _render_section(lines, title, report.get(key, {}))
    orphan = report.get("orphan_monitor", {})
    lines.extend(["\nOrphan Monitor:", f"  installed: {orphan.get('installed', False)}", f"  loaded: {orphan.get('loaded', False)}", f"  running: {orphan.get('running', False)}"])
    lines.extend(["\nLive Trading:", f"  🔒 {gate.get('effective_live_trading', 'DISABLED')}"])
    processes = report.get("processes", {})
    lines.extend(["\nProcesses:", f"  combined dashboard: {_icon(bool(processes.get('combined_dashboard')))}", f"  port 8090: {_icon(bool(processes.get('port_8090_listening')))}"])
    identity = report.get("runtime_identity", {}).get("identity", {})
    lines.extend(["\nRuntime Identity:", f"  code root: {identity.get('code_root', 'UNKNOWN')}", f"  git sha: {identity.get('git_sha', 'UNKNOWN')}", f"  state root: {identity.get('state_root', 'UNKNOWN')}", f"  reports root: {identity.get('reports_root', 'UNKNOWN')}", f"  artifacts root: {identity.get('artifacts_root', 'UNKNOWN')}", f"  logs root: {identity.get('logs_root', 'UNKNOWN')}"])
    lines.extend(["\nOverall:", f"  {report.get('overall', {}).get('status', 'UNKNOWN')}", "  read_only: true", f"\nReport generated: {datetime.now(timezone.utc).isoformat()}", "=" * 60])
    return "\n".join(lines)


def main() -> int:
    report = generate_report()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2, default=str, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
