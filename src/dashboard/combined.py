"""Combined dashboard aggregating the selected TOP3 trading engines."""
import atexit
import csv
import inspect
import json, os, signal, subprocess, threading, urllib.request
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, redirect, render_template_string, request, send_file
import yaml
import re
from zoneinfo import ZoneInfo

from src.ai_selector.config import load_runtime_config
from src.ai_selector.selection_bundle import load_committed_selection_bundle
from src.ai_selector.selection_report import load_latest_ai_selection_state, normalize_provider_audit
from src.ai_selector.settings import load_runtime_settings, save_runtime_settings, resolve_price_band
from src.ai_selector.universe_filter import load_universe_rules
from src.ai_selector.selection_state import configured_top_count, current_top_config_disabled_slots, current_top_config_symbols, has_live_top_configs, load_selection_state, verify_selection_state
from src.config.loader import load_config
from src.config.runtime_values import get_runtime_env, has_longbridge_runtime_credentials
from src.broker.paper_broker import PaperBroker
from src.reports import daily_report as daily_report_module
from src.reports.trade_audit import latest_trade_activity_day, latest_trade_log_day, load_trade_records, summarize_trade_log
from src.research_report.site import build_research_site
from src.candidate_validation.research_report import CandidateDailyResearchReportGenerator
from src.candidate_validation.research_scheduler import latest_research_status
from src.safety.trading_environment_guard import TradingEnvironmentGuard
from src.notifier.alerts import build_provider_audit_sections, build_research_admission_notice
from src.shadow.config import ShadowRuntimeConfig
from src.shadow.universe import default_shadow_output_directory, is_safe_shadow_output_directory, shadow_title_for
from src.candidate_validation import CandidatePerformanceTracker, CandidateValidationStore, ValidationStatus, load_candidate_model_evaluation_snapshot
from src.utils.market_calendar import market_session_context, required_selection_date

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))
TRADING_FLAGS_PATH = STATE_DIR / "trading_flags.json"
LIFECYCLE_DIR = STATE_DIR / "lifecycle"
WEEKEND_PAPER_LIFECYCLE_PATH = LIFECYCLE_DIR / "weekend_paper_lifecycle.json"
LONGBRIDGE_SANDBOX_LIFECYCLE_PATH = LIFECYCLE_DIR / "longbridge_sandbox_lifecycle.json"
RUNTIME_DIR = Path(os.environ.get("SOXS_RUNTIME_DIR", "").strip() or (PROJECT_DIR / "runtime"))
COMBINED_PID_FILE = RUNTIME_DIR / "combined.pid"
SHADOW_OBSERVER_DIR = default_shadow_output_directory("SOXS.US", "15m")

TICKERS = [
    {"name": "TOP1", "desc": "AI优选第1名",    "port": 8091, "config": "TOP1.yaml"},
    {"name": "TOP2", "desc": "AI优选第2名",    "port": 8092, "config": "TOP2.yaml"},
    {"name": "TOP3", "desc": "AI优选第3名",    "port": 8093, "config": "TOP3.yaml"},
]

IGNORED_AUDIT_ACTIONS = {"get_account", "get_positions", "get_realtime_quote"}
_LIVE_ACCOUNT_CACHE = None
_LIVE_ACCOUNT_CACHE_AT = 0.0
_LIVE_ACCOUNT_CACHE_TTL = float(os.getenv("SOXS_LIVE_ACCOUNT_CACHE_TTL", "60"))
_LIVE_ACCOUNT_LOCK = threading.Lock()
_STATUS_CACHE: dict[int, dict] = {}
_STATUS_FAILURES: dict[int, int] = {}
_STATUS_OFFLINE_THRESHOLD = 3
_UNRESOLVED_ALERT_SECONDS = float(os.getenv("SOXS_UNRESOLVED_ALERT_SECONDS", "120"))
_COMBINED_PORT = 8090
_SYNTHETIC_TEST_TICKERS = {"TEST", "MOCK", "FAKE"}
_CHART_CACHE_LOCK = threading.Lock()
_CHART_PRICE_HISTORY: dict[str, list[dict[str, object]]] = {}
_CHART_HISTORY_LIMIT = 300
_CHART_TZ = ZoneInfo("Asia/Shanghai")


def _env(name: str, default: str = "") -> str:
    return get_runtime_env(name, default)


def _combined_pid_file_path() -> Path:
    return COMBINED_PID_FILE


def _read_pid_file() -> int | None:
    path = _combined_pid_file_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _write_pid_file(pid: int) -> None:
    path = _combined_pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)), encoding="utf-8")


def _remove_pid_file() -> None:
    try:
        _combined_pid_file_path().unlink(missing_ok=True)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _is_project_combined_command(command: str) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    markers = (
        "scripts/start_combined.py",
        "src.dashboard.combined",
        "start_combined(8090)",
        "start_combined.py",
    )
    return any(marker in text for marker in markers)


def _port_listeners(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-tiTCP:%s" % port, "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids: list[int] = []
        for token in (result.stdout or "").split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        return pids
    except Exception:
        return []


def _wait_for_port_free(port: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not _port_listeners(port):
            return True
        time.sleep(0.2)
    return not _port_listeners(port)


def _stop_project_combined_process(pid: int, *, force: bool = False) -> bool:
    if pid <= 0 or not _pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    if _wait_for_pid_exit(pid, timeout=2.0):
        return True
    if force:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return False
        return _wait_for_pid_exit(pid, timeout=1.5)
    return False


def _wait_for_pid_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _cleanup_orphan_pid_file() -> tuple[bool, int | None, str]:
    """
    Returns (cleaned_previous_process, pid, reason).
    """
    pid = _read_pid_file()
    if pid is None:
        _remove_pid_file()
        return False, None, "no_pid_file"
    if not _pid_alive(pid):
        _remove_pid_file()
        return True, pid, "stale_pid_removed"
    command = _process_command(pid)
    if _is_project_combined_command(command):
        return False, pid, "existing_project_process"
    return False, pid, "non_project_process"


def _ensure_single_instance(port: int) -> tuple[bool, dict]:
    """
    Ensure the combined dashboard is not started twice.

    Returns (allowed_to_start, metadata).
    """
    metadata: dict = {
        "port": port,
        "pid": os.getpid(),
        "pid_file": str(_combined_pid_file_path()),
        "previous_process_was_cleaned": False,
        "start_success": False,
        "reason": "",
    }

    cleaned_previous_process, pid_from_file, pid_reason = _cleanup_orphan_pid_file()
    metadata["previous_process_was_cleaned"] = cleaned_previous_process
    metadata["existing_pid_file_pid"] = pid_from_file
    metadata["existing_pid_file_reason"] = pid_reason

    listeners = _port_listeners(port)
    if listeners:
        same_project_pids = []
        foreign_pids = []
        for pid in listeners:
            command = _process_command(pid)
            if _is_project_combined_command(command):
                same_project_pids.append(pid)
            else:
                foreign_pids.append(pid)
        metadata["port_listeners"] = listeners
        metadata["same_project_pids"] = same_project_pids
        metadata["foreign_pids"] = foreign_pids
        if foreign_pids:
            metadata["reason"] = "port_occupied_by_non_project_process"
            return False, metadata
        if same_project_pids:
            # If another project instance already runs, do not duplicate.
            if os.getpid() not in same_project_pids:
                metadata["reason"] = "project_combined_already_running"
                return False, metadata

    if pid_from_file and _pid_alive(pid_from_file):
        command = _process_command(pid_from_file)
        if _is_project_combined_command(command) and pid_from_file != os.getpid():
            metadata["reason"] = "project_pid_file_still_active"
            return False, metadata

    _write_pid_file(os.getpid())
    atexit.register(_remove_pid_file)
    metadata["start_success"] = True
    metadata["reason"] = "start_allowed"
    return True, metadata


def _shutdown_single_instance():
    _remove_pid_file()


def _has_live_account_env() -> bool:
    return has_longbridge_runtime_credentials()


def _load_cached_longbridge_account_summary(mode_hint: str | None = None) -> dict | None:
    cache_path = STATE_DIR / "broker_cache" / "longbridge_account.json"
    try:
        if not cache_path.exists():
            return None
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        payload = cached.get("payload") if isinstance(cached, dict) else None
        if not isinstance(payload, dict):
            return None
        summary = dict(payload)
        mode_value = str(mode_hint or "").strip().lower()
        if mode_value not in {"sandbox", "live"}:
            mode_value = "sandbox"
        summary.setdefault("mode", mode_value)
        summary.setdefault("environment", mode_value)
        summary.setdefault("data_stale", False)
        summary.setdefault("account_error", False)
        summary.setdefault("stale_reason", "")
        summary.setdefault("fetched_at", cached.get("fetched_at"))
        return summary
    except Exception:
        return None


def _fetch_live_account_summary():
    """Read live buying power from LongBridge if credentials are present."""
    global _LIVE_ACCOUNT_CACHE, _LIVE_ACCOUNT_CACHE_AT
    now = time.time()
    with _LIVE_ACCOUNT_LOCK:
        if _LIVE_ACCOUNT_CACHE and (now - _LIVE_ACCOUNT_CACHE_AT) < _LIVE_ACCOUNT_CACHE_TTL:
            return _LIVE_ACCOUNT_CACHE
        dashboard_config = _load_dashboard_config()
        dashboard_mode = str(getattr(dashboard_config, "mode", "") or "").strip().lower() if dashboard_config else ""
        if dashboard_mode == "sandbox":
            cached_summary = _load_cached_longbridge_account_summary(dashboard_mode)
            if cached_summary is not None:
                _LIVE_ACCOUNT_CACHE = cached_summary
                _LIVE_ACCOUNT_CACHE_AT = now
                return cached_summary
        if not _has_live_account_env():
            cached_summary = _load_cached_longbridge_account_summary(dashboard_mode)
            if cached_summary is not None:
                _LIVE_ACCOUNT_CACHE = cached_summary
                _LIVE_ACCOUNT_CACHE_AT = now
                return cached_summary
            return None
        return _refresh_live_account_summary(now)


def _refresh_live_account_summary(now: float):
    """Refresh the account once while the caller holds the process lock."""
    global _LIVE_ACCOUNT_CACHE, _LIVE_ACCOUNT_CACHE_AT
    try:
        from src.broker.longbridge_broker import LongBridgeBroker
    except Exception:
        return None
    summary = None
    broker = None
    try:
        broker = LongBridgeBroker(
            app_key=_env("LONGBRIDGE_APP_KEY") or _env("LONGBRIDGE_API_KEY"),
            app_secret=_env("LONGBRIDGE_APP_SECRET") or _env("LONGBRIDGE_API_SECRET"),
            access_token=_env("LONGBRIDGE_ACCESS_TOKEN"),
            region=_env("LONGBRIDGE_REGION", "cn"),
            environment=_env("LONGBRIDGE_ENV", "prod"),
            http_url=_env("LONGBRIDGE_HTTP_URL") or _env("LONGBRIDGE_BASE_URL"),
            quote_ws_url=_env("LONGBRIDGE_QUOTE_WS_URL"),
            trade_ws_url=_env("LONGBRIDGE_TRADE_WS_URL"),
            log_path=_env("LONGBRIDGE_LOG_PATH"),
        )
        if not broker.connect():
            return _stale_live_account(
                getattr(broker, "last_connect_error", lambda: "")() or "券商连接失败"
            )
        positions = broker.get_positions()
        if not getattr(
            broker, "is_positions_snapshot_reliable", lambda: True
        )():
            return _stale_live_account(
                getattr(broker, "last_positions_error", lambda: "")() or "券商持仓快照未确认"
            )
        account = broker.get_account()
        if not getattr(
            broker, "is_account_snapshot_reliable", lambda: True
        )():
            return _stale_live_account(
                getattr(broker, "last_account_error", lambda: "")() or "券商账户快照未确认"
            )
        position_rows = []
        for pos in positions or []:
            position_rows.append(
                {
                    "ticker": str(getattr(pos, "ticker", "") or ""),
                    "quantity": int(getattr(pos, "quantity", 0) or 0),
                    "avg_entry_price": float(getattr(pos, "avg_entry_price", 0.0) or 0.0),
                    "current_price": float(getattr(pos, "current_price", 0.0) or 0.0),
                    "market_value": float(getattr(pos, "market_value", 0.0) or 0.0),
                    "unrealized_pnl": float(getattr(pos, "unrealized_pnl", 0.0) or 0.0),
                    "unrealized_pnl_pct": float(getattr(pos, "unrealized_pnl_pct", 0.0) or 0.0),
                }
            )
        environment = str(_env("LONGBRIDGE_ENV", "prod") or "prod").strip().lower()
        summary = {
            "cash": float(getattr(account, "cash", 0.0) or 0.0),
            "equity": float(getattr(account, "equity", 0.0) or 0.0),
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0),
            "positions_count": len(positions or []),
            "positions": position_rows,
            "mode": "sandbox" if environment == "sandbox" else "live",
            "environment": environment,
            "data_stale": False,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:
        return _stale_live_account(str(exc))
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                pass
    if summary is not None:
        _LIVE_ACCOUNT_CACHE = summary
        _LIVE_ACCOUNT_CACHE_AT = now
    return summary


def _friendly_live_account_error(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return "账户刷新失败"
    lowered = text.lower()
    if "401004" in text or "token invalid" in lowered:
        return "凭证无效，请更新 LongBridge Access Token"
    if "connect" in lowered:
        return "券商连接失败"
    return text


def _failed_live_account(reason: str):
    cached = _LIVE_ACCOUNT_CACHE if isinstance(_LIVE_ACCOUNT_CACHE, dict) else {}
    return {
        "cash": cached.get("cash"),
        "equity": cached.get("equity"),
        "buying_power": cached.get("buying_power"),
        "positions_count": cached.get("positions_count", 0) if cached else 0,
        "positions": list(cached.get("positions") or []),
        "mode": "live_error",
        "data_stale": True,
        "account_error": True,
        "stale_reason": _friendly_live_account_error(reason),
        "fetched_at": cached.get("fetched_at"),
    }


def _stale_live_account(reason: str):
    if not isinstance(_LIVE_ACCOUNT_CACHE, dict):
        return _failed_live_account(reason)
    return {
        **_LIVE_ACCOUNT_CACHE,
        "data_stale": True,
        "account_error": True,
        "stale_reason": _friendly_live_account_error(reason),
    }


def _position_lookup(live_account: dict | None) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    if not isinstance(live_account, dict):
        return lookup
    for pos in live_account.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "").strip().upper()
        if ticker:
            lookup[ticker] = pos
    return lookup


def _filter_live_positions(live_account: dict | None, allowed_tickers: set[str]) -> dict | None:
    if not isinstance(live_account, dict):
        return live_account
    if not allowed_tickers:
        return live_account
    filtered_positions = []
    for pos in live_account.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "").strip().upper()
        if ticker and ticker in allowed_tickers:
            filtered_positions.append(pos)
    return {
        **live_account,
        "positions": filtered_positions,
        "positions_count": len(filtered_positions),
    }


def _selected_stock_positions_count(live_account: dict | None, selected_tickers: set[str]) -> int:
    if not isinstance(live_account, dict) or not selected_tickers:
        return 0
    count = 0
    for pos in live_account.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "").strip().upper()
        if ticker and ticker in selected_tickers:
            count += 1
    return count


def _paper_account_summary_from_cards(cards: list[dict]) -> dict[str, object]:
    cash = round(sum(float(card.get("cash", 0.0) or 0.0) for card in cards), 2)
    equity = round(sum(float(card.get("equity", 0.0) or 0.0) for card in cards), 2)
    position_rows: list[dict[str, object]] = []
    for card in cards:
        shares = int(card.get("shares", 0) or 0)
        if shares <= 0:
            continue
        current_price = float(card.get("price", 0.0) or 0.0)
        pnl = float(card.get("pnl", 0.0) or 0.0)
        pnl_pct = float(card.get("pnl_pct", 0.0) or 0.0)
        market_value = round(shares * current_price, 2)
        avg_entry_price = float(card.get("avg_entry_price", 0.0) or 0.0)
        if avg_entry_price <= 0 and shares > 0 and current_price > 0:
            avg_entry_price = max(0.0, current_price - (pnl / shares))
        position_rows.append(
            {
                "ticker": str(card.get("ticker") or "").strip().upper(),
                "quantity": shares,
                "avg_entry_price": avg_entry_price,
                "current_price": current_price,
                "market_value": market_value,
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": pnl_pct,
            }
        )
    positions_count = len(position_rows)
    return {
        "cash": cash,
        "equity": equity,
        "buying_power": cash,
        "positions_count": positions_count,
        "positions": position_rows,
        "mode": "paper",
        "data_stale": False,
        "account_error": False,
    }


def _normalize_symbol_list(values) -> list[str]:
    symbols: list[str] = []
    for item in values or []:
        raw = str(item or "").strip().upper().split(".")[0]
        if raw:
            symbols.append(raw)
    seen: set[str] = set()
    result: list[str] = []
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def _load_orphan_monitor_symbols() -> list[str]:
    try:
        day = latest_trade_activity_day(PROJECT_DIR / "logs", mode=_desired_audit_mode()) or latest_trade_log_day(PROJECT_DIR / "logs")
        if not day:
            return []
        records = load_trade_records(PROJECT_DIR / "logs", day=day)
    except Exception:
        return []

    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        phase = str(record.get("phase") or "").strip()
        if phase not in {"orphan_assignment_change", "orphan_position_scan"}:
            continue
        raw_symbols = record.get("symbols") or []
        symbols: list[str] = []
        if phase == "orphan_assignment_change":
            if isinstance(raw_symbols, list):
                symbols = _normalize_symbol_list(raw_symbols)
        else:
            if isinstance(raw_symbols, list):
                for item in raw_symbols:
                    if isinstance(item, dict):
                        symbols.append(str(item.get("symbol") or item.get("ticker") or "").strip().upper().split(".")[0])
        symbols = [symbol for symbol in symbols if symbol]
        if symbols:
            return _normalize_symbol_list(symbols)
    return []


def _dashboard_active_symbols(ai_selection: dict | None, selection_sync: dict | None, live_account: dict | None) -> list[str]:
    symbols: list[str] = []
    selection_state_symbols = _normalize_symbol_list((selection_sync or {}).get("selection_state_symbols") or [])
    if selection_state_symbols:
        symbols.extend(selection_state_symbols)
    else:
        symbols.extend(_normalize_symbol_list((selection_sync or {}).get("current_top_config_symbols") or []))
    symbols.extend(_normalize_symbol_list(item.get("ticker") for item in (ai_selection or {}).get("top3") or [] if isinstance(item, dict)))
    symbols.extend(_normalize_symbol_list(item.get("ticker") for item in (ai_selection or {}).get("protected_positions") or [] if isinstance(item, dict)))
    symbols.extend(_normalize_symbol_list(item.get("ticker") for item in (live_account or {}).get("positions") or [] if isinstance(item, dict)))
    symbols.extend(_load_orphan_monitor_symbols())
    return _normalize_symbol_list(symbols)


def _load_ai_selection_report():
    return load_latest_ai_selection_state(PROJECT_DIR)


def _load_latest_research_digest() -> dict[str, object]:
    reports_dir = PROJECT_DIR / "reports" / "research"
    if not reports_dir.exists():
        return {"available": False}
    for path in sorted(reports_dir.glob("daily-paper-report-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        top_cards = data.get("top_cards") if isinstance(data.get("top_cards"), list) else []
        top_symbols = [
            str(item.get("ticker") or "").strip().upper().split(".")[0]
            for item in top_cards
            if isinstance(item, dict) and str(item.get("ticker") or "").strip()
        ]
        quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        strategy = data.get("strategy_review") if isinstance(data.get("strategy_review"), dict) else {}
        strategy_success_count = int(strategy.get("success_count", 0) or 0)
        strategy_observation_correct_count = int(strategy.get("observation_correct_count", 0) or 0)
        strategy_failure_count = int(strategy.get("failure_count", 0) or 0)
        return {
            "available": True,
            "date": str(data.get("date") or ""),
            "generated_at": str(data.get("generated_at") or ""),
            "mode": str(data.get("mode") or "unknown"),
            "top_line": " / ".join(top_symbols) if top_symbols else "暂无",
            "top_symbols": top_symbols,
            "entry_ready": int(quality.get("entry_ready_count", 0) or 0),
            "observation_only": int(quality.get("observation_only_count", 0) or 0),
            "strategy_summary": (
                f"成功 {strategy_success_count} / "
                f"观察正确 {strategy_observation_correct_count} / "
                f"失败 {strategy_failure_count}"
            ),
            "success_count": strategy_success_count,
            "observation_correct_count": strategy_observation_correct_count,
            "failure_count": strategy_failure_count,
            "research_url": "/research",
            "report_path": str(path),
        }
    return {"available": False}


def _dashboard_order_status_summary(active_orders_summary: dict | None) -> dict[str, int]:
    orders = (active_orders_summary or {}).get("orders") if isinstance(active_orders_summary, dict) else []
    orders = orders if isinstance(orders, list) else []
    counts = Counter(str((order or {}).get("status") or "").strip().upper() for order in orders if isinstance(order, dict))
    return {
        "total": len(orders),
        "pending": counts.get("PENDING", 0),
        "partial_filled": counts.get("PARTIAL_FILLED", 0),
        "filled": counts.get("FILLED", 0),
        "cancelled": counts.get("CANCELLED", 0),
        "rejected": counts.get("REJECTED", 0),
    }


def _dashboard_risk_summary(account_summary: dict | None, display_positions: list[dict] | None) -> dict[str, object]:
    positions = display_positions if isinstance(display_positions, list) else []
    equity = float((account_summary or {}).get("equity", 0.0) or 0.0)
    cash = float((account_summary or {}).get("cash", 0.0) or 0.0)
    buying_power = float((account_summary or {}).get("buying_power", cash) or cash or 0.0)
    total_market_value = 0.0
    largest_position = 0.0
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        market_value = float(pos.get("market_value", 0.0) or 0.0)
        total_market_value += market_value
        largest_position = max(largest_position, market_value)
    exposure_pct = (total_market_value / equity * 100.0) if equity > 0 else None
    cash_pct = (cash / equity * 100.0) if equity > 0 else None
    largest_pct = (largest_position / equity * 100.0) if equity > 0 else None
    if equity <= 0:
        risk_level = "UNKNOWN"
        risk_label = "无可用权益"
    elif largest_pct is not None and largest_pct >= 30:
        risk_level = "HIGH"
        risk_label = "高风险"
    elif largest_pct is not None and largest_pct >= 15:
        risk_level = "MEDIUM"
        risk_label = "中等风险"
    else:
        risk_level = "LOW"
        risk_label = "低风险"
    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "total_market_value": round(total_market_value, 2),
        "largest_position_value": round(largest_position, 2),
        "position_count": len([pos for pos in positions if isinstance(pos, dict) and int(pos.get("quantity", 0) or 0) > 0]),
        "exposure_pct": round(exposure_pct, 2) if exposure_pct is not None else None,
        "cash_pct": round(cash_pct, 2) if cash_pct is not None else None,
        "largest_pct": round(largest_pct, 2) if largest_pct is not None else None,
        "risk_level": risk_level,
        "risk_label": risk_label,
    }


def _dashboard_timeline_items(
    *,
    trade_audit: dict | None,
    system_status: dict | None,
    selection_sync: dict | None,
    ai_runtime: dict | None,
    research_digest: dict | None,
    startup_guard: dict | None,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if isinstance(trade_audit, dict):
        if trade_audit.get("latest_submitted_line"):
            items.append({
                "time": str(trade_audit.get("latest_submitted_at") or trade_audit.get("latest_submitted_line")[:16] or "now"),
                "title": "订单提交",
                "detail": str(trade_audit.get("latest_submitted_line")),
                "tone": "cyan",
            })
        if trade_audit.get("latest_filled_line"):
            items.append({
                "time": str(trade_audit.get("latest_filled_at") or trade_audit.get("latest_filled_line")[:16] or "now"),
                "title": "订单成交",
                "detail": str(trade_audit.get("latest_filled_line")),
                "tone": "green",
            })
        if trade_audit.get("latest_line"):
            items.append({
                "time": str(trade_audit.get("latest_submitted_at") or trade_audit.get("latest_filled_at") or "now"),
                "title": "最新审计",
                "detail": str(trade_audit.get("latest_line")),
                "tone": "purple",
            })
    if isinstance(selection_sync, dict) and selection_sync:
        items.append({
            "time": str(selection_sync.get("state_date") or selection_sync.get("required_date") or "now"),
            "title": "选股同步",
            "detail": str(selection_sync.get("detail") or selection_sync.get("label") or "unknown"),
            "tone": "yellow" if not selection_sync.get("ok") else "green",
        })
    if isinstance(startup_guard, dict) and startup_guard:
        items.append({
            "time": "startup",
            "title": "启动校验",
            "detail": str(startup_guard.get("detail") or startup_guard.get("label") or "unknown"),
            "tone": "red" if str(startup_guard.get("level") or "").lower() in {"blocked", "red"} else "cyan",
        })
    if isinstance(ai_runtime, dict) and ai_runtime:
        items.append({
            "time": "AI",
            "title": "AI 选股状态",
            "detail": f"{ai_runtime.get('label') or 'unknown'} · {ai_runtime.get('detail') or ''}".strip(" ·"),
            "tone": "green" if str(ai_runtime.get("level") or "").lower() in {"green", "live"} else "yellow",
        })
    if isinstance(system_status, dict) and system_status:
        lifecycle = system_status.get("lifecycle") if isinstance(system_status.get("lifecycle"), dict) else {}
        for name, label in (
            ("weekend_paper", "Weekend paper lifecycle"),
            ("longbridge_sandbox", "LongBridge sandbox lifecycle"),
        ):
            report = lifecycle.get(name) if isinstance(lifecycle, dict) else {}
            if isinstance(report, dict):
                items.append({
                    "time": str(report.get("generated_at") or "no data"),
                    "title": label,
                    "detail": f"{report.get('status_label') or 'unavailable'} · {report.get('detail') or 'no data'}",
                    "tone": "green" if str(report.get("status_label") or "").upper() == "PASS" else "yellow" if str(report.get("status_label") or "").lower() == "unavailable" else "red",
                })
    if isinstance(research_digest, dict) and research_digest.get("available"):
        items.append({
            "time": str(research_digest.get("generated_at") or research_digest.get("date") or "now"),
            "title": "策略评分复盘",
            "detail": str(research_digest.get("strategy_summary") or "暂无"),
            "tone": "purple",
        })
    return items[:8]


def _ai_selection_price_band(ai_selection: dict | None) -> dict[str, float | bool]:
    settings = (ai_selection or {}).get("settings") if isinstance(ai_selection, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    has_explicit_band = any(
        key in settings
        for key in ("min_price", "max_price", "price_band")
    )
    band_source = settings
    if isinstance(settings.get("price_band"), dict):
        band_source = {
            "min_price": settings["price_band"].get("min"),
            "max_price": settings["price_band"].get("max"),
        }
    min_price, max_price = resolve_price_band(band_source)
    return {
        "min": float(min_price),
        "max": float(max_price),
        "defaulted": not has_explicit_band,
    }


def _ai_universe_filter_summary(ai_selection: dict | None = None) -> dict[str, object]:
    settings = (ai_selection or {}).get("settings") if isinstance(ai_selection, dict) else {}
    configured = settings.get("universe_filter") if isinstance(settings, dict) else None
    rules = {}
    if isinstance(configured, dict) and configured:
        for asset_type, raw in configured.items():
            if isinstance(raw, dict):
                rules[str(asset_type)] = {
                    "price_min": raw.get("price_min"),
                    "price_max": raw.get("price_max"),
                    "min_average_dollar_volume": raw.get("min_average_dollar_volume"),
                    "min_market_cap": raw.get("min_market_cap"),
                    "atr_20_pct_min": raw.get("atr_20_pct_min"),
                    "atr_20_pct_max": raw.get("atr_20_pct_max"),
                }
    else:
        rules = {
            asset_type: {
                "price_min": rule.price_min,
                "price_max": rule.price_max,
                "min_average_dollar_volume": rule.min_average_dollar_volume,
                "min_market_cap": rule.min_market_cap,
                "atr_20_pct_min": rule.atr_20_pct_min,
                "atr_20_pct_max": rule.atr_20_pct_max,
            }
            for asset_type, rule in load_universe_rules().items()
        }
    return {
        "rules": rules,
        "source": "report" if isinstance(configured, dict) and configured else "config/universe.yaml",
        "summary": (
            "普通股 $5-$200 / ETF $5-$300 / 杠杆与反向ETF $5-$100；"
            "先检查20日成交额，再检查价格、市值和ATR波动率。"
        ),
    }


def _ai_selection_rejection_reason_counts(ai_selection: dict | None = None) -> dict[str, int]:
    report = ai_selection if isinstance(ai_selection, dict) else {}
    counts: dict[str, int] = {}
    if isinstance(report.get("rejection_reason_counts"), dict) and report.get("rejection_reason_counts"):
        for key, value in report.get("rejection_reason_counts", {}).items():
            try:
                counts[str(key)] = int(value or 0)
            except Exception:
                continue
        return counts

    trace = report.get("rejection_trace") if isinstance(report.get("rejection_trace"), list) else []
    for item in trace or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("reason_code") or item.get("warning_code") or "unknown").strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        warnings = report.get("warnings_structured") if isinstance(report.get("warnings_structured"), list) else []
        for item in warnings or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("warning_code") or item.get("code") or "unknown").strip() or "unknown"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _resolve_dashboard_config_path() -> Path | None:
    explicit = str(_env("SOXS_CONFIG", "") or "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            PROJECT_DIR / "config.local.yaml",
            PROJECT_DIR / "config.yaml",
            PROJECT_DIR / "config.sample.yaml",
        ]
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


def _load_dashboard_config():
    config_path = _resolve_dashboard_config_path()
    if config_path is None:
        return None
    try:
        return load_config(str(config_path))
    except Exception:
        return None


def _mode_label(mode: str | None) -> str:
    value = str(mode or "").strip().lower()
    return {
        "paper": "PAPER",
        "sandbox": "SANDBOX",
        "live": "PROD",
        "backtest": "BACKTEST",
    }.get(value, "UNKNOWN")


def _market_status_snapshot(now_et: datetime | None = None) -> dict[str, object]:
    try:
        now_et = now_et or datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.utcnow()
    session = market_session_context(now_et)
    open_now = bool(session.is_regular_session)
    return {
        "open": open_now,
        "label": "开盘中" if open_now else "已收盘",
        "detail": "US market open" if open_now else "US market closed",
        "timestamp": session.now_et.isoformat(),
        "session_label": session.session_label,
        "current_session": session.current_session.isoformat(),
        "previous_completed_session": session.previous_completed_session.isoformat(),
        "next_session": session.next_session.isoformat(),
        "is_market_holiday": session.is_market_holiday,
        "is_premarket": session.is_premarket,
        "is_regular_session": session.is_regular_session,
        "is_after_hours": session.is_after_hours,
    }


def _read_json_file(path: Path) -> dict[str, object] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_csv_file(path: Path) -> list[dict[str, object]] | None:
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = []
            for row in reader:
                if isinstance(row, dict):
                    rows.append({str(k): v for k, v in row.items()})
            return rows
    except Exception:
        return None


def _load_shadow_json_artifact(path: Path) -> tuple[str, dict[str, object] | None]:
    if not path.exists():
        return "missing", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ("ok", data) if isinstance(data, dict) else ("invalid", None)
    except Exception:
        return "invalid", None


def _load_shadow_csv_artifact(path: Path) -> tuple[str, list[dict[str, object]] | None]:
    if not path.exists():
        return "missing", None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = []
            for row in reader:
                if isinstance(row, dict):
                    rows.append({str(k): v for k, v in row.items()})
        return "ok", rows
    except Exception:
        return "invalid", None


def _shadow_artifact_path(name: str) -> Path:
    return _shadow_artifact_root() / name


def _candidate_artifact_root() -> Path:
    return PROJECT_DIR / "artifacts" / "candidates"


def _candidate_artifact_path(name: str) -> Path:
    return _candidate_artifact_root() / name


def _research_artifact_root() -> Path:
    return PROJECT_DIR / "artifacts" / "research" / "daily"


def _research_status_snapshot() -> dict[str, object]:
    try:
        return latest_research_status(project_dir=PROJECT_DIR, research_root=_research_artifact_root())
    except Exception as exc:
        return {
            "available": False,
            "state": "STALE",
            "status_label": "unavailable",
            "detail": "research run unavailable",
            "last_research_run": None,
            "research_date": None,
            "candidate_count": 0,
            "report_status": "unavailable",
            "output_dir": "artifacts/research/daily",
            "audit_path": None,
            "report_path": None,
            "error": str(exc),
        }


def _candidate_performance_snapshot() -> dict[str, object]:
    root = _candidate_artifact_root()
    tracker = CandidatePerformanceTracker(root)
    try:
        records = tracker.load_records()
        if not records:
            candidates = CandidateValidationStore(root).load_latest_candidates()
            if candidates:
                return tracker.analyze(candidates)
        return tracker.analyze(CandidateValidationStore(root).load_latest_candidates() or [])
    except Exception as exc:
        return {
            "available": False,
            "state": "STALE",
            "status_label": "STALE",
            "detail": "candidate performance unavailable",
            "title": "Candidate Ranking Performance",
            "candidate_count": 0,
            "average_score": None,
            "high_score_threshold": 80.0,
            "high_score_candidate_count": 0,
            "high_score_success_rate": None,
            "score_bucket_distribution": [],
            "performance_rows": [],
            "last_updated": None,
            "error": str(exc),
        }


def _candidate_research_report_snapshot() -> dict[str, object]:
    try:
        report = CandidateDailyResearchReportGenerator(
            root_dir=PROJECT_DIR / "artifacts" / "research" / "daily",
            candidate_root=_candidate_artifact_root(),
        ).build()
    except Exception as exc:
        return {
            "available": False,
            "state": "STALE",
            "status_label": "STALE",
            "detail": "research report unavailable",
            "title": "AI Candidate Daily Research Report",
            "display_title": "AI Research Report",
            "generated_at": None,
            "candidate_count": 0,
            "average_score": None,
            "score_distribution": [],
            "top_candidates": [],
            "failure_analysis": {"statuses": {"DATA_INVALID": 0, "BACKTEST_FAILED": 0, "WALK_FORWARD_FAILED": 0}},
            "market_regime": {},
            "strategy_selection": {},
            "candidate_strategy_matrix": [],
            "portfolio_composition": {},
            "final_selected": [],
            "final_selected_count": 0,
            "selection_outcome": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
            "actionable_candidate_status": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
            "error": str(exc),
        }
    performance = report.get("performance") or {}
    return {
        "available": True,
        "state": "SAFE",
        "status_label": "SAFE",
        "detail": "daily research report ready",
        "title": report.get("title") or "AI Candidate Daily Research Report",
        "display_title": "AI Research Report",
        "generated_at": report.get("generated_at"),
        "candidate_count": report.get("candidate_count", 0),
        "average_score": report.get("average_score"),
        "score_distribution": report.get("score_distribution") or [],
        "top_candidates": report.get("top_candidates") or [],
        "failure_analysis": report.get("failure_analysis") or {"statuses": {}},
        "market_regime": report.get("market_regime") or {},
        "strategy_selection": report.get("strategy_selection") or {},
        "candidate_strategy_matrix": list(report.get("candidate_strategy_matrix") or []),
        "portfolio_composition": dict(report.get("portfolio_composition") or {}),
        "final_selected": list(report.get("final_selected") or []),
        "final_selected_count": int(report.get("final_selected_count") or 0),
        "selection_outcome": report.get("selection_outcome") or "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "actionable_candidate_status": report.get("actionable_candidate_status") or "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "selection_execution_status": report.get("selection_execution_status") or "COMPLETED",
        "selection_result_quality": report.get("selection_result_quality") or "COMPLETE",
        "selection_research_admission": report.get("selection_research_admission") or "RESEARCH_READY",
        "selection_stage": report.get("selection_stage") or "FINALIZED",
        "selection_top_n_complete": bool(report.get("selection_top_n_complete", False)),
        "selection_top_n_missing_count": int(report.get("selection_top_n_missing_count") or 0),
        "selection_fallback_used": bool(report.get("selection_fallback_used", False)),
        "selection_provider_audit": report.get("selection_provider_audit") or {},
        "selection_provider_outputs": report.get("selection_provider_outputs") or {},
        "selection_warnings_structured": list(report.get("selection_warnings_structured") or []),
        "selection_warnings": list(report.get("selection_warnings") or []),
        "high_score_success_rate": performance.get("high_score_success_rate"),
        "high_score_threshold": performance.get("high_score_threshold", 80.0),
        "performance": performance,
    }


def _candidate_model_evaluation_snapshot() -> dict[str, object]:
    try:
        return load_candidate_model_evaluation_snapshot(
            candidate_root=_candidate_artifact_root(),
            backtest_root=PROJECT_DIR / "artifacts" / "backtests",
            model_root=PROJECT_DIR / "config" / "candidate_models",
        )
    except Exception as exc:
        return {
            "title": "Candidate Model Evaluation",
            "generated_at": None,
            "active_model_version": None,
            "challenger_version": None,
            "training_sample_count": 0,
            "training_period": {"start": None, "end": None},
            "baseline_version": "baseline_v1",
            "baseline_status": "ACTIVE",
            "challenger_status": "DRAFT",
            "approval_status": "DRAFT",
            "recommended_action": "collect_more_samples",
            "baseline_metrics": {},
            "challenger_metrics": {},
            "baseline_weights": {},
            "proposed_weights": {},
            "feature_importance": {},
            "confidence_interval": {},
            "calibration_curve": [],
            "calibration_error": None,
            "sample_size_warning": True,
            "overfitting_warning": True,
            "proxy_target_used": True,
            "warnings": [str(exc)],
            "comparison": {},
            "dataset": {"sample_count": 0, "training_period": {"start": None, "end": None}, "target_definition": "unavailable", "warnings": [str(exc)], "source_paths": {}},
            "model_governance": {},
            "active_model": None,
            "challenger_model": None,
        }


def _research_status_payload() -> dict[str, object]:
    return _research_status_snapshot() or {
        "available": False,
        "state": "STALE",
        "status_label": "unavailable",
        "detail": "research run unavailable",
        "last_research_run": None,
        "research_date": None,
        "candidate_count": 0,
        "report_status": "unavailable",
        "output_dir": "artifacts/research/daily",
        "audit_path": None,
        "report_path": None,
    }


def _candidate_validation_snapshot() -> dict[str, object]:
    root = _candidate_artifact_root()
    store = CandidateValidationStore(root)
    candidates_path = store.candidates_path
    history_path = store.history_path
    summary_path = store.summary_path
    if not any(path.exists() for path in (candidates_path, history_path, summary_path)):
        return {
            "available": False,
            "state": "STALE",
            "status_label": "STALE",
            "detail": "candidate validation data unavailable",
            "title": "AI Candidate Validation",
            "candidate_count": 0,
            "history_count": 0,
            "latest_candidate": {},
            "candidate_validation_rows": [],
            "performance": _candidate_performance_snapshot(),
            "research_report": _candidate_research_report_snapshot(),
            "last_updated": None,
            "status_issue": None,
            "validation_status": "AI_CANDIDATE",
        }
    try:
        candidates = store.load_latest_candidates()
        history = store.load_latest_history()
    except Exception as exc:
        return {
            "available": False,
            "state": "UNSAFE",
            "status_label": "UNSAFE",
            "detail": "data_invalid",
            "error": str(exc),
            "title": "AI Candidate Validation",
            "candidate_count": 0,
            "history_count": 0,
            "latest_candidate": {},
            "candidate_validation_rows": [],
            "performance": _candidate_performance_snapshot(),
            "research_report": _candidate_research_report_snapshot(),
            "last_updated": None,
            "status_issue": "data_invalid",
            "validation_status": "REJECTED",
        }

    if not candidates and not history:
        return {
            "available": False,
            "state": "STALE",
            "status_label": "STALE",
            "detail": "candidate validation data unavailable",
            "title": "AI Candidate Validation",
            "candidate_count": 0,
            "history_count": 0,
            "latest_candidate": {},
            "candidate_validation_rows": [],
            "performance": _candidate_performance_snapshot(),
            "research_report": _candidate_research_report_snapshot(),
            "last_updated": None,
            "status_issue": None,
            "validation_status": "AI_CANDIDATE",
        }

    latest = candidates[0] if candidates else None
    latest_dict = latest.to_dict() if latest is not None else {}
    latest_metadata = dict(latest_dict.get("metadata") or {}) if isinstance(latest_dict, dict) else {}
    candidate_stage = str(
        latest_dict.get("selection_stage")
        or latest_metadata.get("selection_stage")
        or latest_metadata.get("market_selection_stage")
        or ""
    ).strip().upper()
    freshness_status = str(
        latest_dict.get("freshness_status")
        or latest_metadata.get("freshness_status")
        or ""
    ).strip().upper()
    stale_reason = str(
        latest_dict.get("stale_reason")
        or latest_metadata.get("stale_reason")
        or ""
    ).strip()
    daily_data_status = str(
        latest_dict.get("daily_data_status")
        or latest_metadata.get("daily_data_status")
        or ""
    ).strip().upper()
    latest_updated = None
    if latest is not None:
        try:
            latest_updated = datetime.fromisoformat(str(latest.updated_at).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
        except Exception:
            latest_updated = str(latest.updated_at or "")
    invalid_issue = None
    for record in candidates:
        if record.validation_status not in {item.value for item in ValidationStatus}:
            invalid_issue = "invalid_validation_status"
            break
        if record.validation_status == ValidationStatus.REJECTED.value and not record.rejection_reason:
            invalid_issue = "rejection_reason_missing"
            break
        if any(bool(getattr(record, flag, False)) for flag in ("trading_enabled", "shadow_enabled", "paper_enabled", "live_enabled")):
            invalid_issue = "candidate_write_flag_enabled"
            break
        if record.asset_type and record.asset_type not in {"common_stock", "index_etf", "leveraged_etf", "inverse_etf"}:
            invalid_issue = "invalid_asset_type"
            break

    is_stale = False
    if latest is not None:
        freshness_inputs = {
            "freshness_status": freshness_status,
            "daily_data_status": daily_data_status,
            "selection_stage": candidate_stage,
            "stale_reason": stale_reason,
        }
        if freshness_inputs["freshness_status"] == "STALE":
            is_stale = True
        elif freshness_inputs["daily_data_status"] == "STALE":
            is_stale = True
        elif freshness_inputs["selection_stage"] == "STALE":
            is_stale = True
        elif freshness_inputs["selection_stage"] == "INVALID":
            invalid_issue = invalid_issue or "candidate_selection_invalid"
            is_stale = False
        elif not freshness_inputs["freshness_status"] and not freshness_inputs["daily_data_status"]:
            is_stale = True
    state = "UNSAFE" if invalid_issue else "STALE" if is_stale else "SAFE"
    detail = "data_invalid" if invalid_issue else ("candidate data stale" if is_stale else (latest.rejection_reason or "candidate validation ready" if latest else "candidate validation unavailable"))
    if latest is None:
        state = "STALE"
        detail = "candidate validation data unavailable"
    return {
        "available": True,
        "state": state,
        "status_label": state,
        "detail": detail,
        "title": "AI Candidate Validation",
        "candidate_count": len(candidates),
        "history_count": len(history),
        "latest_candidate": {
                **latest_dict,
                **{
                    "selection_stage": candidate_stage or latest_dict.get("validation_status") or "AI_CANDIDATE",
                    "freshness_status": freshness_status or "SAFE",
                "stale_reason": stale_reason,
                "daily_data_status": daily_data_status or "",
                "last_completed_session": latest_metadata.get("last_completed_session") or latest_dict.get("last_completed_session") or "",
                "daily_data_as_of": latest_metadata.get("daily_data_as_of") or latest_dict.get("daily_data_as_of") or "",
                "premarket_snapshot_at": latest_metadata.get("premarket_snapshot_at") or latest_dict.get("premarket_snapshot_at") or "",
                "current_session": latest_metadata.get("current_session") or latest_dict.get("current_session") or "",
                "previous_completed_session": latest_metadata.get("previous_completed_session") or latest_dict.get("previous_completed_session") or "",
                "next_session": latest_metadata.get("next_session") or latest_dict.get("next_session") or "",
                "is_market_holiday": bool(latest_metadata.get("is_market_holiday", latest_dict.get("is_market_holiday", False))),
                "is_premarket": bool(latest_metadata.get("is_premarket", latest_dict.get("is_premarket", False))),
                "is_regular_session": bool(latest_metadata.get("is_regular_session", latest_dict.get("is_regular_session", False))),
                "is_after_hours": bool(latest_metadata.get("is_after_hours", latest_dict.get("is_after_hours", False))),
                "trading_eligible": bool(latest_metadata.get("trading_eligible", latest_dict.get("trading_eligible", False))),
                "data_mode": latest_metadata.get("data_mode") or latest_dict.get("data_mode") or "",
                "data_freshness": latest_metadata.get("data_freshness") or latest_dict.get("data_freshness") or "",
                "data_status": latest_metadata.get("data_status") or latest_dict.get("data_status") or "",
                "scoring_eligible": bool(latest_metadata.get("scoring_eligible", latest_dict.get("scoring_eligible", False))),
                "scoring_block_reason": latest_metadata.get("scoring_block_reason") or latest_dict.get("scoring_block_reason") or "",
                "trade_filter_passed": bool(latest_metadata.get("trade_filter_passed", latest_dict.get("trade_filter_passed", latest_dict.get("scoring_eligible", False)))),
                "missing_fields": latest_metadata.get("missing_fields") or latest_dict.get("missing_fields") or [],
                "candidate_fallback": bool(latest_metadata.get("candidate_fallback", latest_dict.get("candidate_fallback", False))),
                "fallback_sources": latest_metadata.get("fallback_sources") or latest_dict.get("fallback_sources") or [],
                "mock_used": bool(latest_metadata.get("mock_used", latest_dict.get("mock_used", False))),
                "mock_sources": latest_metadata.get("mock_sources") or latest_dict.get("mock_sources") or [],
                "degraded": bool(latest_metadata.get("degraded", latest_dict.get("degraded", False))),
                "degradation_reasons": latest_metadata.get("degradation_reasons") or latest_dict.get("degradation_reasons") or [],
                "current_validation_status": latest_metadata.get("current_validation_status") or latest_dict.get("current_validation_status") or latest_dict.get("validation_status") or "AI_CANDIDATE",
                "trade_admission_status": latest_metadata.get("trade_admission_status") or latest_dict.get("trade_admission_status") or "NOT_TRADABLE",
            },
        } if latest is not None else {},
        "candidate_validation_rows": [record.to_dict() for record in candidates[:5]],
        "performance": _candidate_performance_snapshot(),
        "research_report": _candidate_research_report_snapshot(),
        "last_updated": latest_updated,
        "status_issue": invalid_issue,
        "validation_status": latest.validation_status if latest is not None else "AI_CANDIDATE",
        "selection_stage": candidate_stage or "PRELIMINARY",
        "freshness_status": freshness_status or "SAFE",
        "stale_reason": stale_reason,
        "last_completed_session": latest_metadata.get("last_completed_session") or "",
        "daily_data_as_of": latest_metadata.get("daily_data_as_of") or "",
        "premarket_snapshot_at": latest_metadata.get("premarket_snapshot_at") or "",
        "data_mode": latest_metadata.get("data_mode") or "",
        "data_freshness": latest_metadata.get("data_freshness") or "",
        "data_status": latest_metadata.get("data_status") or "",
        "scoring_eligible": bool(latest_metadata.get("scoring_eligible", False)),
        "scoring_block_reason": latest_metadata.get("scoring_block_reason") or "",
        "trade_filter_passed": bool(latest_metadata.get("trade_filter_passed", latest_dict.get("trade_filter_passed", False))),
        "missing_fields": latest_metadata.get("missing_fields") or [],
        "candidate_fallback": bool(latest_metadata.get("candidate_fallback", False)),
        "fallback_sources": latest_metadata.get("fallback_sources") or [],
        "mock_used": bool(latest_metadata.get("mock_used", False)),
        "mock_sources": latest_metadata.get("mock_sources") or [],
        "degraded": bool(latest_metadata.get("degraded", False)),
        "degradation_reasons": latest_metadata.get("degradation_reasons") or [],
        "current_validation_status": latest_metadata.get("current_validation_status") or latest_dict.get("validation_status") or "AI_CANDIDATE",
        "trade_admission_status": latest_metadata.get("trade_admission_status") or "NOT_TRADABLE",
    }


def _candidate_validation_payload() -> dict[str, object]:
    return _candidate_validation_snapshot()


def _candidate_performance_payload() -> dict[str, object]:
    return _candidate_performance_snapshot() or {
        "available": False,
        "state": "STALE",
        "status_label": "STALE",
        "detail": "candidate performance unavailable",
        "title": "Candidate Ranking Performance",
        "candidate_count": 0,
        "average_score": None,
        "high_score_threshold": 80.0,
        "high_score_candidate_count": 0,
        "high_score_success_rate": None,
        "score_bucket_distribution": [],
        "performance_rows": [],
        "last_updated": None,
    }


def _candidate_research_report_payload() -> dict[str, object]:
    return _candidate_research_report_snapshot() or {
        "available": False,
        "state": "STALE",
        "status_label": "STALE",
        "detail": "research report unavailable",
        "title": "AI Candidate Daily Research Report",
        "display_title": "AI Research Report",
        "generated_at": None,
        "candidate_count": 0,
        "average_score": None,
        "score_distribution": [],
        "top_candidates": [],
        "failure_analysis": {"statuses": {"DATA_INVALID": 0, "BACKTEST_FAILED": 0, "WALK_FORWARD_FAILED": 0}},
        "market_regime": {},
        "strategy_selection": {},
        "candidate_strategy_matrix": [],
        "portfolio_composition": {},
        "final_selected": [],
        "final_selected_count": 0,
        "selection_outcome": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "actionable_candidate_status": "NO_ACTIONABLE_RESEARCH_CANDIDATE",
    }


def _candidate_model_evaluation_payload() -> dict[str, object]:
    return _candidate_model_evaluation_snapshot() or {
        "title": "Candidate Model Evaluation",
        "generated_at": None,
        "active_model_version": None,
        "challenger_version": None,
        "training_sample_count": 0,
        "training_period": {"start": None, "end": None},
        "baseline_version": "baseline_v1",
        "baseline_status": "ACTIVE",
        "challenger_status": "DRAFT",
        "approval_status": "DRAFT",
        "recommended_action": "collect_more_samples",
        "baseline_metrics": {},
        "challenger_metrics": {},
        "baseline_weights": {},
        "proposed_weights": {},
        "feature_importance": {},
        "confidence_interval": {},
        "calibration_curve": [],
        "calibration_error": None,
        "sample_size_warning": True,
        "overfitting_warning": True,
        "proxy_target_used": True,
        "warnings": [],
        "comparison": {},
        "dataset": {"sample_count": 0, "training_period": {"start": None, "end": None}, "target_definition": "unavailable", "warnings": [], "source_paths": {}},
        "model_governance": {},
        "active_model": None,
        "challenger_model": None,
    }


def _shadow_artifact_root() -> Path:
    configured = str(_env("SOXS_SHADOW_OUTPUT_DIR", "") or "").strip()
    if configured:
        try:
            candidate = Path(configured)
            if is_safe_shadow_output_directory(candidate):
                return candidate
        except Exception:
            pass
    default_root = default_shadow_output_directory("SOXS.US", "15m")
    if SHADOW_OBSERVER_DIR != default_root:
        return SHADOW_OBSERVER_DIR
    try:
        runtime_config = ShadowRuntimeConfig.from_env()
        if isinstance(runtime_config.output_dir, Path) and runtime_config.output_dir != default_root and is_safe_shadow_output_directory(runtime_config.output_dir):
            return runtime_config.output_dir
    except Exception:
        pass
    return SHADOW_OBSERVER_DIR


def _shadow_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC"))


def _shadow_rows_to_int(rows: list[dict[str, object]] | None) -> int:
    return len(rows or [])


def _shadow_load_artifacts() -> dict[str, object]:
    safety_status, safety = _load_shadow_json_artifact(_shadow_artifact_path("safety_audit.json"))
    runtime_status, runtime = _load_shadow_json_artifact(_shadow_artifact_path("runtime_state.json"))
    summary_status, summary = _load_shadow_json_artifact(_shadow_artifact_path("comparison_summary.json"))
    daily_status, daily = _load_shadow_csv_artifact(_shadow_artifact_path("daily_summary.csv"))
    signals_status, signals = _load_shadow_csv_artifact(_shadow_artifact_path("shadow_signals.csv"))
    orders_status, orders = _load_shadow_csv_artifact(_shadow_artifact_path("shadow_simulated_orders.csv"))
    trades_status, trades = _load_shadow_csv_artifact(_shadow_artifact_path("shadow_simulated_trades.csv"))
    positions_status, positions = _load_shadow_csv_artifact(_shadow_artifact_path("shadow_positions.csv"))
    equity_status, equity = _load_shadow_csv_artifact(_shadow_artifact_path("shadow_equity.csv"))
    blocked_status, blocked = _load_shadow_csv_artifact(_shadow_artifact_path("blocked_reason_counts.csv"))
    return {
        "statuses": {
            "safety": safety_status,
            "runtime": runtime_status,
            "summary": summary_status,
            "daily": daily_status,
            "signals": signals_status,
            "orders": orders_status,
            "trades": trades_status,
            "positions": positions_status,
            "equity": equity_status,
            "blocked": blocked_status,
        },
        "safety": safety,
        "runtime": runtime,
        "summary": summary,
        "daily": daily,
        "signals": signals,
        "orders": orders,
        "trades": trades,
        "positions": positions,
        "equity": equity,
        "blocked": blocked,
    }


def _shadow_blocked_top5(blocked_rows: list[dict[str, object]] | None) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for row in blocked_rows or []:
        reason = str(row.get("reason") or row.get("blocked_reason") or "").strip()
        if not reason:
            continue
        try:
            count = int(float(row.get("count") or 0))
        except Exception:
            count = 0
        items.append({"reason": reason, "count": count})
    items.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("reason") or "")))
    return items[:5]


