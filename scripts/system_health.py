#!/usr/bin/env python3
"""QuantCairn System Health Report — read-only diagnostic tool.

Never places orders, modifies state, or restarts processes.

Usage:
    python scripts/system_health.py
    python scripts/system_health.py --json   # machine-readable output
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

US_EASTERN = ZoneInfo("America/New_York")

# ── Internal helpers (no imports from trading/broker modules to stay safe) ──


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_jsonl_last(path: Path, n: int = 20) -> list[dict]:
    try:
        lines = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except Exception:
                    continue
        return lines[-n:]
    except Exception:
        return []


def _glob_latest(path: Path, pattern: str) -> Path | None:
    matches = sorted(path.glob(pattern), reverse=True)
    return matches[0] if matches else None


def _check_scheduler(project_dir: Path | None = None) -> dict:
    """Read AI selector decisions from logs."""
    root = project_dir or PROJECT_DIR
    result: dict = {
        "active": False,
        "last_decision": None,
        "last_decision_reason": None,
        "last_decision_at": None,
        "today_skipped_count": 0,
        "today_runs": 0,
        "decisions_today": [],
    }

    log_path = root / "logs" / "ai_selector.err.log"
    if not log_path.exists():
        result["last_decision_reason"] = "no_log_file"
        return result

    # Read last 200 lines for scheduler decisions
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        scheduler_lines = [l for l in lines if "[SCHEDULER]" in l]
    except Exception:
        return result

    if scheduler_lines:
        result["active"] = True
        last = scheduler_lines[-1]

        # Parse structured fields
        for token in last.split():
            if token.startswith("decision="):
                result["last_decision"] = token.split("=", 1)[1]
            elif token.startswith("reason="):
                result["last_decision_reason"] = token.split("=", 1)[1]
            elif token.startswith("et_date="):
                result["last_decision_at"] = token.split("=", 1)[1]

        today_str = date.today().isoformat()
        for sl in scheduler_lines:
            if today_str in sl:
                result["decisions_today"].append(sl.strip())
                if "decision=run" in sl:
                    result["today_runs"] += 1
                elif "decision=skipped" in sl:
                    result["today_skipped_count"] += 1

    return result


def _check_ai_selector(project_dir: Path | None = None) -> dict:
    root = project_dir or PROJECT_DIR
    result: dict = {
        "last_run_date": None,
        "last_selection_date": None,
        "last_selection_symbols": [],
        "last_run_status": None,
        "run_markers": [],
    }

    # Check run markers
    state_dir = root / "state"
    for marker in sorted(state_dir.glob("ai_selector_*.done"), reverse=True):
        marker_date = marker.stem.replace("ai_selector_", "")
        result["run_markers"].append(marker_date)
        if result["last_run_date"] is None:
            result["last_run_date"] = marker_date

    # Check latest selection log
    latest_log = _glob_latest(root / "logs", "selection_*.log")
    if latest_log:
        try:
            data = json.loads(latest_log.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            result["last_selection_date"] = latest_log.stem.replace("selection_", "")
            result["last_selection_symbols"] = summary.get("final_selected_symbols", [])
            result["last_run_status"] = (
                "timed_out" if summary.get("timed_out") else "completed"
            )
        except Exception:
            pass

    # Check latest selection bundle
    manifest = _read_json(state_dir / "selection_bundle_manifest.json")
    if manifest and result["last_selection_date"] is None:
        result["last_selection_date"] = manifest.get("selection_date")

    return result


def _check_market() -> dict:
    try:
        from src.utils.market_calendar import market_session_context

        ctx = market_session_context()
        return {
            "now_et": ctx.now_et.isoformat(),
            "session_label": ctx.session_label,
            "market_open": ctx.market_open,
            "is_market_holiday": ctx.is_market_holiday,
            "is_regular_session": ctx.is_regular_session,
            "current_session_date": ctx.current_session.isoformat(),
            "previous_completed_session": ctx.previous_completed_session.isoformat(),
            "session_reason": ctx.current_session_reason,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _check_execution_mode(project_dir: Path | None = None) -> dict:
    root = project_dir or PROJECT_DIR
    mode = os.environ.get("QUANTCAIRN_EXECUTION_MODE", "")
    if not mode:
        # Try legacy env var
        mode = os.environ.get("OPENALPHA_EXECUTION_MODE", "")
    if not mode:
        mode = "UNKNOWN (defaulting to RESEARCH)"

    live_order_gate = {
        "allow_live_order_env": os.environ.get("OPENALPHA_ALLOW_LIVE_ORDER", "0"),
        "top_allow_live_order_default": "false",
        "effective_live_trading": "DISABLED",
    }

    # Check if any TOP config has mode=live
    has_live_config = False
    for idx in range(1, 6):
        cfg_path = root / "configs" / f"TOP{idx}.yaml"
        if cfg_path.exists():
            try:
                import yaml
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                if str(cfg.get("mode", "")).strip().lower() == "live":
                    has_live_config = True
                    break
            except Exception:
                pass

    if mode.upper() == "LIVE" or has_live_config:
        live_order_gate["effective_live_trading"] = "CONFIGURED_BUT_GATED"
        if live_order_gate["allow_live_order_env"] == "1":
            live_order_gate["effective_live_trading"] = "⚠️  ENABLED — VERIFY SAFETY"

    return {
        "execution_mode": mode,
        "has_live_config": has_live_config,
        "live_order_gate": live_order_gate,
        "env_vars": {
            "YF_DISABLE_CURL_CFFI": os.environ.get("YF_DISABLE_CURL_CFFI", "not set"),
            "SOXS_TELEGRAM_BOT_TOKEN": "***" if os.environ.get("SOXS_TELEGRAM_BOT_TOKEN") else "not set",
            "SOXS_TELEGRAM_CHAT_ID": "***" if os.environ.get("SOXS_TELEGRAM_CHAT_ID") else "not set",
            "QUANTCAIRN_ADMIN_CHAT_ID": "***" if os.environ.get("QUANTCAIRN_ADMIN_CHAT_ID") else "not set",
        },
    }


def _check_notifier(project_dir: Path | None = None) -> dict:
    root = project_dir or PROJECT_DIR
    state_path = root / "state" / "trade_notification_state.json"
    data = _read_json(state_path)
    if not data:
        return {"dedup_state": "missing", "records": 0, "last_updated": None}

    sent_keys = data.get("sent_keys", [])
    notifications = data.get("notifications", {})

    # Count by mode
    paper_count = sum(1 for k in sent_keys if k.startswith("paper:"))
    live_count = sum(1 for k in sent_keys if k.startswith("live:"))

    # Most recent
    newest = None
    for key in sent_keys[-5:]:
        n = notifications.get(key, {})
        newest = {
            "key": key,
            "ticker": n.get("ticker", ""),
            "side": n.get("side", ""),
            "created_at": n.get("created_at", ""),
        }

    return {
        "dedup_state": "active",
        "records": len(sent_keys),
        "paper_records": paper_count,
        "live_records": live_count,
        "last_updated": data.get("updated_at"),
        "newest_notification": newest,
    }


def _check_paper_portfolio(project_dir: Path | None = None) -> dict:
    root = project_dir or PROJECT_DIR
    # Primary path
    primary = root / "state" / "paper" / "paper-default" / "portfolio_state.json"
    # Legacy fallback
    legacy = root / "state" / "paper_portfolio_state.json"

    primary_data = _read_json(primary)
    legacy_data = _read_json(legacy) if primary_data is None else None
    data = primary_data or legacy_data
    source = "primary" if primary_data is not None else ("legacy" if legacy_data is not None else "none")

    if not data:
        return {"state": "missing", "cash": None, "equity": None, "positions": 0}

    positions = data.get("positions", [])

    return {
        "state": "valid" if data.get("equity") is not None else "invalid",
        "cash": data.get("cash"),
        "equity": data.get("equity"),
        "buying_power": data.get("buying_power"),
        "realized_pnl": data.get("realized_pnl"),
        "unrealized_pnl": data.get("unrealized_pnl"),
        "positions": len(positions),
        "position_symbols": [p.get("symbol") or p.get("ticker", "") for p in positions],
        "total_trades": data.get("total_trades", 0),
        "last_updated": data.get("updated_at") or data.get("last_update_time"),
        "source": source,
    }


def _check_processes() -> dict:
    import subprocess

    result: dict = {"combined_dashboard": False, "processes": []}
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "start_combined|ai_selector_wrapper|start_orphan_monitor"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        for line in out.splitlines():
            parts = line.split(" ", 1)
            if len(parts) >= 2:
                cmd = parts[1]
                # Skip the grep/pgrep self-match
                if "pgrep" in cmd or "system_health" in cmd:
                    continue
                result["processes"].append(cmd)
                if "start_combined" in cmd:
                    result["combined_dashboard"] = True
    except subprocess.CalledProcessError:
        pass

    # Also check port 8090 specifically via lsof and cross-reference PID
    try:
        port_out = subprocess.check_output(
            ["lsof", "-i", ":8090", "-P", "-n", "-sTCP:LISTEN", "-F", "p"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if port_out:
            result["port_8090_listening"] = True
            # Try to find the PID's command
            pid_match = None
            for line in port_out.splitlines():
                if line.startswith("p"):
                    pid_match = line[1:]
                    break
            if pid_match:
                try:
                    cmd_out = subprocess.check_output(
                        ["ps", "-p", pid_match, "-o", "command="],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    if cmd_out and "start_combined" in cmd_out:
                        result["combined_dashboard"] = True
                    if cmd_out and cmd_out not in result["processes"]:
                        result["processes"].append(cmd_out)
                except subprocess.CalledProcessError:
                    pass
    except (subprocess.CalledProcessError, FileNotFoundError):
        result["port_8090_listening"] = False

    return result


# ── Report rendering ───────────────────────────────────────────────────────


def _icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _icon_warn(value, warn_on=None) -> str:
    if value == warn_on:
        return "⚠️"
    if value is None or value == "missing":
        return "❌"
    return "✅"


def render_text(report: dict) -> str:
    lines = []
    lines.append("QuantCairn Health Report")
    lines.append("=" * 60)

    # Scheduler
    s = report["scheduler"]
    lines.append(f"\nScheduler:")
    lines.append(f"  {_icon(s['active'])} active: {s['active']}")
    if s["last_decision"]:
        lines.append(f"  last decision: {s['last_decision']}")
        lines.append(f"  reason: {s['last_decision_reason']}")
        lines.append(f"  at: {s['last_decision_at']}")
    if s["decisions_today"]:
        lines.append(f"  today: {s['today_runs']} run(s), {s['today_skipped_count']} skipped")

    # AI Selector
    a = report["ai_selector"]
    lines.append(f"\nAI Selector:")
    lines.append(f"  {_icon(a['last_run_date'] is not None)} last run: {a['last_run_date'] or 'never'}")
    lines.append(f"  last selection date: {a['last_selection_date'] or 'none'}")
    if a["last_selection_symbols"]:
        lines.append(f"  last symbols: {', '.join(a['last_selection_symbols'])}")
    lines.append(f"  last status: {a['last_run_status'] or 'unknown'}")

    # Market
    m = report["market"]
    lines.append(f"\nMarket (US Eastern):")
    if "error" in m:
        lines.append(f"  ❌ error: {m['error']}")
    else:
        lines.append(f"  session: {m['session_label']}")
        lines.append(f"  {_icon(m['market_open'])} market open: {m['market_open']}")
        lines.append(f"  holiday: {m['is_market_holiday']}")
        lines.append(f"  current session: {m['current_session_date']}")
        lines.append(f"  previous completed: {m['previous_completed_session']}")

    # Execution Mode
    e = report["execution_mode"]
    lines.append(f"\nExecution Mode:")
    lines.append(f"  mode: {e['execution_mode']}")
    gate = e["live_order_gate"]
    lines.append(f"  live trading: {gate['effective_live_trading']}")
    lines.append(f"  allow_live_order env: {gate['allow_live_order_env']}")

    # Notifier
    n = report["notifier"]
    lines.append(f"\nNotifier:")
    lines.append(f"  {_icon_warn(n['dedup_state'], 'missing')} dedup state: {n['dedup_state']}")
    lines.append(f"  records: {n['records']} (paper: {n['paper_records']}, live: {n['live_records']})")
    lines.append(f"  last updated: {n['last_updated'] or 'never'}")

    # Paper Portfolio
    p = report["paper_portfolio"]
    lines.append(f"\nPaper Portfolio:")
    lines.append(f"  {_icon_warn(p['state'], 'missing')} state: {p['state']}")
    if p["cash"] is not None:
        lines.append(f"  cash: ${p['cash']:,.2f}")
        lines.append(f"  equity: ${p['equity']:,.2f}")
        lines.append(f"  positions: {p['positions']}")
        if p["position_symbols"]:
            lines.append(f"  symbols: {', '.join(p['position_symbols'])}")
        lines.append(f"  trades: {p['total_trades']}")

    # Live Trading
    liveness = "DISABLED" if gate["effective_live_trading"] != "⚠️  ENABLED — VERIFY SAFETY" else gate["effective_live_trading"]
    lines.append(f"\nLive Trading:")
    lines.append(f"  🔒 {liveness}")

    # Processes
    proc = report["processes"]
    lines.append(f"\nProcesses:")
    lines.append(f"  combined dashboard: {_icon(proc['combined_dashboard'])}")
    lines.append(f"  port 8090: {_icon(proc.get('port_8090_listening', False))}")
    if proc["processes"]:
        lines.append(f"  running:")
        for ps_line in proc["processes"]:
            lines.append(f"    {ps_line[:120]}")

    # Env
    env = e["env_vars"]
    lines.append(f"\nEnvironment:")
    for key, val in sorted(env.items()):
        lines.append(f"  {key}: {val}")

    lines.append(f"\n{'=' * 60}")
    lines.append(f"Report generated: {datetime.now().isoformat()}")
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────


def generate_report(project_dir: Path | None = None) -> dict:
    return {
        "scheduler": _check_scheduler(project_dir),
        "ai_selector": _check_ai_selector(project_dir),
        "market": _check_market(),
        "execution_mode": _check_execution_mode(project_dir),
        "notifier": _check_notifier(project_dir),
        "paper_portfolio": _check_paper_portfolio(project_dir),
        "processes": _check_processes(),
    }


def main() -> int:
    report = generate_report()

    if "--json" in sys.argv:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
