"""Combined dashboard aggregating the selected TOP3 trading engines."""
import atexit
import inspect
import json, os, signal, subprocess, threading, urllib.request
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, redirect, render_template_string, request, send_file
import yaml
import re
from zoneinfo import ZoneInfo

from src.ai_selector.config import load_runtime_config
from src.ai_selector.settings import load_runtime_settings, save_runtime_settings, resolve_price_band
from src.ai_selector.selection_state import current_top_config_symbols, has_live_top_configs, load_selection_state, verify_selection_state
from src.config.runtime_values import get_runtime_env, has_longbridge_runtime_credentials
from src.reports import daily_report as daily_report_module
from src.reports.trade_audit import latest_trade_activity_day, latest_trade_log_day, load_trade_records, summarize_trade_log
from src.research_report.site import build_research_site

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))
TRADING_FLAGS_PATH = STATE_DIR / "trading_flags.json"
RUNTIME_DIR = Path(os.environ.get("SOXS_RUNTIME_DIR", "").strip() or (PROJECT_DIR / "runtime"))
COMBINED_PID_FILE = RUNTIME_DIR / "combined.pid"

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


def _fetch_live_account_summary():
    """Read live buying power from LongBridge if credentials are present."""
    global _LIVE_ACCOUNT_CACHE, _LIVE_ACCOUNT_CACHE_AT
    now = time.time()
    with _LIVE_ACCOUNT_LOCK:
        if _LIVE_ACCOUNT_CACHE and (now - _LIVE_ACCOUNT_CACHE_AT) < _LIVE_ACCOUNT_CACHE_TTL:
            return _LIVE_ACCOUNT_CACHE
        if not _has_live_account_env():
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
        summary = {
            "cash": float(getattr(account, "cash", 0.0) or 0.0),
            "equity": float(getattr(account, "equity", 0.0) or 0.0),
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0),
            "positions_count": len(positions or []),
            "positions": position_rows,
            "mode": "live",
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
    positions_count = sum(1 for card in cards if int(card.get("shares", 0) or 0) > 0)
    return {
        "cash": cash,
        "equity": equity,
        "buying_power": cash,
        "positions_count": positions_count,
        "positions": [],
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
    path = PROJECT_DIR / "reports" / "ai_selection_latest.json"
    if not path.exists():
        return {
            "timestamp": None,
            "report": [],
            "top3": [],
            "top10": [],
            "settings": {},
            "refined_top3": [],
            "refined_top10": [],
            "refined_report": [],
            "refinement_status": None,
            "refinement_selection_stage": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {
                "timestamp": None,
                "report": [],
                "top3": [],
                "top10": [],
                "settings": {},
                "refined_top3": [],
                "refined_top10": [],
                "refined_report": [],
                "refinement_status": None,
                "refinement_selection_stage": None,
            }
        rows = data.get("report") if isinstance(data.get("report"), list) else []
        return {
            "timestamp": data.get("timestamp"),
            "report": rows,
            "top3": data.get("top3") if isinstance(data.get("top3"), list) else [],
            "top10": data.get("top10") if isinstance(data.get("top10"), list) else [],
            "settings": data.get("settings") if isinstance(data.get("settings"), dict) else {},
            "refined_top3": data.get("refined_top3") if isinstance(data.get("refined_top3"), list) else [],
            "refined_top10": data.get("refined_top10") if isinstance(data.get("refined_top10"), list) else [],
            "refined_report": data.get("refined_report") if isinstance(data.get("refined_report"), list) else [],
            "refinement_status": data.get("refinement_status"),
            "refinement_selection_stage": data.get("refinement_selection_stage"),
        }
    except Exception:
        return {
            "timestamp": None,
            "report": [],
            "top3": [],
            "top10": [],
            "settings": {},
            "refined_top3": [],
            "refined_top10": [],
            "refined_report": [],
            "refinement_status": None,
            "refinement_selection_stage": None,
        }


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
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
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
    required_date = _current_et_date()
    ok, reason, state = verify_selection_state(required_et_date=required_date)
    state = state or load_selection_state() or {}
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
    mismatch_reason = ""
    if reason == "top_config_symbols_mismatch":
        mismatch_reason = "top_config_symbols_do_not_match_selection_state"
    elif reason.startswith("selection_state_date_mismatch"):
        mismatch_reason = "selection_state_date_mismatch"
    elif reason == "selection_state_missing":
        mismatch_reason = "selection_state_missing"
    suggestion = "请重新运行 AI Selector 或重新写入 TOP 配置"
    label = "已对齐"
    level = "green"
    detail = f"当天配置已对齐（美东 {required_date}）"
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
            "mismatch_reason": "",
            "suggestion": suggestion,
        }
    if reason == "selection_state_missing":
        label = "未校验"
        level = "yellow"
        detail = "还没有当天选股校验记录，启动前会先重选并校验。"
    elif reason.startswith("selection_state_date_mismatch"):
        label = "不是今天"
        level = "yellow"
        detail = (
            f"当前记录日期是美东 {state_date or '未知'}，不是今天 {required_date}。"
            f" selection_state tickers: {selection_state_symbols or []} · current TOP config tickers: {current_top_config_symbols_list or []}。"
        )
    elif reason == "top_config_symbols_mismatch":
        label = "配置不一致"
        level = "red"
        detail = (
            "TOP1-3 配置和最近一次选股结果不一致，交易启动会被拦下。"
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
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        mode = str(data.get("mode", "paper")).strip().lower() or "paper"
        modes.append(mode)
    return modes


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
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI区间交易总览</title>
<style>
    :root{
        --bg:#05070d;
        --bg2:#0a1020;
        --panel:rgba(11,17,31,.82);
        --panel-strong:rgba(15,22,40,.94);
        --line:rgba(255,255,255,.08);
        --text:#e6edf8;
        --muted:#8c97ab;
        --accent:#86efac;
        --accent2:#7dd3fc;
        --warn:#fbbf24;
        --down:#fb7185;
        --up:#34d399;
        --shadow:0 24px 80px rgba(0,0,0,.45);
        --radius-panel:22px;
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
            radial-gradient(circle at top left, rgba(125,211,252,.14), transparent 28%),
            radial-gradient(circle at top right, rgba(52,211,153,.12), transparent 24%),
            linear-gradient(180deg, #04060b 0%, #060913 44%, #05070d 100%);
        padding:18px 18px 30px;
        overflow-x:hidden;
        overflow-y:auto;
    }
    .page{
        max-width:1680px;
        margin:0 auto;
        min-height:calc(100vh - 44px);
        display:flex;
        flex-direction:column;
        gap:20px;
    }
    .topbar{
        display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
        padding:18px 20px;border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(16,24,44,.92), rgba(9,13,24,.86));
        border-radius:var(--radius-panel);box-shadow:var(--shadow);backdrop-filter:blur(14px);
        position:sticky;top:10px;z-index:5;
    }
    .brand{display:flex;flex-direction:column;gap:10px}
    .brand h1{font-size:32px;line-height:1.04;letter-spacing:.015em;font-weight:780}
    .brand p{color:var(--muted);font-size:14px;line-height:1.5}
    .headline-stats{
        display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;min-width:1040px
    }
    .headline-stat{
        padding:16px 18px;border-radius:18px;
        background:
            linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.02)),
            radial-gradient(circle at top right, rgba(125,211,252,.09), transparent 42%);
        border:1px solid rgba(255,255,255,.08);
        box-shadow:inset 0 1px 0 rgba(255,255,255,.04)
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
    .overview-layout{
        display:grid;grid-template-columns:1fr;gap:16px;
    }
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
        .overview-layout,.control-grid,.two-column,.cards{grid-template-columns:1fr}
        .account-grid,.audit-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
        .selector-head,.selector-row,.position-item{grid-template-columns:repeat(5,minmax(0,1fr))}
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
        .account-grid,.audit-grid,.cards,.grid-quote,.pnl-grid,.summary,.overview-layout,.control-grid,.two-column,.audit-strip{grid-template-columns:1fr}
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
            <h1>AI区间交易总览</h1>
            <p>TOP1 到 TOP3 三路联动监控，当前为 4-30 美元低价杠杆 / 反向工具池模式，每 5 秒自动刷新。</p>
            <div class="headline-stats">
                <div class="headline-stat">
                    <span class="label">今日总收益</span>
                    <span class="value {{ 'green' if today_total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(today_total_pnl) }}</span>
                    <span class="sub">按 3 路策略今日盈亏汇总</span>
                </div>
                <div class="headline-stat">
                    <span class="label">账户浮盈亏</span>
                    <span class="value {{ 'green' if total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(total_pnl) }}</span>
                    <span class="sub">总成交 {{ total_trades }} 笔</span>
                </div>
                <div class="headline-stat">
                    <span class="label">{{ account_labels.footer_capital }}</span>
                    <span class="value">{% if total_capital is not none %}${{ "%.2f"|format(total_capital) }}{% else %}暂无{% endif %}</span>
                    <span class="sub">{% if total_equity is not none %}{{ account_labels.footer_equity }}：${{ "%.2f"|format(total_equity) }}{% else %}{{ account_labels.footer_equity }}：暂无{% endif %}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">最近买点</span>
                    <span class="value">{{ nearest_buy_trigger_name }}</span>
                    <span class="sub">{{ nearest_buy_trigger }}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">最近卖点</span>
                    <span class="value">{{ nearest_sell_trigger_name }}</span>
                    <span class="sub">{{ nearest_sell_trigger }}</span>
                </div>
            </div>
        </div>
        <div class="status-row">
            <span class="pill live">实时监控</span>
            <span class="pill">更新于 {{ update_time }}</span>
            <a class="pill research" href="{{ research_url }}" target="_blank" rel="noopener">只读研究简报</a>
            <span class="pill {{ startup_guard.level }}">
                {{ startup_guard.label }}
            </span>
            <span class="pill {% if live_account and live_account.mode == 'live' %}live{% else %}warn{% endif %}">
                {% if live_account and live_account.mode == 'live' %}实盘账户{% elif live_account and live_account.account_error %}实盘账户异常{% else %}虚拟盘{% endif %}
            </span>
            {% if live_account and live_account.data_stale %}
                {% if live_account.account_error %}
            <span class="pill warn">账户拉取失败 · {{ live_account.stale_reason }}</span>
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
                                <h2>真实仓位</h2>
                                <span class="hint">显示真实账户全部持仓</span>
                            </div>
                            {% if live_account and live_account.positions %}
                            <div class="position-list">
                                {% for pos in live_account.positions %}
                                <div class="position-item">
                                    <div class="position-cell">
                                        <span class="position-ticker">{{ pos.ticker }}</span>
                                        <span class="val">{{ pos.quantity }} 股</span>
                                    </div>
                                    <div class="position-cell"><span class="label">成本</span><span class="val">${{ "%.2f"|format(pos.avg_entry_price) }}</span></div>
                                    <div class="position-cell"><span class="label">现价</span><span class="val">${{ "%.2f"|format(pos.current_price) }}</span></div>
                                    <div class="position-cell"><span class="label">市值</span><span class="val">${{ "%.2f"|format(pos.market_value) }}</span></div>
                                    <div class="position-cell"><span class="label">浮盈亏</span><span class="val {{ 'green' if pos.unrealized_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(pos.unrealized_pnl) }}</span></div>
                                    <div class="position-cell"><span class="label">收益率</span><span class="val {{ 'green' if pos.unrealized_pnl_pct >= 0 else 'red' }}">{{ "%+.2f"|format(pos.unrealized_pnl_pct) }}%</span></div>
                                </div>
                                {% endfor %}
                            </div>
                            {% elif live_account and live_account.account_error %}
                            <div class="position-empty">账户持仓拉取失败：{{ live_account.stale_reason }}</div>
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
                                · 价格范围：${{ "%.2f"|format(ai_selection_price_band.min or 0) }} - ${{ "%.2f"|format(ai_selection_price_band.max or 0) }}{% if ai_selection_price_band.defaulted %} (default){% endif %}
                                · 自动刷新：{{ ai_selection.settings.auto_refresh_minutes or 0 }} 分钟
                                · 扫描数量：{{ ai_selection.settings.max_symbols or 0 }}
                                · 数据模式：{{ ai_selection.settings.data_mode or 'unknown' }}
                                · 启动阶段：{{ ai_selection.settings.selection_stage or 'unknown' }}
                                {% if ai_selection.settings.fallback_used %} · 已回退补齐{% endif %}
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
                    </div>
                    <form class="settings-form" method="post" action="/ai-selector-settings">
                        <div class="settings-field">
                            <label for="min_price">价格下限</label>
                            <input id="min_price" name="min_price" type="number" min="1" max="500" step="0.01" value="{{ runtime_settings.min_price }}">
                        </div>
                        <div class="settings-field">
                            <label for="max_price">价格上限</label>
                            <input id="max_price" name="max_price" type="number" min="1" max="500" step="0.01" value="{{ runtime_settings.max_price }}">
                        </div>
                        <div class="settings-field">
                            <label for="auto_refresh_minutes">自动刷新间隔（分钟）</label>
                            <input id="auto_refresh_minutes" name="auto_refresh_minutes" type="number" min="1" max="1440" step="1" value="{{ runtime_settings.auto_refresh_minutes }}">
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
                        AI 运行状态：<span class="{{ ai_runtime.level }}">{{ ai_runtime.label }}</span> · {{ ai_runtime.detail }} · 当前优先扫描低价高流动性杠杆 / 反向 ETF 与 4-30 美元股票。
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
                <span class="stat-value {{ 'green' if trade_stats.total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(trade_stats.total_pnl) }}</span>
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
                    <div class="quote-item"><span class="label">盈亏</span><span class="val {{ 'green' if card.pnl >= 0 else 'red' }}">${{ "%+.2f"|format(card.pnl) }}</span></div>
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
        window.setInterval(() => {
            charts.forEach((chartEl) => refreshChart(chartEl));
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
    status = _fetch_status(port) if port else None
    if status is not None and not isinstance(status, dict):
        status = {}
    online = bool(status)
    payload_mode = str((status or {}).get("mode") or mode or "unknown").strip().lower() or "unknown"
    signal = str((status or {}).get("last_signal") or (status or {}).get("signal") or ("OFFLINE" if not online else "HOLD")).strip().upper()
    price = (status or {}).get("price") if online else None
    halted = bool((status or {}).get("halted", False)) if online else False
    return {
        "rank": rank,
        "ticker": ticker if ticker else None,
        "port": port,
        "online": online,
        "mode": payload_mode,
        "signal": signal,
        "price": price,
        "halted": halted,
    }


def _fallback_runtime_flags() -> tuple[bool, bool]:
    try:
        runtime_config = load_runtime_config()
        return bool(runtime_config.allow_fallback_live_entries), bool(runtime_config.allow_fallback_paper_entries)
    except Exception:
        return False, False


def _api_status_payload() -> dict[str, object]:
    runtime_config = load_runtime_config()
    ai_selection = _load_ai_selection_report()
    if not isinstance(ai_selection, dict):
        ai_selection = {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}}
    selection_sync = _selection_sync_status()
    trade_audit = summarize_trade_log(PROJECT_DIR / "logs", day=None, mode=_desired_audit_mode())
    execution_mode = _resolve_dashboard_execution_mode(trade_audit)
    top_modes = _load_top_modes()
    top_tickers = list((selection_sync or {}).get("current_top_config_symbols") or current_top_config_symbols(limit=len(TICKERS)))
    top_engines = [
        _top_engine_status(
            item,
            rank=index + 1,
            ticker=top_tickers[index] if index < len(top_tickers) else None,
            mode=top_modes[index] if index < len(top_modes) else "unknown",
        )
        for index, item in enumerate(TICKERS)
    ]
    selection_date = (
        str((selection_sync or {}).get("state_date") or "").strip()
        or str((selection_sync or {}).get("required_date") or "").strip()
        or None
    )
    fallback_used = bool(ai_selection.get("fallback_used"))
    if not fallback_used:
        fallback_used = any(bool((item or {}).get("fallback_used")) for item in (ai_selection.get("top3") or []))
    live_guard_ok = not any(str(mode).strip().lower() == "live" for mode in top_modes) or bool((selection_sync or {}).get("ok"))
    return {
        "ok": True,
        "mode": execution_mode or "paper",
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
        "risk": {
            "live_guard_ok": live_guard_ok,
            "fallback_live_allowed": bool(runtime_config.allow_fallback_live_entries),
            "fallback_paper_allowed": bool(runtime_config.allow_fallback_paper_entries),
        },
        "dashboard": {
            "chart_api_available": True,
        },
        "ai_selection": {
            "price_band": _ai_selection_price_band(ai_selection),
        },
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


@app.route("/api/status")
def api_status():
    try:
        return jsonify(_api_status_payload()), 200
    except Exception as exc:
        fallback_live_allowed, fallback_paper_allowed = _fallback_runtime_flags()
        return jsonify(
            {
                "ok": False,
                "mode": "paper",
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
                },
                "ai_selection": {
                    "price_band": {"min": 4.0, "max": 50.0, "defaulted": True},
                },
                "error": str(exc),
            }
        ), 200


def _load_config_defaults(config_name):
    """Read display defaults from YAML so offline engines do not show $0 capital."""
    cfg_path = PROJECT_DIR / "configs" / config_name
    defaults = {
        "ticker": config_name.replace(".yaml", ""),
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
    runtime_settings = load_runtime_settings()
    live_account = _fetch_live_account_summary()
    account_positions = _position_lookup(live_account)
    ai_selection = _load_ai_selection_report()
    if not isinstance(ai_selection, dict):
        ai_selection = {"timestamp": None, "report": [], "top3": [], "top10": [], "settings": {}}
    _enrich_ticker_descriptions(ai_selection.get("top3", []))
    ai_ranges = _ai_range_lookup(ai_selection)
    ai_selection_price_band = _ai_selection_price_band(ai_selection)
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
    dashboard_status_by_symbol: dict[str, dict | None] = {}
    for item in TICKERS:
        defaults = _load_config_defaults(item["config"])
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
    selected_tickers: set[str] = set()

    for t in TICKERS:
        defaults = _load_config_defaults(t["config"])
        selected_tickers.add(str(defaults["ticker"]).strip().upper())
        d = dashboard_status_by_symbol.get(str(defaults["ticker"]).strip().upper()) or _fetch_status(t["port"])

        if d:
            supp = d.get("support", 0)
            res = d.get("resistance", 0)
            price = d.get("price", 0)
            pos_pct = ((price - supp) / (res - supp) * 100) if res and supp and res != supp else 50
            pos_pct = max(0, min(100, pos_pct))

            sparkline = _build_sparkline([price], price)
            selected_ticker = str(defaults["ticker"]).strip().upper()
            account_pos = account_positions.get(selected_ticker)
            account_shares = int((account_pos or {}).get("quantity", 0) or 0)
            account_pnl = float((account_pos or {}).get("unrealized_pnl", 0.0) or 0.0)
            account_pnl_pct = float((account_pos or {}).get("unrealized_pnl_pct", 0.0) or 0.0)
            hold_source = "真实账户" if account_pos else "引擎状态"
            ai_range = ai_ranges.get(selected_ticker, {})
            chart_snapshot = _chart_snapshot_for_ticker(selected_ticker, refresh=True)

            card = {
                "name": f"{t['name']} · {defaults['ticker']}" if t["name"].startswith("TOP") else t["name"],
                "ticker": selected_ticker,
                "desc": t["desc"],
                "online": True,
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
                "pnl": account_pnl if account_pos else float(d.get("daily_pnl", 0) or 0.0),
                "pnl_pct": account_pnl_pct if account_pos else 0.0,
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
            account_pos = account_positions.get(selected_ticker)
            account_shares = int((account_pos or {}).get("quantity", 0) or 0)
            account_pnl = float((account_pos or {}).get("unrealized_pnl", 0.0) or 0.0)
            account_pnl_pct = float((account_pos or {}).get("unrealized_pnl_pct", 0.0) or 0.0)
            account_price = float((account_pos or {}).get("current_price", 0.0) or 0.0)
            ai_range = ai_ranges.get(selected_ticker, {})
            chart_snapshot = _chart_snapshot_for_ticker(selected_ticker, refresh=True)
            cards.append({
                "name": defaults["ticker"], "desc": t["desc"],
                "ticker": selected_ticker,
                "online": False,
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
                "initial_capital": initial_capital, "cash": initial_capital,
                "pnl": account_pnl,
                "pnl_pct": account_pnl_pct,
                "hold_source": "真实账户" if account_pos else "离线",
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
    if live_account and live_account.get("mode") == "live":
        account_summary = live_account
        selected_positions_count = _selected_stock_positions_count(live_account, selected_tickers)
    else:
        account_summary = paper_account_summary
        selected_positions_count = int(paper_account_summary.get("positions_count", 0) or 0)

    if live_account and live_account.get("mode") == "live":
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

    return render_template_string(HTML,
        cards=cards,
        featured_cards=featured_cards,
        other_cards=other_cards,
        account_labels=account_labels,
        footer_buying_power=footer_buying_power,
        live_account=live_account,
        account_summary=account_summary,
        selected_positions_count=selected_positions_count,
        ai_selection=ai_selection,
        ai_selection_price_band=ai_selection_price_band,
        ai_runtime=ai_runtime,
        selection_sync=selection_sync,
        startup_guard=startup_guard,
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
        # ---- Aggregated trade statistics ----
        trade_stats=_aggregate_trade_stats(cards),
        equity_curve_bars=_build_equity_curve_bars(cards),
        update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
    min_price, max_price = resolve_price_band(settings)
    env.setdefault("AI_SELECTOR_MIN_PRICE", str(min_price))
    env.setdefault("AI_SELECTOR_MAX_PRICE", str(max_price))
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
    raw_min_price = str(request.form.get("min_price", "")).strip()
    raw_max_price = str(request.form.get("max_price", "")).strip()
    raw_auto_refresh_minutes = str(request.form.get("auto_refresh_minutes", "")).strip()
    action = str(request.form.get("action", "save")).strip().lower()
    settings = load_runtime_settings()
    try:
        min_price = float(raw_min_price)
    except (TypeError, ValueError):
        min_price = float(resolve_price_band(settings)[0])
    try:
        max_price = float(raw_max_price)
    except (TypeError, ValueError):
        max_price = float(resolve_price_band(settings)[1])
    try:
        auto_refresh_minutes = int(raw_auto_refresh_minutes)
    except (TypeError, ValueError):
        auto_refresh_minutes = int(settings.get("auto_refresh_minutes", 5) or 5)
    min_price = min(500.0, max(1.0, min_price))
    max_price = min(500.0, max(1.0, max_price))
    if min_price > max_price:
        min_price, max_price = max_price, min_price
    min_price = round(min_price, 2)
    auto_refresh_minutes = max(1, min(1440, auto_refresh_minutes))
    settings["max_price"] = round(max_price, 2)
    settings["min_price"] = min_price
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
