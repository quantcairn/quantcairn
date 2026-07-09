import logging
import yaml
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config.runtime_values import has_longbridge_runtime_credentials
from src.portfolio.risk_allocator import RiskAllocator

BASE = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
TOP_INITIAL_CAPITAL = 700.0
logger = logging.getLogger(__name__)


def _auto_refresh_minutes() -> int:
    raw = os.environ.get("AI_SELECTOR_AUTO_REFRESH_MINUTES", "5")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 5
    return max(1, value)


def _default_top_mode() -> str:
    mode = str(os.environ.get("AI_SELECTOR_TOP_MODE", "")).strip().lower()
    if mode in {"live", "paper"}:
        return mode

    has_live_creds = has_longbridge_runtime_credentials()
    return "live" if has_live_creds else "paper"


def _load_existing_mode(index: int, fallback: str) -> str:
    path = os.path.join(BASE, f"configs/TOP{index}.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        mode = str(cfg.get("mode", "")).strip().lower()
        if mode in {"live", "paper"}:
            return mode
    except Exception:
        pass
    return fallback


def _dynamic_min_profit_per_trade(estimated_price: float) -> float:
    """Scale the minimum tradable range for lower-priced stocks."""
    try:
        price = float(estimated_price)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return 0.8
    return round(max(0.30, min(0.9, price * 0.03)), 2)


def _global_reduce_only_enabled() -> bool:
    state_dir = os.environ.get("SOXS_STATE_DIR") or os.path.join(BASE, "state")
    flags_path = os.path.join(state_dir, "trading_flags.json")
    try:
        with open(flags_path, "r", encoding="utf-8") as f:
            flags = json.load(f)
        return bool((flags or {}).get("reduce_only_all", False))
    except Exception:
        return False


def _selection_date() -> str:
    try:
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _coalesce_float(*values: object, default: float = 0.0) -> float:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return float(default)


def _default_portfolio_config() -> dict:
    return {
        "enabled": False,
        "max_positions": 3,
        "max_total_exposure": 1.0,
        "max_total_risk": 0.05,
        "leveraged_etf_max_single_position": 0.15,
        "leveraged_etf_max_group_exposure": 0.50,
    }


def _load_yaml_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_portfolio_config() -> dict:
    """
    Load portfolio settings using the same priority expected by the project:
    config.local.yaml -> config.yaml -> default values.
    """
    portfolio = _default_portfolio_config()
    for path in (
        os.path.join(BASE, "config.yaml"),
        os.path.join(BASE, "config.local.yaml"),
    ):
        raw = _load_yaml_file(path)
        section = raw.get("portfolio")
        if isinstance(section, dict):
            portfolio.update(section)
    return portfolio


def _default_ai_selector_config() -> dict:
    return {
        "allow_fallback_paper_entries": False,
        "allow_fallback_live_entries": False,
        "fallback_paper_position_multiplier": 0.25,
    }


def _load_ai_selector_config() -> dict:
    """
    Load AI selector fallback settings using the same priority expected by the project:
    config.local.yaml -> config.yaml -> default values.
    """
    ai_selector = _default_ai_selector_config()
    for path in (
        os.path.join(BASE, "config.yaml"),
        os.path.join(BASE, "config.local.yaml"),
    ):
        raw = _load_yaml_file(path)
        section = raw.get("ai_selector")
        if isinstance(section, dict):
            ai_selector.update(section)
    return ai_selector

def write_top_configs(top_items):
    if not top_items:
        logger.warning("write_top_configs called with empty list — refusing to delete existing configs")
        return
    default_mode = _default_top_mode()
    global_reduce_only = _global_reduce_only_enabled()
    portfolio_cfg = _load_portfolio_config()
    ai_selector_cfg = _load_ai_selector_config()
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(list(top_items), TOP_INITIAL_CAPITAL)
    for i, item in enumerate(top_items, start=1):
        support = float(item["range_low"])
        resistance = float(item["range_high"])
        if support <= 0 or resistance <= support:
            continue
        initial_capital = TOP_INITIAL_CAPITAL
        estimated_price = (support + resistance) / 2
        fallback_size = max(1, int((initial_capital * 0.8) // estimated_price))
        requested_size = int(item.get("size") or fallback_size)
        size_per_trade = max(1, min(requested_size, fallback_size))
        mode = _load_existing_mode(i, default_mode)
        live_enabled = mode == "live"
        reduce_only = bool(item.get("reduce_only", False) or global_reduce_only)
        protected_position = bool(item.get("protected_position") or item.get("existing_position"))
        ai_score = _coalesce_float(item.get("ai_score"), item.get("score"), default=0.0)
        range_score = _coalesce_float(item.get("range_score"), default=0.0)
        final_score = _coalesce_float(item.get("final_score"), item.get("score"), ai_score, default=0.0)
        trade_filter_passed = bool(item.get("trade_filter_passed", True))
        fallback_used = bool(item.get("fallback_used", False))
        leveraged_etf = bool(item.get("leveraged_etf", False))
        composition_filter_passed = bool(item.get("composition_filter_passed", True))
        composition_reject_reason = str(item.get("composition_reject_reason") or "")
        final_rank = int(item.get("final_rank") or i)
        allocation = dict(allocations.get(str(item.get("ticker") or "").upper()) or {})

        cfg = {
            "ticker": item["ticker"],
            "mode": mode,
            "selection": {
                "source": "ai_selector",
                "selection_date": str(item.get("selection_date") or _selection_date()),
                "score": final_score,
                "ai_score": ai_score,
                "range_score": range_score,
                "final_score": final_score,
                "confidence": float(item.get("confidence") or 0.0),
                "trade_filter_passed": trade_filter_passed,
                "reject_reason": str(item.get("reject_reason") or ""),
                "fallback_used": fallback_used,
                "leveraged_etf": leveraged_etf,
                "composition_filter_passed": composition_filter_passed,
                "composition_reject_reason": composition_reject_reason,
                "final_rank": final_rank,
                "protected_position": protected_position,
                "reduce_only": reduce_only,
                "reason": str(
                    item.get("reason")
                    or item.get("selection_penalty_reason")
                    or "ai_selector"
                ),
            },
            "allocation": {
                "target_capital": float(allocation.get("capital") or 0.0),
                "target_shares": int(allocation.get("shares") or 0),
                "weight": float(allocation.get("weight") or 0.0),
                "atr_pct": float(allocation.get("atr_pct") or 0.05),
                "risk_pct": float(allocation.get("risk_pct") or 0.0),
                "reason": str(allocation.get("reason") or "no_allocation"),
            },
            "range": {
                "mode": "auto",
                "support_price": None,
                "resistance_price": None,
                "auto_lookback": 60,
                "auto_refresh_minutes": _auto_refresh_minutes(),
                "trend_filter": {
                    "enabled": True,
                    "ma_period": 20,
                    "min_trend_strength": 0.3,
                },
                "tolerance_pct": 1.0,
                "min_profit_per_trade": _dynamic_min_profit_per_trade(estimated_price),
                "min_range_width_pct": 0.6,
                "quick_stop_pct": 3.0,
            },
            "position": {
                "size_per_trade": size_per_trade,
                "max_position": 9999,
                "cool_down_seconds": 30,
                "initial_capital": initial_capital,
                "reduce_only": reduce_only,
            },
            "risk": {
                "stop_loss_pct": float(item.get("risk", {}).get("stop_loss_pct", 1.5)),
                "take_profit_pct": None,
                "daily_loss_limit": 60.0,
                "max_consecutive_losses": 3,
                "max_drawdown_pct": 8.0,
            },
            "portfolio": {
                "enabled": bool(portfolio_cfg.get("enabled", False)),
                "max_positions": int(portfolio_cfg.get("max_positions", 3)),
                "max_total_exposure": float(portfolio_cfg.get("max_total_exposure", 1.0)),
                "max_total_risk": float(portfolio_cfg.get("max_total_risk", 0.05)),
                "leveraged_etf_max_single_position": float(
                    portfolio_cfg.get("leveraged_etf_max_single_position", 0.15)
                ),
                "leveraged_etf_max_group_exposure": float(
                    portfolio_cfg.get("leveraged_etf_max_group_exposure", 0.50)
                ),
            },
            "ai_selector": {
                "allow_fallback_paper_entries": bool(
                    ai_selector_cfg.get("allow_fallback_paper_entries", False)
                ),
                "allow_fallback_live_entries": bool(
                    ai_selector_cfg.get("allow_fallback_live_entries", False)
                ),
                "fallback_paper_position_multiplier": float(
                    ai_selector_cfg.get("fallback_paper_position_multiplier", 0.25)
                ),
            },
            "trading_hours": {
                "timezone": "America/New_York",
                "start": "09:30",
                "end": "16:00",
                "early_close": "13:00",
            },
            "data": {
                "provider": "yfinance",
                "poll_interval_seconds": 10,
            },
            "notifications": {
                "console": True,
                "macos_notification": True,
                "webhook_url": None,
                "trade_summary_interval": 3,
            },
            "broker": {
                "longbridge": {
                    "app_key": "",
                    "app_secret": "",
                    "access_token": "",
                    "region": "cn",
                    "enabled": live_enabled,
                },
            },
        }
        path = os.path.join(BASE, f"configs/TOP{i}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

    max_slots = 5
    for i in range(len(top_items) + 1, max_slots + 1):
        path = os.path.join(BASE, f"configs/TOP{i}.yaml")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
