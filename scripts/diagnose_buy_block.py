#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.ai_selector.selection_state import (
    current_top_config_symbols,
    load_selection_state,
    verify_selection_state,
)
from src.config.loader import load_config
from src.portfolio.manager import PortfolioManager


STATE_DIR = Path(os.environ.get("SOXS_STATE_DIR", str(PROJECT_DIR / "state")))
LOG_DIR = PROJECT_DIR / "logs"
REPORT_PATH = PROJECT_DIR / "reports" / "ai_selection_latest.json"
TOP_CONFIG_DIR = PROJECT_DIR / "configs"
TOP_PORTS = {1: 8091, 2: 8092, 3: 8093}
ORDER_STATE_DIRS = [STATE_DIR / "order_state", STATE_DIR / "order_state_test"]
RISK_STATE_DIR = STATE_DIR / "risk"
BUYING_POWER_FEE_BUFFER = 5.0

LEVERAGED_ETFS = {
    "SOXS",
    "LABD",
    "DRIP",
    "YINN",
    "TQQQ",
    "SQQQ",
    "SOXL",
    "LABU",
    "BOIL",
    "KOLD",
    "UVXY",
    "SPXS",
    "SPXL",
    "FAS",
    "FAZ",
}


def _current_et_date() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _read_top_configs() -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for index in range(1, 6):
        path = TOP_CONFIG_DIR / f"TOP{index}.yaml"
        raw = _read_yaml(path)
        if not raw:
            continue
        ticker = _normalize_ticker(raw.get("ticker"))
        if not ticker:
            continue
        selection = raw.get("selection") if isinstance(raw.get("selection"), dict) else {}
        allocation = raw.get("allocation") if isinstance(raw.get("allocation"), dict) else {}
        portfolio = raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {}
        ai_selector = raw.get("ai_selector") if isinstance(raw.get("ai_selector"), dict) else {}
        configs.append(
            {
                "index": index,
                "path": path,
                "ticker": ticker,
                "mode": str(raw.get("mode") or "paper").strip().lower() or "paper",
                "selection": selection,
                "allocation": allocation,
                "portfolio": portfolio,
                "ai_selector": ai_selector,
                "raw": raw,
            }
        )
    return configs


