import logging
import yaml
import os
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config.runtime_values import has_longbridge_runtime_credentials
from src.portfolio.risk_allocator import RiskAllocator
from src.utils.market_calendar import market_session_context, required_selection_date

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
        now_et = datetime.now(ZoneInfo("America/New_York"))
        return required_selection_date(now_et)
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
        "entry_proximity_enabled": True,
        "entry_proximity_weight": 0.0,
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


def _entry_payload(item: dict) -> dict:
    entry = item.get("entry")
    if isinstance(entry, dict):
        payload = dict(entry)
    else:
        payload = {}
    payload.setdefault("entry_proximity_score", _coalesce_float(item.get("entry_proximity_score"), default=50.0))
    payload.setdefault("good_for_entry_now", bool(item.get("good_for_entry_now", False)))
    payload.setdefault("entry_quality", str(item.get("entry_quality") or "unknown"))
    payload.setdefault("entry_reason", str(item.get("entry_reason") or ""))
    payload.setdefault("range_position", item.get("range_position"))
    payload.setdefault("dist_to_support", item.get("dist_to_support"))
    payload.setdefault("dist_to_resistance", item.get("dist_to_resistance"))
    return payload


def _top_slot_count(top_items: list[dict], limit: int | None = None) -> int:
    configured = _load_configured_top_count()
    if limit is not None:
        try:
            return max(1, int(limit))
        except Exception:
            configured = max(1, configured)
    return max(3, configured, len(list(top_items or [])))


def _load_configured_top_count() -> int:
    try:
        from src.ai_selector.selection_state import configured_top_count

        return max(1, int(configured_top_count()))
    except Exception:
        return 3


def _slot_disabled_payload(
    *,
    slot: int,
    requested_top_n: int | None = None,
    selected_top_n: int | None = None,
    selection_run_id: str,
    selection_date: str,
    generated_at: str,
    disabled_reason: str,
    result_quality: str | None = None,
    research_admission: str | None = None,
    top_sync_status: str | None = None,
    top_sync_error: str | None = None,
    selection_bundle_manifest_path: str | None = None,
    selection_bundle_hash: str | None = None,
    selection_bundle_version: str | None = None,
) -> dict:
    return {
        "enabled": False,
        "ticker": None,
        "slot": int(slot),
        "reason": str(disabled_reason or "top_n_not_filled"),
        "selection_run_id": selection_run_id,
        "top_sync_run_id": selection_run_id,
        "selection_date": selection_date,
        "generated_at": generated_at,
        "result_quality": str(result_quality or ""),
        "research_admission": str(research_admission or ""),
        "top_sync_status": str(top_sync_status or "OK"),
        "top_sync_error": str(top_sync_error or ""),
        "selection_bundle_manifest_path": str(selection_bundle_manifest_path or ""),
        "selection_bundle_hash": str(selection_bundle_hash or ""),
        "selection_bundle_version": str(selection_bundle_version or ""),
        "requested_top_n": int(requested_top_n or slot),
        "selected_top_n": int(selected_top_n or 0),
        "top_slot_count": int(requested_top_n or slot),
        "mode": "paper",
        "broker": {
            "longbridge": {
                "enabled": False,
                "environment": "prod",
                "account_type": "",
                "allow_live_order": False,
            }
        },
    }


def _write_yaml_atomic(path: str, payload: dict) -> None:
    tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    os.replace(tmp_path, path)


def _top_config_dir(output_dir: str | os.PathLike[str] | None = None) -> str:
    if output_dir is None:
        return os.path.join(BASE, "configs")
    return os.fspath(output_dir)