def _shadow_equity_series(equity_rows: list[dict[str, object]] | None) -> list[dict[str, object]]:
    series: list[dict[str, object]] = []
    for row in equity_rows or []:
        ts = _shadow_datetime(row.get("timestamp_utc") or row.get("timestamp"))
        if ts is None:
            continue
        try:
            equity_value = float(row.get("equity") or 0.0)
        except Exception:
            equity_value = 0.0
        try:
            cash_value = float(row.get("cash") or 0.0)
        except Exception:
            cash_value = 0.0
        series.append(
            {
                "timestamp_utc": ts.isoformat(),
                "timestamp_et": ts.astimezone(ZoneInfo("America/New_York")).isoformat(),
                "equity": round(equity_value, 6),
                "cash": round(cash_value, 6),
                "version": row.get("version"),
            }
        )
    series.sort(key=lambda item: str(item.get("timestamp_utc") or ""))
    return series


def _shadow_status_snapshot() -> dict[str, object]:
    artifacts = _shadow_load_artifacts()
    statuses = artifacts.get("statuses") if isinstance(artifacts.get("statuses"), dict) else {}
    safety = artifacts["safety"]
    runtime = artifacts["runtime"]
    summary = artifacts["summary"]
    blocked_rows = artifacts["blocked"]
    equity_rows = artifacts["equity"]
    signals_rows = artifacts["signals"]
    orders_rows = artifacts["orders"]
    trades_rows = artifacts["trades"]
    positions_rows = artifacts["positions"]
    daily_rows = artifacts["daily"]

    state = "STALE"
    safety_gate = "STALE"
    status_detail = "shadow data unavailable"
    safety_ok = False
    quote_api_only = False
    trade_api_used = None
    trade_context_initialized = None
    benchmark_status = "unavailable"
    alignment_status = "unavailable"
    last_run_at = None
    latest_processed_bar_utc = None
    latest_processed_bar_et = None
    data_freshness = "unavailable"
    benchmark_sensitive = None
    active_windows = None
    benchmark_symbol = None
    simulated_return = None
    simulated_drawdown = None
    simulated_equity = None
    open_simulated_positions = None
    symbol = "SOXS.US"
    timeframe = "15m"
    strategy_family = ""
    strategy_version = ""
    symbol_class = ""
    regular_session_only = True
    shadow_enabled = True
    trading_enabled = False
    benchmark_symbols: list[str] = []
    shadow_title = shadow_title_for(symbol, timeframe)
    output_directory_name = _shadow_artifact_root().name

    if any(str(status).lower() == "invalid" for status in (statuses or {}).values()):
        return {
            "available": False,
            "state": "UNSAFE",
            "state_label": "UNSAFE",
            "safety_gate": "UNSAFE",
            "detail": "data_invalid",
            "mode": "READ-ONLY SHADOW",
            "mode_label": "READ-ONLY SHADOW",
            "title": shadow_title,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_family": strategy_family,
            "strategy_version": strategy_version,
            "symbol_class": symbol_class,
            "regular_session_only": regular_session_only,
            "shadow_enabled": shadow_enabled,
            "trading_enabled": trading_enabled,
            "benchmark_symbols": benchmark_symbols,
            "output_directory": output_directory_name,
            "quote_api_only": False,
            "trade_api_used": None,
            "trade_context_initialized": None,
            "last_run_at": None,
            "latest_processed_bar_utc": None,
            "latest_processed_bar_et": None,
            "data_freshness": "data_invalid",
            "benchmark_status": "data_invalid",
            "alignment_status": "data_invalid",
            "signals_generated": 0,
            "simulated_orders": 0,
            "simulated_trades": 0,
            "open_simulated_positions": 0,
            "simulated_equity": None,
            "simulated_return": None,
            "simulated_drawdown": None,
            "blocked_reason_top5": [],
            "daily_summary_rows": 0,
            "processed_bar_count": 0,
            "benchmark_sensitive": False,
            "benchmark_symbol": None,
            "runtime_state": {},
            "safety_audit": {},
            "comparison_summary": {},
        }

    if isinstance(safety, dict):
        safety_ok = bool(safety.get("ok"))
        quote_api_only = bool(safety.get("quote_api_only"))
        trade_api_used = bool(safety.get("trade_api_used"))
        trade_context_initialized = bool(safety.get("trade_context_initialized"))
        benchmark_status = str((summary or {}).get("benchmark_alignment", {}).get("status") or "unavailable")
        alignment_status = benchmark_status
        last_run_at = str((runtime or {}).get("last_run_at") or safety.get("generated_at") or "")
        latest_processed_bar_utc = str((runtime or {}).get("last_processed_timestamp_utc") or "")
        symbol = str((runtime or {}).get("symbol") or (summary or {}).get("symbol") or symbol).strip().upper() or symbol
        timeframe = str((runtime or {}).get("timeframe") or (summary or {}).get("timeframe") or (runtime or {}).get("frequency") or (summary or {}).get("frequency") or timeframe).strip().lower() or timeframe
        strategy_family = str((runtime or {}).get("strategy_family") or (summary or {}).get("strategy_family") or strategy_family).strip()
        strategy_version = str((runtime or {}).get("strategy_version") or (summary or {}).get("strategy_version") or strategy_version).strip()
        symbol_class = str((runtime or {}).get("symbol_class") or (summary or {}).get("symbol_class") or symbol_class).strip()
        regular_session_only = bool((runtime or {}).get("regular_session_only", regular_session_only))
        shadow_enabled = bool((runtime or {}).get("shadow_enabled", shadow_enabled))
        trading_enabled = bool((runtime or {}).get("trading_enabled", trading_enabled))
        benchmark_symbols = list((runtime or {}).get("benchmark_symbols") or (summary or {}).get("benchmark_symbols") or benchmark_symbols)
        shadow_title = str((runtime or {}).get("shadow_title") or (summary or {}).get("shadow_title") or shadow_title_for(symbol, timeframe))
        output_directory_name = str((summary or {}).get("output_dir") or output_directory_name)
        if latest_processed_bar_utc:
            try:
                ts = _shadow_datetime(latest_processed_bar_utc)
            except Exception:
                ts = None
            if ts is not None:
                latest_processed_bar_utc = ts.isoformat()
                latest_processed_bar_et = ts.astimezone(ZoneInfo("America/New_York")).isoformat()
                age_minutes = max(0.0, (datetime.now(ZoneInfo("UTC")) - ts).total_seconds() / 60.0)
                if age_minutes <= 45.0:
                    data_freshness = "fresh"
                elif age_minutes <= 240.0:
                    data_freshness = "stale"
                else:
                    data_freshness = "old"
        summary_metrics = list((summary or {}).get("strategy_metrics") or [])
        eligible_rank = list((summary or {}).get("eligible_ranking") or [])
        strategy_rank = list((summary or {}).get("strategy_ranking") or [])
        best_metric = eligible_rank[0] if eligible_rank else (strategy_rank[0] if strategy_rank else (summary_metrics[0] if summary_metrics else {}))
        if isinstance(best_metric, dict):
            best_version = str(best_metric.get("version") or "")
            benchmark_symbol = str(best_metric.get("benchmark_symbol") or "")
            try:
                simulated_return = float(best_metric.get("total_return")) if best_metric.get("total_return") is not None else None
            except Exception:
                simulated_return = None
            try:
                simulated_drawdown = float(best_metric.get("max_drawdown")) if best_metric.get("max_drawdown") is not None else None
            except Exception:
                simulated_drawdown = None
            try:
                simulated_equity = float(best_metric.get("equity") or best_metric.get("ending_equity")) if (best_metric.get("equity") is not None or best_metric.get("ending_equity") is not None) else None
            except Exception:
                simulated_equity = None
            try:
                open_simulated_positions = int(best_metric.get("open_position_count") or 0)
            except Exception:
                open_simulated_positions = None
            if best_version:
                version_equity_rows = [row for row in (equity_rows or []) if str(row.get("version") or "") == best_version]
                if version_equity_rows:
                    last_equity_row = version_equity_rows[-1]
                    try:
                        simulated_equity = float(last_equity_row.get("equity") or 0.0)
                    except Exception:
                        simulated_equity = None
        if not safety_ok or not quote_api_only or trade_api_used or trade_context_initialized:
            state = "UNSAFE"
            safety_gate = "UNSAFE"
            status_detail = "安全审计失败"
        elif any(str(status).lower() == "missing" for status in (statuses or {}).values()):
            state = "STALE"
            safety_gate = "STALE"
            status_detail = "shadow artifacts unavailable"
        elif benchmark_status not in {"VALID", "valid"}:
            state = "UNSAFE"
            safety_gate = "UNSAFE"
            status_detail = "benchmark alignment invalid"
        elif runtime is None or summary is None:
            state = "STALE"
            safety_gate = "STALE"
            status_detail = "shadow artifacts unavailable"
        elif data_freshness in {"stale", "old"}:
            state = "STALE"
            safety_gate = "STALE"
            status_detail = f"shadow data {data_freshness}"
        else:
            state = "SAFE"
            safety_gate = "SAFE"
            status_detail = "read-only shadow observer healthy"

        if isinstance(summary, dict):
            benchmark_sensitive = bool(summary.get("benchmark_sensitive"))
            active_windows = len(summary.get("eligible_ranking") or [])

    if not isinstance(runtime, dict):
        runtime = {}
    processed_count = runtime.get("processed_bar_count")
    if processed_count is None:
        processed_count = len(signals_rows or [])

    return {
        "available": bool(safety) and bool(runtime) and bool(summary),
        "state": state,
        "state_label": state,
        "safety_gate": safety_gate,
        "detail": status_detail,
        "mode": "READ-ONLY SHADOW",
        "mode_label": "READ-ONLY SHADOW",
        "title": shadow_title,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_family": strategy_family,
        "strategy_version": strategy_version,
        "symbol_class": symbol_class,
        "regular_session_only": regular_session_only,
        "shadow_enabled": shadow_enabled,
        "trading_enabled": trading_enabled,
        "benchmark_symbols": benchmark_symbols,
        "output_directory": output_directory_name,
        "quote_api_only": quote_api_only,
        "trade_api_used": trade_api_used,
        "trade_context_initialized": trade_context_initialized,
        "last_run_at": last_run_at,
        "latest_processed_bar_utc": latest_processed_bar_utc,
        "latest_processed_bar_et": latest_processed_bar_et,
        "data_freshness": data_freshness,
        "benchmark_status": benchmark_status,
        "alignment_status": alignment_status,
        "signals_generated": len(signals_rows or []),
        "simulated_orders": len(orders_rows or []),
        "simulated_trades": len(trades_rows or []),
        "open_simulated_positions": open_simulated_positions if open_simulated_positions is not None else sum(
            1 for row in (positions_rows or []) if str(row.get("quantity") or "0").strip() not in {"", "0", "0.0"}
        ),
        "simulated_equity": simulated_equity,
        "simulated_return": simulated_return,
        "simulated_drawdown": simulated_drawdown,
        "blocked_reason_top5": _shadow_blocked_top5(blocked_rows),
        "daily_summary_rows": len(daily_rows or []),
        "processed_bar_count": int(processed_count or 0),
        "benchmark_sensitive": benchmark_sensitive,
        "benchmark_symbol": benchmark_symbol,
        "runtime_state": runtime if isinstance(runtime, dict) else {},
        "safety_audit": safety if isinstance(safety, dict) else {},
        "comparison_summary": summary if isinstance(summary, dict) else {},
    }


def _shadow_status_payload() -> dict[str, object]:
    snapshot = _shadow_status_snapshot()
    return {
        "ok": snapshot["state"] != "UNSAFE",
        "state": snapshot["state"],
        "status_label": snapshot["state_label"],
        "detail": snapshot["detail"],
        "mode": snapshot["mode"],
        "title": snapshot["title"],
        "symbol": snapshot["symbol"],
        "timeframe": snapshot["timeframe"],
        "strategy_family": snapshot["strategy_family"],
        "strategy_version": snapshot["strategy_version"],
        "symbol_class": snapshot["symbol_class"],
        "regular_session_only": snapshot["regular_session_only"],
        "shadow_enabled": snapshot["shadow_enabled"],
        "trading_enabled": snapshot["trading_enabled"],
        "benchmark_symbols": snapshot["benchmark_symbols"],
        "output_directory": snapshot["output_directory"],
        "quote_api_only": snapshot["quote_api_only"],
        "trade_api_used": snapshot["trade_api_used"],
        "trade_context_initialized": snapshot["trade_context_initialized"],
        "last_run_at": snapshot["last_run_at"],
        "latest_processed_bar_utc": snapshot["latest_processed_bar_utc"],
        "latest_processed_bar_et": snapshot["latest_processed_bar_et"],
        "data_freshness": snapshot["data_freshness"],
        "benchmark_status": snapshot["benchmark_status"],
        "alignment_status": snapshot["alignment_status"],
        "signals_generated": snapshot["signals_generated"],
        "simulated_orders": snapshot["simulated_orders"],
        "simulated_trades": snapshot["simulated_trades"],
        "open_simulated_positions": snapshot["open_simulated_positions"],
        "simulated_equity": snapshot["simulated_equity"],
        "simulated_return": snapshot["simulated_return"],
        "simulated_drawdown": snapshot["simulated_drawdown"],
        "blocked_reason_top5": snapshot["blocked_reason_top5"],
        "benchmark_sensitive": snapshot["benchmark_sensitive"],
        "benchmark_symbol": snapshot["benchmark_symbol"],
        "available": snapshot["available"],
        "processed_bar_count": snapshot["processed_bar_count"],
    }


def _shadow_summary_payload() -> dict[str, object]:
    snapshot = _shadow_status_snapshot()
    runtime = snapshot.get("runtime_state") if isinstance(snapshot.get("runtime_state"), dict) else {}
    daily_rows = _read_csv_file(_shadow_artifact_path("daily_summary.csv")) or []
    latest_daily = daily_rows[-1] if daily_rows else {}
    return {
        "ok": snapshot["state"] != "UNSAFE",
        "state": snapshot["state"],
        "status_label": snapshot["state_label"],
        "detail": snapshot["detail"],
        "mode": snapshot["mode"],
        "title": snapshot["title"],
        "symbol": snapshot["symbol"],
        "timeframe": snapshot["timeframe"],
        "strategy_family": snapshot["strategy_family"],
        "strategy_version": snapshot["strategy_version"],
        "symbol_class": snapshot["symbol_class"],
        "last_run_at": snapshot["last_run_at"],
        "latest_processed_bar_utc": snapshot["latest_processed_bar_utc"],
        "latest_processed_bar_et": snapshot["latest_processed_bar_et"],
        "data_freshness": snapshot["data_freshness"],
        "daily_summary_rows": snapshot["daily_summary_rows"],
        "latest_daily_summary": latest_daily,
        "processed_bar_count": snapshot["processed_bar_count"],
        "runtime_state": runtime,
    }


def _shadow_blocked_reasons_payload() -> dict[str, object]:
    snapshot = _shadow_status_snapshot()
    blocked_rows = _read_csv_file(_shadow_artifact_path("blocked_reason_counts.csv")) or []
    return {
        "ok": snapshot["state"] != "UNSAFE",
        "state": snapshot["state"],
        "status_label": snapshot["state_label"],
        "title": snapshot["title"],
        "items": _shadow_blocked_top5(blocked_rows),
        "count": len(blocked_rows),
    }


def _shadow_equity_payload() -> dict[str, object]:
    snapshot = _shadow_status_snapshot()
    equity_rows = _shadow_equity_series(_read_csv_file(_shadow_artifact_path("shadow_equity.csv")))
    latest = equity_rows[-1] if equity_rows else {}
    return {
        "ok": snapshot["state"] != "UNSAFE",
        "state": snapshot["state"],
        "status_label": snapshot["state_label"],
        "title": snapshot["title"],
        "items": equity_rows,
        "latest": latest,
        "count": len(equity_rows),
    }


def _format_lifecycle_detail(report: dict[str, object], *, kind: str) -> str:
    if kind == "weekend_paper":
        buy = report.get("buy") if isinstance(report.get("buy"), dict) else {}
        sell = report.get("sell") if isinstance(report.get("sell"), dict) else {}
        checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
        parts = [
            "BUY " + str(buy.get("status") or "unknown").upper(),
            "SELL " + str(sell.get("status") or "unknown").upper(),
            f"position {'0' if checks.get('position_returned_to_zero') else 'not zero'}",
        ]
        return " · ".join(parts)
    if kind == "sandbox":
        precheck = report.get("precheck") if isinstance(report.get("precheck"), dict) else {}
        buy = report.get("buy") if isinstance(report.get("buy"), dict) else {}
        sell = report.get("sell") if isinstance(report.get("sell"), dict) else {}
        checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
        parts = [
            f"bootstrap {'PASS' if checks.get('bootstrap_confirmed') else 'FAIL'}",
            "BUY " + str(buy.get("final_status") or buy.get("status") or "unknown").upper(),
            "SELL " + str(sell.get("final_status") or sell.get("status") or "unknown").upper(),
        ]
        if precheck.get("current_quote"):
            parts.append("read-only bootstrap")
        return " · ".join(parts)
    return "no data"


def _load_lifecycle_summary(kind: str) -> dict[str, object]:
    path = WEEKEND_PAPER_LIFECYCLE_PATH if kind == "weekend_paper" else LONGBRIDGE_SANDBOX_LIFECYCLE_PATH
    raw = _read_json_file(path)
    if raw is None:
        return {
            "available": False,
            "status": "unavailable",
            "status_label": "unavailable",
            "detail": "no data",
            "generated_at": None,
            "path": str(path),
        }
    report = raw.get("report") if isinstance(raw.get("report"), dict) else raw
    report = report if isinstance(report, dict) else {}
    if kind == "weekend_paper":
        buy = report.get("buy") if isinstance(report.get("buy"), dict) else {}
        sell = report.get("sell") if isinstance(report.get("sell"), dict) else {}
        checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
        ok = bool(checks.get("overall"))
        mode = str(report.get("mode") or "paper").strip().lower()
        broker = str(report.get("broker") or "PaperBroker")
        account_type = str(report.get("account_type") or "paper").strip().lower()
        ticker = str(buy.get("ticker") or sell.get("ticker") or report.get("ticker") or "TEST")
    else:
        precheck = report.get("precheck") if isinstance(report.get("precheck"), dict) else {}
        buy = report.get("buy") if isinstance(report.get("buy"), dict) else {}
        sell = report.get("sell") if isinstance(report.get("sell"), dict) else {}
        checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
        ok = bool(report.get("ok"))
        mode = str(report.get("mode") or "sandbox").strip().lower()
        broker = str(report.get("broker") or "Longbridge")
        account_type = str(report.get("account_type") or "paper").strip().lower()
        ticker = str(report.get("ticker") or "SOFI")
    generated_at = str(raw.get("generated_at") or report.get("generated_at") or "")
    if not generated_at:
        try:
            generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=ZoneInfo("Asia/Shanghai")).isoformat()
        except Exception:
            generated_at = None
    detail = _format_lifecycle_detail(report, kind=kind)
    if not detail or detail == "no data":
        detail = str(report.get("reason") or "no data")
    return {
        "available": True,
        "status": "PASS" if ok else "FAIL",
        "status_label": "PASS" if ok else "FAIL",
        "detail": detail,
        "generated_at": generated_at,
        "path": str(path),
        "mode": _mode_label(mode),
        "broker": broker,
        "account_type": account_type.upper() if account_type else "UNKNOWN",
        "ticker": ticker,
        "report": report,
    }