def _fetch_status(port: int) -> dict[str, Any] | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1.5)
        payload = json.loads(req.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _status_map(top_configs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for index, port in TOP_PORTS.items():
        status = _fetch_status(port)
        if not isinstance(status, dict):
            continue
        status = dict(status)
        status["port"] = port
        status.setdefault("ticker", None)
        cfg = next((item for item in top_configs if int(item.get("index", 0) or 0) == index), None)
        if cfg and _normalize_ticker(cfg.get("ticker")):
            ticker = _normalize_ticker(cfg.get("ticker"))
            mapping[ticker] = status
        ticker = _normalize_ticker(status.get("ticker"))
        if ticker and ticker not in mapping:
            mapping[ticker] = status
    return mapping


def _load_report_ticker(ticker: str) -> dict[str, Any] | None:
    report = _read_json(REPORT_PATH)
    if not report:
        return None
    for section_name in ("top3", "top10", "report"):
        rows = report.get(section_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if _normalize_ticker(row.get("ticker")) == ticker:
                return row
    return None


def _load_order_state_snapshot(ticker: str) -> dict[str, Any]:
    ticker = _normalize_ticker(ticker)
    for base in ORDER_STATE_DIRS:
        for bucket in (base / ticker, base):
            path = bucket / f"{ticker}.json"
            data = _read_json(path)
            if not data:
                continue
            blocked = data.get("blocked") if isinstance(data.get("blocked"), dict) else None
            blocked_until = str(blocked.get("blocked_until") or "").strip() if blocked else ""
            remaining_seconds = 0
            blocked_active = False
            if blocked_until:
                try:
                    when = datetime.fromisoformat(blocked_until)
                    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
                    remaining_seconds = max(0, int((when - now).total_seconds()))
                    blocked_active = remaining_seconds > 0
                except Exception:
                    blocked_active = True
            failed_orders = data.get("failed_orders_today") if isinstance(data.get("failed_orders_today"), list) else []
            return {
                "runtime_scope": data.get("runtime_scope", "runtime"),
                "blocked": blocked_active,
                "blocked_reason": str(blocked.get("reason") or "") if blocked else "",
                "blocked_until": blocked_until or None,
                "remaining_seconds": remaining_seconds,
                "failed_orders_today": len(failed_orders),
                "raw": data,
            }
    return {
        "runtime_scope": "runtime",
        "blocked": False,
        "blocked_reason": "",
        "blocked_until": None,
        "remaining_seconds": 0,
        "failed_orders_today": 0,
        "raw": None,
    }


def _load_pending_order_snapshot(ticker: str) -> dict[str, Any]:
    path = STATE_DIR / "pending_orders" / f"{_normalize_ticker(ticker)}.json"
    data = _read_json(path)
    if not data:
        return {"pending_order": False, "path": str(path), "raw": None}
    side = str(data.get("side") or "").strip().upper()
    order_id = str(data.get("order_id") or "").strip()
    return {
        "pending_order": bool(order_id),
        "path": str(path),
        "side": side or None,
        "order_id": order_id or None,
        "signal_type": str(data.get("signal_type") or "").strip() or None,
        "raw": data,
    }


def _load_risk_state_snapshot(ticker: str) -> dict[str, Any]:
    path = RISK_STATE_DIR / f"{_normalize_ticker(ticker)}.json"
    data = _read_json(path)
    if not data:
        return {
            "path": str(path),
            "halted": False,
            "halt_reason": "",
            "halt_until": None,
            "current_equity": 0.0,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "last_trade_time": None,
            "trade_history": [],
            "raw": None,
        }
    daily_pnl_map = data.get("daily_pnl") if isinstance(data.get("daily_pnl"), dict) else {}
    tday = _current_et_date()
    halt_until = str(data.get("halt_until") or "").strip() or None
    return {
        "path": str(path),
        "halted": bool(data.get("halted", False)),
        "halt_reason": str(data.get("halt_reason") or ""),
        "halt_until": halt_until,
        "current_equity": float(data.get("current_equity", 0.0) or 0.0),
        "daily_pnl": float(daily_pnl_map.get(tday, 0.0) or 0.0),
        "consecutive_losses": int(data.get("consecutive_losses", 0) or 0),
        "last_trade_time": str(data.get("last_trade_time") or "").strip() or None,
        "trade_history": data.get("trade_history") if isinstance(data.get("trade_history"), list) else [],
        "daily_peak_equity": data.get("daily_peak_equity") if isinstance(data.get("daily_peak_equity"), dict) else {},
        "raw": data,
    }


def _load_live_account_cache() -> dict[str, Any] | None:
    path = STATE_DIR / "broker_cache" / "longbridge_account.json"
    data = _read_json(path)
    if not data:
        return None
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else None
    if not payload:
        return None
    return payload


def _current_engine_snapshot(ticker: str, status_map: dict[str, dict[str, Any]], mode: str) -> dict[str, Any]:
    ticker = _normalize_ticker(ticker)
    status = dict(status_map.get(ticker) or {})
    if not status:
        for payload in status_map.values():
            if _normalize_ticker(payload.get("ticker")) == ticker:
                status = dict(payload)
                break
    if not status:
        return {
            "ticker": ticker,
            "signal": "UNKNOWN",
            "price": 0.0,
            "cash": 0.0,
            "buying_power": 0.0,
            "equity": 0.0,
            "position_shares": 0,
            "trade_in_progress": False,
            "last_signal_reason": "引擎状态不可用",
            "mode": mode,
        }
    status.setdefault("ticker", ticker)
    status.setdefault("mode", mode)
    return status


def _engine_portfolio_state(
    ticker: str,
    config_mode: str,
    engine_status: dict[str, Any],
) -> dict[str, Any]:
    ticker = _normalize_ticker(ticker)
    price = float(engine_status.get("price", 0.0) or 0.0)
    cash = float(engine_status.get("cash", 0.0) or 0.0)
    equity = float(engine_status.get("equity", 0.0) or 0.0)
    quantity = int(engine_status.get("position_shares", 0) or 0)
    positions: dict[str, dict[str, float]] = {}

    if config_mode == "live":
        live_account = _load_live_account_cache()
        if isinstance(live_account, dict):
            cash = float(live_account.get("cash", cash) or cash)
            equity = float(live_account.get("equity", equity) or equity)
            for pos in live_account.get("positions") or []:
                if not isinstance(pos, dict):
                    continue
                pos_ticker = _normalize_ticker(pos.get("ticker"))
                if not pos_ticker:
                    continue
                positions[pos_ticker] = {
                    "market_value": float(pos.get("market_value", 0.0) or 0.0),
                    "quantity": float(pos.get("quantity", 0.0) or 0.0),
                }
    if ticker and quantity > 0:
        positions.setdefault(
            ticker,
            {
                "market_value": float(quantity * price if price > 0 else 0.0),
                "quantity": float(quantity),
            },
        )
    return {
        "account_equity": equity,
        "cash": cash,
        "positions": positions,
    }


def _current_position_from_state(portfolio_state: dict[str, Any], ticker: str) -> int:
    positions = portfolio_state.get("positions") if isinstance(portfolio_state, dict) else {}
    if not isinstance(positions, dict):
        return 0
    item = positions.get(_normalize_ticker(ticker)) or {}
    if not isinstance(item, dict):
        return 0
    try:
        return max(0, int(float(item.get("quantity", 0) or 0)))
    except Exception:
        return 0


def _current_trade_status(engine_status: dict[str, Any]) -> str:
    signal = str(engine_status.get("last_signal") or engine_status.get("signal") or "UNKNOWN").strip().upper()
    return signal or "UNKNOWN"


def _top_selection_for_ticker(report: dict[str, Any] | None, ticker: str) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    for key in ("top3", "top10", "report"):
        rows = report.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if _normalize_ticker(row.get("ticker")) == ticker:
                return row
    return None


def _top_config_for_ticker(top_configs: list[dict[str, Any]], ticker: str) -> dict[str, Any] | None:
    for cfg in top_configs:
        if _normalize_ticker(cfg.get("ticker")) == ticker:
            return cfg
    return None


def _fallback_policy(top_config: dict[str, Any] | None, engine_status: dict[str, Any]) -> tuple[bool, bool, float]:
    ai_selector = (top_config or {}).get("ai_selector") if isinstance(top_config, dict) else {}
    if not isinstance(ai_selector, dict):
        ai_selector = {}
    allow_paper = bool(ai_selector.get("allow_fallback_paper_entries", False))
    allow_live = bool(ai_selector.get("allow_fallback_live_entries", False))
    try:
        multiplier = float(ai_selector.get("fallback_paper_position_multiplier", 0.25) or 0.25)
    except (TypeError, ValueError):
        multiplier = 0.25
    if not top_config:
        # If we do not have a local TOP config, use the runtime signal as a hint.
        pass
    return allow_paper, allow_live, max(0.0, multiplier)


def _selection_allocation_weight(selection_report: dict[str, Any] | None, ticker: str) -> float:
    report_item = _top_selection_for_ticker(selection_report, ticker)
    if not isinstance(report_item, dict):
        return 0.0
    try:
        return float((report_item.get("allocation") or {}).get("weight") or 0.0)
    except Exception:
        return 0.0


def _buying_power_check(price: float, quantity: int, available_cash: float, fee_buffer: float = BUYING_POWER_FEE_BUFFER) -> tuple[bool, str]:
    required = price * quantity + fee_buffer
    if available_cash < required:
        return False, (
            f"BUY_BLOCKED: insufficient buying power — 需要 ${required:.2f} (${price:.2f}×{quantity}+fee), "
            f"可用 ${available_cash:.2f}"
        )
    return True, ""


def _risk_allowed(
    *,
    ticker: str,
    price: float,
    shares: int,
    current_position: int,
    portfolio_state: dict[str, Any],
    risk_config: Any,
    risk_state: dict[str, Any],
) -> tuple[bool, str, str | None]:
    account_equity = float(portfolio_state.get("account_equity", 0.0) or 0.0)
    if bool(risk_state.get("halted", False)):
        halt_until = str(risk_state.get("halt_until") or "").strip()
        if halt_until:
            try:
                when = datetime.fromisoformat(halt_until)
                now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
                if when > now:
                    return False, f"Trading halted: {risk_state.get('halt_reason') or 'halted'}", "halt"
            except Exception:
                return False, f"Trading halted: {risk_state.get('halt_reason') or 'halted'}", "halt"

    new_position = current_position + shares
    if new_position > int(getattr(risk_config, "max_position", 300) or 300):
        return False, f"Position limit: {new_position} > {int(getattr(risk_config, 'max_position', 300) or 300)} shares max", "max_position"

    last_trade_time = str(risk_state.get("last_trade_time") or "").strip()
    if last_trade_time:
        try:
            last_trade = datetime.fromisoformat(last_trade_time)
            elapsed = (datetime.now(last_trade.tzinfo) if last_trade.tzinfo else datetime.now()) - last_trade
            cool_down_seconds = int(getattr(risk_config, "cool_down_seconds", 30) or 30)
            if elapsed.total_seconds() < cool_down_seconds:
                remaining = cool_down_seconds - elapsed.total_seconds()
                return False, f"Cool-down: {remaining:.0f}s remaining before next trade", "cool_down"
        except Exception:
            pass

    if ticker in LEVERAGED_ETFS:
        account_equity = max(1.0, account_equity)
        order_value = price * shares
        pos_pct = order_value / account_equity * 100.0
        max_pos_pct = float(getattr(risk_config, "leveraged_etf_max_single_position", 0.15) or 0.15) * 100.0
        if pos_pct > max_pos_pct:
            return False, f"Instrument profile: {ticker} position {pos_pct:.1f}% > {max_pos_pct:.1f}% limit", "instrument_profile_single"
        positions = portfolio_state.get("positions") if isinstance(portfolio_state, dict) else {}
        current_leveraged_exposure = 0.0
        if isinstance(positions, dict):
            for pos_ticker, raw in positions.items():
                if _normalize_ticker(pos_ticker) not in LEVERAGED_ETFS:
                    continue
                if not isinstance(raw, dict):
                    continue
                mv = float(raw.get("market_value", 0.0) or 0.0)
                if mv <= 0:
                    qty = float(raw.get("quantity", 0.0) or 0.0)
                    mv = qty * price if qty > 0 and price > 0 else 0.0
                current_leveraged_exposure += mv / account_equity * 100.0
        max_group = float(getattr(risk_config, "leveraged_etf_max_group_exposure", 0.50) or 0.50) * 100.0
        if current_leveraged_exposure + pos_pct > max_group:
            return False, (
                f"Instrument profile: leveraged group exposure "
                f"{current_leveraged_exposure + pos_pct:.1f}% > {max_group:.1f}% limit"
            ), "instrument_profile_group"

    daily_pnl = float(risk_state.get("daily_pnl", 0.0) or 0.0)
    daily_loss_limit = float(getattr(risk_config, "daily_loss_limit", 500.0) or 500.0)
    if daily_pnl <= -daily_loss_limit:
        return False, (
            f"Daily loss limit: ${-daily_pnl:.2f} lost today (limit: ${daily_loss_limit}) — halted until next day"
        ), "daily_loss_limit"

    consecutive_losses = int(risk_state.get("consecutive_losses", 0) or 0)
    max_consecutive_losses = int(getattr(risk_config, "max_consecutive_losses", 3) or 3)
    if consecutive_losses >= max_consecutive_losses:
        return False, (
            f"Consecutive losses: {consecutive_losses} in a row, paused 30 min"
        ), "consecutive_losses"

    current_equity = float(risk_state.get("current_equity", 0.0) or 0.0)
    daily_peak_equity = risk_state.get("daily_peak_equity")
    if current_equity > 0 and isinstance(daily_peak_equity, dict):
        today = _current_et_date()
        peak = max(
            float(daily_peak_equity.get(today, current_equity) or current_equity),
            current_equity,
        )
        if peak > 0:
            drawdown = (peak - current_equity) / peak * 100.0
            max_drawdown_pct = float(getattr(risk_config, "max_drawdown_pct", 10.0) or 10.0)
            if drawdown > max_drawdown_pct:
                return False, f"Drawdown: {drawdown:.1f}% from peak (limit: {max_drawdown_pct}%)", "max_drawdown"

    return True, "OK", None


def diagnose_buy_block(ticker: str) -> dict[str, Any]:
    ticker = _normalize_ticker(ticker)
    top_configs = _read_top_configs()
    top_symbols = [cfg["ticker"] for cfg in top_configs]
    top_config = _top_config_for_ticker(top_configs, ticker)
    top_index = int(top_config["index"]) if top_config else None
    top_mode = str(top_config.get("mode") if top_config else "paper").lower()
    current_top_config_symbols_list = current_top_config_symbols(limit=max(3, len(top_configs)))

    selection_state = load_selection_state() or {}
    selection_ok, selection_reason, selection_state_payload = verify_selection_state(required_et_date=_current_et_date())
    selection_item = _top_selection_for_ticker(_read_json(REPORT_PATH), ticker)
    fallback_used = bool(
        (selection_item or {}).get("fallback_used", False)
        or bool((_read_json(REPORT_PATH) or {}).get("fallback_used", False))
    )

    status_map = _status_map(top_configs)
    engine_status = _current_engine_snapshot(ticker, status_map, top_mode)
    signal = _current_trade_status(engine_status)
    current_price = float(engine_status.get("price", 0.0) or 0.0)
    cash = float(engine_status.get("cash", 0.0) or 0.0)
    buying_power = float(engine_status.get("buying_power", 0.0) or 0.0)
    equity = float(engine_status.get("equity", 0.0) or 0.0)
    position_shares = int(engine_status.get("position_shares", 0) or 0)
    has_position = position_shares > 0
    reduce_only = bool((top_config or {}).get("selection", {}).get("reduce_only", False))
    pending_order_snapshot = _load_pending_order_snapshot(ticker)
    order_state_snapshot = _load_order_state_snapshot(ticker)
    risk_state_snapshot = _load_risk_state_snapshot(ticker)
    config = load_config(str((top_config or {}).get("path")) if top_config else None) if top_config else None
    ai_selector_cfg = getattr(config, "ai_selector", None)
    portfolio_cfg = getattr(config, "portfolio", None)
    risk_cfg = getattr(config, "risk", None)
    position_cfg = getattr(config, "position", None)

    allow_fallback_paper_entries = bool(getattr(ai_selector_cfg, "allow_fallback_paper_entries", False))
    allow_fallback_live_entries = bool(getattr(ai_selector_cfg, "allow_fallback_live_entries", False))
    fallback_paper_position_multiplier = float(getattr(ai_selector_cfg, "fallback_paper_position_multiplier", 0.25) or 0.25)

    target_shares = 0
    if isinstance(selection_item, dict):
        try:
            target_shares = int((selection_item.get("allocation") or {}).get("target_shares") or 0)
        except Exception:
            target_shares = 0
    if target_shares <= 0 and isinstance(top_config, dict):
        try:
            target_shares = int((top_config.get("allocation") or {}).get("target_shares") or 0)
        except Exception:
            target_shares = 0
    if target_shares <= 0 and position_cfg is not None:
        try:
            target_shares = int(getattr(position_cfg, "size_per_trade", 0) or 0)
        except Exception:
            target_shares = 0

    selection_top3 = [str(item.get("ticker") or "").strip().upper() for item in (_read_json(REPORT_PATH) or {}).get("top3", []) if isinstance(item, dict)]
    in_top_config = ticker in top_symbols
    selection_synced = bool(selection_ok)
    signal_is_buy = signal == "BUY"
    top3_rank = None
    selection_weight = 0.0
    if isinstance(selection_item, dict):
        for idx, item in enumerate((_read_json(REPORT_PATH) or {}).get("top3", []) or [], start=1):
            if isinstance(item, dict) and _normalize_ticker(item.get("ticker")) == ticker:
                top3_rank = idx
                break
        selection_weight = _selection_allocation_weight(_read_json(REPORT_PATH), ticker)

    available_cash = max(cash, buying_power)
    if selection_item and signal_is_buy and in_top_config and top3_rank is not None:
        try:
            top3_len = max(1, len((_read_json(REPORT_PATH) or {}).get("top3") or []))
            available_cash = min(available_cash, equity * min(0.30, 1.0 / top3_len))
        except Exception:
            pass

    adjusted_target_shares = target_shares
    fallback_reason = ""
    if fallback_used:
        if top_mode == "live" and not allow_fallback_live_entries:
            adjusted_target_shares = 0
            fallback_reason = "fallback_used_live_blocked"
        elif top_mode == "paper":
            if not allow_fallback_paper_entries:
                adjusted_target_shares = 0
                fallback_reason = "fallback_used_blocked"
            else:
                adjusted_target_shares = int(math.floor(target_shares * fallback_paper_position_multiplier))
                if adjusted_target_shares < 1:
                    fallback_reason = "fallback_reduced_size_below_minimum"
                else:
                    fallback_reason = "fallback_used_paper_allowed_with_reduced_size"
        else:
            adjusted_target_shares = 0
            fallback_reason = "fallback_used_blocked"

    if signal_is_buy and in_top_config and not reduce_only and not pending_order_snapshot.get("pending_order") and not order_state_snapshot.get("blocked"):
        if fallback_used and adjusted_target_shares < 1:
            final_action = "BLOCKED_BY_FALLBACK"
            blocked_by = "fallback"
            reason = fallback_reason or "fallback_reduced_size_below_minimum"
        else:
            risk_preapproved = bool(selection_item) and (not fallback_used or fallback_reason == "fallback_used_paper_allowed_with_reduced_size")
            execution_price = float(engine_status.get("ask", current_price) or current_price)
            if execution_price <= 0:
                execution_price = current_price
            bp_ok, bp_reason = _buying_power_check(execution_price, adjusted_target_shares or target_shares, available_cash)
            portfolio_state = _engine_portfolio_state(ticker, top_mode, engine_status)
            pm = PortfolioManager(
                max_positions=int(getattr(portfolio_cfg, "max_positions", 3) or 3) if portfolio_cfg is not None else 3,
                max_total_exposure=float(getattr(portfolio_cfg, "max_total_exposure", 1.0) or 1.0) if portfolio_cfg is not None else 1.0,
                max_total_risk=float(getattr(portfolio_cfg, "max_total_risk", 0.05) or 0.05) if portfolio_cfg is not None else 0.05,
                leveraged_etf_max_single_position=float(getattr(portfolio_cfg, "leveraged_etf_max_single_position", 0.15) or 0.15) if portfolio_cfg is not None else 0.15,
                leveraged_etf_max_group_exposure=float(getattr(portfolio_cfg, "leveraged_etf_max_group_exposure", 0.50) or 0.50) if portfolio_cfg is not None else 0.50,
            )
            portfolio_check = pm.check_portfolio_risk(
                {
                    "ticker": ticker,
                    "side": "BUY",
                    "quantity": adjusted_target_shares or target_shares,
                    "price": execution_price,
                    "target_capital": (adjusted_target_shares or target_shares) * execution_price,
                    "reduce_only": False,
                    "regime": str((selection_item or {}).get("regime") or "RANGE").upper(),
                },
                portfolio_state,
            )
            risk_check, risk_reason, risk_rule = _risk_allowed(
                ticker=ticker,
                price=execution_price,
                shares=adjusted_target_shares or target_shares,
                current_position=_current_position_from_state(portfolio_state, ticker),
                portfolio_state=portfolio_state,
                risk_config=risk_cfg or position_cfg or config.risk if config else None,
                risk_state=risk_state_snapshot,
            )

            if not risk_preapproved:
                final_action = "BLOCKED_BY_FALLBACK"
                blocked_by = "fallback"
                reason = fallback_reason or "fallback_used_blocked"
            elif not bp_ok:
                final_action = "BLOCKED_BY_BUYING_POWER"
                blocked_by = "buying_power"
                reason = bp_reason
            elif not risk_check:
                final_action = "BLOCKED_BY_RISK"
                blocked_by = risk_rule or "risk"
                reason = risk_reason
            elif not bool(portfolio_check.get("allowed", False)):
                final_action = "BLOCKED_BY_PORTFOLIO"
                blocked_by = str(portfolio_check.get("reason") or "portfolio")
                reason = str(portfolio_check.get("reason") or "portfolio_blocked")
            else:
                final_action = "BUY_ALLOWED"
                blocked_by = ""
                reason = "ok"
            risk_approved = risk_preapproved
            buying_power_ok = bp_ok
            portfolio_allowed = bool(portfolio_check.get("allowed", False))
            risk_manager_allowed = risk_check
        if fallback_used and adjusted_target_shares < 1 and final_action != "BLOCKED_BY_FALLBACK":
            final_action = "BLOCKED_BY_FALLBACK"
            blocked_by = "fallback"
            reason = fallback_reason or "fallback_reduced_size_below_minimum"
    elif signal_is_buy and not in_top_config:
        final_action = "BLOCKED_BY_RISK"
        blocked_by = "not_in_top_config"
        reason = "ticker not in current TOP config"
        risk_approved = False
        buying_power_ok = None
        portfolio_allowed = None
        risk_manager_allowed = None
    elif signal == "BUY" and has_position:
        final_action = "ALREADY_HAS_POSITION"
        blocked_by = "position"
        reason = "已有持仓"
        risk_approved = False
        buying_power_ok = None
        portfolio_allowed = None
        risk_manager_allowed = None
    elif signal != "BUY":
        final_action = "HOLD_SIGNAL"
        blocked_by = "signal"
        reason = f"当前信号是 {signal}"
        risk_approved = False
        buying_power_ok = None
        portfolio_allowed = None
        risk_manager_allowed = None
    else:
        final_action = "BLOCKED_BY_ORDER_STATE" if pending_order_snapshot.get("pending_order") or order_state_snapshot.get("blocked") else "BLOCKED_BY_RISK"
        blocked_by = "order_state" if pending_order_snapshot.get("pending_order") or order_state_snapshot.get("blocked") else "unknown"
        reason = order_state_snapshot.get("blocked_reason") or "buy gate not passed"
        risk_approved = False
        buying_power_ok = None
        portfolio_allowed = None
        risk_manager_allowed = None

    checks = {
        "in_top_config": bool(in_top_config),
        "selection_synced": bool(selection_synced),
        "has_position": bool(has_position),
        "pending_order": bool(pending_order_snapshot.get("pending_order")),
        "order_state_blocked": bool(order_state_snapshot.get("blocked")),
        "failure_cooldown": bool(order_state_snapshot.get("blocked")),
        "fallback_used": bool(fallback_used),
        "mode": top_mode,
        "allow_fallback_paper_entries": bool(allow_fallback_paper_entries),
        "allow_fallback_live_entries": bool(allow_fallback_live_entries),
        "fallback_paper_position_multiplier": round(float(fallback_paper_position_multiplier), 4),
        "risk_approved": bool(locals().get("risk_approved", False)),
        "target_shares": int(target_shares),
        "adjusted_target_shares": int(adjusted_target_shares),
        "buying_power_ok": bool(locals().get("buying_power_ok", False)) if locals().get("buying_power_ok", None) is not None else None,
        "risk_manager_allowed": bool(locals().get("risk_manager_allowed", False)) if locals().get("risk_manager_allowed", None) is not None else None,
        "portfolio_allowed": bool(locals().get("portfolio_allowed", False)) if locals().get("portfolio_allowed", None) is not None else None,
        "buying_power": round(float(buying_power), 2),
        "cash": round(float(cash), 2),
        "equity": round(float(equity), 2),
        "current_price": round(float(current_price), 4),
        "signal": signal,
        "reduce_only": bool(reduce_only),
        "blocked_reason": reason,
        "blocked_by": blocked_by,
        "current_port_symbols": top_symbols,
        "selection_state_symbols": list(selection_state_payload.get("selection_state_symbols") or selection_state.get("selected_symbols") or []),
        "current_top_config_symbols": current_top_config_symbols_list,
        "selection_state_reason": selection_reason,
        "top3_rank": top3_rank,
        "selection_weight": round(float(selection_weight), 4),
        "portfolio_state_positions": portfolio_state.get("positions") if 'portfolio_state' in locals() else {},
        "order_state_remaining_seconds": int(order_state_snapshot.get("remaining_seconds", 0) or 0),
        "order_state_failed_orders_today": int(order_state_snapshot.get("failed_orders_today", 0) or 0),
        "pending_order_id": pending_order_snapshot.get("order_id"),
        "engine_last_signal_reason": str(engine_status.get("last_signal_reason") or ""),
        "engine_trade_in_progress": bool(engine_status.get("trade_in_progress", False)),
    }

    return {
        "ticker": ticker,
        "signal": signal,
        "final_action": final_action,
        "reason": reason,
        "blocked_by": blocked_by,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose why a ticker did not place a BUY order.")
    parser.add_argument("ticker", help="Ticker to diagnose, e.g. F, SOFI, DRIP")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    report = diagnose_buy_block(args.ticker)
    if args.pretty:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