def write_top_configs(
    top_items,
    *,
    selection_run_id: str | None = None,
    selection_date: str | None = None,
    generated_at: str | None = None,
    disabled_reason: str | None = None,
    result_quality: str | None = None,
    research_admission: str | None = None,
    top_sync_status: str | None = None,
    top_sync_error: str | None = None,
    slot_limit: int | None = None,
    selection_bundle_manifest_path: str | None = None,
    selection_bundle_hash: str | None = None,
    selection_bundle_version: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
):
    default_mode = _default_top_mode()
    global_reduce_only = _global_reduce_only_enabled()
    portfolio_cfg = _load_portfolio_config()
    ai_selector_cfg = _load_ai_selector_config()
    items = [dict(item or {}) for item in list(top_items or [])]
    selection_run_id = str(selection_run_id or uuid.uuid4().hex)
    generated_at = str(generated_at or datetime.now().isoformat())
    selection_date = str(selection_date or _selection_date())
    disabled_reason = str(disabled_reason or ("selection_blocked" if result_quality in {"INVALID"} or research_admission == "BLOCKED" else "top_n_not_filled"))
    slot_count = _top_slot_count(items, limit=slot_limit)
    allocator = RiskAllocator()
    allocations = allocator.allocate_positions(items, TOP_INITIAL_CAPITAL) if items else {}
    writes: list[tuple[str, dict]] = []

    top_dir = _top_config_dir(output_dir)
    for i in range(1, slot_count + 1):
        path = os.path.join(top_dir, f"TOP{i}.yaml")
        if i <= len(items):
            item = items[i - 1]
            support = float(item.get("range_low") or 0.0)
            resistance = float(item.get("range_high") or 0.0)
            if support <= 0 or resistance <= support:
                logger.warning("Skipping invalid TOP%d payload for %s", i, item.get("ticker"))
                payload = _slot_disabled_payload(
                    slot=i,
                    requested_top_n=slot_count,
                    selected_top_n=len(items),
                    selection_run_id=selection_run_id,
                    selection_date=selection_date,
                    generated_at=generated_at,
                    disabled_reason="selection_blocked" if result_quality == "INVALID" else "top_n_not_filled",
                    result_quality=result_quality,
                    research_admission=research_admission,
                    top_sync_status=top_sync_status,
                    top_sync_error=top_sync_error,
                    selection_bundle_manifest_path=selection_bundle_manifest_path,
                    selection_bundle_hash=selection_bundle_hash,
                    selection_bundle_version=selection_bundle_version,
                )
            else:
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
                entry = _entry_payload(item)
                payload = {
                    "enabled": True,
                    "slot": i,
                    "selection_run_id": selection_run_id,
                    "top_sync_run_id": selection_run_id,
                    "top_sync_status": str(top_sync_status or "OK"),
                    "top_sync_error": str(top_sync_error or ""),
                    "selection_bundle_manifest_path": str(selection_bundle_manifest_path or ""),
                    "selection_bundle_hash": str(selection_bundle_hash or ""),
                    "selection_bundle_version": str(selection_bundle_version or ""),
                    "selection_date": selection_date,
                    "generated_at": generated_at,
                    "result_quality": str(result_quality or ""),
                    "research_admission": str(research_admission or ""),
                    "requested_top_n": int(slot_count),
                    "selected_top_n": int(len(items)),
                    "top_slot_count": int(slot_count),
                    "ticker": item["ticker"],
                    "mode": mode,
                    "selection": {
                        "source": "ai_selector",
                        "selection_date": str(item.get("selection_date") or selection_date),
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
                        "entry": entry,
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
                        "entry_proximity_enabled": bool(
                            ai_selector_cfg.get("entry_proximity_enabled", True)
                        ),
                        "entry_proximity_weight": float(
                            ai_selector_cfg.get("entry_proximity_weight", 0.0)
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
        else:
            payload = _slot_disabled_payload(
                slot=i,
                requested_top_n=slot_count,
                selected_top_n=len(items),
                selection_run_id=selection_run_id,
                selection_date=selection_date,
                generated_at=generated_at,
                disabled_reason=disabled_reason,
                result_quality=result_quality,
                research_admission=research_admission,
                top_sync_status=top_sync_status,
                top_sync_error=top_sync_error,
                selection_bundle_manifest_path=selection_bundle_manifest_path,
                selection_bundle_hash=selection_bundle_hash,
                selection_bundle_version=selection_bundle_version,
            )
        writes.append((path, payload))

    for path, payload in writes:
        _write_yaml_atomic(path, payload)


def clear_top_configs(max_slots: int = 5) -> list[str]:
    selection_run_id = uuid.uuid4().hex
    generated_at = datetime.now().isoformat()
    selection_date = _selection_date()
    write_top_configs(
        [],
        selection_run_id=selection_run_id,
        selection_date=selection_date,
        generated_at=generated_at,
        disabled_reason="top_n_not_filled",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
        top_sync_status="OK",
        top_sync_error="",
        slot_limit=max_slots,
    )
    slot_count = max(3, int(max_slots or 0), _load_configured_top_count())
    return [os.path.join(BASE, f"configs/TOP{i}.yaml") for i in range(1, slot_count + 1)]