def _load_active_orders_summary(tickers: list[str]) -> dict[str, object]:
    root = STATE_DIR / "broker_cache"
    orders: list[dict[str, object]] = []
    sources: list[str] = []
    for ticker in tickers or []:
        normalized = _chart_ticker(ticker)
        path = root / f"longbridge_active_orders_{normalized}.json"
        payload = _read_json_file(path)
        if not payload:
            continue
        sources.append(str(path))
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        items = inner.get("orders") if isinstance(inner.get("orders"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            orders.append(
                {
                    "ticker": _chart_ticker(item.get("ticker") or normalized),
                    "side": str(item.get("side") or "").upper() or "UNKNOWN",
                    "quantity": int(item.get("quantity", 0) or 0),
                    "filled_quantity": int(item.get("filled_quantity", 0) or 0),
                    "status": str(item.get("status") or "UNKNOWN"),
                    "limit_price": item.get("limit_price"),
                    "avg_fill_price": item.get("avg_fill_price"),
                    "order_id": str(item.get("order_id") or ""),
                }
            )
    available = bool(sources)
    status_label = "no data" if not available else (f"{len(orders)}" if orders else "0")
    detail = "no data" if not available else ("无活动订单" if not orders else "LongBridge active order cache")
    return {
        "available": available,
        "count": len(orders),
        "orders": orders,
        "sources": sources,
        "status_label": status_label,
        "detail": detail,
    }


def _system_status_snapshot(
    *,
    runtime_config=None,
    live_account: dict | None = None,
    trade_audit: dict | None = None,
    active_orders: dict | None = None,
    update_time: str | None = None,
    mode_override: str | None = None,
) -> dict[str, object]:
    config = runtime_config or _load_dashboard_config()
    config_mode = str(getattr(config, "mode", "") or "paper").strip().lower() if config is not None else "paper"
    override_mode = str(mode_override or "").strip().lower()
    mode = override_mode if override_mode in {"paper", "sandbox", "live"} else config_mode
    guard = TradingEnvironmentGuard().validate(config) if config is not None else None
    longbridge_cfg = getattr(getattr(config, "broker", None), "longbridge", None) if config is not None else None
    live_order_enabled = bool(getattr(longbridge_cfg, "allow_live_order", False)) if longbridge_cfg else False
    reduce_only = bool((trade_audit or {}).get("reduce_only", False))
    live_account_mode = str((live_account or {}).get("mode") or "").strip().lower()
    broker_connected = False
    broker_connection_label = "not connected"
    data_source = "no data"
    account_source = "no data"
    if mode == "paper":
        broker_connected = True
        broker_connection_label = "local memory"
        data_source = "PaperBroker / TOP engine runtime"
        account_source = "PaperBroker runtime state"
    elif live_account and not (live_account or {}).get("account_error") and live_account_mode in {"live", "sandbox"}:
        broker_connected = True
        broker_connection_label = "connected"
        if live_account_mode == "sandbox":
            data_source = "LongBridge sandbox snapshot"
            account_source = "LongBridge sandbox account"
        else:
            data_source = "LongBridge production snapshot"
            account_source = "LongBridge production account"
    elif mode in {"sandbox", "live"}:
        broker_connection_label = "not connected"
        data_source = "no data"
        account_source = "not connected"
    market = _market_status_snapshot()
    lifecycle_weekend = _load_lifecycle_summary("weekend_paper")
    lifecycle_sandbox = _load_lifecycle_summary("sandbox")
    return {
        "api_status": "OK",
        "mode": _mode_label(mode),
        "mode_key": mode or "paper",
        "broker_type": "PaperBroker" if mode == "paper" else "LongBridge",
        "broker_connection": broker_connection_label,
        "broker_connected": broker_connected,
        "data_source": data_source,
        "account_source": account_source,
        "market_open": market.get("open"),
        "market_open_label": market.get("label"),
        "market_open_detail": market.get("detail"),
        "last_updated": update_time or datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "global_reduce_only": reduce_only,
        "live_order_enabled": live_order_enabled,
        "lifecycle": {
            "weekend_paper": lifecycle_weekend,
            "longbridge_sandbox": lifecycle_sandbox,
        },
        "active_orders": active_orders or {"available": False, "count": 0, "orders": [], "sources": [], "status_label": "no data", "detail": "no data"},
        "environment": (guard.summary if guard is not None else {}),
        "warnings": list(getattr(guard, "warnings", []) or []),
        "errors": list(getattr(guard, "errors", []) or []),
    }


def _enrich_ticker_descriptions(top3_list: list) -> list:
    """Add human-readable description for each selected ticker."""
    try:
        from src.risk.instrument_profile import LEVERAGED_ETF_REGISTRY
    except Exception:
        LEVERAGED_ETF_REGISTRY = {}

    descriptions = {
        "SOXS": "3倍做空半导体ETF",
        "SOXL": "3倍做多半导体ETF",
        "LABD": "3倍做空生物科技ETF",
        "LABU": "3倍做多生物科技ETF",
        "DRIP": "3倍做空能源ETF",
        "GUSH": "3倍做多能源ETF",
        "YINN": "2倍做多中国ETF",
        "YANG": "2倍做空中国ETF",
        "TQQQ": "3倍做多纳斯达克ETF",
        "SQQQ": "3倍做空纳斯达克ETF",
        "SOFI": "金融科技公司",
        "NVDA": "AI芯片龙头",
        "PLTR": "大数据分析公司",
        "AAPL": "消费电子龙头",
        "TSLA": "电动汽车公司",
        "AMZN": "电商云计算龙头",
        "NIO": "电动汽车公司（中概）",
        "SMR": "小型核反应堆公司",
        "QBTS": "量子计算公司",
        "WULF": "比特币矿商",
        "SMCI": "AI服务器制造商",
    }

    enriched = []
    for item in (top3_list or []):
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        profile = LEVERAGED_ETF_REGISTRY.get(ticker, {})

        is_etf = bool(profile)
        leverage = profile.get("leverage", 1) if profile else 1
        is_inverse = profile.get("inverse", False) if profile else False

        if is_inverse:
            direction = "\U0001f43b 做空"
            direction_cn = "做空"
        elif is_etf:
            direction = "\U0001f402 做多"
            direction_cn = "做多"
        else:
            direction = "\u2014"
            direction_cn = "个股"

        item["_type"] = "ETF" if is_etf else "股票"
        item["_leverage"] = f"{leverage}x"
        item["_direction"] = direction
        item["_description"] = descriptions.get(ticker, profile.get("sector", ""))

    return top3_list


def _current_et_date() -> str:
    try:
        return market_session_context(datetime.now(ZoneInfo("America/New_York"))).current_session.isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _ai_runtime_status() -> dict:
    enabled = str(_env("SOXS_AI_SELECTOR_ENABLED", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return {
            "level": "yellow",
            "label": "AI 已关闭",
            "detail": "当前仍按原有选股/配置链路运行。",
        }

    missing = []
    if not _env("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    tradingagents_ready = bool(_env("SOXS_TRADINGAGENTS_PATH"))
    finrobot_ready = bool(_env("SOXS_FINROBOT_PATH"))
    fmp_enabled = bool(_env("FMP_API_KEY"))

    if not missing and tradingagents_ready and finrobot_ready:
        return {
            "level": "green",
            "label": "完整真实",
            "detail": "TradingAgents 和 FinRobot 都可走真实分析链路。",
        }

    if tradingagents_ready or finrobot_ready:
        detail = "已接通外部项目路径"
        if missing:
            detail += f"，缺少 {' / '.join(missing)}，当前会自动降级回退。"
        else:
            detail += "，当前可走真实分析。"
        if not fmp_enabled:
            detail += " FMP 已禁用，不影响运行。"
        return {
            "level": "yellow",
            "label": "部分降级",
            "detail": detail,
        }

    return {
        "level": "yellow",
        "label": "仅回退",
        "detail": "外部 AI 项目未接通，当前只使用本地回退评分。",
    }


def _selection_sync_status() -> dict:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    session = market_session_context(now_et)
    latest_report = _load_ai_selection_report() or {}
    latest_report_date = str(latest_report.get("selection_date") or latest_report.get("date") or "").strip()
    latest_report_stage = str(
        latest_report.get("selection_stage")
        or latest_report.get("market_selection_stage")
        or latest_report.get("settings", {}).get("selection_stage")
        or ""
    ).strip().upper()
    latest_execution_status = str(latest_report.get("execution_status") or "").strip().upper()
    committed_bundle = load_committed_selection_bundle(PROJECT_DIR)
    committed_state = committed_bundle.get("state") if isinstance(committed_bundle, dict) else None
    selection_completed = (
        latest_report_date == session.current_session.isoformat()
        and latest_execution_status == "COMPLETED"
        and latest_report_stage == "FINALIZED"
    )
    required_date = required_selection_date(now_et, selection_completed=selection_completed)
    ok, reason, state = verify_selection_state(required_et_date=required_date, state=committed_state if isinstance(committed_state, dict) else None)
    state = state or (committed_state if isinstance(committed_state, dict) else None) or load_selection_state() or {}
    state_date = str(state.get("et_date") or "").strip() or None
    selection_state_symbols = [
        str(item or "").strip().upper()
        for item in (state.get("selection_state_symbols") or state.get("selected_symbols") or [])
        if str(item or "").strip()
    ]
    current_top_config_symbols_list = [
        str(item or "").strip().upper()
        for item in (state.get("current_top_config_symbols") or current_top_config_symbols(limit=max(configured_top_count(), len(selection_state_symbols) or 1)))
        if str(item or "").strip()
    ]
    state_top_config_symbols = [
        str(item or "").strip().upper()
        for item in (state.get("state_top_config_symbols") or state.get("top_config_symbols") or [])
        if str(item or "").strip()
    ]
    disabled_slots = [
        int(item)
        for item in (state.get("disabled_slots") or current_top_config_disabled_slots(limit=max(configured_top_count(), len(selection_state_symbols) or 1)))
        if str(item).strip()
    ]
    selection_run_id = str(state.get("selection_run_id") or "")
    top_sync_run_id = str(state.get("top_sync_run_id") or "")
    mismatch_reason = ""
    if reason in {"symbol_mismatch", "top_config_symbols_mismatch"}:
        mismatch_reason = "symbol_mismatch"
    elif reason.startswith("selection_state_date_mismatch"):
        mismatch_reason = "selection_state_date_mismatch"
    elif reason == "missing_top_slot":
        mismatch_reason = "missing_top_slot"
    elif reason == "selection_state_missing":
        mismatch_reason = "selection_state_missing"
    suggestion = "请重新运行 AI Selector 或重新写入 TOP 配置"
    label = "已对齐"
    level = "green"
    detail = f"当前配置已对齐（美东 {required_date}）"
    if ok:
        return {
            "ok": True,
            "level": level,
            "label": label,
            "detail": detail,
            "required_date": required_date,
            "state_date": state_date,
            "selection_state_symbols": selection_state_symbols,
            "current_top_config_symbols": current_top_config_symbols_list,
            "state_top_config_symbols": state_top_config_symbols,
            "disabled_slots": disabled_slots,
            "selection_run_id": selection_run_id,
            "top_sync_run_id": top_sync_run_id,
            "synced": True,
            "reason": "ok",
            "mismatch_reason": "",
            "suggestion": suggestion,
        }
    if reason == "selection_state_missing":
        label = "未校验"
        level = "yellow"
        detail = "还没有当天选股校验记录，启动前会先重选并校验。"
    elif reason.startswith("selection_state_date_mismatch"):
        if session.is_regular_session and not selection_completed:
            label = "等待本次选股"
            level = "yellow"
            detail = (
                f"当前是美东 {session.current_session.isoformat()} 的交易时段，正在等待本次选股完成。"
                f"上一完整交易日状态仍可继续使用。 selection_state tickers: {selection_state_symbols or []}"
                f" · current TOP config tickers: {current_top_config_symbols_list or []}。"
            )
            mismatch_reason = "awaiting_current_session_selection"
        else:
            label = "不是今天"
            level = "yellow"
            detail = (
                f"当前记录日期是美东 {state_date or '未知'}，不是当前要求日期 {required_date}。"
                f" selection_state tickers: {selection_state_symbols or []} · current TOP config tickers: {current_top_config_symbols_list or []}。"
            )
    elif reason == "missing_top_slot":
        label = "配置不完整"
        level = "red"
        detail = (
            f"TOP1-3 配置槽位不完整。 selection_state tickers: {selection_state_symbols or []}"
            f" · current TOP config tickers: {current_top_config_symbols_list or []}。"
            f" 缺失槽位: {disabled_slots or []}。"
        )
    elif reason in {"symbol_mismatch", "top_config_symbols_mismatch"}:
        label = "配置不一致"
        level = "red"
        detail = (
            f"TOP1-3 配置和最近一次选股结果不一致。"
            f" selection_state tickers: {selection_state_symbols or []} · current TOP config tickers: {current_top_config_symbols_list or []}。"
            f" mismatch reason: {mismatch_reason}。"
            f" 建议操作：{suggestion}。"
        )
    else:
        label = "校验失败"
        level = "red"
        detail = (
            f"选股配置校验失败：{reason}"
            f" selection_state tickers: {selection_state_symbols or []} · current TOP config tickers: {current_top_config_symbols_list or []}。"
        )
    return {
        "ok": False,
        "level": level,
        "label": label,
        "detail": detail,
        "required_date": required_date,
        "state_date": state_date,
        "selection_state_symbols": selection_state_symbols,
        "current_top_config_symbols": current_top_config_symbols_list,
        "state_top_config_symbols": state_top_config_symbols,
        "disabled_slots": disabled_slots,
        "selection_run_id": selection_run_id,
        "top_sync_run_id": top_sync_run_id,
        "synced": False,
        "reason": reason,
        "mismatch_reason": mismatch_reason,
        "suggestion": suggestion,
    }


def _startup_guard_status(selection_sync: dict, execution_mode: str | None = None) -> dict:
    live_top_active = has_live_top_configs()
    mode = str(execution_mode or "").strip().lower()
    if not live_top_active and mode != "live":
        return {
            "level": "live",
            "label": "虚拟盘运行中",
            "detail": "当前是 paper 模式，未启用 live TOP 校验，虚拟盘会按当天 TOP 配置继续交易。",
            "required_date": str((selection_sync or {}).get("required_date") or _current_et_date()),
            "state_date": str((selection_sync or {}).get("state_date") or "") or None,
        }
    if not live_top_active:
        return {
            "level": "warn",
            "label": "启动校验待命",
            "detail": "当前没有 live TOP 配置，今天的启动校验不会触发。",
            "required_date": str((selection_sync or {}).get("required_date") or _current_et_date()),
            "state_date": str((selection_sync or {}).get("state_date") or "") or None,
        }
    if bool((selection_sync or {}).get("ok")):
        return {
            "level": "live",
            "label": "启动校验通过",
            "detail": str((selection_sync or {}).get("detail") or "当天美东选股状态已通过。"),
            "required_date": str((selection_sync or {}).get("required_date") or _current_et_date()),
            "state_date": str((selection_sync or {}).get("state_date") or "") or None,
        }
    return {
        "level": "warn" if str((selection_sync or {}).get("level")) != "red" else "",
        "label": f"启动校验阻止 · {str((selection_sync or {}).get('label') or '未通过')}",
        "detail": str((selection_sync or {}).get("detail") or "当天美东选股状态未通过。"),
        "required_date": str((selection_sync or {}).get("required_date") or _current_et_date()),
        "state_date": str((selection_sync or {}).get("state_date") or "") or None,
    }


def _desired_audit_mode() -> str:
    value = _env("SOXS_AUDIT_MODE", "live").strip().lower()
    return value if value in {"live", "paper"} else "live"


def _load_top_modes() -> list[str]:
    modes: list[str] = []
    for item in TICKERS:
        cfg_path = PROJECT_DIR / "configs" / item["config"]
        if not cfg_path.exists():
            modes.append("disabled")
            continue
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        mode = str(data.get("mode", "paper")).strip().lower() or "paper"
        modes.append(mode)
    return modes


def _top_config_exists(config_name: str) -> bool:
    return (PROJECT_DIR / "configs" / config_name).exists()


def _load_top_ai_selector_flags() -> tuple[bool | None, bool | None]:
    """Read fallback flags from current TOP configs when available."""
    paper_flags: list[bool] = []
    live_flags: list[bool] = []
    seen_any = False
    for item in TICKERS:
        cfg_path = PROJECT_DIR / "configs" / item["config"]
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        ai_selector = data.get("ai_selector") if isinstance(data.get("ai_selector"), dict) else {}
        if "allow_fallback_live_entries" in ai_selector:
            seen_any = True
            live_flags.append(bool(ai_selector.get("allow_fallback_live_entries")))
        if "allow_fallback_paper_entries" in ai_selector:
            seen_any = True
            paper_flags.append(bool(ai_selector.get("allow_fallback_paper_entries")))
    if not seen_any:
        return None, None
    live_allowed = all(live_flags) if live_flags else None
    paper_allowed = all(paper_flags) if paper_flags else None
    return live_allowed, paper_allowed


def _resolve_dashboard_execution_mode(trade_audit: dict) -> str:
    explicit = str(trade_audit.get("execution_mode") or "").strip().lower()
    if explicit == "live" and int(trade_audit.get("execution_count", 0) or 0) > 0:
        return "live"

    top_modes = _load_top_modes()
    if top_modes and all(mode == "live" for mode in top_modes):
        return "live"
    if top_modes and any(mode == "live" for mode in top_modes):
        return "mixed"
    return explicit or "paper"


def _latest_trade_line(trade_audit: dict) -> str | None:
    latest_execution = trade_audit.get("latest_execution") if isinstance(trade_audit, dict) else {}
    latest_record = trade_audit.get("latest_record") if isinstance(trade_audit, dict) else {}
    latest_decision = trade_audit.get("latest_decision") if isinstance(trade_audit, dict) else {}

    candidates = [latest_execution, latest_decision, latest_record]
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate:
            continue
        action = str(candidate.get("action") or "").strip().lower()
        if action in IGNORED_AUDIT_ACTIONS:
            continue
        ticker = candidate.get("ticker") or candidate.get("symbol") or ""
        phase = candidate.get("phase") or action or "record"
        order = candidate.get("order") if isinstance(candidate.get("order"), dict) else {}
        side = order.get("side") or candidate.get("trade_signal", {}).get("action") or ""
        qty = order.get("qty") or ""
        status = ""
        response = candidate.get("response")
        if isinstance(response, dict):
            status = response.get("status") or response.get("body", {}).get("status") or response.get("ok")
        line = " ".join(str(part) for part in [phase, ticker, side, qty, status] if part not in ("", None, False))
        if line:
            return line
    return None


def _nearest_trigger(
    cards: list[dict],
    side: str,
    *,
    new_entries_allowed: bool = True,
) -> tuple[str, str]:
    if side == "buy" and not new_entries_allowed:
        return "暂停开仓", "当前已暂停新开仓"

    best_name = "暂无"
    best_hint = "暂无接近买点的标的" if side == "buy" else "暂无接近卖点的标的"
    best_rank = None
    buy_blocked_by_position = False

    for card in cards:
        if not card.get("online") or not card.get("range_ready") or card.get("halted"):
            continue
        if side == "buy" and card.get("reduce_only"):
            continue

        price = float(card.get("price", 0) or 0.0)
        support = float(card.get("support", 0) or 0.0)
        resistance = float(card.get("resistance", 0) or 0.0)
        shares = int(card.get("shares", 0) or 0)

        if price <= 0 or support <= 0 or resistance <= support:
            continue

        if side == "buy":
            if shares > 0:
                buy_blocked_by_position = True
                continue
            distance = abs(price - support)
            distance_pct = (distance / support * 100.0) if support > 0 else 0.0
            rank = (distance, distance_pct)
            hint = f"{card['name']} 距买点 ${distance:.2f} ({distance_pct:.1f}%)"
        else:
            distance = abs(resistance - price)
            distance_pct = (distance / resistance * 100.0) if resistance > 0 else 0.0
            rank = (0 if shares > 0 else 1, distance, distance_pct)
            hint = f"{card['name']} 距卖点 ${distance:.2f} ({distance_pct:.1f}%)"

        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_name = card["name"]
            best_hint = hint

    if side == "buy" and best_rank is None and buy_blocked_by_position:
        return "暂无可买", "当前标的都有持仓，暂无新的买点提示"

    return best_name, best_hint

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI区间交易总览</title>
<style>
    :root{
        --bg:#030816;
        --bg2:#071126;
        --panel:rgba(8,14,28,.88);
        --panel-strong:rgba(13,20,38,.96);
        --line:rgba(125,211,252,.14);
        --text:#edf4ff;
        --muted:#93a4bf;
        --accent:#34d399;
        --accent2:#7dd3fc;
        --accent3:#c084fc;
        --warn:#fbbf24;
        --down:#fb7185;
        --up:#34d399;
        --shadow:0 28px 110px rgba(0,0,0,.55);
        --radius-panel:24px;
        --radius-card:18px;
    }
    *{margin:0;padding:0;box-sizing:border-box}
    body{
        min-height:100vh;
        color:var(--text);
        font-family:"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
        font-size:15px;
        line-height:1.58;
        letter-spacing:.01em;
        background:
            radial-gradient(circle at 10% 5%, rgba(125,211,252,.16), transparent 24%),
            radial-gradient(circle at 88% 8%, rgba(192,132,252,.14), transparent 18%),
            radial-gradient(circle at 50% 0%, rgba(52,211,153,.08), transparent 28%),
            linear-gradient(180deg, #02050d 0%, #050b18 40%, #030816 100%);
        padding:18px 18px 30px;
        overflow-x:hidden;
        overflow-y:auto;
        position:relative;
    }
    body::before{
        content:"";
        position:fixed;
        inset:0;
        pointer-events:none;
        background:
            linear-gradient(rgba(125,211,252,.05) 1px, transparent 1px),
            linear-gradient(90deg, rgba(125,211,252,.05) 1px, transparent 1px);
        background-size:64px 64px;
        mask-image: linear-gradient(180deg, rgba(0,0,0,.6), rgba(0,0,0,.2) 52%, rgba(0,0,0,.05));
        opacity:.32;
        z-index:0;
    }
    body::after{
        content:"";
        position:fixed;
        inset:-40px;
        pointer-events:none;
        background:
            radial-gradient(circle at 20% 25%, rgba(125,211,252,.08), transparent 10%),
            radial-gradient(circle at 78% 18%, rgba(168,85,247,.08), transparent 9%),
            radial-gradient(circle at 64% 72%, rgba(52,211,153,.07), transparent 12%);
        filter:blur(6px);
        opacity:.75;
        z-index:0;
    }
    .page{
        max-width:1680px;
        margin:0 auto;
        min-height:calc(100vh - 44px);
        display:flex;
        flex-direction:column;
        gap:20px;
        position:relative;
        z-index:1;
    }
    .topbar{
        display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
        padding:22px 22px 18px;border:1px solid rgba(125,211,252,.18);
        background:
            linear-gradient(180deg, rgba(11,17,31,.94), rgba(5,10,22,.88)),
            radial-gradient(circle at 18% 16%, rgba(125,211,252,.10), transparent 30%),
            radial-gradient(circle at 82% 0%, rgba(192,132,252,.08), transparent 20%);
        border-radius:var(--radius-panel);
        box-shadow:0 0 0 1px rgba(255,255,255,.03), var(--shadow);
        backdrop-filter:blur(18px);
        position:sticky;top:10px;z-index:5;
        overflow:hidden;
    }
    .topbar::before{
        content:"";
        position:absolute;
        inset:0;
        border-radius:inherit;
        padding:1px;
        background:linear-gradient(135deg, rgba(125,211,252,.7), rgba(52,211,153,.3), rgba(192,132,252,.55));
        -webkit-mask:linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
        -webkit-mask-composite:xor;
        mask-composite:exclude;
        pointer-events:none;
    }
    .brand{display:flex;flex-direction:column;gap:10px}
    .brand h1{font-size:34px;line-height:1.04;letter-spacing:.02em;font-weight:860}
    .brand p{color:#aab7cc;font-size:14px;line-height:1.5;max-width:920px}
    .headline-stats{
        display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;min-width:1140px
    }
    .headline-stat{
        padding:16px 18px;border-radius:18px;
        background:
            linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.02)),
            radial-gradient(circle at top right, rgba(125,211,252,.12), transparent 46%),
            linear-gradient(135deg, rgba(16,24,44,.92), rgba(7,12,24,.88));
        border:1px solid rgba(125,211,252,.16);
        box-shadow:inset 0 1px 0 rgba(255,255,255,.04), 0 10px 30px rgba(0,0,0,.25);
        min-width:0;
    }
    .headline-stat .label{
        display:block;color:#a8b4c8;font-size:11px;letter-spacing:.12em;text-transform:uppercase
    }
    .headline-stat .value{
        display:block;margin-top:10px;font-size:28px;font-weight:820;font-variant-numeric:tabular-nums;line-height:1.05;
        letter-spacing:-.02em
    }
    .headline-stat .sub{
        display:block;margin-top:10px;color:var(--muted);font-size:12px;line-height:1.5
    }
    .status-row{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}
    .pill{
        display:inline-flex;align-items:center;gap:8px;padding:9px 13px;border-radius:999px;
        background:rgba(255,255,255,.04);border:1px solid var(--line);color:var(--text);
        font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase
    }
    a.pill{text-decoration:none}
    .pill.live{background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.22);color:#b8f5d0}
    .pill.warn{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.24);color:#fde68a}
    .pill.research{background:rgba(59,130,246,.08);border-color:rgba(59,130,246,.24);color:#bfdbfe}
    .pill.mode-paper{background:rgba(192,132,252,.12);border-color:rgba(192,132,252,.28);color:#e9d5ff}
    .pill.mode-sandbox{background:rgba(125,211,252,.12);border-color:rgba(125,211,252,.28);color:#cffafe}
    .pill.mode-live{background:rgba(251,113,133,.12);border-color:rgba(251,113,133,.28);color:#fecdd3;box-shadow:0 0 0 1px rgba(251,113,133,.12), 0 0 28px rgba(251,113,133,.12)}
    .pill.mode-live strong{color:#fff}
    .pill.mode-live::before{content:"⚠";font-size:12px}
    .pill.status-live{background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.24);color:#b8f5d0}
    .pill.status-warn{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.24);color:#fde68a}
    .pill.status-offline{background:rgba(148,163,184,.08);border-color:rgba(148,163,184,.18);color:#cbd5e1}
    .hero-status-grid{
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:8px;
        margin-top:10px;
        min-width:320px;
    }
    .hero-status-item{
        display:flex;justify-content:space-between;gap:12px;align-items:center;
        padding:9px 11px;border-radius:12px;
        border:1px solid rgba(125,211,252,.14);
        background:rgba(8,14,28,.62);
        font-size:12px;color:#dbeafe
    }
    .hero-status-item .k{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}
    .hero-status-item .v{font-weight:800;font-variant-numeric:tabular-nums;white-space:nowrap}
    .overview-layout{
        display:grid;grid-template-columns:1fr;gap:16px;
    }
    .board-section{
        display:grid;
        gap:14px;
        padding:20px;
        border-radius:var(--radius-panel);
        border:1px solid rgba(125,211,252,.12);
        background:linear-gradient(180deg, rgba(9,14,27,.92), rgba(5,9,18,.88));
        box-shadow:var(--shadow);
        backdrop-filter:blur(14px);
        position:relative;
        overflow:hidden;
    }
    .board-section::before{
        content:"";
        position:absolute;
        inset:0;
        background:
            linear-gradient(135deg, rgba(125,211,252,.04), transparent 30%),
            linear-gradient(315deg, rgba(192,132,252,.04), transparent 28%);
        pointer-events:none;
    }
    .board-section > *{position:relative;z-index:1}
    .board-section-head{
        display:flex;
        justify-content:space-between;
        gap:12px;
        align-items:flex-end;
        flex-wrap:wrap;
    }
    .board-section-head h2{
        font-size:17px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:#dbe7ff
    }
    .board-section-head p{color:var(--muted);font-size:13px;line-height:1.55}
    .viz-grid{
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:14px;
    }
    .viz-card{
        padding:16px;
        border-radius:18px;
        border:1px solid rgba(125,211,252,.12);
        background:linear-gradient(180deg, rgba(13,20,38,.92), rgba(8,12,24,.86));
        box-shadow:inset 0 1px 0 rgba(255,255,255,.03);
        min-width:0;
        overflow:hidden;
    }
    .viz-card.wide{grid-column:span 2}
    .viz-head{
        display:flex;justify-content:space-between;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px
    }
    .viz-title{
        font-size:14px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#eef4ff
    }
    .viz-subtitle{
        color:var(--muted);font-size:12px;line-height:1.45
    }
    .viz-metrics{
        display:grid;
        grid-template-columns:repeat(4,minmax(0,1fr));
        gap:10px;
        margin-top:10px;
    }
    .viz-metric{
        padding:11px 12px;border-radius:14px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.05)
    }
    .viz-metric .k{display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .viz-metric .v{display:block;margin-top:6px;font-size:16px;font-weight:800;font-variant-numeric:tabular-nums}
    .viz-metric .s{display:block;margin-top:5px;color:var(--muted);font-size:11px;line-height:1.45}
    .risk-grid{
        display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px
    }
    .risk-meter{
        height:8px;border-radius:999px;background:rgba(255,255,255,.06);overflow:hidden;margin-top:8px
    }
    .risk-meter-fill{
        height:100%;border-radius:999px;background:linear-gradient(90deg, #34d399, #7dd3fc, #c084fc);min-width:2%;
    }
    .order-bars{display:grid;gap:10px}
    .order-bar{
        display:grid;
        grid-template-columns:110px minmax(0,1fr) 62px;
        gap:10px;
        align-items:center
    }
    .order-bar .k{color:var(--muted);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
    .order-bar-fill{
        height:10px;border-radius:999px;background:rgba(255,255,255,.06);overflow:hidden
    }
    .order-bar-fill > span{
        display:block;height:100%;border-radius:999px
    }
    .timeline-list{display:grid;gap:10px;max-height:260px;overflow:auto;padding-right:4px}
    .timeline-item{
        display:grid;
        grid-template-columns:120px 1fr;
        gap:12px;
        align-items:start;
        padding:12px 13px;
        border-radius:14px;
        background:rgba(255,255,255,.03);
        border:1px solid rgba(255,255,255,.06)
    }
    .timeline-time{color:var(--muted);font-size:12px;line-height:1.45;font-variant-numeric:tabular-nums}
    .timeline-title{font-size:13px;font-weight:800;color:#fff}
    .timeline-detail{margin-top:4px;color:#cbd5e1;font-size:12px;line-height:1.5}
    .timeline-tone-cyan .timeline-title{color:#bfdbfe}
    .timeline-tone-green .timeline-title{color:#b8f5d0}
    .timeline-tone-yellow .timeline-title{color:#fde68a}
    .timeline-tone-red .timeline-title{color:#fecdd3}
    .timeline-tone-purple .timeline-title{color:#e9d5ff}
    .pause-button{
        display:inline-flex;align-items:center;gap:6px;
        padding:8px 12px;border-radius:999px;border:1px solid rgba(125,211,252,.22);
        background:rgba(125,211,252,.08);color:#d7f0ff;font-size:12px;font-weight:800;letter-spacing:.05em;
        cursor:pointer
    }
    .pause-button.paused{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.22);color:#fde68a}
    .control-grid{
        display:grid;
        grid-template-columns:minmax(0,1.08fr) minmax(0,1fr);
        gap:16px;
        align-items:start
    }
    .two-column{
        display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px;
    }
    .overview-panel{
        padding:20px;border-radius:var(--radius-panel);border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(15,22,40,.92), rgba(9,13,24,.88));
        box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .overview-panel.compact{padding:18px}
    .panel-head{
        display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:12px
    }
    .overview-panel.compact .panel-head{margin-bottom:10px}
    .control-grid .overview-panel{
        min-height:100%
    }
    .panel-head h2{
        font-size:14px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#dbe7ff
    }
    .panel-head .hint{color:var(--muted);font-size:13px;line-height:1.45}
    .summary{
        display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;
    }
    .account-strip{
        display:flex;flex-direction:column;gap:14px
    }
    .account-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
    .order-state-strip{
        display:flex;flex-wrap:wrap;gap:12px;padding:14px 18px;
        background:var(--panel);border:1px solid var(--line);border-radius:var(--radius-card);
        margin-top:16px
    }
    .order-state-section{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
    .order-state-title{font-weight:700;font-size:13px;color:var(--muted);white-space:nowrap}
    .order-state-badge{
        display:inline-block;padding:4px 12px;border-radius:6px;font-size:12px;
        font-weight:600;white-space:nowrap;max-width:420px;overflow:hidden;text-overflow:ellipsis
    }
    .order-state-badge.blocked{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}
    .order-state-badge.failed{background:#fee2e2;color:#991b1b;border:1px solid #fecaca}
    .metric,.section,.card{
        background:var(--panel);border:1px solid var(--line);border-radius:var(--radius-card);box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .metric{padding:16px 18px}
    .metric span{display:block}
    .metric-label{color:var(--muted);font-size:12px;letter-spacing:.12em;text-transform:uppercase}
    .metric-value{margin-top:9px;font-size:26px;font-weight:760;font-variant-numeric:tabular-nums;line-height:1.2}
    .metric-value.small{font-size:21px}
    .position-list{
        display:grid;gap:10px;max-height:520px;overflow:auto;padding-right:4px
    }
    .position-item{
        display:grid;grid-template-columns:minmax(68px,.8fr) minmax(78px,.8fr) minmax(80px,.8fr) minmax(80px,.8fr) minmax(92px,.95fr) minmax(92px,.95fr);
        gap:12px;align-items:center;padding:14px 16px;border-radius:16px;background:rgba(255,255,255,.035);
        border:1px solid var(--line)
    }
    .position-ticker{font-size:16px;font-weight:800;letter-spacing:.02em}
    .position-cell .label{
        display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase
    }
    .position-cell .val{
        display:block;margin-top:6px;color:#fff;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.38
    }
    .position-empty{
        padding:14px 16px;border-radius:16px;background:rgba(255,255,255,.03);
        border:1px solid rgba(255,255,255,.06);color:var(--muted);font-size:13px;line-height:1.45
    }
    .account-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
    .selector-table{display:grid;gap:8px;max-height:520px;overflow:auto;padding-right:4px}
    .selector-head,.selector-row{
        display:grid;
        grid-template-columns:minmax(40px,.4fr) minmax(72px,.8fr) minmax(58px,.55fr) minmax(64px,.6fr) minmax(90px,.95fr);
        gap:6px;
        align-items:center;
    }
    .selector-head{
        color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
        padding:0 4px 4px;
    }
    .selector-row{
        padding:12px 13px;border-radius:14px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06);
        font-size:13px;
    }
    .selector-row .ticker{font-weight:800;color:#fff}
    .selector-row .num{font-weight:700;font-variant-numeric:tabular-nums}
    .selector-row .sector{color:var(--muted)}
    .selector-empty{
        padding:18px;border-radius:16px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06);
        color:var(--muted);font-size:14px;line-height:1.55;
    }
    .compact .selector-table{gap:6px}
    .compact .selector-row{padding:9px 10px;font-size:11px}
    .compact .selector-head{font-size:10px}
    .compact .settings-form{
        padding:9px 10px;margin-bottom:10px
    }
    .compact .settings-field{
        min-width:130px
    }
    .compact .section-meta{
        font-size:11px
    }
    .compact .selector-head,.compact .selector-row{
        grid-template-columns:minmax(40px,.4fr) minmax(72px,.8fr) minmax(58px,.55fr) minmax(64px,.6fr) minmax(90px,.95fr);
    }
    .section-meta{margin-bottom:12px;color:var(--muted);font-size:13px;line-height:1.55}
    .research-brief{
        margin-top:12px;padding:12px 13px;border-radius:14px;
        background:linear-gradient(180deg, rgba(59,130,246,.12), rgba(59,130,246,.06));
        border:1px solid rgba(59,130,246,.22)
    }
    .research-brief-head{
        display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap
    }
    .research-tag{
        display:inline-flex;align-items:center;justify-content:center;
        padding:5px 9px;border-radius:999px;background:rgba(59,130,246,.18);
        color:#dbeafe;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase
    }
    .research-meta{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
    .research-brief-body{margin-top:8px;display:grid;gap:6px}
    .research-brief-title{color:#fff;font-size:14px;font-weight:800}
    .research-brief-summary{color:#e5eefc;font-size:13px;line-height:1.55}
    .research-brief-detail{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:12px;line-height:1.45}
    .research-brief-link{
        display:inline-flex;align-self:start;justify-self:start;margin-top:4px;
        color:#bfdbfe;text-decoration:none;font-size:12px;font-weight:700
    }
    .research-brief-link:hover{text-decoration:underline}
    .system-status{
        margin-top:12px;
        padding:12px;
        border-radius:16px;
        border:1px solid rgba(148,163,184,.18);
        background:linear-gradient(180deg, rgba(15,23,42,.92), rgba(15,23,42,.7));
        box-shadow:0 12px 30px rgba(2,6,23,.16);
        display:grid;
        gap:10px;
    }
    .system-status-head{
        display:flex;
        justify-content:space-between;
        gap:10px;
        align-items:center;
        flex-wrap:wrap;
    }
    .system-status-grid{
        display:grid;
        grid-template-columns:repeat(4,minmax(0,1fr));
        gap:10px;
    }
    .system-status-card{
        min-width:0;
        padding:12px;
        border-radius:14px;
        border:1px solid rgba(148,163,184,.16);
        background:rgba(15,23,42,.56);
        display:grid;
        gap:6px;
    }
    .system-status-card.wide{grid-column:span 2}
    .system-status-card.full{grid-column:1 / -1}
    .shadow-observer-card.status-live{
        border-color:rgba(52,211,153,.28);
        box-shadow:0 0 0 1px rgba(52,211,153,.08), inset 0 1px 0 rgba(255,255,255,.02);
    }
    .shadow-observer-card.status-warn{
        border-color:rgba(251,191,36,.28);
        box-shadow:0 0 0 1px rgba(251,191,36,.08), inset 0 1px 0 rgba(255,255,255,.02);
    }
    .shadow-observer-card.status-offline{
        border-color:rgba(251,113,133,.28);
        box-shadow:0 0 0 1px rgba(251,113,133,.08), inset 0 1px 0 rgba(255,255,255,.02);
    }
    .shadow-metrics-grid{
        display:grid;
        grid-template-columns:repeat(4,minmax(0,1fr));
        gap:8px;
        margin-top:6px;
    }
    .shadow-metric{
        min-width:0;
        padding:8px 10px;
        border-radius:12px;
        border:1px solid rgba(148,163,184,.12);
        background:rgba(2,6,23,.28);
        display:grid;
        gap:4px;
    }
    .shadow-metric span{
        font-size:11px;
        letter-spacing:.08em;
        text-transform:uppercase;
        color:var(--muted);
    }
    .shadow-metric strong{
        font-size:13px;
        font-weight:800;
        color:#eef4ff;
        word-break:break-word;
        font-variant-numeric:tabular-nums;
    }
    .shadow-blocked-top{
        display:flex;
        flex-wrap:wrap;
        gap:6px;
        margin-top:8px;
    }
    .shadow-chip{
        display:inline-flex;
        align-items:center;
        gap:6px;
        padding:6px 10px;
        border-radius:999px;
        border:1px solid rgba(125,211,252,.18);
        background:rgba(125,211,252,.08);
        color:#d7f0ff;
        font-size:11px;
        font-weight:700;
        letter-spacing:.04em;
    }
    .system-status-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
    .system-status-value{font-size:15px;font-weight:800;color:#fff;word-break:break-word}
    .system-status-detail{font-size:12px;color:var(--muted);line-height:1.45;word-break:break-word}
    .system-status-orders{
        display:flex;
        gap:8px;
        flex-wrap:wrap;
    }
    .system-order-pill{
        display:inline-flex;
        align-items:center;
        gap:6px;
        padding:6px 10px;
        border-radius:999px;
        border:1px solid rgba(148,163,184,.18);
        background:rgba(15,23,42,.58);
        color:#dbeafe;
        font-size:12px;
        font-variant-numeric:tabular-nums;
    }
    .settings-form{
        display:flex;gap:8px;align-items:end;flex-wrap:wrap;
        margin-bottom:12px;padding:10px 12px;border-radius:14px;
        background:var(--panel-strong);border:1px solid rgba(255,255,255,.06)
    }
    .settings-field{display:flex;flex-direction:column;gap:5px;min-width:125px}
    .settings-field label{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .settings-field input{
        border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:#fff;
        border-radius:10px;padding:9px 11px;font-size:14px;font-weight:600
    }
    .settings-button{
        border:1px solid rgba(52,211,153,.24);background:rgba(52,211,153,.12);color:#b8f5d0;
        border-radius:10px;padding:9px 13px;font-size:13px;font-weight:800;cursor:pointer
    }
    .settings-button.secondary{
        border-color:rgba(125,211,252,.24);background:rgba(125,211,252,.12);color:#d7f0ff
    }
    .settings-note{color:var(--muted);font-size:12px;line-height:1.45;margin-left:auto}
    .stat-box{
        padding:16px;border-radius:16px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06)
    }
    .stat-label{display:block;color:var(--muted);font-size:12px;letter-spacing:.09em;text-transform:uppercase}
    .stat-value{
        margin-top:10px;display:block;color:#fff;font-size:20px;font-weight:700;line-height:1.4;
        font-variant-numeric:tabular-nums;word-break:break-word
    }
    .stat-value.muted{color:var(--muted);font-weight:500}
    .cards-section{
        display:flex;
        flex-direction:column;
        gap:14px;
        padding:20px;
        border-radius:var(--radius-panel);
        border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(15,22,40,.92), rgba(9,13,24,.88));
        box-shadow:var(--shadow);
        backdrop-filter:blur(14px)
    }
    .cards-section-head{
        display:flex;justify-content:space-between;gap:12px;align-items:flex-end;margin-bottom:10px
    }
    .cards-section-head h2{
        font-size:17px;font-weight:760;letter-spacing:.08em;text-transform:uppercase;color:#dbe7ff
    }
    .cards-section-head p{color:var(--muted);font-size:13px;line-height:1.55}
    .cards{
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:16px;
        align-items:stretch;
        overflow:visible;
    }
    .card{
        padding:20px;min-width:0;min-height:100%;
        background:
            linear-gradient(180deg, rgba(16,24,44,.96), rgba(9,13,24,.88)),
            radial-gradient(circle at top right, rgba(125,211,252,.06), transparent 34%);
        overflow:hidden;border:1px solid var(--line);border-radius:var(--radius-card);box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .card.featured-buy{border-color:rgba(52,211,153,.28)}
    .card.featured-sell{border-color:rgba(251,113,133,.28)}
    .card.featured-dual{border-color:rgba(125,211,252,.32)}
    .card-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}
    .card-title{min-width:0}
    .card-title .ticker{display:block;font-size:20px;font-weight:800;letter-spacing:.02em;line-height:1.1}
    .card-title .desc{display:block;margin-top:6px;color:var(--muted);font-size:13px;line-height:1.5}
    .card-spot{
        display:inline-flex;align-items:center;gap:6px;margin-top:8px;padding:5px 9px;border-radius:999px;
        font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase
    }
    .card-spot.buy{background:rgba(52,211,153,.12);color:#b8f5d0}
    .card-spot.sell{background:rgba(251,113,133,.12);color:#fecdd3}
    .card-spot.dual{background:rgba(125,211,252,.12);color:#d7f0ff}
    .badges{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}
    .badge{
        display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;font-size:11px;font-weight:700;
        letter-spacing:.08em;text-transform:uppercase;border:1px solid transparent
    }
    .badge.live{background:rgba(52,211,153,.1);color:#b8f5d0;border-color:rgba(52,211,153,.2)}
    .badge.offline{background:rgba(148,163,184,.1);color:#cbd5e1;border-color:rgba(148,163,184,.16)}
    .badge.halted{background:rgba(251,191,36,.1);color:#fde68a;border-color:rgba(251,191,36,.22)}
    .green{color:var(--up)} .red{color:var(--down)} .yellow{color:var(--warn)}
    .price-row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:6px 0 12px}
    .price{font-size:30px;line-height:1;font-weight:800;font-variant-numeric:tabular-nums}
    .change{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
    .quote-strip{
        display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px
    }
    .strip-box{
        padding:11px 12px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
    }
    .strip-box .label{display:block;color:var(--muted);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
    .strip-box .val{display:block;margin-top:6px;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.38}
    .range-block{margin-bottom:12px}
    .row{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:13px;line-height:1.48}
    .row .label{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:12px}
    .row .val{font-weight:700;font-variant-numeric:tabular-nums}
    .range-bar{
        margin-top:8px;height:7px;border-radius:999px;background:rgba(255,255,255,.07);overflow:hidden
    }
    .range-fill{height:100%;border-radius:999px;transition:width .45s ease}
    .signal{
        display:flex;align-items:center;justify-content:center;min-height:38px;margin-bottom:8px;border-radius:12px;
        border:1px solid transparent;font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;text-align:center
    }
    .signal-note{
        margin-top:0;margin-bottom:12px;color:var(--muted);font-size:12px;line-height:1.5;
        min-height:34px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden
    }
    .pnl-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
    .grid-quote{
        display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-bottom:6px
    }
    .quote-item{
        padding:11px 12px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
    }
    .quote-item .label{display:block;color:var(--muted);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
    .quote-item .val{
        display:block;margin-top:6px;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.38
    }
    .selection-brief{
        display:grid;gap:10px;margin-top:10px
    }
    .selection-brief-item{
        display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:start;
        padding:12px 13px;border-radius:14px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
    }
    .selection-tag{
        display:inline-flex;align-items:center;justify-content:center;min-width:62px;
        padding:6px 10px;border-radius:999px;background:rgba(125,211,252,.12);border:1px solid rgba(125,211,252,.2);
        color:#d7f0ff;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase
    }
    .selection-tag.live{
        background:rgba(52,211,153,.12);border-color:rgba(52,211,153,.22);color:#b8f5d0
    }
    .selection-copy{min-width:0}
    .selection-copy .symbols{
        display:block;color:#fff;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.45;word-break:break-word
    }
    .selection-copy .note{
        display:block;margin-top:5px;color:var(--muted);font-size:12px;line-height:1.45
    }
    .selection-status{
        margin-top:10px;color:var(--muted);font-size:12px;line-height:1.45
    }
    .source-chip{
        display:inline-flex;align-items:center;gap:6px;margin-top:10px;
        padding:6px 10px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.08em;
        text-transform:uppercase;background:rgba(125,211,252,.12);color:#c6ecff;border:1px solid rgba(125,211,252,.2)
    }
    .source-chip.fallback{background:rgba(251,191,36,.12);color:#fde68a;border-color:rgba(251,191,36,.2)}
    .source-chip.offline{background:rgba(148,163,184,.12);color:#cbd5e1;border-color:rgba(148,163,184,.2)}
    .audit-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
    .scope-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
    .scope-tab{
        display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;
        border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.03);color:var(--muted);
        text-decoration:none;font-size:12px;font-weight:700;letter-spacing:.05em
    }
    .scope-tab.active{background:rgba(52,211,153,.12);color:#b8f5d0;border-color:rgba(52,211,153,.22)}
    .warning-banner{
        margin-top:12px;padding:12px 14px;border-radius:14px;background:rgba(251,113,133,.12);
        color:#fecdd3;border:1px solid rgba(251,113,133,.24);font-size:13px;font-weight:700
    }
    .guard-banner{
        margin-top:-4px;padding:12px 14px;border-radius:14px;font-size:13px;font-weight:700;
        border:1px solid rgba(255,255,255,.08)
    }
    .guard-banner.live{
        background:rgba(52,211,153,.12);color:#b8f5d0;border-color:rgba(52,211,153,.22)
    }
    .guard-banner.warn{
        background:rgba(251,191,36,.12);color:#fde68a;border-color:rgba(251,191,36,.22)
    }
    .guard-banner.blocked{
        background:rgba(251,113,133,.12);color:#fecdd3;border-color:rgba(251,113,133,.24)
    }
    .ticker-audit-list{display:grid;grid-template-columns:1fr;gap:10px;margin-top:12px}
    .ticker-audit-item{
        padding:12px 13px;border-radius:14px;background:rgba(255,255,255,.03);
        border:1px solid rgba(255,255,255,.06)
    }
    .ticker-audit-head{display:flex;justify-content:space-between;gap:10px;align-items:center}
    .ticker-audit-title{font-size:15px;font-weight:800;color:#eef4ff}
    .ticker-audit-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}
    .ticker-audit-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
    .mini-chart{
        margin-top:12px;padding:12px 13px;border-radius:14px;background:rgba(255,255,255,.03);
        border:1px solid rgba(255,255,255,.06)
    }
    .mini-chart-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}
    .mini-chart-title{font-size:13px;font-weight:800;color:#eef4ff}
    .mini-chart-meta{color:var(--muted);font-size:12px;white-space:nowrap}
    .mini-chart-body{position:relative;min-height:124px}
    .mini-chart-empty{
        position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
        color:var(--muted);font-size:12px;letter-spacing:.02em
    }
    .mini-chart-svg{width:100%;height:124px;display:block;overflow:visible}
    .mini-chart-line{fill:none;stroke:#7dd3fc;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
    .mini-chart-grid{stroke:rgba(148,163,184,.12);stroke-width:1}
    .mini-chart-point-buy{fill:#34d399;stroke:#0f172a;stroke-width:1.2}
    .mini-chart-point-sell{fill:#fb7185;stroke:#0f172a;stroke-width:1.2}
    .mini-chart-label{
        font-size:10px;font-weight:800;fill:#fff;text-anchor:middle;dominant-baseline:middle
    }
    .mini-chart-trades{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
    .mini-chart-trade{
        display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;
        font-size:11px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.03);color:#d1d5db
    }
    .mini-chart-trade.buy{border-color:rgba(52,211,153,.22);color:#b8f5d0;background:rgba(52,211,153,.08)}
    .mini-chart-trade.sell{border-color:rgba(251,113,133,.24);color:#fecdd3;background:rgba(251,113,133,.08)}
    .mini-chart-trade .side{font-weight:900;letter-spacing:.08em}
    .sparkline{display:none}
    .spark-bar{flex:1;min-width:2px;border-radius:999px;opacity:.95}
    .sig-buy{background:rgba(52,211,153,.1);color:#b8f5d0;border-color:rgba(52,211,153,.22)}
    .sig-sell{background:rgba(251,113,133,.1);color:#fecdd3;border-color:rgba(251,113,133,.24)}
    .sig-hold{background:rgba(148,163,184,.08);color:#d1d5db;border-color:rgba(148,163,184,.16)}
    .sig-block{background:rgba(251,191,36,.1);color:#fde68a;border-color:rgba(251,191,36,.22)}
    .refresh{text-align:right;color:var(--muted);font-size:12px;line-height:1.45}
    .offline{opacity:.72}
    @media (max-width:1180px){
        .headline-stats{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0}
        .overview-layout,.control-grid,.two-column,.cards,.viz-grid,.risk-grid{grid-template-columns:1fr}
        .account-grid,.audit-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
        .selector-head,.selector-row,.position-item{grid-template-columns:repeat(5,minmax(0,1fr))}
        .viz-card.wide{grid-column:span 1}
        .board-section{padding:16px}
    }
    @media (max-width:760px){
        body{padding:14px;overflow:auto}
        .page{min-height:auto}
        .topbar{flex-direction:column;position:static}
        .brand h1{font-size:27px}
        .brand p{font-size:13px}
        .headline-stats{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0}
        .headline-stat{padding:14px 15px}
        .headline-stat .value{font-size:24px}
        .account-grid,.audit-grid,.cards,.grid-quote,.pnl-grid,.summary,.overview-layout,.control-grid,.two-column,.audit-strip,.system-status-grid,.shadow-metrics-grid{grid-template-columns:1fr}
        .viz-grid,.risk-grid{grid-template-columns:1fr}
        .hero-status-grid{grid-template-columns:1fr}
        .viz-card.wide{grid-column:span 1}
        .settings-form{align-items:stretch}
        .settings-note{margin-left:0;width:100%}
        .price{font-size:30px}
        .selector-head{display:none}
        .selector-row,.position-item{grid-template-columns:repeat(2,minmax(0,1fr))}
        .position-list,.selector-table{max-height:none}
    }
</style>
</head>
<body>
<div class="page">
    <div class="topbar">
        <div class="brand">
            <h1>SOXS 区间套利交易系统</h1>
            <p>AUTOMATED TRADING · RISK CONTROL · LONGBRIDGE EXECUTION · 只读监控大屏，展示 PAPER / SANDBOX / PROD 的实时状态、区间位置、成交与风控。</p>
            <div class="headline-stats">
                <div class="headline-stat">
                    <span class="label">账户总资产</span>
                    <span class="value {% if account_equity_value is not none and account_equity_value >= 0 %}green{% else %}yellow{% endif %}" id="headline-total-equity">
                        {% if account_equity_value is not none %}${{ "%.2f"|format(account_equity_value) }}{% else %}Unavailable{% endif %}
                    </span>
                    <span class="sub" id="headline-total-equity-sub">数据源：{{ system_status.account_source or 'unavailable' }}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">可用现金</span>
                    <span class="value {% if available_cash_display is not none and available_cash_display >= 0 %}green{% else %}yellow{% endif %}" id="headline-available-cash">
                        {% if available_cash_display is not none %}${{ "%.2f"|format(available_cash_display) }}{% else %}Unavailable{% endif %}
                    </span>
                    <span class="sub" id="headline-available-cash-sub">资金来源：{{ system_status.account_source or 'no data' }}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">当前持仓</span>
                    <span class="value" id="headline-position-count">{{ selected_positions_count }}</span>
                    <span class="sub" id="headline-position-sub">
                        {% if display_positions %}
                            {{ display_positions|map(attribute='ticker')|join(' / ') }}
                        {% else %}
                            暂无持仓
                        {% endif %}
                    </span>
                </div>
                <div class="headline-stat">
                    <span class="label">今日盈亏</span>
                    <span class="value {% if today_total_pnl >= 0 %}green{% else %}red{% endif %}" id="headline-today-pnl">${{ "%+.2f"|format(today_total_pnl) }}</span>
                    <span class="sub" id="headline-today-pnl-sub">按 3 路策略今日盈亏汇总 / 总成交 {{ total_trades }} 笔</span>
                </div>
                <div class="headline-stat">
                    <span class="label">活动订单</span>
                    <span class="value" id="headline-active-orders">{{ active_order_summary.pending }}</span>
                    <span class="sub" id="headline-active-orders-sub">PENDING {{ active_order_summary.pending }} · PARTIAL {{ active_order_summary.partial_filled }}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">系统状态</span>
                    <span class="value {{ runtime_state_class }}" id="headline-system-state">{{ runtime_state_value }}</span>
                    <span class="sub" id="headline-system-state-sub">Reduce-Only {{ 'ON' if system_status.global_reduce_only else 'OFF' }} · Live Order {{ 'ON' if system_status.live_order_enabled else 'OFF' }}</span>
                </div>
            </div>
        </div>
        <div class="status-row">
            <span class="pill {{ mode_class }}" id="mode-pill">
                <strong>{{ mode_display }}</strong>
            </span>
            <span class="pill {{ market_pill_class }}" id="market-pill">市场：{{ system_status.market_open_label or 'unavailable' }}</span>
            <span class="pill {{ system_status.broker_connected and 'status-live' or 'status-offline' }}" id="broker-pill">Broker：{{ system_status.broker_connection or 'not connected' }}</span>
            <a class="pill research" href="{{ research_url }}" target="_blank" rel="noopener">只读研究简报</a>
            <span class="pill {{ startup_guard.level }}">
                {{ startup_guard.label }}
            </span>
            <span class="pill">最后更新时间 <span id="last-updated-pill">{{ update_time }}</span></span>
            {% if live_account and live_account.data_stale %}
                {% if live_account.account_error %}
            <span class="pill warn">实盘账户异常 · 账户拉取失败 · {{ live_account.stale_reason }}</span>
                {% else %}
            <span class="pill warn">账户数据已过期 · {{ live_account.fetched_at or '未知时间' }}</span>
                {% endif %}
            {% endif %}
            {% if footer_buying_power is not none %}
            <span class="pill live">{{ account_labels.footer_buying_power }} ${{ "%.2f"|format(footer_buying_power) }}</span>
            {% endif %}
            {% if trade_audit.unresolved_alert %}
            <span class="pill warn">未决订单告警 · {{ trade_audit.broker_unresolved_count }} 笔</span>
            {% endif %}
        </div>
    </div>
    <div class="guard-banner {% if startup_guard.level == 'live' %}live{% elif startup_guard.level == 'warn' %}warn{% else %}blocked{% endif %}">
        {{ startup_guard.detail }}
        · 要求美东日期 {{ startup_guard.required_date }}
        {% if startup_guard.state_date %} · 当前状态日期 {{ startup_guard.state_date }}{% endif %}
    </div>
    <div class="system-status">
        <div class="system-status-head">
            <div>
                <h2 style="margin:0;font-size:18px;letter-spacing:.08em;text-transform:uppercase;color:#dbe7ff">系统状态</h2>
                <div class="hint" style="margin-top:4px">只读展示 · 不触发下单 · 不修改环境变量或状态</div>
            </div>
            <span class="pill {{ 'mode-live' if system_status.mode_key == 'live' else 'mode-sandbox' if system_status.mode_key == 'sandbox' else 'mode-paper' }}" id="system-pill">
                {{ system_status.mode }}{% if system_status.mode_key == 'paper' %} · 虚拟盘{% elif system_status.mode_key == 'sandbox' %} · 沙盒{% elif system_status.mode_key == 'live' %} · 实盘账户{% endif %}
            </span>
        </div>
        <div class="system-status-grid">
            <div class="system-status-card">
                <span class="system-status-label">API 状态</span>
                <span class="system-status-value" id="system-api-status">{{ system_status.api_status or 'unavailable' }}</span>
                <span class="system-status-detail" id="system-last-updated">最后更新时间：{{ system_status.last_updated or 'unavailable' }}</span>
            </div>
            <div class="system-status-card">
                <span class="system-status-label">broker 类型</span>
                <span class="system-status-value" id="system-broker-type">{{ system_status.broker_type or 'unavailable' }}</span>
                <span class="system-status-detail" id="system-broker-connection">连接状态：{{ system_status.broker_connection or 'not connected' }}</span>
            </div>
            <div class="system-status-card {{ 'status-warn' if mode_consistency.mixed else '' }}">
                <span class="system-status-label">TOP 引擎模式</span>
                <span class="system-status-value" id="system-top-engine-mode">{{ mode_consistency.label }}</span>
                <span class="system-status-detail" id="system-top-engine-mode-detail">{{ mode_consistency.detail }}</span>
            </div>
            <div class="system-status-card">
                <span class="system-status-label">数据来源</span>
                <span class="system-status-value" id="system-data-source">{{ system_status.data_source or 'no data' }}</span>
                <span class="system-status-detail" id="system-account-source">{{ system_status.account_source or 'no data' }}</span>
            </div>
            <div class="system-status-card">
                <span class="system-status-label">市场状态</span>
                <span class="system-status-value" id="system-market-label">{{ system_status.market_open_label or 'unavailable' }}</span>
                <span class="system-status-detail" id="system-market-detail">{{ system_status.market_open_detail or 'no data' }}</span>
            </div>
            <div class="system-status-card">
                <span class="system-status-label">Global Reduce-Only</span>
                <span class="system-status-value" id="system-reduce-only">{{ 'ENABLED' if system_status.global_reduce_only else 'DISABLED' }}</span>
                <span class="system-status-detail">全局只减仓开关</span>
            </div>
            <div class="system-status-card">
                <span class="system-status-label">Live Order</span>
                <span class="system-status-value" id="system-live-order">{{ 'ENABLED' if system_status.live_order_enabled else 'DISABLED' }}</span>
                <span class="system-status-detail">sandbox / paper 下默认应为 disabled</span>
            </div>
            <div class="system-status-card wide">
                <span class="system-status-label">活动订单</span>
                <span class="system-status-value" id="system-orders-status">{{ system_status.active_orders.status_label or 'no data' }}</span>
                <span class="system-status-detail" id="system-orders-detail">{{ system_status.active_orders.detail or 'no data' }}</span>
                <div class="system-status-orders" style="margin-top:6px">
                    {% for order in system_status.active_orders.orders[:4] %}
                    <span class="system-order-pill">
                        {{ order.ticker }} {{ order.side }} {{ order.quantity }}股 · {{ order.status }}
                    </span>
                    {% endfor %}
                    {% if not system_status.active_orders.orders %}
                    <span class="system-status-detail">no data</span>
                    {% endif %}
                </div>
            </div>
            <div class="system-status-card wide">
                <span class="system-status-label">最近一次 weekend paper lifecycle</span>
                <span class="system-status-value" id="system-weekend-lifecycle">{{ system_status.lifecycle.weekend_paper.status_label or 'unavailable' }}</span>
                <span class="system-status-detail" id="system-weekend-detail">{{ system_status.lifecycle.weekend_paper.detail or 'no data' }}</span>
                <span class="system-status-detail" id="system-weekend-time">报告：{{ system_status.lifecycle.weekend_paper.generated_at or 'no data' }}</span>
            </div>
            <div class="system-status-card wide">
                <span class="system-status-label">最近一次 LongBridge sandbox lifecycle</span>
                <span class="system-status-value" id="system-sandbox-lifecycle">{{ system_status.lifecycle.longbridge_sandbox.status_label or 'unavailable' }}</span>
                <span class="system-status-detail" id="system-sandbox-detail">{{ system_status.lifecycle.longbridge_sandbox.detail or 'no data' }}</span>
                <span class="system-status-detail" id="system-sandbox-time">报告：{{ system_status.lifecycle.longbridge_sandbox.generated_at or 'no data' }}</span>
            </div>
            <div class="system-status-card wide research-status-card {{ 'status-live' if research_status.state == 'SAFE' else 'status-warn' if research_status.state == 'STALE' else 'status-offline' }}" id="research-status-card">
                <span class="system-status-label" id="research-status-title">AI Research Scheduler</span>
                <span class="system-status-value" id="research-status-state">{{ research_status.status_label or 'unavailable' }}</span>
                <span class="system-status-detail" id="research-status-detail">{{ research_status.detail or 'no data' }}</span>
                <div class="shadow-metrics-grid" style="margin-top: 8px;">
                    <div class="shadow-metric">
                        <span>Last Research Run</span>
                        <strong id="research-status-last-run">{{ research_status.last_research_run or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Research Date</span>
                        <strong id="research-status-date">{{ research_status.research_date or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Candidate Count</span>
                        <strong id="research-status-candidate-count">{{ research_status.candidate_count if research_status.candidate_count is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Report Status</span>
                        <strong id="research-status-report-status">{{ research_status.report_status or 'unavailable' }}</strong>
                    </div>
                </div>
            </div>
            <div class="system-status-card full shadow-observer-card {{ shadow_status_class }}" id="shadow-observer-card">
                <span class="system-status-label" id="shadow-title">{{ shadow_status.title or 'Shadow Observer' }}</span>
                <span class="system-status-value" id="shadow-state">{{ shadow_status.state_label or 'STALE' }}</span>
                <span class="system-status-detail" id="shadow-detail">{{ shadow_status.detail or 'no data' }}</span>
                <div class="shadow-metrics-grid">
                    <div class="shadow-metric">
                        <span>Mode</span>
                        <strong id="shadow-mode">{{ shadow_status.mode or 'READ-ONLY SHADOW' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Safety Gate</span>
                        <strong id="shadow-safety-gate">{{ shadow_status.safety_gate or 'STALE' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Quote API Only</span>
                        <strong id="shadow-quote-only">{{ 'true' if shadow_status.quote_api_only else 'false' if shadow_status.quote_api_only is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Trade API Used</span>
                        <strong id="shadow-trade-api">{{ 'true' if shadow_status.trade_api_used else 'false' if shadow_status.trade_api_used is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>TradeContext Initialized</span>
                        <strong id="shadow-trade-context">{{ 'true' if shadow_status.trade_context_initialized else 'false' if shadow_status.trade_context_initialized is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Last Run</span>
                        <strong id="shadow-last-run">{{ shadow_status.last_run_at or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Latest Bar UTC</span>
                        <strong id="shadow-latest-bar-utc">{{ shadow_status.latest_processed_bar_utc or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Latest Bar ET</span>
                        <strong id="shadow-latest-bar-et">{{ shadow_status.latest_processed_bar_et or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Data Freshness</span>
                        <strong id="shadow-data-freshness">{{ shadow_status.data_freshness or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Benchmark Status</span>
                        <strong id="shadow-benchmark-status">{{ shadow_status.benchmark_status or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>SOXX / SMH Alignment</span>
                        <strong id="shadow-alignment-status">{{ shadow_status.alignment_status or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Signals / Orders / Trades</span>
                        <strong id="shadow-flow-counts">{{ shadow_status.signals_generated or 0 }} / {{ shadow_status.simulated_orders or 0 }} / {{ shadow_status.simulated_trades or 0 }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Open Simulated Positions</span>
                        <strong id="shadow-open-positions">{{ shadow_status.open_simulated_positions if shadow_status.open_simulated_positions is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Simulated Equity</span>
                        <strong id="shadow-equity">{{ "%.2f"|format(shadow_status.simulated_equity) if shadow_status.simulated_equity is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Simulated Return</span>
                        <strong id="shadow-return">{{ "%+.2f"|format((shadow_status.simulated_return or 0) * 100) if shadow_status.simulated_return is not none else 'unavailable' }}{% if shadow_status.simulated_return is not none %}%{% endif %}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Simulated Drawdown</span>
                        <strong id="shadow-drawdown">{{ "%+.2f"|format((shadow_status.simulated_drawdown or 0) * 100) if shadow_status.simulated_drawdown is not none else 'unavailable' }}{% if shadow_status.simulated_drawdown is not none %}%{% endif %}</strong>
                    </div>
                </div>
                <div class="shadow-blocked-top" id="shadow-blocked-top">
                    {% for item in shadow_status.blocked_reason_top5 or [] %}
                    <span class="shadow-chip">{{ item.reason }} · {{ item.count }}</span>
                    {% endfor %}
                    {% if not shadow_status.blocked_reason_top5 %}
                    <span class="system-status-detail">no data</span>
                    {% endif %}
                </div>
            </div>
            <div class="system-status-card full candidate-validation-card {{ candidate_status_class }}" id="candidate-validation-card">
                <span class="system-status-label" id="candidate-title">{{ candidate_validation.title or 'AI Candidate Validation' }}</span>
                <span class="system-status-value" id="candidate-state">{{ candidate_validation.status_label or 'STALE' }}</span>
                <span class="system-status-detail" id="candidate-detail">{{ candidate_validation.detail or 'no data' }}</span>
                <div class="board-section-head" style="margin-top: 8px;">
                    <span>Factor Scores</span>
                </div>
                <div class="shadow-metrics-grid">
                    <div class="shadow-metric">
                        <span>Symbol</span>
                        <strong id="candidate-symbol">{{ candidate_validation.latest_candidate.symbol or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Asset Type</span>
                        <strong id="candidate-asset-type">{{ candidate_validation.latest_candidate.asset_type or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>AI Score</span>
                        <strong id="candidate-ai-score">{{ candidate_validation.latest_candidate.ai_score if candidate_validation.latest_candidate.ai_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Candidate Score</span>
                        <strong id="candidate-candidate-score">{{ candidate_validation.latest_candidate.candidate_score if candidate_validation.latest_candidate.candidate_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Benchmarks</span>
                        <strong id="candidate-benchmarks">{{ candidate_validation.latest_candidate.benchmarks|join(' / ') if candidate_validation.latest_candidate.benchmarks else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Strategy Family</span>
                        <strong id="candidate-strategy-family">{{ candidate_validation.latest_candidate.strategy_family or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Liquidity Score</span>
                        <strong id="candidate-liquidity-score">{{ candidate_validation.latest_candidate.liquidity_score if candidate_validation.latest_candidate.liquidity_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Trend Score</span>
                        <strong id="candidate-trend-score">{{ candidate_validation.latest_candidate.trend_score if candidate_validation.latest_candidate.trend_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Volatility Score</span>
                        <strong id="candidate-volatility-score">{{ candidate_validation.latest_candidate.volatility_score if candidate_validation.latest_candidate.volatility_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Risk Score</span>
                        <strong id="candidate-risk-score">{{ candidate_validation.latest_candidate.risk_score if candidate_validation.latest_candidate.risk_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Strategy Fit</span>
                        <strong id="candidate-strategy-fit-score">{{ candidate_validation.latest_candidate.strategy_fit_score if candidate_validation.latest_candidate.strategy_fit_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Recommended Strategy</span>
                        <strong id="candidate-recommended-strategy">{{ candidate_validation.latest_candidate.recommended_strategy or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>AI Ranking Reason</span>
                        <strong id="candidate-score-reason">{{ candidate_validation.latest_candidate.score_reason or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Validation Status</span>
                        <strong id="candidate-validation-status">{{ candidate_validation.latest_candidate.validation_status or 'AI_CANDIDATE' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Selection Stage</span>
                        <strong id="candidate-selection-stage">{{ candidate_validation.latest_candidate.selection_stage or candidate_validation.selection_stage or 'PRELIMINARY' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Last Completed Session</span>
                        <strong id="candidate-last-completed-session">{{ candidate_validation.latest_candidate.last_completed_session or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Daily Data As Of</span>
                        <strong id="candidate-daily-data-as-of">{{ candidate_validation.latest_candidate.daily_data_as_of or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Premarket Snapshot At</span>
                        <strong id="candidate-premarket-snapshot-at">{{ candidate_validation.latest_candidate.premarket_snapshot_at or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Premarket Change</span>
                        <strong id="candidate-premarket-change">{{ candidate_validation.latest_candidate.premarket_change_pct if candidate_validation.latest_candidate.premarket_change_pct is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Gap</span>
                        <strong id="candidate-gap-pct">{{ candidate_validation.latest_candidate.gap_pct if candidate_validation.latest_candidate.gap_pct is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Premarket Volume</span>
                        <strong id="candidate-premarket-volume">{{ candidate_validation.latest_candidate.premarket_volume if candidate_validation.latest_candidate.premarket_volume is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Data Freshness</span>
                        <strong id="candidate-freshness-status">{{ candidate_validation.latest_candidate.freshness_status or candidate_validation.status_label or 'STALE' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Data Mode</span>
                        <strong id="candidate-data-mode">{{ candidate_validation.latest_candidate.data_mode or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Universe Filter</span>
                        <strong id="candidate-universe-filter">{{ '通过' if candidate_validation.latest_candidate.trade_filter_passed else '拒绝' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Data Sufficiency</span>
                        <strong id="candidate-data-sufficiency">{{ '通过' if candidate_validation.latest_candidate.data_status == 'VALID' else '失败' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Scoring Eligible</span>
                        <strong id="candidate-scoring-eligible">{{ 'YES' if candidate_validation.latest_candidate.scoring_eligible else 'NO' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Trade Admission</span>
                        <strong id="candidate-trade-admission">{{ candidate_validation.latest_candidate.trade_admission_status or 'NOT_TRADABLE' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Scoring Block Reason</span>
                        <strong id="candidate-scoring-block-reason">{{ candidate_validation.latest_candidate.scoring_block_reason or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Stale Reason</span>
                        <strong id="candidate-stale-reason">{{ candidate_validation.latest_candidate.stale_reason or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Evidence Status</span>
                        <strong id="candidate-evidence-status">{{ candidate_validation.latest_candidate.evidence_status or 'INSUFFICIENT_EVIDENCE' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Profitability Status</span>
                        <strong id="candidate-profitability-status">{{ candidate_validation.latest_candidate.profitability_status or 'INELIGIBLE' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Deployment Status</span>
                        <strong id="candidate-deployment-status">{{ candidate_validation.latest_candidate.deployment_status or 'INELIGIBLE' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Rejection Reason</span>
                        <strong id="candidate-rejection-reason">{{ candidate_validation.latest_candidate.rejection_reason or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Last Updated</span>
                        <strong id="candidate-last-updated">{{ candidate_validation.last_updated or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Records</span>
                        <strong id="candidate-record-count">{{ candidate_validation.candidate_count if candidate_validation.candidate_count is not none else 'unavailable' }}</strong>
                    </div>
                </div>
                <div class="board-section-head" style="margin-top: 12px;">
                    <span>Candidate Ranking Performance</span>
                </div>
                <div class="shadow-metrics-grid">
                    <div class="shadow-metric">
                        <span>Average Score</span>
                        <strong id="candidate-performance-average-score">{{ candidate_validation.performance.average_score if candidate_validation.performance and candidate_validation.performance.average_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>High Score Success Rate</span>
                        <strong id="candidate-performance-high-score-rate">{{ candidate_validation.performance.high_score_success_rate if candidate_validation.performance and candidate_validation.performance.high_score_success_rate is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Score Bucket Distribution</span>
                        <strong id="candidate-performance-buckets">
                            {% if candidate_validation.performance and candidate_validation.performance.score_bucket_distribution %}
                                {% for bucket in candidate_validation.performance.score_bucket_distribution %}
                                    {{ bucket.score_bucket }} · {{ bucket.candidate_count }} · {{ bucket.data_valid_rate if bucket.data_valid_rate is not none else 'n/a' }}% · {{ bucket.backtest_complete_rate if bucket.backtest_complete_rate is not none else 'n/a' }}% · {{ bucket.walk_forward_complete_rate if bucket.walk_forward_complete_rate is not none else 'n/a' }}%{% if not loop.last %}<br>{% endif %}
                                {% endfor %}
                            {% else %}
                                unavailable
                            {% endif %}
                        </strong>
                    </div>
                </div>
            </div>
            <div class="system-status-card full candidate-model-card {{ candidate_model_status_class }}" id="candidate-model-card">
                <span class="system-status-label" id="candidate-model-title">{{ candidate_model_evaluation.title or 'Candidate Model Evaluation' }}</span>
                <span class="system-status-value" id="candidate-model-status">{{ candidate_model_evaluation.approval_status or 'DRAFT' }}</span>
                <span class="system-status-detail" id="candidate-model-detail">{{ candidate_model_evaluation.recommended_action or 'collect_more_samples' }}</span>
                <div class="board-section-head" style="margin-top: 8px;">
                    <span>Model Snapshot</span>
                </div>
                <div class="shadow-metrics-grid">
                    <div class="shadow-metric">
                        <span>Active Model</span>
                        <strong id="candidate-model-active-version">{{ candidate_model_evaluation.active_model_version or 'baseline_v1' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Challenger</span>
                        <strong id="candidate-model-challenger-version">{{ candidate_model_evaluation.challenger_version or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Training Samples</span>
                        <strong id="candidate-model-training-sample-count">{{ candidate_model_evaluation.training_sample_count if candidate_model_evaluation.training_sample_count is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Training Period</span>
                        <strong id="candidate-model-training-period">{{ candidate_model_evaluation.training_period.start or 'unavailable' }} -> {{ candidate_model_evaluation.training_period.end or 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Baseline Score</span>
                        <strong id="candidate-model-baseline-score">{{ candidate_model_evaluation.comparison.baseline_score if candidate_model_evaluation.comparison and candidate_model_evaluation.comparison.baseline_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Challenger Score</span>
                        <strong id="candidate-model-challenger-score">{{ candidate_model_evaluation.comparison.challenger_score if candidate_model_evaluation.comparison and candidate_model_evaluation.comparison.challenger_score is not none else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Approval Status</span>
                        <strong id="candidate-model-approval-status">{{ candidate_model_evaluation.approval_status or 'DRAFT' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Recommended Action</span>
                        <strong id="candidate-model-recommended-action">{{ candidate_model_evaluation.recommended_action or 'collect_more_samples' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Sample Size Warning</span>
                        <strong id="candidate-model-sample-warning">{{ 'YES' if candidate_model_evaluation.sample_size_warning else 'NO' }}</strong>
                    </div>
                    <div class="shadow-metric">
                        <span>Overfitting Warning</span>
                        <strong id="candidate-model-overfitting-warning">{{ 'YES' if candidate_model_evaluation.overfitting_warning else 'NO' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Baseline Metrics</span>
                        <strong id="candidate-model-baseline-metrics">{{ candidate_model_evaluation.baseline_metrics|tojson if candidate_model_evaluation.baseline_metrics else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Challenger Metrics</span>
                        <strong id="candidate-model-challenger-metrics">{{ candidate_model_evaluation.challenger_metrics|tojson if candidate_model_evaluation.challenger_metrics else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Candidate Weights</span>
                        <strong id="candidate-model-weights">{{ candidate_model_evaluation.proposed_weights|tojson if candidate_model_evaluation.proposed_weights else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Calibration Curve</span>
                        <strong id="candidate-model-calibration-curve">{{ candidate_model_evaluation.calibration_curve|tojson if candidate_model_evaluation.calibration_curve else 'unavailable' }}</strong>
                    </div>
                    <div class="shadow-metric full">
                        <span>Warnings</span>
                        <strong id="candidate-model-warnings">{{ candidate_model_evaluation.warnings|join(' · ') if candidate_model_evaluation.warnings else 'none' }}</strong>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <div class="board-section">
        <div class="board-section-head">
            <div>
                <h2>交易大屏</h2>
                <p>上方看六张核心卡，下面看图表、风险、订单和审计时间线。全部数据都来自只读状态，不触发任何下单。</p>
            </div>
            <button class="pause-button" id="auto-refresh-toggle" type="button">⏸ 暂停自动刷新</button>
        </div>
        <div class="system-status-card full research-report-card {{ 'status-live' if research_report.state == 'SAFE' else 'status-warn' if research_report.state == 'STALE' else 'status-offline' }}" id="research-report-card">
            <span class="system-status-label" id="research-report-title">{{ research_report.display_title or research_report.title or 'AI Research Report' }}</span>
            <span class="system-status-value" id="research-report-state">{{ research_report.status_label or 'STALE' }}</span>
            <span class="system-status-detail" id="research-report-detail">{{ research_report.detail or 'no data' }}</span>
            <div class="board-section-head" style="margin-top: 8px;">
                <span>Report Summary</span>
            </div>
            <div class="shadow-metrics-grid">
                <div class="shadow-metric">
                    <span>Candidate Count</span>
                    <strong id="research-report-candidate-count">{{ research_report.candidate_count if research_report.candidate_count is not none else 'unavailable' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Average Score</span>
                    <strong id="research-report-average-score">{{ research_report.average_score if research_report.average_score is not none else 'unavailable' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Execution Status</span>
                    <strong id="research-report-execution-status">{{ research_report.selection_execution_status or 'COMPLETED' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Result Quality</span>
                    <strong id="research-report-result-quality">{{ research_report.selection_result_quality or 'COMPLETE' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Research Admission</span>
                    <strong id="research-report-research-admission">{{ research_report.selection_research_admission or 'RESEARCH_READY' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>High Score Success Rate</span>
                    <strong id="research-report-high-score-rate">{{ research_report.high_score_success_rate if research_report.high_score_success_rate is not none else 'unavailable' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Market Regime</span>
                    <strong id="research-report-market-regime">{{ research_report.market_regime.regime if research_report.market_regime and research_report.market_regime.regime else 'UNKNOWN' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Regime Confidence</span>
                    <strong id="research-report-market-regime-confidence">{{ research_report.market_regime.confidence if research_report.market_regime and research_report.market_regime.confidence is not none else 'unavailable' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Strategy Outcome</span>
                    <strong id="research-report-selection-outcome">{{ research_report.selection_outcome or 'NO_ACTIONABLE_RESEARCH_CANDIDATE' }}</strong>
                </div>
                <div class="shadow-metric">
                    <span>Final Selected</span>
                    <strong id="research-report-final-selected-count">{{ research_report.final_selected_count if research_report.final_selected_count is not none else 0 }}</strong>
                </div>
                <div class="shadow-metric full">
                    <span>Score Distribution</span>
                    <strong id="research-report-score-distribution">
                        {% if research_report.score_distribution %}
                            {% for bucket in research_report.score_distribution %}
                                {{ bucket.score_bucket }} · {{ bucket.candidate_count }} · {{ bucket.data_valid_rate if bucket.data_valid_rate is not none else 'n/a' }}% · {{ bucket.backtest_complete_rate if bucket.backtest_complete_rate is not none else 'n/a' }}% · {{ bucket.walk_forward_complete_rate if bucket.walk_forward_complete_rate is not none else 'n/a' }}%{% if not loop.last %}<br>{% endif %}
                            {% endfor %}
                        {% else %}
                            unavailable
                        {% endif %}
                    </strong>
                </div>
                <div class="shadow-metric full">
                    <span>Top Candidates</span>
                    <strong id="research-report-top-candidates">
                        {% if research_report.top_candidates %}
                            {% for item in research_report.top_candidates %}
                                {{ item.symbol or item.candidate_id }} · {{ item.candidate_score if item.candidate_score is not none else 'unavailable' }} · {{ item.recommended_strategy or 'unavailable' }}{% if not loop.last %}<br>{% endif %}
                            {% endfor %}
                        {% else %}
                            unavailable
                        {% endif %}
                    </strong>
                </div>
                <div class="shadow-metric full">
                    <span>Candidate Strategy Matrix</span>
                    <strong id="research-report-strategy-matrix">
                        {% if research_report.candidate_strategy_matrix %}
                            {% for item in research_report.candidate_strategy_matrix %}
                                {{ item.symbol or item.candidate_id }} · {{ item.strategy_id or 'unavailable' }} · fit {{ item.fit_score if item.fit_score is not none else 'n/a' }} · {{ item.allowed if item.allowed is not none else 'n/a' }}{% if item.blocked_reason %} · {{ item.blocked_reason }}{% endif %}{% if not loop.last %}<br>{% endif %}
                            {% endfor %}
                        {% else %}
                            unavailable
                        {% endif %}
                    </strong>
                </div>
                <div class="shadow-metric full">
                    <span>Portfolio Composition</span>
                    <strong id="research-report-portfolio-composition">
                        {% if research_report.portfolio_composition %}
                            selected {{ research_report.portfolio_composition.selected_count if research_report.portfolio_composition.selected_count is not none else 0 }}
                            · blocked {{ research_report.portfolio_composition.blocked_count if research_report.portfolio_composition.blocked_count is not none else 0 }}
                            · leveraged/inverse {{ research_report.portfolio_composition.leveraged_inverse_selected_count if research_report.portfolio_composition.leveraged_inverse_selected_count is not none else 0 }}
                            {% if research_report.portfolio_composition.selected_symbols %}
                                <br>symbols {{ research_report.portfolio_composition.selected_symbols | join(' / ') }}
                            {% endif %}
                        {% else %}
                            unavailable
                        {% endif %}
                    </strong>
                </div>
                <div class="shadow-metric full">
                    <span>Failure Analysis</span>
                    <strong id="research-report-failure-analysis">
                        {% set failure_statuses = research_report.failure_analysis.statuses if research_report.failure_analysis and research_report.failure_analysis.statuses else {} %}
                        DATA_INVALID {{ failure_statuses.get('DATA_INVALID', 0) }} · BACKTEST_FAILED {{ failure_statuses.get('BACKTEST_FAILED', 0) }} · WALK_FORWARD_FAILED {{ failure_statuses.get('WALK_FORWARD_FAILED', 0) }}
                    </strong>
                </div>
            </div>
        </div>
        <div class="viz-grid">
            <div class="viz-card wide">
                <div class="viz-head">
                    <div>
                        <div class="viz-title">账户权益变化</div>
                        <div class="viz-subtitle">展示当前引擎权益对比与今日盈亏快照</div>
                    </div>
                    <div class="viz-subtitle">总权益 {{ "$%.2f"|format(total_equity) if total_equity is not none else 'Unavailable' }} · 今日盈亏 {{ "%+.2f"|format(today_total_pnl) }}</div>
                </div>
                {% if equity_curve_bars %}
                    <div>{{ equity_curve_bars|safe }}</div>
                {% else %}
                    <div class="selector-empty">暂无权益曲线数据</div>
                {% endif %}
            </div>
            <div class="viz-card">
                <div class="viz-head">
                    <div>
                        <div class="viz-title">SOXS 价格与交易点</div>
                        <div class="viz-subtitle">价格折线 + BUY / SELL 标记 + 最近成交</div>
                    </div>
                    <div class="viz-subtitle">{{ main_chart_card.ticker if main_chart_card else 'N/A' }}</div>
                </div>
                {% if main_chart_card %}
                <div class="mini-chart" data-ticker="{{ main_chart_card.ticker }}">
                    <div class="mini-chart-head">
                        <span class="mini-chart-title">主图 · {{ main_chart_card.ticker }}</span>
                        <span class="mini-chart-meta chart-meta">等待数据</span>
                    </div>
                    <div class="mini-chart-body">
                        <div class="mini-chart-empty">暂无图表数据</div>
                        <svg class="mini-chart-svg" viewBox="0 0 320 124" preserveAspectRatio="none" aria-label="{{ main_chart_card.ticker }} price chart"></svg>
                    </div>
                    <div class="mini-chart-trades"></div>
                </div>
                <div class="viz-metrics">
                    <div class="viz-metric">
                        <span class="k">平均成本</span>
                        <span class="v">{% if main_chart_card.avg_entry_price %}${{ "%.2f"|format(main_chart_card.avg_entry_price) }}{% else %}--{% endif %}</span>
                        <span class="s">持仓成本</span>
                    </div>
                    <div class="viz-metric">
                        <span class="k">当前价格</span>
                        <span class="v">{% if main_chart_card.price is not none %}${{ "%.2f"|format(main_chart_card.price) }}{% else %}--{% endif %}</span>
                        <span class="s">最近行情</span>
                    </div>
                    <div class="viz-metric">
                        <span class="k">未实现盈亏</span>
                        <span class="v {{ 'green' if (main_chart_card.pnl or 0) >= 0 else 'red' }}">{% if main_chart_card.pnl is not none %}${{ "%+.2f"|format(main_chart_card.pnl) }}{% else %}--{% endif %}</span>
                        <span class="s">真实持仓</span>
                    </div>
                    <div class="viz-metric">
                        <span class="k">交易点</span>
                        <span class="v">{{ (main_chart_card.chart_trades|length) if main_chart_card.chart_trades is defined else 0 }}</span>
                        <span class="s">FILLED 成交</span>
                    </div>
                </div>
                {% else %}
                <div class="selector-empty">暂无主图数据</div>
                {% endif %}
            </div>
            <div class="viz-card">
                <div class="viz-head">
                    <div>
                        <div class="viz-title">持仓与风险监控</div>
                        <div class="viz-subtitle">当前仓位、现金比例、单票暴露、风险等级</div>
                    </div>
                    <div class="viz-subtitle">{{ risk_summary.risk_label }}</div>
                </div>
                <div class="risk-grid">
                    <div class="viz-metric">
                        <span class="k">持仓数量</span>
                        <span class="v">{{ risk_summary.position_count }}</span>
                        <span class="s">当前真实 / 虚拟仓位</span>
                    </div>
                    <div class="viz-metric">
                        <span class="k">现金占比</span>
                        <span class="v">{% if risk_summary.cash_pct is not none %}{{ "%.1f"|format(risk_summary.cash_pct) }}%{% else %}--{% endif %}</span>
                        <span class="s">按账户权益计算</span>
                    </div>
                    <div class="viz-metric">
                        <span class="k">最大单票占比</span>
                        <span class="v">{% if risk_summary.largest_pct is not none %}{{ "%.1f"|format(risk_summary.largest_pct) }}%{% else %}--{% endif %}</span>
                        <span class="s">最大允许仓位</span>
                    </div>
                </div>
                <div class="risk-meter"><span class="risk-meter-fill" style="width:{% if risk_summary.exposure_pct is not none %}{{ [risk_summary.exposure_pct, 100]|min }}%{% else %}8%{% endif %}"></span></div>
                <div class="viz-metrics">
                    {% for pos in display_positions[:3] %}
                    <div class="viz-metric">
                        <span class="k">{{ pos.ticker }}</span>
                        <span class="v">{{ pos.quantity }} 股</span>
                        <span class="s">
                            {% if pos.market_value is not none %}市值 ${{ "%.2f"|format(pos.market_value) }}{% else %}市值 unavailable{% endif %}
                        </span>
                    </div>
                    {% endfor %}
                    {% if not display_positions %}
                    <div class="selector-empty" style="grid-column:1/-1">暂无持仓数据</div>
                    {% endif %}
                </div>
            </div>
            <div class="viz-card">
                <div class="viz-head">
                    <div>
                        <div class="viz-title">订单状态分布</div>
                        <div class="viz-subtitle">PENDING / PARTIAL / FILLED / CANCELLED / REJECTED</div>
                    </div>
                    <div class="viz-subtitle">总计 {{ active_order_summary.total }}</div>
                </div>
                <div class="order-bars">
                    {% for label, value, color in [
                        ('PENDING', active_order_summary.pending, '#fbbf24'),
                        ('PARTIAL', active_order_summary.partial_filled, '#7dd3fc'),
                        ('FILLED', active_order_summary.filled, '#34d399'),
                        ('CANCELLED', active_order_summary.cancelled, '#94a3b8'),
                        ('REJECTED', active_order_summary.rejected, '#fb7185'),
                    ] %}
                    <div class="order-bar">
                        <span class="k">{{ label }}</span>
                        <div class="order-bar-fill">
                            <span style="width:{% if active_order_summary.total > 0 %}{{ (value / active_order_summary.total * 100) if value else 2 }}%{% else %}2%{% endif %};background:{{ color }}"></span>
                        </div>
                        <span class="v" style="font-variant-numeric:tabular-nums">{{ value }}</span>
                    </div>
                    {% endfor %}
                </div>
            </div>
            <div class="viz-card">
                <div class="viz-head">
                    <div>
                        <div class="viz-title">交易与审计事件</div>
                        <div class="viz-subtitle">按时间倒序的关键事件摘要</div>
                    </div>
                    <div class="viz-subtitle">{{ trade_audit.execution_mode or 'unknown' }}</div>
                </div>
                <div class="timeline-list">
                    {% for event in timeline_items %}
                    <div class="timeline-item timeline-tone-{{ event.tone or 'cyan' }}">
                        <div class="timeline-time">{{ event.time or 'now' }}</div>
                        <div>
                            <div class="timeline-title">{{ event.title }}</div>
                            <div class="timeline-detail">{{ event.detail }}</div>
                        </div>
                    </div>
                    {% endfor %}
                    {% if not timeline_items %}
                    <div class="selector-empty">暂无审计事件</div>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>
    {% if trade_audit.unresolved_alert %}
    <div class="warning-banner">
        存在未决订单超过 {{ trade_audit.unresolved_alert_threshold_seconds|int }} 秒。
        最久未决约 {{ trade_audit.broker_unresolved_oldest_seconds|int }} 秒。
        {{ trade_audit.latest_submitted_line or '请检查最近提交订单' }}
    </div>
    {% endif %}

    <div class="overview-layout">
        <div class="overview-panel">
            <div class="panel-head">
                <h2>控制台总览</h2>
                <span class="hint">第一屏看账户与选股，第二屏看全部标的卡片</span>
            </div>
            <div class="control-grid">
                <div class="overview-panel compact">
                    <div class="panel-head">
                        <h2>账户与持仓</h2>
                        <span class="hint">可用资金与真实仓位</span>
                    </div>
                    <div class="account-strip">
                        <div class="account-summary">
                                <div class="metric">
                                    <span class="metric-label">账户现金</span>
                                    <span class="metric-value">{% if account_summary and account_summary.cash is not none %}${{ "%.2f"|format(account_summary.cash) }}{% else %}暂无{% endif %}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">账户权益</span>
                                    <span class="metric-value">{% if account_summary and account_summary.equity is not none %}${{ "%.2f"|format(account_summary.equity) }}{% else %}暂无{% endif %}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">可买额度</span>
                                    <span class="metric-value">{% if account_summary and account_summary.buying_power is not none %}${{ "%.2f"|format(account_summary.buying_power) }}{% else %}暂无{% endif %}</span>
                                </div>
                                <div class="metric">
                                    <span class="metric-label">精选持仓数量</span>
                                    <span class="metric-value small">{% if account_summary %}{{ selected_positions_count }}{% else %}暂无{% endif %}</span>
                                </div>
                            </div>
                        <div>
                            <div class="panel-head" style="margin:0 0 8px 0;">
                                <h2>{{ display_positions_title }}</h2>
                                <span class="hint">{{ display_positions_hint }}</span>
                            </div>
                            {% if display_positions %}
                            <div class="position-list">
                                {% for pos in display_positions %}
                                <div class="position-item">
                                    <div class="position-cell">
                                        <span class="position-ticker">{{ pos.ticker }}</span>
                                        <span class="val">{{ pos.quantity }} 股</span>
                                    </div>
                                    <div class="position-cell"><span class="label">成本</span><span class="val">${{ "%.2f"|format(pos.avg_entry_price) }}</span></div>
                                    <div class="position-cell"><span class="label">现价</span><span class="val">${{ "%.2f"|format(pos.current_price) }}</span></div>
                                    <div class="position-cell"><span class="label">市值</span><span class="val">${{ "%.2f"|format(pos.market_value) }}</span></div>
                                    <div class="position-cell"><span class="label">浮盈亏</span><span class="val {{ 'red' if pos.unrealized_pnl >= 0 else 'green' }}">${{ "%+.2f"|format(pos.unrealized_pnl) }}</span></div>
                                    <div class="position-cell"><span class="label">收益率</span><span class="val {{ 'red' if pos.unrealized_pnl_pct >= 0 else 'green' }}">{{ "%+.2f"|format(pos.unrealized_pnl_pct) }}%</span></div>
                                </div>
                                {% endfor %}
                            </div>
                            {% elif live_account and live_account.account_error %}
                            <div class="position-empty">实盘账户异常 / 账户拉取失败：{{ live_account.stale_reason }}</div>
                            {% else %}
                            <div class="position-empty">当前没有持仓。</div>
                            {% endif %}
                        </div>
                    </div>
                </div>

                <div class="overview-panel compact">
                    <div class="panel-head">
                        <h2>AI 区间选股</h2>
                        <span class="hint">低价工具池 + 杠杆 / 反向 ETF，真实持仓单独保护</span>
                    </div>
                    <div style="margin:6px 0;padding:8px 12px;background:rgba(255,255,255,.03);border-radius:6px;font-size:12px;line-height:1.6;color:var(--muted)">
                        <div style="font-weight:600;color:var(--accent2);margin-bottom:6px">ℹ️ AI 选股说明</div>
                        <table style="width:100%;border-collapse:collapse;font-size:12px">
                            <tr style="border-bottom:1px solid rgba(255,255,255,.08)">
                                <td style="padding:4px 8px;font-weight:600;white-space:nowrap;width:80px">每日流程</td>
                                <td style="padding:4px 8px">
                                    09:00 ET AI选股 → 多模型分析 → 评分排序 → 写入TOP配置 → 自动重启引擎<br>
                                    09:25 ET auto_trade.sh启动引擎 → 09:30 ET 开盘交易 → 16:00 ET 收盘停机
                                </td>
                            </tr>
                            <tr style="border-bottom:1px solid rgba(255,255,255,.08)">
                                <td style="padding:4px 8px;font-weight:600;white-space:nowrap">评分维度</td>
                                <td style="padding:4px 8px">
                                    波动率100 · 成交量100 · 趋势拟合100 · 可重复性100 · 回撤安全100
                                </td>
                            </tr>
                            <tr style="border-bottom:1px solid rgba(255,255,255,.08)">
                                <td style="padding:4px 8px;font-weight:600;white-space:nowrap">数据源</td>
                                <td style="padding:4px 8px">
                                    TradingAgents (AI分析) · FinRobot (研报) · OpenBB (基本面) · yfinance (行情)
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:4px 8px;font-weight:600;white-space:nowrap">运行环境</td>
                                <td style="padding:4px 8px">
                                    独立进程 (launchd) · 每60s轮询 · 09:00 ET ±90s触发 · 同一天仅执行一次
                                </td>
                            </tr>
                        </table>
                    </div>
                    <div class="section-meta">
                        {% if ai_selection and ai_selection.timestamp %}
                            最新选股时间：{{ ai_selection.timestamp }}
                            {% if ai_selection.settings %}
                                · Universe筛选：{{ ai_universe_filter.summary }}
                                · 自动刷新：{{ ai_selection.settings.auto_refresh_minutes or 0 }} 分钟
                                · 扫描数量：{{ ai_selection.settings.max_symbols or 0 }}
                                · 数据模式：{{ ai_selection.settings.data_mode or 'unknown' }}
                                · 启动阶段：{{ ai_selection.settings.selection_stage or 'unknown' }}
                                {% if ai_selection.settings.fallback_used %} · 已回退补齐{% endif %}
                                {% if ai_selection.execution_status %} · <span id="ai-selection-execution-status">执行状态：{{ ai_selection.execution_status }}</span>{% endif %}
                                {% if ai_selection.result_quality %} · <span id="ai-selection-result-quality">结果质量：{{ ai_selection.result_quality }}</span>{% endif %}
                                {% if ai_selection.research_admission %} · <span id="ai-selection-research-admission">研究准入：{{ ai_selection.research_admission }}</span>{% endif %}
                            {% endif %}
                            <div style="margin-top:8px;line-height:1.5" id="ai-selection-research-notice">提示：{{ ai_selection.research_admission_notice or '候选可进入独立数据验证，不代表具备交易资格。' }}</div>
                            <div style="margin-top:8px;line-height:1.5" id="ai-selection-provider-attempted">Provider 尝试：{{ ai_selection.provider_audit_sections.attempted if ai_selection.provider_audit_sections else '无' }}</div>
                            <div style="line-height:1.5" id="ai-selection-provider-success">Provider 成功：{{ ai_selection.provider_audit_sections.success if ai_selection.provider_audit_sections else '无' }}</div>
                            <div style="line-height:1.5" id="ai-selection-provider-failure">Provider 失败：{{ ai_selection.provider_audit_sections.failure if ai_selection.provider_audit_sections else '无' }}</div>
                            <div style="line-height:1.5" id="ai-selection-provider-timeout">Provider 超时：{{ ai_selection.provider_audit_sections.timeout if ai_selection.provider_audit_sections else '无' }}</div>
                            <div style="line-height:1.5" id="ai-selection-provider-fallback">Provider Fallback：{{ ai_selection.provider_audit_sections.fallback if ai_selection.provider_audit_sections else '无' }}</div>
                            <div style="line-height:1.5" id="ai-selection-provider-mock">Provider Mock：{{ ai_selection.provider_audit_sections.mock if ai_selection.provider_audit_sections else '无' }}</div>
                            <div style="line-height:1.5" id="ai-selection-provider-contributor">Provider 实际贡献：{{ ai_selection.provider_audit_sections.contributor if ai_selection.provider_audit_sections else '无' }}</div>
                            {% if ai_selection.selection_funnel %}
                                <div style="line-height:1.5" id="ai-selection-funnel">候选漏斗：{{ ai_selection.selection_funnel | safe }}</div>
                            {% endif %}
                            {% if ai_selection.rejection_reason_counts %}
                                <div style="line-height:1.5" id="ai-selection-rejection-counts">拒绝统计：{{ ai_selection.rejection_reason_counts | safe }}</div>
                            {% endif %}
                            {% if ai_selection.protected_positions %}
                                · 保护持仓：{{ ai_selection.protected_positions | map(attribute='ticker') | join(' / ') }}
                            {% endif %}
                            {% if ai_selection.refinement_status %}
                                · 后台精筛：{{ ai_selection.refinement_status }}
                                {% if ai_selection.refinement_selection_stage %}（{{ ai_selection.refinement_selection_stage }}）{% endif %}
                            {% endif %}
                        {% else %}
                            暂无 AI 选股报告。
                        {% endif %}
                        <br>
                        选股配置校验：<span class="{{ selection_sync.level }}">{{ selection_sync.label }}</span> · {{ selection_sync.detail }}
                        {% if not selection_sync.ok %}
                        <div class="warning-banner" style="margin-top:10px;background:rgba(255,255,255,.03);color:#e5eefc;border-color:rgba(255,255,255,.08)">
                            <div>selection_state tickers: {{ (selection_sync.selection_state_symbols or []) | safe }}</div>
                            <div>current TOP config tickers: {{ (selection_sync.current_top_config_symbols or []) | safe }}</div>
                            <div>mismatch reason: {{ (selection_sync.mismatch_reason or 'unknown') | safe }}</div>
                            <div>建议操作：{{ (selection_sync.suggestion or '请重新运行 AI Selector 或重新写入 TOP 配置') | safe }}</div>
                        </div>
                        {% endif %}
                        {% if research_digest and research_digest.available %}
                        <div class="research-brief">
                            <div class="research-brief-head">
                                <span class="research-tag">研究简报</span>
                                <span class="research-meta">最新 {{ research_digest.date or 'unknown' }}{% if research_digest.generated_at %} · {{ research_digest.generated_at }}{% endif %}</span>
                            </div>
                            <div class="research-brief-body">
                                <div class="research-brief-title">策略评分复盘</div>
                                <div class="research-brief-summary">{{ research_digest.strategy_summary }}</div>
                                <div class="research-brief-summary">
                                    执行 {{ research_report.selection_execution_status or 'COMPLETED' }} ·
                                    结果 {{ research_report.selection_result_quality or 'COMPLETE' }} ·
                                    准入 {{ research_report.selection_research_admission or 'RESEARCH_READY' }}
                                </div>
                                <div class="research-brief-detail">
                                    <span>TOP：{{ research_digest.top_line or '暂无' }}</span>
                                    <span>可开仓：{{ research_digest.entry_ready }}</span>
                                    <span>观察级：{{ research_digest.observation_only }}</span>
                                </div>
                                <a class="research-brief-link" href="{{ research_digest.research_url }}" target="_blank" rel="noopener">打开研究简报</a>
                            </div>
                        </div>
                        {% endif %}
                    </div>
                    <form class="settings-form" method="post" action="/ai-selector-settings">
                        <div class="settings-field">
                            <label for="auto_refresh_minutes">自动刷新间隔（分钟）</label>
                            <input id="auto_refresh_minutes" name="auto_refresh_minutes" type="number" min="1" max="1440" step="1" value="{{ runtime_settings.auto_refresh_minutes }}">
                        </div>
                        <div class="settings-field wide">
                            <label>Universe 筛选规则</label>
                            <div class="settings-static">
                                {{ ai_universe_filter.summary }}
                                <br>配置来源：{{ ai_universe_filter.source }}
                            </div>
                        </div>
                        <button class="settings-button" type="submit" name="action" value="save">保存设置</button>
                        <button class="settings-button secondary" type="submit" name="action" value="rerun">立即重选</button>
                    </form>
                    {% if ai_selection and ai_selection.report %}
                    <div class="selector-table">
                        <div class="selector-head">
                            <span>排名</span>
                            <span>标的</span>
                            <span>总分</span>
                            <span>波动</span>
                            <span>区间</span>
                        </div>
                        {% for row in ai_selection.report[:3] %}
                        <div class="selector-row">
                            <span class="num">{{ row.get('rank', loop.index) }}</span>
                            <span class="ticker">{{ row.get('ticker', 'N/A') }}</span>
                            <span class="num">{{ "%.2f"|format(row.get('score', 0) or 0) }}</span>
                            <span class="num">{{ "%.2f"|format(row.get('volatility', row.get('volatility_score', 0)) or 0) }}</span>
                            <span class="num">{{ row.get('suggested_range', row.get('range_display', '暂无')) }}</span>
                        </div>
                        {% endfor %}
                    </div>
                    {% if ai_selection and ai_selection.top3 %}
                    <div style="margin-top:10px">
                        <div style="font-weight:600;color:var(--accent2);margin-bottom:6px;font-size:13px">📋 标的说明</div>
                        <div class="selector-table" style="max-height:none">
                            <div class="selector-head">
                                <span>标的</span>
                                <span>类型</span>
                                <span>杠杆</span>
                                <span>方向</span>
                                <span>说明</span>
                            </div>
                            {% for row in ai_selection.top3 %}
                            <div class="selector-row">
                                <span class="ticker">{{ row.ticker }}</span>
                                <span class="num">{{ row._type }}</span>
                                <span class="num">{{ row._leverage }}</span>
                                <span class="num">{{ row._direction }}</span>
                                <span>{{ row._description }}</span>
                            </div>
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}
                    <div class="selection-brief">
                        <div class="selection-brief-item">
                            <span class="selection-tag live">启用中</span>
                            <div class="selection-copy">
                                <span class="symbols">
                                    {% if ai_selection.top3 %}
                                        {{ ai_selection.top3 | map(attribute='ticker') | join(' / ') }}
                                    {% else %}
                                        暂无
                                    {% endif %}
                                </span>
                                <span class="note">这里显示新的 TOP3 工具；真实持仓只做保护监控，不占这里的名额。</span>
                            </div>
                        </div>
                        <div class="selection-brief-item">
                            <span class="selection-tag">保护持仓</span>
                            <div class="selection-copy">
                                <span class="symbols">
                                    {% if ai_selection.protected_positions %}
                                        {{ ai_selection.protected_positions | map(attribute='ticker') | join(' / ') }}
                                    {% else %}
                                        暂无
                                    {% endif %}
                                </span>
                                <span class="note">这些是账户里已有的真实股票仓位，继续跟踪退出风险，但不挤占新选股 TOP3。</span>
                            </div>
                        </div>
                    </div>
                    <div class="selection-status">
                        前台阶段：{{ ai_selection.settings.selection_stage or 'unknown' }}
                        {% if ai_selection.refinement_status %}
                            · 后台精筛：{{ ai_selection.refinement_status }}{% if ai_selection.refinement_selection_stage %} / {{ ai_selection.refinement_selection_stage }}{% endif %}
                        {% endif %}
                    </div>
                    <div class="selection-status">
                        AI 运行状态：<span class="{{ ai_runtime.level }}">{{ ai_runtime.label }}</span> · {{ ai_runtime.detail }} · 当前使用按资产类型、20日成交额、市值和ATR波动率的 Universe 筛选。
                    </div>
                    {% else %}
                    <div class="selector-empty">先运行一次 `scripts/run_ai_selector.py`，这里就会显示最新的 AI 区间选股结果。</div>
                    {% endif %}
                </div>

                <div class="overview-panel compact">
                    <div class="panel-head">
                        <h2>通知与成交对账</h2>
                        <span class="hint">提交、成交、未决订单快速核对</span>
                    </div>
                    <div class="scope-tabs">
                        <a class="scope-tab {{ 'active' if audit_scope == 'today' else '' }}" href="/?audit_scope=today">只看今天</a>
                        <a class="scope-tab {{ 'active' if audit_scope == 'latest' else '' }}" href="/?audit_scope=latest">最新有记录</a>
                    </div>
                    <div class="audit-strip">
                        <div class="quote-item">
                            <span class="label">已提交</span>
                            <span class="val">{{ trade_audit.broker_submitted_count }}</span>
                        </div>
                        <div class="quote-item">
                            <span class="label">已成交</span>
                            <span class="val">{{ trade_audit.broker_filled_count }}</span>
                        </div>
                        <div class="quote-item">
                            <span class="label">部分成交</span>
                            <span class="val">{{ trade_audit.broker_partial_filled_count }}</span>
                        </div>
                        <div class="quote-item">
                            <span class="label">未决</span>
                            <span class="val {{ 'red' if trade_audit.broker_unresolved_count > 0 else 'green' }}">{{ trade_audit.broker_unresolved_count }}</span>
                        </div>
                    </div>
                    <div class="pnl-grid" style="margin-top:10px">
                        <div class="quote-item">
                            <span class="label">最新提交</span>
                            <span class="val">{{ trade_audit.latest_submitted_line or '暂无' }}</span>
                            <span class="label" style="margin-top:6px">{{ trade_audit.latest_submitted_at or audit_day_label }}</span>
                        </div>
                        <div class="quote-item">
                            <span class="label">最新成交</span>
                            <span class="val">{{ trade_audit.latest_filled_line or '暂无' }}</span>
                            <span class="label" style="margin-top:6px">{{ trade_audit.latest_filled_at or audit_day_label }}</span>
                        </div>
                        <div class="quote-item">
                            <span class="label">对账状态</span>
                            <span class="val {{ 'green' if trade_audit.notification_reconcile_ok else 'red' }}">
                                {{ '正常' if trade_audit.notification_reconcile_ok else '存在未决订单' }}
                            </span>
                        </div>
                        <div class="quote-item">
                            <span class="label">最新审计</span>
                            <span class="val">{{ trade_audit.latest_line or '暂无' }}</span>
                        </div>
                    </div>
                    {% if trade_audit.broker_activity_by_ticker %}
                    <div class="ticker-audit-list">
                        {% for row in trade_audit.broker_activity_by_ticker %}
                        <div class="ticker-audit-item">
                            <div class="ticker-audit-head">
                                <span class="ticker-audit-title">{{ row.ticker }}</span>
                                <span class="pill {{ 'warn' if row.unresolved_count > 0 else 'live' }}">
                                    {{ '有未决订单' if row.unresolved_count > 0 else '已对齐' }}
                                </span>
                            </div>
                            <div class="ticker-audit-stats">
                                <div class="quote-item"><span class="label">提交</span><span class="val">{{ row.submitted_count }}</span></div>
                                <div class="quote-item"><span class="label">成交</span><span class="val">{{ row.filled_count }}</span></div>
                                <div class="quote-item"><span class="label">部分成交</span><span class="val">{{ row.partial_filled_count }}</span></div>
                                <div class="quote-item"><span class="label">未决</span><span class="val {{ 'red' if row.unresolved_count > 0 else 'green' }}">{{ row.unresolved_count }}</span></div>
                            </div>
                            <div class="ticker-audit-meta">
                                <div class="quote-item"><span class="label">最新提交</span><span class="val">{{ row.latest_submitted_line or '暂无' }}</span></div>
                                <div class="quote-item"><span class="label">最新成交</span><span class="val">{{ row.latest_filled_line or '暂无' }}</span></div>
                            </div>
                        </div>
                        {% endfor %}
                    </div>
                    {% endif %}
                </div>
            </div>
        </div>
        {% if order_states.blocked_tickers or order_states.failed_orders_today > 0 or order_states.historical_ticker_details %}
        <div class="order-state-strip">
            {% if order_states.blocked_tickers %}
            <div class="order-state-section">
                <span class="order-state-title">🚫 买入暂停</span>
                {% for ticker in order_states.blocked_tickers %}
                <span class="order-state-badge blocked" title="{{ ticker.blocked.reason }}">
                    {{ ticker.ticker }} — 冷却 {{ ticker.blocked.remaining_min }}分{{ ticker.blocked.remaining_sec }}秒
                </span>
                {% endfor %}
            </div>
            {% endif %}
            {% if order_states.failed_orders_today > 0 %}
            <div class="order-state-section">
                <span class="order-state-title">
                    ❌ 今日失败问题: {{ order_states.failed_orders_today }}
                    {% if order_states.failed_orders_total_today > order_states.failed_orders_today %}
                    <span style="font-weight:500;opacity:.78">· 原始拒单 {{ order_states.failed_orders_total_today }}</span>
                    {% endif %}
                </span>
                {% for ticker in order_states.ticker_details %}
                {% if ticker.last_failed %}
                <span class="order-state-badge failed" title="{{ ticker.last_failed.reason }}">
                    {{ ticker.ticker }}: {{ ticker.last_failed.reason[:60] }}{% if ticker.last_failed.reason|length > 60 %}…{% endif %}
                    {% if ticker.failed_count > 1 %}· {{ ticker.failed_count }}次{% endif %}
                    ({{ ticker.last_failed.timestamp[:16] | replace('T', ' ') }})
                </span>
                {% endif %}
                {% endfor %}
            </div>
            {% endif %}
            {% if order_states.historical_ticker_details %}
            <div class="order-state-section" style="width:100%">
                <span class="order-state-title">
                    🕒 历史/非当前标的失败记录
                    {% if order_states.historical_failed_orders_today > 0 %}
                        · {{ order_states.historical_failed_orders_today }}个标的 / 原始拒单 {{ order_states.historical_failed_orders_total_today }}
                    {% endif %}
                </span>
                {% for ticker in order_states.historical_ticker_details %}
                {% if ticker.last_failed %}
                <span class="order-state-badge" style="background:rgba(148,163,184,.12);color:#cbd5e1;border:1px solid rgba(148,163,184,.22)" title="{{ ticker.last_failed.reason }}">
                    {{ ticker.ticker }}: {{ ticker.last_failed.reason[:60] }}{% if ticker.last_failed.reason|length > 60 %}…{% endif %}
                    {% if ticker.failed_count > 1 %}· {{ ticker.failed_count }}次{% endif %}
                    ({{ ticker.last_failed.timestamp[:16] | replace('T', ' ') }})
                </span>
                {% endif %}
                {% endfor %}
            </div>
            {% endif %}
        </div>
        {% endif %}
    </div>

    <div class="overview-panel">
        <div class="panel-head">
            <h2>交易统计数据</h2>
            <span class="hint">今日累计，三路引擎汇总</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:10px">
            <div class="stat-box">
                <span class="stat-label">今日交易</span>
                <span class="stat-value">{{ trade_stats.trades_today }}</span>
            </div>
            <div class="stat-box">
                <span class="stat-label">胜率</span>
                <span class="stat-value {{ 'green' if trade_stats.win_rate >= 50 else 'yellow' if trade_stats.win_rate > 0 else 'muted' }}">{{ "%.1f"|format(trade_stats.win_rate) }}%</span>
            </div>
            <div class="stat-box">
                <span class="stat-label">平均盈利</span>
                <span class="stat-value green">${{ "%.2f"|format(trade_stats.avg_win) }}</span>
            </div>
            <div class="stat-box">
                <span class="stat-label">平均亏损</span>
                <span class="stat-value red">${{ "%.2f"|format(trade_stats.avg_loss) }}</span>
            </div>
            <div class="stat-box">
                <span class="stat-label">今日总盈亏</span>
                <span class="stat-value {{ 'red' if trade_stats.total_pnl >= 0 else 'green' }}">${{ "%+.2f"|format(trade_stats.total_pnl) }}</span>
            </div>
        </div>
        <div class="panel-head" style="margin:4px 0 2px">
            <h2>权益曲线</h2>
            <span class="hint">每路引擎当前权益对比</span>
        </div>
        <div style="padding:6px 0">
            {% if equity_curve_bars %}
                {{ equity_curve_bars|safe }}
            {% else %}
                <span class="hint">暂无权益数据</span>
            {% endif %}
        </div>
    </div>

        <div class="cards-section">
            <div class="cards-section-head">
                <div>
                    <h2>全部标的</h2>
                    <p>第二屏重点看每只票的区间位置、当前信号、持仓和盈亏，距离近的会自动高亮。</p>
                </div>
            </div>
        <div class="cards">
    {% for card in cards %}
            <div class="card {% if not card.online %}offline{% endif %} {{ card.featured_class }}">
                <div class="card-head">
                    <div class="card-title">
                        <span class="ticker">{{ card.name }}</span>
                        <span class="desc">{{ card.desc }}</span>
                        {% if card.featured_label %}<span class="card-spot {{ card.featured_class }}">{{ card.featured_label }}</span>{% endif %}
                    </div>
                    <div class="badges">
                        {% if card.halted %}<span class="badge halted">已暂停</span>{% endif %}
                        {% if card.trade_in_progress %}<span class="badge live">交易中</span>{% endif %}
                        {% if card.reduce_only %}<span class="badge halted">仅减仓</span>{% endif %}
                        {% if card.range_ready %}<span class="badge live">区间就绪</span>{% else %}<span class="badge halted">区间未就绪</span>{% endif %}
                        {% if card.online %}<span class="badge live">在线</span>{% else %}<span class="badge offline">离线</span>{% endif %}
                    </div>
                </div>

                <div class="price-row">
                    <span class="price {{ 'green' if card.price_change >= 0 else 'red' }}">${{ "%.2f"|format(card.price) }}</span>
                    <span class="change {{ 'green' if card.price_change >= 0 else 'red' }}">
                        {{ '+' if card.price_change >= 0 else '' }}{{ "%.2f"|format(card.price_change) }}%
                    </span>
                </div>

                <div class="quote-strip">
                    <div class="strip-box">
                        <span class="label">高 / 低</span>
                        <span class="val">${{ "%.2f"|format(card.day_high) }} / ${{ "%.2f"|format(card.day_low) }}</span>
                    </div>
                    <div class="strip-box">
                        <span class="label">买 / 卖</span>
                        <span class="val">${{ "%.2f"|format(card.bid) }} / ${{ "%.2f"|format(card.ask) }}</span>
                    </div>
                </div>

                <div class="mini-chart" data-ticker="{{ card.ticker }}">
                    <div class="mini-chart-head">
                        <span class="mini-chart-title">轻量图 · {{ card.ticker }}</span>
                        <span class="mini-chart-meta chart-meta">等待数据</span>
                    </div>
                    <div class="mini-chart-body">
                        <div class="mini-chart-empty">暂无图表数据</div>
                        <svg class="mini-chart-svg" viewBox="0 0 320 124" preserveAspectRatio="none" aria-label="{{ card.ticker }} price chart"></svg>
                    </div>
                    <div class="mini-chart-trades"></div>
                </div>

                <div class="range-block">
                    <div class="row"><span class="label">运行区间</span><span class="val">${{ "%.2f"|format(card.support) }} - ${{ "%.2f"|format(card.resistance) }} ({{ "%.1f"|format(card.spread_pct) }}%)</span></div>
                    <div class="row" style="margin-top:6px"><span class="label">AI参考区间</span><span class="val">{% if card.ai_range_low is not none and card.ai_range_high is not none %}${{ "%.2f"|format(card.ai_range_low) }} - ${{ "%.2f"|format(card.ai_range_high) }}{% else %}{{ card.ai_suggested_range }}{% endif %}</span></div>
                    <div class="range-bar">
                        <div class="range-fill" style="width:{{ card.pos_pct }}%;background:{% if card.pos_pct > 70 %}#fb7185{% elif card.pos_pct < 30 %}#34d399{% else %}#fbbf24{% endif %}"></div>
                    </div>
                    <div class="row" style="margin-top:8px"><span class="label">区间位置</span><span class="val">{{ "%.0f"|format(card.pos_pct) }}%</span></div>
                    <div class="source-chip {% if 'fallback' in card.range_source %}fallback{% elif card.range_source == 'offline' %}offline{% endif %}">
                        区间来源 · {{ card.range_source }}
                    </div>
                </div>

                <div class="signal {% if card.signal == 'BUY' %}sig-buy{% elif card.signal == 'SELL' %}sig-sell{% elif 'TREND' in card.signal %}sig-block{% else %}sig-hold{% endif %}">
                    {{ card.signal_cn }}
                </div>
                <div class="signal-note">{{ card.signal_reason }}</div>

                <div class="pnl-grid">
                    <div class="quote-item"><span class="label">持股</span><span class="val">{{ card.shares }}</span></div>
                    <div class="quote-item"><span class="label">持仓来源</span><span class="val">{{ card.hold_source }}</span></div>
                    <div class="quote-item"><span class="label">成交</span><span class="val">{{ card.trades }}</span></div>
                    <div class="quote-item"><span class="label">盈亏</span><span class="val {{ 'red' if card.pnl >= 0 else 'green' }}">${{ "%+.2f"|format(card.pnl) }}</span></div>
                    <div class="quote-item"><span class="label">区间源</span><span class="val {{ 'muted' if not card.range_ready else '' }}">{{ card.range_source }}</span></div>
                    <div class="quote-item"><span class="label">AI区间</span><span class="val">{{ card.ai_suggested_range }}</span></div>
                </div>
            </div>
    {% endfor %}
        </div>
    </div>

    <div class="refresh">每 5 秒自动刷新 · {{ update_time }}</div>
</div>
<script>
(function() {
    const charts = Array.from(document.querySelectorAll('.mini-chart[data-ticker]'));
    if (!charts.length) {
        return;
    }

    const ns = "http://www.w3.org/2000/svg";

    function formatPrice(value) {
        const number = Number(value);
        return Number.isFinite(number) ? `$${number.toFixed(2)}` : '$0.00';
    }

    function parseTime(value) {
        const ts = Date.parse(value);
        return Number.isFinite(ts) ? ts : null;
    }

    function priceToY(price, minPrice, maxPrice, height, padding) {
        if (!Number.isFinite(price)) {
            return height / 2;
        }
        const low = Number.isFinite(minPrice) ? minPrice : price;
        const high = Number.isFinite(maxPrice) ? maxPrice : price;
        const range = Math.max(high - low, 0.0001);
        const usable = Math.max(height - padding * 2, 1);
        const ratio = (price - low) / range;
        return height - padding - Math.max(0, Math.min(1, ratio)) * usable;
    }

    function buildSvg(prices, trades) {
        const width = 320;
        const height = 124;
        const padding = 12;
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        svg.setAttribute('class', 'mini-chart-svg');
        svg.setAttribute('preserveAspectRatio', 'none');

        const validPrices = prices.filter((item) => Number(item && item.price) > 0);
        if (!validPrices.length) {
            return { svg, empty: true };
        }

        const values = validPrices.map((item) => Number(item.price));
        const minPrice = Math.min(...values);
        const maxPrice = Math.max(...values);
        const points = validPrices.map((item, index) => {
            const x = padding + (validPrices.length === 1 ? (width - padding * 2) / 2 : (index / (validPrices.length - 1)) * (width - padding * 2));
            const y = priceToY(Number(item.price), minPrice, maxPrice, height, padding);
            return { x, y, time: item.time, price: Number(item.price) };
        });

        const grid = document.createElementNS(ns, 'line');
        grid.setAttribute('x1', '12');
        grid.setAttribute('x2', '308');
        grid.setAttribute('y1', '62');
        grid.setAttribute('y2', '62');
        grid.setAttribute('class', 'mini-chart-grid');
        svg.appendChild(grid);

        if (points.length > 1) {
            const polyline = document.createElementNS(ns, 'polyline');
            polyline.setAttribute('class', 'mini-chart-line');
            polyline.setAttribute('points', points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '));
            svg.appendChild(polyline);
        } else {
            const single = document.createElementNS(ns, 'circle');
            single.setAttribute('cx', points[0].x.toFixed(1));
            single.setAttribute('cy', points[0].y.toFixed(1));
            single.setAttribute('r', '3.5');
            single.setAttribute('class', 'mini-chart-point-buy');
            svg.appendChild(single);
        }

        trades.forEach((trade) => {
            const tradeTime = parseTime(trade.time);
            if (tradeTime == null) {
                return;
            }
            const price = Number(trade.price);
            const side = String(trade.side || '').toUpperCase();
            let closest = points[points.length - 1];
            let smallest = Infinity;
            points.forEach((candidate) => {
                const candidateTime = parseTime(candidate.time);
                if (candidateTime == null) {
                    return;
                }
                const diff = Math.abs(candidateTime - tradeTime);
                if (diff < smallest) {
                    smallest = diff;
                    closest = candidate;
                }
            });
            const circle = document.createElementNS(ns, 'circle');
            circle.setAttribute('cx', closest.x.toFixed(1));
            circle.setAttribute('cy', priceToY(price, minPrice, maxPrice, height, padding).toFixed(1));
            circle.setAttribute('r', '8');
            circle.setAttribute('class', side === 'SELL' ? 'mini-chart-point-sell' : 'mini-chart-point-buy');

            const title = document.createElementNS(ns, 'title');
            title.textContent = `${trade.side || 'FILLED'} ${formatPrice(trade.price)} · ${trade.qty || 0}股 · ${trade.time || ''}`;
            circle.appendChild(title);
            svg.appendChild(circle);

            const label = document.createElementNS(ns, 'text');
            label.setAttribute('x', closest.x.toFixed(1));
            label.setAttribute('y', priceToY(price, minPrice, maxPrice, height, padding).toFixed(1));
            label.setAttribute('class', 'mini-chart-label');
            label.textContent = side === 'SELL' ? 'S' : 'B';
            svg.appendChild(label);
        });

        return { svg, empty: false };
    }

    function render(chartEl, payload) {
        const svgHost = chartEl.querySelector('.mini-chart-body');
        const meta = chartEl.querySelector('.chart-meta');
        const tradesBox = chartEl.querySelector('.mini-chart-trades');
        const empty = chartEl.querySelector('.mini-chart-empty');
        const prices = Array.isArray(payload && payload.prices) ? payload.prices : [];
        const trades = Array.isArray(payload && payload.trades) ? payload.trades : [];

        if (meta) {
            meta.textContent = payload && payload.current_price != null ? `现价 ${formatPrice(payload.current_price)}` : (prices.length ? `${prices.length} 个点` : '暂无现价');
        }

        if (tradesBox) {
            tradesBox.innerHTML = '';
            trades.slice(-4).forEach((trade) => {
                const pill = document.createElement('span');
                pill.className = `mini-chart-trade ${String(trade.side || '').toUpperCase() === 'SELL' ? 'sell' : 'buy'}`;
                pill.textContent = `${String(trade.side || 'FILLED').toUpperCase().slice(0, 1)} ${formatPrice(trade.price)} ×${trade.qty || 0}`;
                tradesBox.appendChild(pill);
            });
        }

        const built = buildSvg(prices, trades);
        svgHost.querySelectorAll('.mini-chart-svg').forEach((node) => node.remove());
        svgHost.appendChild(built.svg);
        if (empty) {
            empty.style.display = built.empty ? 'flex' : 'none';
        }
    }

    function formatMoney(value, fallback = 'Unavailable') {
        const number = Number(value);
        return Number.isFinite(number) ? `$${number.toFixed(2)}` : fallback;
    }

    function setText(id, value, fallback = 'Unavailable') {
        const node = document.getElementById(id);
        if (!node) {
            return;
        }
        if (value === null || value === undefined || value === '') {
            node.textContent = fallback;
        } else {
            node.textContent = value;
        }
    }

    function setClass(id, className) {
        const node = document.getElementById(id);
        if (!node) {
            return;
        }
        node.className = className;
    }

    function renderShadowBlockedReasons(items) {
        const node = document.getElementById('shadow-blocked-top');
        if (!node) {
            return;
        }
        const rows = Array.isArray(items) ? items : [];
        if (!rows.length) {
            node.innerHTML = '<span class="system-status-detail">no data</span>';
            return;
        }
        node.innerHTML = rows.map((item) => {
            const reason = String(item && item.reason ? item.reason : 'unknown');
            const count = Number(item && item.count ? item.count : 0);
            return `<span class="shadow-chip">${reason} · ${count}</span>`;
        }).join('');
    }

    let statusRefreshPaused = false;
    const refreshToggle = document.getElementById('auto-refresh-toggle');
    if (refreshToggle) {
        refreshToggle.addEventListener('click', () => {
            statusRefreshPaused = !statusRefreshPaused;
            refreshToggle.textContent = statusRefreshPaused ? '▶ 恢复自动刷新' : '⏸ 暂停自动刷新';
            refreshToggle.classList.toggle('paused', statusRefreshPaused);
        });
    }

    async function refreshStatus() {
        if (statusRefreshPaused) {
            return;
        }
        try {
            const response = await fetch('/api/status', { cache: 'no-store' });
            if (!response.ok) {
                throw new Error(`status ${response.status}`);
            }
            const payload = await response.json();
            const system = payload.system || {};
            const summary = (payload.dashboard && payload.dashboard.summary) || {};

            setText('last-updated-pill', payload.timestamp || '');
            setText('system-api-status', system.api_status || 'unavailable');
            setText('system-last-updated', `最后更新时间：${system.last_updated || 'unavailable'}`);
            setText('system-broker-type', system.broker_type || 'unavailable');
            setText('system-broker-connection', `连接状态：${system.broker_connection || 'not connected'}`);
            const modeConsistency = payload.mode_consistency || {};
            setText('system-top-engine-mode', modeConsistency.mixed ? 'TOP 引擎模式不一致' : (Array.isArray(modeConsistency.top_modes) && modeConsistency.top_modes.length ? 'TOP 引擎模式一致' : '无启用 TOP 引擎'));
            setText('system-top-engine-mode-detail', Array.isArray(modeConsistency.top_modes) && modeConsistency.top_modes.length ? `Dashboard: ${modeConsistency.dashboard_mode || payload.mode || 'unknown'} · TOP: ${modeConsistency.top_modes.join(', ')}` : '当前未生成 TOP 配置');
            setText('system-data-source', system.data_source || 'no data');
            setText('system-account-source', system.account_source || 'no data');
            setText('system-market-label', system.market_open_label || 'unavailable');
            setText('system-market-detail', system.market_open_detail || 'no data');
            setText('system-reduce-only', system.global_reduce_only ? 'ENABLED' : 'DISABLED');
            setText('system-live-order', system.live_order_enabled ? 'ENABLED' : 'DISABLED');
            setText('system-orders-status', system.active_orders && system.active_orders.status_label ? system.active_orders.status_label : 'no data');
            setText('system-orders-detail', system.active_orders && system.active_orders.detail ? system.active_orders.detail : 'no data');
            setText('system-weekend-lifecycle', system.lifecycle && system.lifecycle.weekend_paper ? (system.lifecycle.weekend_paper.status_label || 'unavailable') : 'unavailable');
            setText('system-weekend-detail', system.lifecycle && system.lifecycle.weekend_paper ? (system.lifecycle.weekend_paper.detail || 'no data') : 'no data');
            setText('system-weekend-time', `报告：${system.lifecycle && system.lifecycle.weekend_paper ? (system.lifecycle.weekend_paper.generated_at || 'no data') : 'no data'}`);
            setText('system-sandbox-lifecycle', system.lifecycle && system.lifecycle.longbridge_sandbox ? (system.lifecycle.longbridge_sandbox.status_label || 'unavailable') : 'unavailable');
            setText('system-sandbox-detail', system.lifecycle && system.lifecycle.longbridge_sandbox ? (system.lifecycle.longbridge_sandbox.detail || 'no data') : 'no data');
            setText('system-sandbox-time', `报告：${system.lifecycle && system.lifecycle.longbridge_sandbox ? (system.lifecycle.longbridge_sandbox.generated_at || 'no data') : 'no data'}`);
            setText('headline-total-equity', formatMoney(summary.equity));
            setText('headline-total-equity-sub', `数据源：${system.account_source || 'unavailable'}`);
            setText('headline-available-cash', formatMoney(summary.buying_power ?? summary.cash));
            setText('headline-available-cash-sub', `资金来源：${system.account_source || 'no data'}`);
            setText('headline-position-count', summary.positions_count != null ? String(summary.positions_count) : '--');
            setText('headline-position-sub', summary.positions_count ? `${summary.positions_count} 个仓位` : '暂无持仓');
            setText('headline-today-pnl', formatMoney(summary.today_total_pnl ?? 0, '$0.00'));
            setText('headline-today-pnl-sub', `按 3 路策略今日盈亏汇总 / 总成交 ${(summary.total_trades ?? 0)} 笔`);
            setText('headline-active-orders', summary.active_orders_pending != null ? String(summary.active_orders_pending) : '0');
            setText('headline-active-orders-sub', `PENDING ${summary.active_orders_pending ?? 0} · PARTIAL ${summary.active_orders_partial_filled ?? 0}`);
            setText('headline-system-state', system.broker_connected ? 'RUNNING' : 'DEGRADED');
            setText('headline-system-state-sub', `Reduce-Only ${system.global_reduce_only ? 'ON' : 'OFF'} · Live Order ${system.live_order_enabled ? 'ON' : 'OFF'}`);

            const shadow = payload.shadow || {};
            setText('shadow-title', shadow.title || 'Shadow Observer');
            setText('shadow-state', shadow.status_label || shadow.state || 'STALE');
            setText('shadow-detail', shadow.detail || 'no data');
            setText('shadow-mode', shadow.mode || 'READ-ONLY SHADOW');
            setText('shadow-safety-gate', shadow.safety_gate || 'STALE');
            setText('shadow-quote-only', shadow.quote_api_only === true ? 'true' : shadow.quote_api_only === false ? 'false' : 'unavailable');
            setText('shadow-trade-api', shadow.trade_api_used === true ? 'true' : shadow.trade_api_used === false ? 'false' : 'unavailable');
            setText('shadow-trade-context', shadow.trade_context_initialized === true ? 'true' : shadow.trade_context_initialized === false ? 'false' : 'unavailable');
            setText('shadow-last-run', shadow.last_run_at || 'unavailable');
            setText('shadow-latest-bar-utc', shadow.latest_processed_bar_utc || 'unavailable');
            setText('shadow-latest-bar-et', shadow.latest_processed_bar_et || 'unavailable');
            setText('shadow-data-freshness', shadow.data_freshness || 'unavailable');
            setText('shadow-benchmark-status', shadow.benchmark_status || 'unavailable');
            setText('shadow-alignment-status', shadow.alignment_status || 'unavailable');
            setText('shadow-flow-counts', `${shadow.signals_generated ?? 0} / ${shadow.simulated_orders ?? 0} / ${shadow.simulated_trades ?? 0}`);
            setText('shadow-open-positions', shadow.open_simulated_positions != null ? String(shadow.open_simulated_positions) : 'unavailable');
            setText('shadow-equity', shadow.simulated_equity != null ? formatMoney(shadow.simulated_equity) : 'unavailable');
            setText('shadow-return', shadow.simulated_return != null ? `${(Number(shadow.simulated_return) * 100).toFixed(2)}%` : 'unavailable');
            setText('shadow-drawdown', shadow.simulated_drawdown != null ? `${(Number(shadow.simulated_drawdown) * 100).toFixed(2)}%` : 'unavailable');
            renderShadowBlockedReasons(shadow.blocked_reason_top5 || []);
            const shadowCard = document.getElementById('shadow-observer-card');
            if (shadowCard) {
                const shadowState = String(shadow.state || shadow.status_label || 'STALE').toUpperCase();
                shadowCard.className = `system-status-card full shadow-observer-card ${shadowState === 'SAFE' ? 'status-live' : shadowState === 'STALE' ? 'status-warn' : 'status-offline'}`;
            }

            const candidateValidation = payload.candidate_validation || {};
            setText('candidate-title', candidateValidation.title || 'AI Candidate Validation');
            setText('candidate-state', candidateValidation.status_label || candidateValidation.state || 'STALE');
            setText('candidate-detail', candidateValidation.detail || 'no data');
            setText('candidate-symbol', candidateValidation.latest_candidate && candidateValidation.latest_candidate.symbol ? candidateValidation.latest_candidate.symbol : 'unavailable');
            setText('candidate-asset-type', candidateValidation.latest_candidate && candidateValidation.latest_candidate.asset_type ? candidateValidation.latest_candidate.asset_type : 'unavailable');
            setText('candidate-ai-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.ai_score != null ? String(candidateValidation.latest_candidate.ai_score) : 'unavailable');
            setText('candidate-candidate-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.candidate_score != null ? String(candidateValidation.latest_candidate.candidate_score) : 'unavailable');
            setText('candidate-benchmarks', candidateValidation.latest_candidate && Array.isArray(candidateValidation.latest_candidate.benchmarks) && candidateValidation.latest_candidate.benchmarks.length ? candidateValidation.latest_candidate.benchmarks.join(' / ') : 'unavailable');
            setText('candidate-strategy-family', candidateValidation.latest_candidate && candidateValidation.latest_candidate.strategy_family ? candidateValidation.latest_candidate.strategy_family : 'unavailable');
            setText('candidate-liquidity-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.liquidity_score != null ? String(candidateValidation.latest_candidate.liquidity_score) : 'unavailable');
            setText('candidate-trend-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.trend_score != null ? String(candidateValidation.latest_candidate.trend_score) : 'unavailable');
            setText('candidate-volatility-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.volatility_score != null ? String(candidateValidation.latest_candidate.volatility_score) : 'unavailable');
            setText('candidate-risk-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.risk_score != null ? String(candidateValidation.latest_candidate.risk_score) : 'unavailable');
            setText('candidate-strategy-fit-score', candidateValidation.latest_candidate && candidateValidation.latest_candidate.strategy_fit_score != null ? String(candidateValidation.latest_candidate.strategy_fit_score) : 'unavailable');
            setText('candidate-recommended-strategy', candidateValidation.latest_candidate && candidateValidation.latest_candidate.recommended_strategy ? candidateValidation.latest_candidate.recommended_strategy : 'unavailable');
            setText('candidate-score-reason', candidateValidation.latest_candidate && candidateValidation.latest_candidate.score_reason ? candidateValidation.latest_candidate.score_reason : 'unavailable');
            setText('candidate-validation-status', candidateValidation.latest_candidate && candidateValidation.latest_candidate.validation_status ? candidateValidation.latest_candidate.validation_status : 'AI_CANDIDATE');
            setText('candidate-selection-stage', candidateValidation.latest_candidate && candidateValidation.latest_candidate.selection_stage ? candidateValidation.latest_candidate.selection_stage : (candidateValidation.selection_stage || 'PRELIMINARY'));
            setText('candidate-last-completed-session', candidateValidation.latest_candidate && candidateValidation.latest_candidate.last_completed_session ? candidateValidation.latest_candidate.last_completed_session : 'unavailable');
            setText('candidate-daily-data-as-of', candidateValidation.latest_candidate && candidateValidation.latest_candidate.daily_data_as_of ? candidateValidation.latest_candidate.daily_data_as_of : 'unavailable');
            setText('candidate-premarket-snapshot-at', candidateValidation.latest_candidate && candidateValidation.latest_candidate.premarket_snapshot_at ? candidateValidation.latest_candidate.premarket_snapshot_at : 'unavailable');
            setText('candidate-premarket-change', candidateValidation.latest_candidate && candidateValidation.latest_candidate.premarket_change_pct != null ? String(candidateValidation.latest_candidate.premarket_change_pct) : 'unavailable');
            setText('candidate-gap-pct', candidateValidation.latest_candidate && candidateValidation.latest_candidate.gap_pct != null ? String(candidateValidation.latest_candidate.gap_pct) : 'unavailable');
            setText('candidate-premarket-volume', candidateValidation.latest_candidate && candidateValidation.latest_candidate.premarket_volume != null ? String(candidateValidation.latest_candidate.premarket_volume) : 'unavailable');
            setText('candidate-freshness-status', candidateValidation.latest_candidate && candidateValidation.latest_candidate.freshness_status ? candidateValidation.latest_candidate.freshness_status : (candidateValidation.status_label || 'STALE'));
            setText('candidate-data-mode', candidateValidation.latest_candidate && candidateValidation.latest_candidate.data_mode ? candidateValidation.latest_candidate.data_mode : 'unavailable');
            setText('candidate-universe-filter', candidateValidation.latest_candidate && candidateValidation.latest_candidate.trade_filter_passed ? '通过' : '拒绝');
            setText('candidate-data-sufficiency', candidateValidation.latest_candidate && candidateValidation.latest_candidate.data_status === 'VALID' ? '通过' : '失败');
            setText('candidate-scoring-eligible', candidateValidation.latest_candidate && candidateValidation.latest_candidate.scoring_eligible ? 'YES' : 'NO');
            setText('candidate-trade-admission', candidateValidation.latest_candidate && candidateValidation.latest_candidate.trade_admission_status ? candidateValidation.latest_candidate.trade_admission_status : 'NOT_TRADABLE');
            setText('candidate-scoring-block-reason', candidateValidation.latest_candidate && candidateValidation.latest_candidate.scoring_block_reason ? candidateValidation.latest_candidate.scoring_block_reason : 'unavailable');
            setText('candidate-stale-reason', candidateValidation.latest_candidate && candidateValidation.latest_candidate.stale_reason ? candidateValidation.latest_candidate.stale_reason : 'unavailable');
            setText('candidate-evidence-status', candidateValidation.latest_candidate && candidateValidation.latest_candidate.evidence_status ? candidateValidation.latest_candidate.evidence_status : 'INSUFFICIENT_EVIDENCE');
            setText('candidate-profitability-status', candidateValidation.latest_candidate && candidateValidation.latest_candidate.profitability_status ? candidateValidation.latest_candidate.profitability_status : 'INELIGIBLE');
            setText('candidate-deployment-status', candidateValidation.latest_candidate && candidateValidation.latest_candidate.deployment_status ? candidateValidation.latest_candidate.deployment_status : 'INELIGIBLE');
            setText('candidate-rejection-reason', candidateValidation.latest_candidate && candidateValidation.latest_candidate.rejection_reason ? candidateValidation.latest_candidate.rejection_reason : 'unavailable');
            setText('candidate-last-updated', candidateValidation.last_updated || 'unavailable');
            setText('candidate-record-count', candidateValidation.candidate_count != null ? String(candidateValidation.candidate_count) : 'unavailable');
            const candidatePerformance = candidateValidation.performance || {};
            setText('candidate-performance-average-score', candidatePerformance.average_score != null ? String(candidatePerformance.average_score) : 'unavailable');
            setText('candidate-performance-high-score-rate', candidatePerformance.high_score_success_rate != null ? `${candidatePerformance.high_score_success_rate}%` : 'unavailable');
            const bucketRows = Array.isArray(candidatePerformance.score_bucket_distribution) ? candidatePerformance.score_bucket_distribution : [];
            const bucketText = bucketRows.length
                ? bucketRows.map((bucket) => {
                    const label = bucket.score_bucket || 'unknown';
                    const count = bucket.candidate_count != null ? bucket.candidate_count : 0;
                    const dataValidRate = bucket.data_valid_rate != null ? `${bucket.data_valid_rate}%` : 'n/a';
                    const backtestCompleteRate = bucket.backtest_complete_rate != null ? `${bucket.backtest_complete_rate}%` : 'n/a';
                    const walkForwardCompleteRate = bucket.walk_forward_complete_rate != null ? `${bucket.walk_forward_complete_rate}%` : 'n/a';
                    return `${label} · ${count} · ${dataValidRate} · ${backtestCompleteRate} · ${walkForwardCompleteRate}`;
                }).join(' | ')
                : 'unavailable';
            setText('candidate-performance-buckets', bucketText);
            const candidateModel = payload.candidate_model_evaluation || {};
            setText('candidate-model-title', candidateModel.title || 'Candidate Model Evaluation');
            setText('candidate-model-status', candidateModel.approval_status || candidateModel.status_label || 'DRAFT');
            setText('candidate-model-detail', candidateModel.recommended_action || 'collect_more_samples');
            setText('candidate-model-active-version', candidateModel.active_model_version || 'baseline_v1');
            setText('candidate-model-challenger-version', candidateModel.challenger_version || 'unavailable');
            setText('candidate-model-training-sample-count', candidateModel.training_sample_count != null ? String(candidateModel.training_sample_count) : 'unavailable');
            const trainingPeriod = candidateModel.training_period || {};
            setText('candidate-model-training-period', `${trainingPeriod.start || 'unavailable'} -> ${trainingPeriod.end || 'unavailable'}`);
            setText('candidate-model-baseline-score', candidateModel.comparison && candidateModel.comparison.baseline_score != null ? String(candidateModel.comparison.baseline_score) : 'unavailable');
            setText('candidate-model-challenger-score', candidateModel.comparison && candidateModel.comparison.challenger_score != null ? String(candidateModel.comparison.challenger_score) : 'unavailable');
            setText('candidate-model-approval-status', candidateModel.approval_status || 'DRAFT');
            setText('candidate-model-recommended-action', candidateModel.recommended_action || 'collect_more_samples');
            setText('candidate-model-sample-warning', candidateModel.sample_size_warning ? 'YES' : 'NO');
            setText('candidate-model-overfitting-warning', candidateModel.overfitting_warning ? 'YES' : 'NO');
            setText('candidate-model-baseline-metrics', candidateModel.baseline_metrics ? JSON.stringify(candidateModel.baseline_metrics) : 'unavailable');
            setText('candidate-model-challenger-metrics', candidateModel.challenger_metrics ? JSON.stringify(candidateModel.challenger_metrics) : 'unavailable');
            setText('candidate-model-weights', candidateModel.proposed_weights ? JSON.stringify(candidateModel.proposed_weights) : 'unavailable');
            setText('candidate-model-calibration-curve', Array.isArray(candidateModel.calibration_curve) ? JSON.stringify(candidateModel.calibration_curve) : 'unavailable');
            setText('candidate-model-warnings', Array.isArray(candidateModel.warnings) && candidateModel.warnings.length ? candidateModel.warnings.join(' · ') : 'none');
            const candidateModelCard = document.getElementById('candidate-model-card');
            if (candidateModelCard) {
                const candidateModelState = String(candidateModel.approval_status || candidateModel.status_label || candidateModel.state || 'DRAFT').toUpperCase();
                candidateModelCard.className = `system-status-card full candidate-model-card ${candidateModelState === 'ACTIVE' || candidateModelState === 'APPROVED' || candidateModelState === 'REVIEW_REQUIRED' ? 'status-live' : candidateModelState === 'DRAFT' || candidateModelState === 'BACKTESTED' || candidateModelState === 'WALK_FORWARD_VALIDATED' ? 'status-warn' : 'status-offline'}`;
            }
            const researchReport = payload.research_report || candidateValidation.research_report || {};
            setText('research-report-title', researchReport.display_title || researchReport.title || 'AI Research Report');
            setText('research-report-state', researchReport.status_label || researchReport.state || 'STALE');
            setText('research-report-detail', researchReport.detail || 'no data');
            setText('research-report-candidate-count', researchReport.candidate_count != null ? String(researchReport.candidate_count) : 'unavailable');
            setText('research-report-average-score', researchReport.average_score != null ? String(researchReport.average_score) : 'unavailable');
            setText('research-report-high-score-rate', researchReport.high_score_success_rate != null ? `${researchReport.high_score_success_rate}%` : 'unavailable');
            setText('research-report-market-regime', researchReport.market_regime && researchReport.market_regime.regime ? researchReport.market_regime.regime : 'UNKNOWN');
            setText('research-report-market-regime-confidence', researchReport.market_regime && researchReport.market_regime.confidence != null ? String(researchReport.market_regime.confidence) : 'unavailable');
            setText('research-report-selection-outcome', researchReport.selection_outcome || 'NO_ACTIONABLE_RESEARCH_CANDIDATE');
            setText('research-report-final-selected-count', researchReport.final_selected_count != null ? String(researchReport.final_selected_count) : '0');
            const researchDistribution = Array.isArray(researchReport.score_distribution) ? researchReport.score_distribution : [];
            setText('research-report-score-distribution', researchDistribution.length ? researchDistribution.map((bucket) => {
                const label = bucket.score_bucket || 'unknown';
                const count = bucket.candidate_count != null ? bucket.candidate_count : 0;
                const dataValidRate = bucket.data_valid_rate != null ? `${bucket.data_valid_rate}%` : 'n/a';
                const backtestCompleteRate = bucket.backtest_complete_rate != null ? `${bucket.backtest_complete_rate}%` : 'n/a';
                const walkForwardCompleteRate = bucket.walk_forward_complete_rate != null ? `${bucket.walk_forward_complete_rate}%` : 'n/a';
                return `${label} · ${count} · ${dataValidRate} · ${backtestCompleteRate} · ${walkForwardCompleteRate}`;
            }).join(' | ') : 'unavailable');
            const strategyCandidates = Array.isArray(researchReport.candidate_strategy_matrix) ? researchReport.candidate_strategy_matrix : [];
            setText('research-report-strategy-matrix', strategyCandidates.length ? strategyCandidates.map((item) => {
                const symbol = item.symbol || item.candidate_id || 'unknown';
                const strategy = item.strategy_id || 'unavailable';
                const fit = item.fit_score != null ? item.fit_score : 'n/a';
                const allowed = item.allowed != null ? (item.allowed ? 'YES' : 'NO') : 'n/a';
                const blockedReason = item.blocked_reason ? ` · ${item.blocked_reason}` : '';
                return `${symbol} · ${strategy} · fit ${fit} · ${allowed}${blockedReason}`;
            }).join(' | ') : 'unavailable');
            const portfolioComposition = researchReport.portfolio_composition || {};
            setText('research-report-portfolio-composition', Object.keys(portfolioComposition).length ? `selected ${portfolioComposition.selected_count ?? 0} · blocked ${portfolioComposition.blocked_count ?? 0} · leveraged/inverse ${portfolioComposition.leveraged_inverse_selected_count ?? 0}` + (Array.isArray(portfolioComposition.selected_symbols) && portfolioComposition.selected_symbols.length ? ` · symbols ${portfolioComposition.selected_symbols.join(' / ')}` : '') : 'unavailable');
            setText('research-report-top-candidates', Array.isArray(researchReport.top_candidates) && researchReport.top_candidates.length
                ? researchReport.top_candidates.map((item) => {
                    const symbol = item.symbol || item.candidate_id || 'unknown';
                    const score = item.candidate_score != null ? item.candidate_score : 'unavailable';
                    const strategy = item.recommended_strategy || 'unavailable';
                    return `${symbol} · ${score} · ${strategy}`;
                }).join(' | ')
                : 'unavailable');
            const researchFailure = researchReport.failure_analysis && researchReport.failure_analysis.statuses ? researchReport.failure_analysis.statuses : {};
            setText('research-report-failure-analysis', `DATA_INVALID ${researchFailure.DATA_INVALID || 0} · BACKTEST_FAILED ${researchFailure.BACKTEST_FAILED || 0} · WALK_FORWARD_FAILED ${researchFailure.WALK_FORWARD_FAILED || 0}`);
            const candidateCard = document.getElementById('candidate-validation-card');
            if (candidateCard) {
                const candidateState = String(candidateValidation.state || candidateValidation.status_label || 'STALE').toUpperCase();
                candidateCard.className = `system-status-card full candidate-validation-card ${candidateState === 'SAFE' ? 'status-live' : candidateState === 'STALE' ? 'status-warn' : 'status-offline'}`;
            }
            const researchCard = document.getElementById('research-report-card');
            if (researchCard) {
                const researchState = String(researchReport.state || researchReport.status_label || 'STALE').toUpperCase();
                researchCard.className = `system-status-card full research-report-card ${researchState === 'SAFE' ? 'status-live' : researchState === 'STALE' ? 'status-warn' : 'status-offline'}`;
            }
            const researchStatus = payload.research_status || {};
            setText('research-status-state', researchStatus.status_label || researchStatus.state || 'unavailable');
            setText('research-status-detail', researchStatus.detail || 'no data');
            setText('research-status-last-run', researchStatus.last_research_run || 'unavailable');
            setText('research-status-date', researchStatus.research_date || 'unavailable');
            setText('research-status-candidate-count', researchStatus.candidate_count != null ? String(researchStatus.candidate_count) : 'unavailable');
            setText('research-status-report-status', researchStatus.report_status || 'unavailable');
            const researchStatusCard = document.getElementById('research-status-card');
            if (researchStatusCard) {
                const researchState = String(researchStatus.state || researchStatus.status_label || 'STALE').toUpperCase();
                researchStatusCard.className = `system-status-card wide research-status-card ${researchState === 'SAFE' ? 'status-live' : researchState === 'STALE' ? 'status-warn' : 'status-offline'}`;
            }

            const modeLabel = String(payload.mode || 'paper').toLowerCase();
            const modeChip = document.getElementById('mode-pill');
            if (modeChip) {
                const modeText = modeLabel === 'sandbox' ? 'SANDBOX · 沙盒' : modeLabel === 'live' ? 'PROD · 实盘账户' : 'PAPER · 虚拟盘';
                modeChip.className = `pill ${modeLabel === 'sandbox' ? 'mode-sandbox' : modeLabel === 'live' ? 'mode-live' : 'mode-paper'}`;
                modeChip.innerHTML = `<strong>${modeText}</strong>`;
            }
            const marketChip = document.getElementById('market-pill');
            if (marketChip) {
                const marketOpen = !!system.market_open;
                marketChip.className = `pill ${marketOpen ? 'status-live' : 'status-warn'}`;
                marketChip.textContent = `市场：${system.market_open_label || 'unavailable'}`;
            }
            const brokerChip = document.getElementById('broker-pill');
            if (brokerChip) {
                brokerChip.className = `pill ${system.broker_connected ? 'status-live' : 'status-offline'}`;
                brokerChip.textContent = `Broker：${system.broker_connection || 'not connected'}`;
            }
            const systemChip = document.getElementById('system-pill');
            if (systemChip) {
                systemChip.className = `pill ${system.broker_connected ? 'status-live' : 'status-offline'}`;
                systemChip.innerHTML = `<strong>${system.mode || 'UNKNOWN'}</strong> · ${system.broker_connection || 'not connected'}`;
            }

            const aiSelection = payload.ai_selection || {};
            setText('ai-selection-execution-status', aiSelection.execution_status ? `执行状态：${aiSelection.execution_status}` : '执行状态：COMPLETED');
            setText('ai-selection-result-quality', aiSelection.result_quality ? `结果质量：${aiSelection.result_quality}` : '结果质量：COMPLETE');
            setText('ai-selection-research-admission', aiSelection.research_admission ? `研究准入：${aiSelection.research_admission}` : '研究准入：RESEARCH_READY');
            setText('ai-selection-research-notice', aiSelection.research_admission_notice || '候选可进入独立数据验证，不代表具备交易资格。');
            const providerSections = aiSelection.provider_audit_sections || {};
            setText('ai-selection-provider-attempted', `Provider 尝试：${providerSections.attempted || '无'}`);
            setText('ai-selection-provider-success', `Provider 成功：${providerSections.success || '无'}`);
            setText('ai-selection-provider-failure', `Provider 失败：${providerSections.failure || '无'}`);
            setText('ai-selection-provider-timeout', `Provider 超时：${providerSections.timeout || '无'}`);
            setText('ai-selection-provider-fallback', `Provider Fallback：${providerSections.fallback || '无'}`);
            setText('ai-selection-provider-mock', `Provider Mock：${providerSections.mock || '无'}`);
            setText('ai-selection-provider-contributor', `Provider 实际贡献：${providerSections.contributor || '无'}`);
        } catch (error) {
            // Keep last known data on failures.
        }
    }

    async function refreshChart(chartEl) {
        const ticker = chartEl.dataset.ticker || '';
        if (!ticker) {
            return;
        }
        try {
            const response = await fetch(`/api/chart/${encodeURIComponent(ticker)}`, { cache: 'no-store' });
            if (!response.ok) {
                throw new Error(`chart ${ticker} status ${response.status}`);
            }
            const payload = await response.json();
            render(chartEl, payload);
        } catch (error) {
            render(chartEl, { prices: [], trades: [] });
        }
    }

    function schedule() {
        charts.forEach((chartEl) => refreshChart(chartEl));
        refreshStatus();
        window.setInterval(() => {
            charts.forEach((chartEl) => refreshChart(chartEl));
            refreshStatus();
        }, 10000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', schedule, { once: true });
    } else {
        schedule();
    }
})();
</script>
</body>
</html>"""


def _fetch_status(port):
    """Fetch /api/status JSON from an engine."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1)
        data = json.loads(req.read().decode())
        if isinstance(data, dict):
            _STATUS_CACHE[port] = data
        _STATUS_FAILURES[port] = 0
        return data
    except Exception:
        failures = _STATUS_FAILURES.get(port, 0) + 1
        _STATUS_FAILURES[port] = failures
        if failures < _STATUS_OFFLINE_THRESHOLD and port in _STATUS_CACHE:
            return _STATUS_CACHE[port]
        return None


def _combined_process_count() -> int:
    try:
        result = subprocess.run(
            ["pgrep", "-af", r"scripts/start_combined.py|src.dashboard.combined|start_combined\(8090\)"],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = []
        for line in (result.stdout or "").splitlines():
            text = str(line or "").strip()
            if not text or "health_check.sh" in text:
                continue
            lines.append(text)
        return len(lines)
    except Exception:
        return 1 if _pid_alive(os.getpid()) else 0


def _top_engine_status(item: dict, rank: int, ticker: str | None, mode: str | None) -> dict:
    port = int(item.get("port", 0) or 0)
    configured = bool(ticker) or _top_config_exists(str(item.get("config") or ""))
    status = _fetch_status(port) if configured and port else None
    if status is not None and not isinstance(status, dict):
        status = {}
    online = bool(status)
    payload_mode = str((status or {}).get("mode") or mode or ("paper" if configured else "disabled")).strip().lower() or "unknown"
    signal = str((status or {}).get("last_signal") or (status or {}).get("signal") or ("OFFLINE" if not online else "HOLD")).strip().upper()
    price = (status or {}).get("price") if online else None
    halted = bool((status or {}).get("halted", False)) if online else False
    return {
        "rank": rank,
        "ticker": ticker if ticker else None,
        "configured": configured,
        "port": port,
        "online": online,
        "mode": payload_mode,
        "signal": signal,
        "price": price,
        "halted": halted,
        "daily_pnl": (status or {}).get("daily_pnl"),
        "equity": (status or {}).get("equity"),
        "cash": (status or {}).get("cash"),
        "buying_power": (status or {}).get("buying_power"),
        "position_shares": (status or {}).get("position_shares"),
        "avg_entry_price": (status or {}).get("avg_entry_price"),
        "unrealized_pnl": (status or {}).get("unrealized_pnl"),
        "unrealized_pnl_pct": (status or {}).get("unrealized_pnl_pct"),
        "trade_in_progress": bool((status or {}).get("trade_in_progress", False)) if online else False,
        "range_ready": bool((status or {}).get("range_ready", False)) if online else False,
        "range_source": (status or {}).get("range_source"),
        "support": (status or {}).get("support"),
        "resistance": (status or {}).get("resistance"),
        "spread_pct": (status or {}).get("spread_pct"),
        "bid": (status or {}).get("bid"),
        "ask": (status or {}).get("ask"),
        "volume": (status or {}).get("volume"),
        "last_signal_reason": (status or {}).get("last_signal_reason"),
    }


def _fallback_runtime_flags() -> tuple[bool, bool]:
    try:
        runtime_config = load_runtime_config()
        live_config_value = getattr(runtime_config, "allow_fallback_live_entries", None)
        paper_config_value = getattr(runtime_config, "allow_fallback_paper_entries", None)
        top_live_allowed, top_paper_allowed = _load_top_ai_selector_flags()
        live_candidates = [bool(value) for value in (live_config_value, top_live_allowed) if value is not None]
        paper_candidates = [bool(value) for value in (paper_config_value, top_paper_allowed) if value is not None]
        live_allowed = any(live_candidates) if live_candidates else False
        paper_allowed = any(paper_candidates) if paper_candidates else False
        return live_allowed, paper_allowed
    except Exception:
        top_live_allowed, top_paper_allowed = _load_top_ai_selector_flags()
        return bool(top_live_allowed), bool(top_paper_allowed)


def _api_status_payload() -> dict[str, object]:
    runtime_config = load_runtime_config()
    ai_selection = _load_ai_selection_report()
    if not isinstance(ai_selection, dict):
        ai_selection = {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}}
    selection_sync = _selection_sync_status()
    trade_audit = summarize_trade_log(PROJECT_DIR / "logs", day=None, mode=_desired_audit_mode())
    top_modes = _load_top_modes()
    top_tickers = list((selection_sync or {}).get("current_top_config_symbols") or current_top_config_symbols(limit=len(TICKERS)))
    dashboard_config = _load_dashboard_config()
    dashboard_mode = str(getattr(dashboard_config, "mode", "") or "paper").strip().lower() if dashboard_config else "paper"
    live_account = _fetch_live_account_summary() if dashboard_mode in {"sandbox", "live"} else None
    active_orders = _load_active_orders_summary(top_tickers)
    top_engines = [
        _top_engine_status(
            item,
            rank=index + 1,
            ticker=top_tickers[index] if index < len(top_tickers) else None,
            mode=top_modes[index] if index < len(top_modes) else "unknown",
        )
        for index, item in enumerate(TICKERS)
    ]
    order_counts = _dashboard_order_status_summary(active_orders)
    selection_date = (
        str((selection_sync or {}).get("state_date") or "").strip()
        or str((selection_sync or {}).get("required_date") or "").strip()
        or None
    )
    fallback_used = bool(ai_selection.get("fallback_used"))
    if not fallback_used:
        fallback_used = any(bool((item or {}).get("fallback_used")) for item in (ai_selection.get("top3") or []))
    live_guard_ok = not any(str(mode).strip().lower() == "live" for mode in top_modes) or bool((selection_sync or {}).get("ok"))
    configured_top_modes = [
        str(item.get("mode") or "").strip().lower()
        for item in top_engines
        if item.get("configured")
    ]
    distinct_top_modes = sorted({mode for mode in configured_top_modes if mode and mode != "disabled"})
    mode_consistency = {
        "dashboard_mode": dashboard_mode or "paper",
        "top_modes": configured_top_modes,
        "mixed": bool(distinct_top_modes and (dashboard_mode or "paper") not in distinct_top_modes),
        "reason": "",
    }
    if mode_consistency["mixed"]:
        mode_consistency["reason"] = "dashboard_mode_differs_from_top_engine_mode"
    fallback_live_allowed, fallback_paper_allowed = _fallback_runtime_flags()
    top_daily_pnl = 0.0
    top_unrealized_pnl = 0.0
    top_equity = 0.0
    top_cash = 0.0
    top_buying_power = 0.0
    for item in top_engines:
        try:
            top_daily_pnl += float(item.get("daily_pnl") or 0.0)
            top_unrealized_pnl += float(item.get("unrealized_pnl") or 0.0)
            top_equity += float(item.get("equity") or 0.0)
            top_cash += float(item.get("cash") or 0.0)
            top_buying_power += float(item.get("buying_power") or 0.0)
        except Exception:
            continue
    system_status = _system_status_snapshot(
        runtime_config=dashboard_config,
        live_account=live_account,
        trade_audit=trade_audit,
        active_orders=active_orders,
        mode_override=dashboard_mode,
    )
    shadow_status = _shadow_status_payload()
    candidate_validation = _candidate_validation_payload()
    ai_selection_top3 = list(ai_selection.get("top3") or [])
    ai_selection_data_status = str(ai_selection.get("data_status") or "")
    ai_selection_notice_status = str((ai_selection_top3[0].get("data_status") if ai_selection_top3 else ai_selection_data_status) or "")
    return {
        "ok": True,
        "mode": dashboard_mode or "paper",
        "runtime_mode": dashboard_mode or "paper",
        "top_modes": configured_top_modes,
        "mode_consistency": mode_consistency,
        "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "combined": {
            "port": _COMBINED_PORT,
            "process_count": _combined_process_count(),
        },
        "selection": {
            "synced": bool((selection_sync or {}).get("ok")),
            "selection_date": selection_date,
            "selection_state_tickers": list((selection_sync or {}).get("selection_state_symbols") or []),
            "top_config_tickers": list((selection_sync or {}).get("current_top_config_symbols") or top_tickers),
            "fallback_used": fallback_used,
            "reason": str((selection_sync or {}).get("mismatch_reason") or ""),
        },
        "top_engines": top_engines,
        "candidate_validation_api_available": True,
        "risk": {
            "live_guard_ok": live_guard_ok,
            "fallback_live_allowed": fallback_live_allowed,
            "fallback_paper_allowed": fallback_paper_allowed,
        },
        "dashboard": {
            "chart_api_available": True,
            "candidate_validation_api_available": True,
            "summary": {
                "cash": live_account.get("cash") if isinstance(live_account, dict) else None,
                "equity": live_account.get("equity") if isinstance(live_account, dict) else None,
                "buying_power": live_account.get("buying_power") if isinstance(live_account, dict) else None,
                "positions_count": len(live_account.get("positions") or []) if isinstance(live_account, dict) and isinstance(live_account.get("positions"), list) else (live_account or {}).get("positions_count"),
                "top_engine_online_count": sum(1 for item in top_engines if item.get("online")),
                "active_orders_total": order_counts["total"],
                "active_orders_pending": order_counts["pending"],
                "active_orders_partial_filled": order_counts["partial_filled"],
                "today_total_pnl": round(top_daily_pnl, 2),
                "total_pnl": round(top_unrealized_pnl, 2),
                "total_equity": round(top_equity, 2),
                "total_cash": round(top_cash, 2),
                "total_buying_power": round(top_buying_power, 2),
                "total_trades": int(trade_audit.get("execution_count", 0) or 0),
            },
        },
        "shadow": shadow_status,
        "candidate_validation": candidate_validation,
        "candidate_model_evaluation": _candidate_model_evaluation_payload(),
        "research_status": _research_status_payload(),
        "research_report": _candidate_research_report_payload(),
        "ai_selection": {
            "price_band": _ai_selection_price_band(ai_selection),
            "execution_status": str(ai_selection.get("execution_status") or "").strip().upper() or "COMPLETED",
            "result_quality": str(ai_selection.get("result_quality") or "").strip().upper() or "COMPLETE",
            "research_admission": str(ai_selection.get("research_admission") or "").strip().upper() or "RESEARCH_READY",
            "research_admission_notice": build_research_admission_notice(
                str(ai_selection.get("execution_status") or "").strip().upper() or "COMPLETED",
                str(ai_selection.get("result_quality") or "").strip().upper() or "COMPLETE",
                str(ai_selection.get("research_admission") or "").strip().upper() or "RESEARCH_READY",
                bool(ai_selection.get("mock_used", False)),
                ai_selection_notice_status,
            ),
            "selection_stage": str(ai_selection.get("selection_stage") or "").strip().upper() or "FINALIZED",
            "fallback_used": bool(ai_selection.get("fallback_used", False)),
            "provider_fallback_used": bool(ai_selection.get("provider_fallback_used", False)),
            "top_n_complete": bool(ai_selection.get("top_n_complete", False)),
            "top_n_missing_count": int(ai_selection.get("top_n_missing_count") or 0),
            "warnings_structured": list(ai_selection.get("warnings_structured") or []),
            "provider_audit": ai_selection.get("provider_audit") or {},
            "provider_audit_sections": dict(
                ai_selection.get("provider_audit_sections")
                or build_provider_audit_sections(
                    dict(ai_selection.get("provider_audit") or {}),
                    dict(ai_selection.get("provider_outputs") or {}),
                )
            ),
            "provider_audit_normalized": dict(
                ai_selection.get("provider_audit_normalized")
                or normalize_provider_audit(
                    dict(ai_selection.get("provider_audit") or {}),
                    dict(ai_selection.get("provider_outputs") or {}),
                )
            ),
            "selection_funnel": dict(
                ai_selection.get("selection_funnel")
                or {
                    "universe_scanned": len(ai_selection.get("top10") or ai_selection.get("top3") or []),
                    "universe_passed": len(ai_selection.get("top10") or ai_selection.get("top3") or []),
                    "data_complete": int(sum(1 for item in (ai_selection.get("top10") or ai_selection.get("top3") or []) if str((item or {}).get("data_status") or "").strip().upper() == "VALID")),
                    "scoring_eligible": int(sum(1 for item in (ai_selection.get("top10") or ai_selection.get("top3") or []) if bool((item or {}).get("scoring_eligible", False)))),
                    "ranked_candidates": len(ai_selection.get("top10") or ai_selection.get("top3") or []),
                    "quality_threshold_passed": int(sum(1 for item in (ai_selection.get("top10") or ai_selection.get("top3") or []) if bool((item or {}).get("trade_filter_passed", False)))),
                    "preliminary_selected": len(ai_selection.get("top3") or ai_selection.get("top5") or []),
                    "refined_selected": len(ai_selection.get("top3") or ai_selection.get("top5") or []),
                    "final_selected": len(ai_selection.get("top3") or ai_selection.get("top5") or []),
                    "provider_timeouts": int((ai_selection.get("provider_audit") or {}).get("provider_timeouts", 0) or 0),
                    "provider_failures": int((ai_selection.get("provider_audit") or {}).get("provider_failures", 0) or 0),
                    "total_budget_seconds": int(os.environ.get("AI_SELECTOR_TOTAL_BUDGET_SECONDS", "0") or 0),
                    "budget_exhausted": bool((ai_selection.get("quality_filter_report") or {}).get("timed_out", False)),
                    "run_mode": "full",
                }
            ),
            "rejection_trace": list(ai_selection.get("rejection_trace") or []),
            "rejection_reason_counts": dict(
                ai_selection.get("rejection_reason_counts")
                or {
                    str(item.get("warning_code") or item.get("reason_code") or "top_n_not_filled" if str(item.get("warning_code") or item.get("reason_code") or "").strip() == "top_n_not_filled" else "unknown"): int(item.get("count", 1) or 1)
                    for item in (ai_selection.get("rejection_trace") or [])
                    if isinstance(item, dict)
                }
                or {
                    str(item.get("warning_code") or item.get("code") or "top_n_not_filled"): int(1)
                    for item in (ai_selection.get("warnings_structured") or [])
                    if isinstance(item, dict)
                    and str(item.get("warning_code") or item.get("code") or "").strip().lower() == "top_n_not_filled"
                }
            ),
            "top3": list(ai_selection.get("top3") or []),
        },
        "system": system_status,
    }


@app.route("/api/chart/<ticker>")
def api_chart(ticker):
    try:
        snapshot = _chart_snapshot_for_ticker(ticker, refresh=True)
        return jsonify(
            {
                "ticker": _chart_ticker(ticker),
                "prices": snapshot.get("prices", []),
                "trades": snapshot.get("trades", []),
                "current_price": snapshot.get("current_price"),
            }
        )
    except Exception:
        return jsonify({"ticker": _chart_ticker(ticker), "prices": [], "trades": []})


@app.route("/api/candidates/status")
def api_candidate_validation_status():
    try:
        return jsonify(_candidate_validation_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/candidates/performance")
def api_candidate_performance():
    try:
        return jsonify(_candidate_performance_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/candidate-model/evaluation")
def api_candidate_model_evaluation():
    try:
        return jsonify(_candidate_model_evaluation_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/research/report")
def api_research_report():
    try:
        return jsonify(_candidate_research_report_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/research/status")
def api_research_status():
    try:
        return jsonify(_research_status_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/shadow/status")
def api_shadow_status():
    try:
        return jsonify(_shadow_status_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/shadow/summary")
def api_shadow_summary():
    try:
        return jsonify(_shadow_summary_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "detail": "data_invalid", "error": str(exc)}), 200


@app.route("/api/shadow/blocked-reasons")
def api_shadow_blocked_reasons():
    try:
        return jsonify(_shadow_blocked_reasons_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "items": [], "count": 0, "error": str(exc)}), 200


@app.route("/api/shadow/equity")
def api_shadow_equity():
    try:
        return jsonify(_shadow_equity_payload()), 200
    except Exception as exc:
        return jsonify({"ok": False, "state": "STALE", "status_label": "unavailable", "items": [], "latest": {}, "count": 0, "error": str(exc)}), 200


@app.route("/api/status")
def api_status():
    try:
        return jsonify(_api_status_payload()), 200
    except Exception as exc:
        fallback_live_allowed, fallback_paper_allowed = _fallback_runtime_flags()
        dashboard_config = _load_dashboard_config()
        dashboard_mode = str(getattr(dashboard_config, "mode", "") or "paper").strip().lower() if dashboard_config else "paper"
        return jsonify(
            {
                "ok": False,
                "mode": dashboard_mode or "paper",
                "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "combined": {
                    "port": _COMBINED_PORT,
                    "process_count": _combined_process_count(),
                },
                "selection": {
                    "synced": False,
                    "selection_date": None,
                    "selection_state_tickers": [],
                    "top_config_tickers": [],
                    "fallback_used": False,
                    "reason": str(exc),
                },
                "top_engines": [],
                "risk": {
                    "live_guard_ok": False,
                    "fallback_live_allowed": fallback_live_allowed,
                    "fallback_paper_allowed": fallback_paper_allowed,
                },
                "dashboard": {
                    "chart_api_available": True,
                    "candidate_validation_api_available": True,
                },
                "candidate_validation_api_available": True,
                "shadow": _shadow_status_payload(),
                "candidate_validation": _candidate_validation_payload(),
                "candidate_model_evaluation": _candidate_model_evaluation_payload(),
                "research_status": _research_status_payload(),
                "ai_selection": {
                    "price_band": {"min": 5.0, "max": 300.0, "defaulted": True},
                },
                "system": _system_status_snapshot(
                    runtime_config=dashboard_config,
                    live_account=_fetch_live_account_summary() if dashboard_mode in {"sandbox", "live"} else None,
                    trade_audit={},
                    active_orders=None,
                    mode_override=dashboard_mode,
                ),
                "error": str(exc),
            }
        ), 200


def _load_config_defaults(config_name):
    """Read display defaults from YAML so offline engines do not show $0 capital."""
    cfg_path = PROJECT_DIR / "configs" / config_name
    defaults = {
        "ticker": config_name.replace(".yaml", ""),
        "configured": cfg_path.exists(),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
        "reduce_only": False,
    }
    try:
        flags = json.loads(TRADING_FLAGS_PATH.read_text(encoding="utf-8")) if TRADING_FLAGS_PATH.exists() else {}
        if not isinstance(flags, dict):
            flags = {}
        data = yaml.safe_load(cfg_path.read_text()) or {}
        defaults["ticker"] = data.get("ticker") or defaults["ticker"]
        position = data.get("position") or {}
        defaults["initial_capital"] = float(position.get("initial_capital") or 0.0)
        defaults["reduce_only"] = bool(position.get("reduce_only", False)) or bool(flags.get("reduce_only_all", False))
        range_cfg = data.get("range") or {}
        defaults["support"] = float(range_cfg.get("support_price") or 0.0)
        defaults["resistance"] = float(range_cfg.get("resistance_price") or 0.0)
    except Exception:
        pass
    return defaults


def _build_sparkline(prices, current_price):
    """Turn price list into sparkline bar dicts."""
    bars = []
    if not prices or len(prices) < 2:
        # Empty sparkline
        for _ in range(30):
            bars.append({"height": 50, "color": "#222"})
        return bars

    valid = [p for p in prices if p > 0]
    if len(valid) < 2:
        for _ in range(30):
            bars.append({"height": 50, "color": "#222"})
        return bars

    lo = min(valid)
    hi = max(valid)
    rng = hi - lo if hi != lo else 1

    prev = valid[0]
    for p in valid[-30:]:
        h = (p - lo) / rng * 100
        if h < 0: h = 0
        if h > 100: h = 100
        color = "#00d4aa" if p >= prev else "#ff4757"
        bars.append({"height": round(h, 1), "color": color})
        prev = p

    # Pad to 30
    while len(bars) < 30:
        bars.insert(0, {"height": 50, "color": "#222"})

    return bars


def _chart_ticker(value: object) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _chart_parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if number != number:
            return None
        return number
    except (TypeError, ValueError):
        return None


def _chart_parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _chart_display_time(value: object) -> str:
    parsed = _chart_parse_timestamp(value)
    if parsed is None:
        return str(value or "")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    try:
        parsed = parsed.astimezone(_CHART_TZ)
    except Exception:
        pass
    return parsed.isoformat(timespec="seconds")


def _chart_normalize_points(points: list[dict[str, object]] | None) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for raw in points or []:
        if not isinstance(raw, dict):
            continue
        price = _chart_parse_float(raw.get("price"))
        time_value = str(raw.get("time") or "").strip()
        if price is None or price <= 0 or not time_value:
            continue
        cleaned.append({"time": time_value, "price": round(price, 4)})
    return cleaned[-_CHART_HISTORY_LIMIT:]


def _chart_cache_price_point(ticker: str, price: float, timestamp: object | None = None) -> None:
    normalized = _chart_ticker(ticker)
    if not normalized:
        return
    price = _chart_parse_float(price)
    if price is None or price <= 0:
        return
    time_value = _chart_display_time(timestamp or datetime.now(ZoneInfo("UTC")))
    point = {"time": time_value, "price": round(price, 4)}
    with _CHART_CACHE_LOCK:
        history = _CHART_PRICE_HISTORY.setdefault(normalized, [])
        if history and history[-1].get("time") == point["time"] and history[-1].get("price") == point["price"]:
            return
        history.append(point)
        if len(history) > _CHART_HISTORY_LIMIT:
            del history[:-_CHART_HISTORY_LIMIT]


def _chart_history_for_ticker(ticker: str) -> list[dict[str, object]]:
    normalized = _chart_ticker(ticker)
    if not normalized:
        return []
    with _CHART_CACHE_LOCK:
        history = list(_CHART_PRICE_HISTORY.get(normalized, []))
    return _chart_normalize_points(history)


def _chart_port_for_ticker(ticker: str) -> int | None:
    normalized = _chart_ticker(ticker)
    if not normalized:
        return None
    for item in TICKERS:
        cfg = _load_config_defaults(item["config"])
        if _chart_ticker(cfg.get("ticker")) == normalized:
            try:
                return int(item["port"])
            except (TypeError, ValueError):
                return None
    return None


def _chart_snapshot_for_ticker(ticker: str, *, refresh: bool = True) -> dict[str, object]:
    normalized = _chart_ticker(ticker)
    if not normalized:
        return {"ticker": "", "prices": [], "trades": []}
    if refresh:
        port = _chart_port_for_ticker(normalized)
        if port is not None:
            try:
                status = _fetch_status(port)
            except Exception:
                status = None
            if isinstance(status, dict):
                price = _chart_parse_float(status.get("price"))
                if price is not None and price > 0:
                    _chart_cache_price_point(normalized, price, status.get("timestamp") or datetime.now(ZoneInfo("UTC")))
    prices = _chart_history_for_ticker(normalized)
    trades = _chart_trades_for_ticker(normalized)
    current_price = None
    if prices:
        current_price = prices[-1].get("price")
    elif trades:
        current_price = trades[-1].get("price")
    return {
        "ticker": normalized,
        "prices": prices,
        "trades": trades,
        "current_price": current_price,
    }


def _chart_trade_day() -> str | None:
    today = datetime.now(_CHART_TZ).strftime("%Y%m%d")
    today_path = PROJECT_DIR / "logs" / f"trades-{today}.jsonl"
    if today_path.exists():
        return today
    return None


_FILL_PRICE_PATTERN = re.compile(r"executed_price:\s*Some\(([^)]+)\)")
_SIDE_PATTERN = re.compile(r"side:\s*(Buy|Sell)")
_SYMBOL_PATTERN = re.compile(r"symbol:\s*\"([A-Za-z0-9.-]+)\"")
_QTY_PATTERN = re.compile(r"quantity:\s*(\d+)")
_STATUS_PATTERN = re.compile(r"status:\s*Filled", re.IGNORECASE)
_ORDER_ID_PATTERN = re.compile(r"order_id:\s*\"([^\"]+)\"")
_TIME_PATTERN = re.compile(r'submitted_at:\s*"([^"]+)"')


def _chart_trades_for_ticker(ticker: str) -> list[dict[str, object]]:
    normalized = _chart_ticker(ticker)
    if not normalized:
        return []
    day = _chart_trade_day()
    if not day:
        return []
    records = load_trade_records(PROJECT_DIR / "logs", day=day)
    trades: list[dict[str, object]] = []
    seen_order_ids: set[str] = set()
    seen_trade_keys: set[tuple[object, ...]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("action") or "").strip().lower() != "get_order":
            continue
        request = record.get("request") if isinstance(record.get("request"), dict) else {}
        response = record.get("response") if isinstance(record.get("response"), dict) else {}
        raw_order = str(response.get("order") or "")
        if not raw_order or not _STATUS_PATTERN.search(raw_order):
            continue
        symbol_match = _SYMBOL_PATTERN.search(raw_order)
        symbol = _chart_ticker(symbol_match.group(1) if symbol_match else "")
        if symbol != normalized:
            continue
        order_id = str(request.get("order_id") or "")
        if order_id and order_id in seen_order_ids:
            continue
        seen_order_ids.add(order_id or f"{normalized}:{record.get('timestamp')}")
        side_match = _SIDE_PATTERN.search(raw_order)
        price_match = _FILL_PRICE_PATTERN.search(raw_order)
        qty_match = _QTY_PATTERN.search(raw_order)
        time_match = _TIME_PATTERN.search(raw_order)
        price = _chart_parse_float(price_match.group(1) if price_match else None)
        qty = int(qty_match.group(1)) if qty_match else 0
        if price is None:
            continue
        trade_key = (
            normalized,
            str(side_match.group(1) if side_match else "").upper() or "FILLED",
            round(price, 4),
            qty,
            _chart_display_time(time_match.group(1) if time_match else record.get("timestamp")),
        )
        if trade_key in seen_trade_keys:
            continue
        seen_trade_keys.add(trade_key)
        trades.append(
            {
                "time": trade_key[4],
                "ticker": normalized,
                "side": trade_key[1],
                "price": round(price, 4),
                "qty": qty,
                "status": "FILLED",
                "order_id": order_id,
            }
        )
    trades.sort(key=lambda item: str(item.get("time") or ""))
    return trades[-100:]


def _fmt_vol(v):
    if v >= 1_000_000_000:
        return f"{v/1e9:.1f}B"
    if v >= 1_000_000:
        return f"{v/1e6:.1f}M"
    if v >= 1_000:
        return f"{v/1e3:.1f}K"
    return str(int(v))


def _signal_cn(signal: str) -> str:
    normalized = str(signal or "").strip().upper()
    mapping = {
        "BUY": "买入",
        "SELL": "卖出",
        "HOLD": "观察",
        "OFFLINE": "离线",
    }
    if "TREND" in normalized:
        return "趋势过滤"
    return mapping.get(normalized, normalized or "暂无")


def _ai_range_lookup(ai_selection: dict | None) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    if not isinstance(ai_selection, dict):
        return lookup
    for item in ai_selection.get("top3") or []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            low = float(item.get("range_low")) if item.get("range_low") is not None else None
        except (TypeError, ValueError):
            low = None
        try:
            high = float(item.get("range_high")) if item.get("range_high") is not None else None
        except (TypeError, ValueError):
            high = None
        lookup[ticker] = {
            "range_low": low,
            "range_high": high,
            "suggested_range": str(item.get("suggested_range") or "").strip(),
        }
    return lookup


def _load_order_states(
    active_symbols: set[str] | None = None,
    current_signals: dict[str, str] | None = None,
) -> dict:
    """Read order-state files from disk for dashboard display."""
    result = {
        "blocked_tickers": [],
        "failed_orders_today": 0,
        "failed_orders_total_today": 0,
        "ticker_details": [],
        "historical_ticker_details": [],
        "historical_failed_orders_today": 0,
        "historical_failed_orders_total_today": 0,
    }
    active_symbols = {str(symbol or "").strip().upper() for symbol in (active_symbols or set()) if str(symbol or "").strip()}
    current_signals = {
        str(symbol or "").strip().upper(): str(signal or "").strip().upper()
        for symbol, signal in (current_signals or {}).items()
        if str(symbol or "").strip()
    }
    order_state_dir = STATE_DIR / "order_state"
    if not order_state_dir.is_dir():
        return result
    for path in sorted(order_state_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            ticker = data.get("ticker", path.stem)
            ticker_upper = str(ticker or "").strip().upper()
            if data.get("runtime_scope") == "test":
                continue
            if ticker_upper in _SYNTHETIC_TEST_TICKERS:
                continue
            if active_symbols and ticker_upper not in active_symbols and not data.get("blocked") and not data.get("failed_orders_today"):
                continue
            current_signal = current_signals.get(ticker_upper, "") if current_signals else ""
            blocked = data.get("blocked")
            blocked_active = False
            blocked_detail = None
            if isinstance(blocked, dict):
                blocked_until = str(blocked.get("blocked_until", "") or "").strip()
                try:
                    from datetime import datetime as dt

                    bu = dt.fromisoformat(blocked_until) if blocked_until else None
                    if bu and bu > dt.now():
                        remaining = int((bu - dt.now()).total_seconds())
                        blocked_active = True
                        blocked_detail = {
                            "until": blocked_until,
                            "reason": blocked.get("reason", ""),
                            "remaining_min": remaining // 60,
                            "remaining_sec": remaining % 60,
                        }
                except Exception:
                    blocked_active = bool(blocked)
            detail = {
                "ticker": ticker_upper,
                "blocked": None,
                "failed_count": len(data.get("failed_orders_today", [])),
                "last_failed": None,
                "current_active": ticker_upper in active_symbols if active_symbols else True,
                "current_signal": current_signal or None,
            }
            failed_orders = data.get("failed_orders_today", [])
            is_active = detail["current_active"]
            is_current_buy_focus = bool(current_signals) and current_signal == "BUY"
            keep_active_failures = is_current_buy_focus or blocked_active
            if is_active:
                if keep_active_failures or not current_signals:
                    result["failed_orders_total_today"] += len(failed_orders)
                else:
                    result["historical_failed_orders_total_today"] += len(failed_orders)
            else:
                result["historical_failed_orders_total_today"] += len(failed_orders)
            if failed_orders:
                last = failed_orders[-1]
                detail["last_failed"] = {
                    "reason": last.get("reason", ""),
                    "timestamp": last.get("timestamp", ""),
                    "quantity": last.get("quantity", 0),
                    "buying_power": last.get("buying_power", 0.0),
                }
                if is_active and (keep_active_failures or not current_signals):
                    result["failed_orders_today"] += 1
                else:
                    result["historical_failed_orders_today"] += 1
            if blocked_active and blocked_detail:
                detail["blocked"] = blocked_detail
                if is_active:
                    result["blocked_tickers"].append(detail)
                else:
                    result.setdefault("historical_blocked_tickers", []).append(detail)
            if is_active and (keep_active_failures or not current_signals):
                result["ticker_details"].append(detail)
            else:
                result["historical_ticker_details"].append(detail)
        except Exception:
            pass
    result["ticker_details"].sort(
        key=lambda item: str(((item.get("last_failed") or {}).get("timestamp") or "")),
        reverse=True,
    )
    result["historical_ticker_details"].sort(
        key=lambda item: str(((item.get("last_failed") or {}).get("timestamp") or "")),
        reverse=True,
    )
    return result


@app.route("/")
def index():
    cards = []
    total_pnl = 0.0
    today_total_pnl = 0.0
    total_capital = 0.0
    total_equity = 0.0
    total_trades = 0
    dashboard_config = _load_dashboard_config()
    runtime_mode = str(getattr(dashboard_config, "mode", "") or "paper").strip().lower() if dashboard_config else "paper"
    runtime_settings = load_runtime_settings()
    live_account = None
    account_positions = {}
    live_account_mode = ""
    use_external_account_positions = False
    ai_selection = _load_ai_selection_report()
    if not isinstance(ai_selection, dict):
        ai_selection = {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}}
    _enrich_ticker_descriptions(ai_selection.get("top3", []))
    ai_ranges = _ai_range_lookup(ai_selection)
    ai_selection_price_band = _ai_selection_price_band(ai_selection)
    ai_universe_filter = _ai_universe_filter_summary(ai_selection)
    research_digest = _load_latest_research_digest()
    ai_runtime = _ai_runtime_status()
    selection_sync = _selection_sync_status()
    audit_scope = str(request.args.get("audit_scope", "today") or "today").strip().lower()
    audit_day = None if audit_scope == "today" else latest_trade_activity_day(PROJECT_DIR / "logs", mode=_desired_audit_mode())
    trade_audit = summarize_trade_log(PROJECT_DIR / "logs", day=audit_day, mode=_desired_audit_mode())
    try:
        guard_params = inspect.signature(_startup_guard_status).parameters
        if len(guard_params) >= 2:
            startup_guard = _startup_guard_status(selection_sync, trade_audit.get("execution_mode"))
        else:
            startup_guard = _startup_guard_status(selection_sync)
    except (TypeError, ValueError):
        startup_guard = _startup_guard_status(selection_sync)
    update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latest_line = _latest_trade_line(trade_audit)
    trade_audit = {
        "broker_unresolved_count": int(trade_audit.get("broker_unresolved_count", 0) or 0),
        "broker_unresolved_oldest_seconds": float(trade_audit.get("broker_unresolved_oldest_seconds", 0.0) or 0.0),
        "latest_submitted_line": str(trade_audit.get("latest_submitted_line", "") or ""),
        **trade_audit,
        "latest_line": latest_line,
        "execution_mode": _resolve_dashboard_execution_mode(trade_audit),
        "new_entries_allowed": bool(trade_audit.get("new_entries_allowed", True)),
        "reduce_only": bool(trade_audit.get("reduce_only", False)),
        "unresolved_alert_threshold_seconds": _UNRESOLVED_ALERT_SECONDS,
        "unresolved_alert": bool(
            int(trade_audit.get("broker_unresolved_count", 0) or 0) > 0
            and float(trade_audit.get("broker_unresolved_oldest_seconds", 0.0) or 0.0) >= _UNRESOLVED_ALERT_SECONDS
        ),
    }
    dashboard_execution_mode = _resolve_dashboard_execution_mode(trade_audit)
    runtime_account_mode = str(trade_audit.get("execution_mode") or "").strip().lower()
    effective_mode = runtime_account_mode if runtime_account_mode in {"paper", "sandbox", "live"} else runtime_mode
    live_account = _fetch_live_account_summary() if runtime_mode in {"sandbox", "live"} else None
    account_positions = _position_lookup(live_account)
    live_account_mode = str((live_account or {}).get("mode") or "").strip().lower()
    use_external_account_positions = bool(
        live_account
        and live_account_mode in {"live", "sandbox"}
        and not (live_account or {}).get("account_error")
    )
    dashboard_status_by_symbol: dict[str, dict | None] = {}
    for item in TICKERS:
        defaults = _load_config_defaults(item["config"])
        if defaults.get("configured") is False:
            continue
        symbol = str(defaults["ticker"]).strip().upper()
        dashboard_status_by_symbol[symbol] = _fetch_status(item["port"])
    dashboard_active_symbols = _dashboard_active_symbols(ai_selection, selection_sync, live_account)
    try:
        order_states = _load_order_states(
            active_symbols=set(dashboard_active_symbols),
            current_signals={
                symbol: str((payload or {}).get("last_signal") or (payload or {}).get("signal") or "").strip().upper()
                for symbol, payload in dashboard_status_by_symbol.items()
                if symbol
            },
        )
    except TypeError as exc:
        if "unexpected keyword argument" in str(exc):
            order_states = _load_order_states()
        else:
            raise
    audit_day_label = (
        (audit_day or datetime.now().strftime("%Y%m%d"))
        if audit_scope in {"today", "latest"}
        else datetime.now().strftime("%Y%m%d")
    )
    active_orders_summary = _load_active_orders_summary(
        [
            str(_load_config_defaults(t["config"]).get("ticker") or "").strip().upper()
            for t in TICKERS
            if _load_config_defaults(t["config"]).get("configured") is not False
        ]
    )
    selected_tickers: set[str] = set()

    for t in TICKERS:
        defaults = _load_config_defaults(t["config"])
        if defaults.get("configured") is False:
            cards.append({
                "name": t["name"],
                "desc": f"{t['desc']} · 未生成配置",
                "ticker": "",
                "online": False,
                "configured": False,
                "price": 0,
                "price_change": 0,
                "day_high": 0,
                "day_low": 0,
                "bid": 0,
                "ask": 0,
                "vol_display": "0",
                "support": 0,
                "resistance": 0,
                "spread_pct": 0,
                "range_ready": False,
                "range_source": "disabled",
                "pos_pct": 50,
                "sparkline": _build_sparkline([], 0),
                "signal": "DISABLED",
                "signal_cn": "未启用",
                "signal_reason": "当前 AI 选股不足 3 个，未生成该 TOP 配置。",
                "ai_range_low": None,
                "ai_range_high": None,
                "ai_suggested_range": "暂无",
                "shares": 0,
                "avg_entry_price": 0,
                "current_price_for_position": 0,
                "market_value": 0.0,
                "initial_capital": 0.0,
                "cash": 0.0,
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "hold_source": "未生成配置",
                "reduce_only": False,
                "equity": 0.0,
                "trades": 0,
                "win_rate": 0,
                "wins": 0,
                "losses": 0,
                "best_trade": 0,
                "worst_trade": 0,
                "avg_pnl": 0,
                "halted": False,
                "trade_in_progress": False,
                "chart_prices": [],
                "chart_trades": [],
            })
            continue
        selected_tickers.add(str(defaults["ticker"]).strip().upper())
        d = dashboard_status_by_symbol.get(str(defaults["ticker"]).strip().upper()) or _fetch_status(t["port"])

        if d:
            supp = d.get("support", 0)
            res = d.get("resistance", 0)
            price = d.get("price", 0)
            entry_price = float(d.get("entry_price", 0.0) or 0.0)
            position_shares = int(d.get("position_shares", 0) or 0)
            unrealized_pnl = float(d.get("unrealized_pnl", 0.0) or 0.0)
            unrealized_pnl_pct = float(d.get("unrealized_pnl_pct", 0.0) or 0.0)
            if position_shares > 0 and float(price or 0.0) > 0.0 and entry_price > 0.0:
                derived_pnl = round((float(price or 0.0) - entry_price) * position_shares, 6)
                if unrealized_pnl == 0.0:
                    unrealized_pnl = derived_pnl
                if unrealized_pnl_pct == 0.0:
                    unrealized_pnl_pct = round(((float(price or 0.0) - entry_price) / entry_price) * 100.0, 6)
            pos_pct = ((price - supp) / (res - supp) * 100) if res and supp and res != supp else 50
            pos_pct = max(0, min(100, pos_pct))

            sparkline = _build_sparkline([price], price)
            selected_ticker = str(defaults["ticker"]).strip().upper()
            account_pos = account_positions.get(selected_ticker) if use_external_account_positions else None
            account_shares = int((account_pos or {}).get("quantity", 0) or 0)
            account_pnl = float((account_pos or {}).get("unrealized_pnl", 0.0) or 0.0)
            account_pnl_pct = float((account_pos or {}).get("unrealized_pnl_pct", 0.0) or 0.0)
            if account_shares > 0 and float(price or 0.0) > 0.0:
                account_entry_price = float((account_pos or {}).get("avg_entry_price", 0.0) or entry_price or 0.0)
                if account_entry_price > 0.0 and account_pnl == 0.0:
                    account_pnl = round((float(price or 0.0) - account_entry_price) * account_shares, 6)
                if account_entry_price > 0.0 and account_pnl_pct == 0.0:
                    account_pnl_pct = round(((float(price or 0.0) - account_entry_price) / account_entry_price) * 100.0, 6)
                if effective_mode == "paper":
                    hold_source = "PaperBroker" if account_pos or account_summary else "PaperBroker / 引擎状态"
                elif effective_mode == "sandbox":
                    if live_account and live_account_mode == "sandbox":
                        hold_source = "LongBridge sandbox"
                        if not account_pos:
                            hold_source += " / 账户无该标的"
                    else:
                        hold_source = "LongBridge sandbox / not connected"
                elif effective_mode == "live":
                    if live_account and live_account_mode == "live":
                        hold_source = "LongBridge prod"
                        if not account_pos:
                            hold_source += " / 账户无该标的"
                    else:
                        hold_source = "LongBridge prod / not connected"
            else:
                hold_source = "引擎状态"
            ai_range = ai_ranges.get(selected_ticker, {})
            chart_snapshot = _chart_snapshot_for_ticker(selected_ticker, refresh=True)

            card = {
                "name": f"{t['name']} · {defaults['ticker']}" if t["name"].startswith("TOP") else t["name"],
                "ticker": selected_ticker,
                "desc": t["desc"],
                "online": True,
                "configured": True,
                "price": price,
                "price_change": d.get("change", 0),
                "day_high": d.get("high_1m", price),
                "day_low": d.get("low_1m", price),
                "bid": d.get("bid", price),
                "ask": d.get("ask", price),
                "vol_display": _fmt_vol(d.get("volume", 0)),
                "support": supp,
                "resistance": res,
                "spread_pct": d.get("spread_pct", 0),
                "range_ready": bool(d.get("range_ready")),
                "range_source": d.get("range_source", "unknown"),
                "pos_pct": pos_pct,
                "sparkline": sparkline,
                "signal": d.get("last_signal", "N/A"),
                "signal_cn": _signal_cn(d.get("last_signal", "N/A")),
                "signal_reason": d.get("last_signal_reason", "暂无"),
                "ai_range_low": ai_range.get("range_low"),
                "ai_range_high": ai_range.get("range_high"),
                "ai_suggested_range": ai_range.get("suggested_range") or "暂无",
                "initial_capital": d.get("initial_capital", 0),
                "cash": d.get("cash", 0),
                "shares": account_shares if account_pos else int(d.get("position_shares", 0) or 0),
                "avg_entry_price": account_pos.get("avg_entry_price") if account_pos else entry_price,
                "current_price_for_position": price,
                "market_value": float((account_pos or {}).get("market_value", 0.0) or (position_shares * float(price or 0.0))),
                "pnl": account_pnl if account_pos else unrealized_pnl,
                "pnl_pct": account_pnl_pct if account_pos else unrealized_pnl_pct,
                "hold_source": hold_source,
                "reduce_only": defaults.get("reduce_only", False),
                "equity": d.get("equity", 0),
                "trades": d.get("trades_today", 0),
                "win_rate": float(d.get("win_rate", 0) or 0.0),
                "wins": int(d.get("wins", 0) or 0),
                "losses": int(d.get("losses", 0) or 0),
                "best_trade": float(d.get("best_trade", 0) or 0.0),
                "worst_trade": float(d.get("worst_trade", 0) or 0.0),
                "avg_pnl": float(d.get("avg_pnl", 0) or 0.0),
                "halted": d.get("halted", False),
                "trade_in_progress": bool(d.get("trade_in_progress", False)),
                "chart_prices": chart_snapshot.get("prices", []),
                "chart_trades": chart_snapshot.get("trades", []),
            }
            cards.append(card)
            day_pnl = float(d.get("daily_pnl", 0) or 0.0)
            today_total_pnl += day_pnl
            total_pnl += day_pnl
            total_capital += d.get("initial_capital", 0) or 0
            total_equity += d.get("equity", 0) or 0
            total_trades += d.get("trades_today", 0) or 0
        else:
            initial_capital = defaults["initial_capital"]
            selected_ticker = str(defaults["ticker"]).strip().upper()
            account_pos = account_positions.get(selected_ticker) if use_external_account_positions else None
            account_shares = int((account_pos or {}).get("quantity", 0) or 0)
            account_pnl = float((account_pos or {}).get("unrealized_pnl", 0.0) or 0.0)
            account_pnl_pct = float((account_pos or {}).get("unrealized_pnl_pct", 0.0) or 0.0)
            account_price = float((account_pos or {}).get("current_price", 0.0) or 0.0)
            account_entry_price = float((account_pos or {}).get("avg_entry_price", 0.0) or 0.0)
            if account_shares > 0 and account_price > 0.0 and account_entry_price > 0.0:
                if account_pnl == 0.0:
                    account_pnl = round((account_price - account_entry_price) * account_shares, 6)
                if account_pnl_pct == 0.0:
                    account_pnl_pct = round(((account_price - account_entry_price) / account_entry_price) * 100.0, 6)
            ai_range = ai_ranges.get(selected_ticker, {})
            chart_snapshot = _chart_snapshot_for_ticker(selected_ticker, refresh=True)
            if effective_mode == "paper":
                hold_source = "PaperBroker" if account_pos or account_shares > 0 else "PaperBroker / 离线"
            elif effective_mode == "sandbox":
                if live_account and live_account_mode == "sandbox":
                    hold_source = "LongBridge sandbox"
                    if not account_pos:
                        hold_source += " / 账户无该标的"
                else:
                    hold_source = "LongBridge sandbox / not connected"
            elif effective_mode == "live":
                if live_account and live_account_mode == "live":
                    hold_source = "LongBridge prod"
                    if not account_pos:
                        hold_source += " / 账户无该标的"
                else:
                    hold_source = "LongBridge prod / not connected"
            else:
                hold_source = "离线"
            cards.append({
                "name": defaults["ticker"], "desc": t["desc"],
                "ticker": selected_ticker,
                "online": False,
                "configured": True,
                "price": account_price,
                "price_change": 0,
                "day_high": account_price,
                "day_low": account_price,
                "bid": account_price,
                "ask": account_price,
                "vol_display": "0",
                "support": defaults["support"], "resistance": defaults["resistance"], "spread_pct": 0,
                "range_ready": False, "range_source": "offline",
                "pos_pct": 50,
                "sparkline": _build_sparkline([], 0),
                "signal": "OFFLINE",
                "signal_cn": _signal_cn("OFFLINE"),
                "signal_reason": "引擎离线，仓位仍按真实账户显示" if account_pos else "暂无",
                "ai_range_low": ai_range.get("range_low"),
                "ai_range_high": ai_range.get("range_high"),
                "ai_suggested_range": ai_range.get("suggested_range") or "暂无",
                "shares": account_shares,
                "avg_entry_price": account_pos.get("avg_entry_price") if account_pos else account_entry_price,
                "current_price_for_position": account_price,
                "market_value": float((account_pos or {}).get("market_value", 0.0) or (account_shares * account_price)),
                "initial_capital": initial_capital, "cash": initial_capital,
                "pnl": account_pnl,
                "pnl_pct": account_pnl_pct,
                "hold_source": hold_source,
                "reduce_only": defaults.get("reduce_only", False), "equity": initial_capital,
                "trades": 0, "win_rate": 0, "wins": 0, "losses": 0,
                "best_trade": 0, "worst_trade": 0, "avg_pnl": 0,
                "halted": False,
                "trade_in_progress": False,
                "chart_prices": chart_snapshot.get("prices", []),
                "chart_trades": chart_snapshot.get("trades", []),
            })
            total_capital += initial_capital
            total_equity += initial_capital

    paper_account_summary = _paper_account_summary_from_cards(cards)
    if live_account and live_account_mode in {"live", "sandbox"} and not live_account.get("account_error"):
        account_summary = live_account
        selected_positions_count = _selected_stock_positions_count(live_account, selected_tickers)
        display_positions = list(live_account.get("positions") or [])
        if live_account_mode == "sandbox":
            display_positions_title = "LongBridge sandbox 持仓"
            display_positions_hint = "显示 sandbox 账户全部持仓"
        else:
            display_positions_title = "真实仓位"
            display_positions_hint = "显示真实账户全部持仓"
    else:
        account_summary = paper_account_summary
        selected_positions_count = int(paper_account_summary.get("positions_count", 0) or 0)
        display_positions = list(paper_account_summary.get("positions") or [])
        if effective_mode == "paper":
            display_positions_title = "PaperBroker 虚拟持仓"
            display_positions_hint = "显示当前 paper 运行中的虚拟仓位"
        elif effective_mode == "sandbox":
            display_positions_title = "LongBridge sandbox 持仓"
            display_positions_hint = "sandbox 账户未连接时显示 unavailable"
            account_summary = None
            display_positions = []
            selected_positions_count = 0
        elif effective_mode == "live":
            display_positions_title = "LongBridge 真实持仓"
            display_positions_hint = "live 账户未连接时显示 unavailable"
            account_summary = None
            display_positions = []
            selected_positions_count = 0
        else:
            display_positions_title = "账户持仓"
            display_positions_hint = "显示当前运行中的仓位"

    if effective_mode in {"live", "sandbox"} and live_account and live_account_mode in {"live", "sandbox"} and not live_account.get("account_error"):
        total_pnl = sum(float((pos or {}).get("unrealized_pnl", 0.0) or 0.0) for pos in (live_account.get("positions") or []))
        total_capital = float(live_account.get("cash") or 0.0)
        total_equity = float(live_account.get("equity") or 0.0)
        footer_buying_power = float(live_account.get("buying_power") or 0.0)
        account_labels = {
            "footer_capital": "账户现金",
            "footer_equity": "账户权益",
            "footer_buying_power": "可买额度",
        }
    elif live_account and live_account.get("account_error"):
        total_capital = None
        total_equity = None
        footer_buying_power = None
        account_labels = {
            "footer_capital": "账户现金",
            "footer_equity": "账户权益",
            "footer_buying_power": "可买额度",
        }
    else:
        footer_buying_power = None
        account_labels = {
            "footer_capital": "总本金",
            "footer_equity": "总权益",
            "footer_buying_power": "可买额度",
        }

    active_symbols = " / ".join(
        card["name"].split("·", 1)[-1].strip() if "·" in card["name"] else str(card["name"]).strip()
        for card in cards
        if card.get("configured", True)
    ) or "N/A"
    nearest_buy_trigger_name, nearest_buy_trigger = _nearest_trigger(
        cards,
        "buy",
        new_entries_allowed=trade_audit["new_entries_allowed"],
    )
    nearest_sell_trigger_name, nearest_sell_trigger = _nearest_trigger(cards, "sell")

    highlight_names = []
    if nearest_buy_trigger_name and nearest_buy_trigger_name not in {"暂无", "暂无可买", "暂停开仓"}:
        highlight_names.append(nearest_buy_trigger_name)
    if (
        nearest_sell_trigger_name
        and nearest_sell_trigger_name not in {"暂无", "暂无可买", "暂停开仓"}
        and nearest_sell_trigger_name not in highlight_names
    ):
        highlight_names.append(nearest_sell_trigger_name)

    featured_cards = []
    featured_set = set()
    for name in highlight_names:
        for card in cards:
            if card["name"] != name or name in featured_set:
                continue
            labels = []
            classes = []
            if name == nearest_buy_trigger_name:
                labels.append("最接近买点")
                classes.append("featured-buy")
            if name == nearest_sell_trigger_name:
                labels.append("最接近卖点")
                classes.append("featured-sell")
            if len(labels) > 1:
                labels = ["买卖都接近"]
                classes = ["featured-dual"]
            card["featured_label"] = " / ".join(labels)
            card["featured_class"] = " ".join(classes)
            featured_cards.append(card)
            featured_set.add(name)
            break

    for card in cards:
        if card["name"] in featured_set:
            continue
        card["featured_label"] = ""
        card["featured_class"] = ""

    other_cards = [card for card in cards if card["name"] not in featured_set]
    system_status = _system_status_snapshot(
        runtime_config=dashboard_config,
        live_account=live_account,
        trade_audit=trade_audit,
        active_orders=active_orders_summary,
        update_time=update_time,
        mode_override=effective_mode,
    )
    active_order_summary = _dashboard_order_status_summary(active_orders_summary)
    account_equity_value = total_equity if total_equity is not None else (account_summary.get("equity") if isinstance(account_summary, dict) else None)
    if footer_buying_power is not None:
        available_cash_display = float(footer_buying_power)
    elif isinstance(account_summary, dict):
        available_cash_display = account_summary.get("cash")
    else:
        available_cash_display = None
    if isinstance(account_summary, dict):
        account_positions_list = list(account_summary.get("positions") or [])
    else:
        account_positions_list = []
    risk_summary = _dashboard_risk_summary(account_summary if isinstance(account_summary, dict) else None, account_positions_list)
    risk_summary.setdefault("risk_label", "未知")
    risk_summary.setdefault("risk_level", "UNKNOWN")
    timeline_items = _dashboard_timeline_items(
        trade_audit=trade_audit,
        system_status=system_status,
        selection_sync=selection_sync,
        ai_runtime=ai_runtime,
        research_digest=research_digest,
        startup_guard=startup_guard,
    )
    main_chart_card = featured_cards[0] if featured_cards else (cards[0] if cards else None)
    mode_display = (
        "PAPER · 虚拟盘" if system_status["mode_key"] == "paper"
        else "SANDBOX · 沙盒" if system_status["mode_key"] == "sandbox"
        else "PROD · 实盘账户" if system_status["mode_key"] == "live"
        else "UNKNOWN"
    )
    if system_status["mode_key"] == "live":
        mode_class = "mode-live"
    elif system_status["mode_key"] == "sandbox":
        mode_class = "mode-sandbox"
    else:
        mode_class = "mode-paper"
    startup_guard_level = str(getattr(startup_guard, "level", startup_guard.get("level") if isinstance(startup_guard, dict) else "") or "").strip().lower()
    startup_guard_label = str(getattr(startup_guard, "label", startup_guard.get("label") if isinstance(startup_guard, dict) else "") or "")
    startup_guard_detail = str(getattr(startup_guard, "detail", startup_guard.get("detail") if isinstance(startup_guard, dict) else "") or "")
    runtime_state_value = "RUNNING"
    runtime_state_class = "status-live"
    runtime_state_detail = "系统正在同步读取只读状态"
    if startup_guard_level in {"blocked", "red"}:
        runtime_state_value = "BLOCKED"
        runtime_state_class = "status-offline"
        runtime_state_detail = startup_guard_label or startup_guard_detail or "启动校验阻断"
    elif startup_guard_level in {"warn", "yellow"} or system_status.get("broker_connection") in {"not connected", "no data"}:
        runtime_state_value = "DEGRADED"
        runtime_state_class = "status-warn"
        runtime_state_detail = startup_guard_detail or system_status.get("broker_connection") or "状态降级"
    if system_status.get("broker_connected") and system_status.get("market_open"):
        market_pill_class = "status-live"
    elif system_status.get("broker_connected"):
        market_pill_class = "status-warn"
    else:
        market_pill_class = "status-offline"
    shadow_status = _shadow_status_payload()
    candidate_validation = _candidate_validation_payload()
    candidate_model_evaluation = _candidate_model_evaluation_payload()
    shadow_state = str(shadow_status.get("state") or "STALE").upper()
    shadow_status_class = "status-live" if shadow_state == "SAFE" else "status-warn" if shadow_state == "STALE" else "status-offline"
    candidate_state = str(candidate_validation.get("state") or "STALE").upper()
    candidate_status_class = "status-live" if candidate_state == "SAFE" else "status-warn" if candidate_state == "STALE" else "status-offline"
    candidate_model_state = str(
        candidate_model_evaluation.get("approval_status")
        or candidate_model_evaluation.get("status_label")
        or candidate_model_evaluation.get("state")
        or "DRAFT"
    ).upper()
    candidate_model_status_class = "status-live" if candidate_model_state in {"APPROVED", "ACTIVE", "REVIEW_REQUIRED"} else "status-warn" if candidate_model_state in {"DRAFT", "BACKTESTED", "WALK_FORWARD_VALIDATED"} else "status-offline"
    top_modes = [
        str(mode or "").strip().lower()
        for mode in _load_top_modes()
        if str(mode or "").strip().lower() not in {"", "disabled"}
    ]
    mode_consistency = {
        "dashboard_mode": system_status.get("mode_key") or effective_mode or "paper",
        "top_modes": top_modes,
        "mixed": bool(top_modes and str(system_status.get("mode_key") or effective_mode or "paper").strip().lower() not in set(top_modes)),
    }
    mode_consistency["label"] = (
        "TOP 引擎模式不一致"
        if mode_consistency["mixed"]
        else "TOP 引擎模式一致"
        if top_modes
        else "无启用 TOP 引擎"
    )
    mode_consistency["detail"] = (
        f"Dashboard: {mode_consistency['dashboard_mode']} · TOP: {', '.join(top_modes)}"
        if top_modes
        else "当前未生成 TOP 配置"
    )

    return render_template_string(HTML,
        cards=cards,
        featured_cards=featured_cards,
        other_cards=other_cards,
        account_labels=account_labels,
        footer_buying_power=footer_buying_power,
        live_account=live_account,
        account_summary=account_summary,
        selected_positions_count=selected_positions_count,
        display_positions=display_positions,
        display_positions_title=display_positions_title,
        display_positions_hint=display_positions_hint,
        ai_selection=ai_selection,
        ai_selection_price_band=ai_selection_price_band,
        ai_universe_filter=ai_universe_filter,
        research_digest=research_digest,
        research_report=_candidate_research_report_payload(),
        ai_runtime=ai_runtime,
        selection_sync=selection_sync,
        startup_guard=startup_guard,
        system_status=system_status,
        mode_consistency=mode_consistency,
        active_orders_summary=active_orders_summary,
        runtime_settings={
            "min_price": float(resolve_price_band(runtime_settings or ai_selection.get("settings", {}))[0]),
            "max_price": float(resolve_price_band(runtime_settings or ai_selection.get("settings", {}))[1]),
            "auto_refresh_minutes": int(runtime_settings.get("auto_refresh_minutes", ai_selection.get("settings", {}).get("auto_refresh_minutes", 5)) or 5),
        },
        active_symbols=active_symbols,
        nearest_buy_trigger_name=nearest_buy_trigger_name,
        nearest_buy_trigger=nearest_buy_trigger,
        nearest_sell_trigger_name=nearest_sell_trigger_name,
        nearest_sell_trigger=nearest_sell_trigger,
        trade_audit=trade_audit,
        audit_scope=audit_scope,
        audit_day_label=audit_day_label,
        research_url="/research",
        order_states=order_states,
        total_pnl=round(total_pnl, 2),
        today_total_pnl=round(today_total_pnl, 2),
        total_capital=round(total_capital, 2) if total_capital is not None else None,
        total_equity=round(total_equity, 2) if total_equity is not None else None,
        total_trades=total_trades,
        available_cash_display=available_cash_display,
        account_equity_value=account_equity_value,
        active_order_summary=active_order_summary,
        risk_summary=risk_summary,
        timeline_items=timeline_items,
        main_chart_card=main_chart_card,
        mode_display=mode_display,
        mode_class=mode_class,
        runtime_state_value=runtime_state_value,
        runtime_state_class=runtime_state_class,
        runtime_state_detail=runtime_state_detail,
        market_pill_class=market_pill_class,
        shadow_status=shadow_status,
        shadow_status_class=shadow_status_class,
        candidate_validation=candidate_validation,
        candidate_status_class=candidate_status_class,
        candidate_model_evaluation=candidate_model_evaluation,
        candidate_model_status_class=candidate_model_status_class,
        research_status=_research_status_payload(),
        # ---- Aggregated trade statistics ----
        trade_stats=_aggregate_trade_stats(cards),
        equity_curve_bars=_build_equity_curve_bars(cards),
        update_time=update_time,
    )


def _aggregate_trade_stats(cards: list[dict]) -> dict:
    """Aggregate trade statistics across all engine cards."""
    total_trades_today = sum(c.get("trades", 0) or 0 for c in cards if c.get("online"))
    total_wins = sum(c.get("wins", 0) or 0 for c in cards if c.get("online"))
    total_losses = sum(c.get("losses", 0) or 0 for c in cards if c.get("online"))
    all_win_rates = [
        c.get("win_rate", 0) or 0
        for c in cards
        if c.get("online") and (c.get("trades", 0) or 0) > 0
    ]
    avg_win_rate = round(
        sum(all_win_rates) / len(all_win_rates), 1
    ) if all_win_rates else 0.0

    # Average win / loss amounts: use best_trade and worst_trade as proxies
    # when detailed breakdown is unavailable
    wins_amounts = [
        c.get("best_trade", 0) or 0
        for c in cards if c.get("online") and (c.get("wins", 0) or 0) > 0
    ]
    losses_amounts = [
        abs(c.get("worst_trade", 0) or 0)
        for c in cards if c.get("online") and (c.get("losses", 0) or 0) > 0
    ]
    days_pnl = [c.get("pnl", 0) or 0 for c in cards if c.get("online")]
    total_pnl_today = round(sum(days_pnl), 2)

    return {
        "trades_today": total_trades_today,
        "win_rate": avg_win_rate,
        "avg_win": round(
            sum(wins_amounts) / len(wins_amounts), 2
        ) if wins_amounts else 0.0,
        "avg_loss": round(
            sum(losses_amounts) / len(losses_amounts), 2
        ) if losses_amounts else 0.0,
        "total_pnl": total_pnl_today,
    }


def _build_equity_curve_bars(cards: list[dict]) -> str:
    """Build a simple HTML bar-chart snippet from engine equity values.

    Returns an inline SVG bar chart for the equity curve.
    """
    equities = [
        float(c.get("equity", 0) or 0)
        for c in cards
        if c.get("online") and (c.get("equity", 0) or 0) > 0
    ]
    if not equities:
        return ""

    max_eq = max(equities)
    min_eq = min(equities)
    range_eq = max(max_eq - min_eq, 1.0)

    bar_w = 36
    gap = 8
    total_w = len(equities) * (bar_w + gap) - gap
    svg_h = 80

    bars: list[str] = []
    labels: list[str] = []
    for i, eq in enumerate(equities):
        h = max(6.0, (eq - min_eq) / range_eq * (svg_h - 16))
        x = i * (bar_w + gap) + 2
        y = svg_h - 8 - h
        color = "#34d399" if (i == 0 or eq >= (equities[i - 1] if i > 0 else eq)) else "#fb7185"
        bars.append(
            '<rect x="' + str(x) + '" y="' + f"{y:.1f}"
            + '" width="' + str(bar_w - 4) + '" height="' + f"{h:.1f}"
            + '" rx="3" fill="' + color + '" opacity="0.85"/>'
        )
        labels.append(
            '<text x="' + str(x + (bar_w - 4) / 2) + '" y="' + str(svg_h + 10)
            + '" text-anchor="middle" fill="#8c97ab" font-size="9">'
            + 'TOP' + str(i + 1) + '</text>'
        )

    svg = (
        '<svg width="' + str(total_w) + '" height="' + str(svg_h + 18)
        + '" viewBox="0 0 ' + str(total_w) + ' ' + str(svg_h + 18)
        + '" style="display:block;margin:6px 0 0">'
        + "".join(bars)
        + "".join(labels)
        + "</svg>"
    )
    return svg


def _run_ai_selector_now() -> None:
    project_dir = str(PROJECT_DIR)
    env = os.environ.copy()
    settings = load_runtime_settings()
    env.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    env.setdefault("AI_SELECTOR_MAX_SYMBOLS", "50")
    env.setdefault("AI_SELECTOR_AUTO_REFRESH_MINUTES", str(settings.get("auto_refresh_minutes", 5)))
    python_bin = PROJECT_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(os.environ.get("PYTHON", "")) if os.environ.get("PYTHON") else Path("python3")

    subprocess.run(
        [str(python_bin), "scripts/run_ai_selector.py"],
        cwd=project_dir,
        env=env,
        check=False,
    )


@app.route("/ai-selector-settings", methods=["POST"])
def update_ai_selector_settings():
    raw_auto_refresh_minutes = str(request.form.get("auto_refresh_minutes", "")).strip()
    action = str(request.form.get("action", "save")).strip().lower()
    settings = load_runtime_settings()
    try:
        auto_refresh_minutes = int(raw_auto_refresh_minutes)
    except (TypeError, ValueError):
        auto_refresh_minutes = int(settings.get("auto_refresh_minutes", 5) or 5)
    auto_refresh_minutes = max(1, min(1440, auto_refresh_minutes))
    settings.pop("min_price", None)
    settings.pop("max_price", None)
    settings.pop("price_band", None)
    settings["auto_refresh_minutes"] = auto_refresh_minutes
    save_runtime_settings(settings)
    if action == "rerun":
        _run_ai_selector_now()
    return redirect("/")


@app.route("/daily-report")
def daily_report():
    payload, status = daily_report_module.latest_daily_report_response()
    return jsonify(payload), status


@app.route("/research")
@app.route("/research/")
def research_report_home():
    index_path = build_research_site(project_dir=PROJECT_DIR)
    if not index_path.exists():
        return ("research report unavailable", 404)
    return send_file(index_path)


def start_combined(port=8090):
    """Start combined dashboard as a foreground Flask server."""
    import socket
    from werkzeug.serving import make_server
    if has_longbridge_runtime_credentials():
        daily_report_module.ensure_daily_report_scheduler()
    else:
        print(
            "Daily report scheduler disabled: missing LongBridge runtime credentials",
            flush=True,
        )
    allowed, metadata = _ensure_single_instance(port)
    print(
        "Combined dashboard startup:",
        f"port={metadata.get('port')}",
        f"pid={metadata.get('pid')}",
        f"pid_file={metadata.get('pid_file')}",
        f"previous_process_was_cleaned={metadata.get('previous_process_was_cleaned')}",
        f"reason={metadata.get('reason')}",
        flush=True,
    )
    if not allowed:
        if metadata.get("reason") == "port_occupied_by_non_project_process":
            print("Port 8090 occupied by non-project process", flush=True)
        elif metadata.get("reason") == "project_combined_already_running":
            print("Combined dashboard already running; skipping duplicate start", flush=True)
        elif metadata.get("reason") == "project_pid_file_still_active":
            print("Combined dashboard pid file points to active project process; skipping duplicate start", flush=True)
        raise SystemExit(0 if metadata.get("reason") != "port_occupied_by_non_project_process" else 1)

    def _cleanup_on_exit(_signum, _frame):
        _shutdown_single_instance()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _cleanup_on_exit)
    signal.signal(signal.SIGINT, _cleanup_on_exit)
    print(
        f"Combined dashboard starting on port: {port}",
        f"pid={os.getpid()}",
        f"pid_file={_combined_pid_file_path()}",
        flush=True,
    )

    # Set SO_REUSEADDR so the port can be rebound immediately after restart
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(None)
    try:
        sock.bind(("0.0.0.0", port))
        sock.listen(128)
        server = make_server("0.0.0.0", port, app, threaded=True, fd=sock.fileno())
        sock.detach()
        print(
            f"Combined dashboard started successfully on port {port}",
            f"pid={os.getpid()}",
            f"pid_file={_combined_pid_file_path()}",
            flush=True,
        )
        server.serve_forever()
    except OSError as exc:
        print(
            f"Combined dashboard failed to start: port={port} pid={os.getpid()} pid_file={_combined_pid_file_path()} reason={exc}",
            flush=True,
        )
        _shutdown_single_instance()
        raise
    finally:
        _shutdown_single_instance()
