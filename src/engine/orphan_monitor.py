from __future__ import annotations

import logging
import os
import re
import time
import json
import urllib.request
from pathlib import Path

import yaml

from ..broker.base import Position
from ..config.loader import AppConfig, PositionConfig
from ..ai_selector.selection_report import load_latest_ai_selection_state
from .trading_engine import (
    TradingEngine,
    append_runtime_audit,
    check_exit_conditions,
    is_inverse_etf_symbol,
)

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")
TOP_CONFIGS = [PROJECT_DIR / "configs" / f"TOP{idx}.yaml" for idx in range(1, 6)]
TOP_PORTS = [8091, 8092, 8093, 8094, 8095]

# ── Orphan ownership: identity verification status ──────────────────
_ASSIGNED_ACTIVE = "ASSIGNED_ACTIVE"
_ASSIGNED_UNVERIFIED = "ASSIGNED_UNVERIFIED"
_ASSIGNED_STALE = "ASSIGNED_STALE"
_ORPHAN_CONFIRMED = "ORPHAN_CONFIRMED"


def _normalize_ticker(value: str) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _load_assigned_symbols() -> set[str]:
    symbols: set[str] = set()
    for path in TOP_CONFIGS:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ticker = _normalize_ticker(data.get("ticker"))
        if ticker and EQUITY_SYMBOL_RE.fullmatch(ticker):
            symbols.add(ticker)
    return symbols


def _load_configured_assignments() -> dict[int, dict[str, str]]:
    """Return {port: {ticker, expected_mode, expected_environment, expected_account_type}}."""
    assignments: dict[int, dict[str, str]] = {}
    for path, port in zip(TOP_CONFIGS, TOP_PORTS):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ticker = _normalize_ticker(data.get("ticker"))
        if not ticker or not EQUITY_SYMBOL_RE.fullmatch(ticker):
            continue
        lb = (data.get("broker") or {}).get("longbridge") or {}
        assignments[port] = {
            "ticker": ticker,
            "expected_mode": str(data.get("mode") or "").strip().lower(),
            "expected_environment": str(lb.get("environment") or "").strip().lower(),
            "expected_account_type": str(lb.get("account_type") or "").strip().lower(),
        }
    return assignments


def _is_equity_position(position: Position) -> bool:
    return bool(EQUITY_SYMBOL_RE.fullmatch(_normalize_ticker(getattr(position, "ticker", ""))))


def _load_report_exit_range(symbol: str) -> dict | None:
    payload = load_latest_ai_selection_state(PROJECT_DIR)
    if not isinstance(payload, dict):
        return None

    ticker = _normalize_ticker(symbol)
    buckets: list[tuple[str, list[dict]]] = []
    for key in ("protected_positions", "top3", "top5", "top10"):
        items = payload.get(key)
        if isinstance(items, list):
            buckets.append((key, [item for item in items if isinstance(item, dict)]))

    for source_key, items in buckets:
        for item in items:
            if _normalize_ticker(item.get("ticker")) != ticker:
                continue
            try:
                support = float(item.get("range_low"))
                resistance = float(item.get("range_high"))
            except (TypeError, ValueError):
                return None
            if support <= 0 or resistance <= support:
                return None
            return {
                "support": round(support, 4),
                "resistance": round(resistance, 4),
                "source": source_key,
                "selection_date": str(payload.get("selection_date") or "").strip() or None,
            }
    return None


class OrphanPositionMonitor:
    def __init__(self, broker, poll_interval_seconds: int = 60):
        self.broker = broker
        self.poll_interval_seconds = max(60, int(poll_interval_seconds or 60))
        self._running = False
        self._engines: dict[str, TradingEngine] = {}
        self._startup_at = time.monotonic()
        self._assignment_failures: dict[int, int] = {}
        self._last_orphan_symbols: set[str] | None = None
        # ── Identity tracking per port ──
        self._identity_status: dict[int, str] = {}

    def verify_broker_positions(self) -> list[Position] | None:
        positions = self.broker.get_positions()
        reliable = getattr(self.broker, "is_positions_snapshot_reliable", lambda: True)()
        if not reliable:
            return None
        return list(positions or [])

    def verify_startup_safety(self) -> tuple[list[Position], object] | None:
        """Require fresh, broker-confirmed positions and account data before live startup."""
        invalidate = getattr(self.broker, "invalidate_cache", None)
        if callable(invalidate):
            invalidate()
        positions = self.verify_broker_positions()
        if positions is None:
            append_runtime_audit(
                {
                    "phase": "startup_safety_check",
                    "execution_mode": "live",
                    "broker_position_verified": False,
                    "broker_account_verified": False,
                    "startup_allowed": False,
                    "reason": "broker_position_verification_failed",
                }
            )
            return None

        account = self.broker.get_account()
        account_reliable = getattr(
            self.broker, "is_account_snapshot_reliable", lambda: True
        )()
        if not account_reliable:
            append_runtime_audit(
                {
                    "phase": "startup_safety_check",
                    "execution_mode": "live",
                    "broker_position_verified": True,
                    "broker_account_verified": False,
                    "startup_allowed": False,
                    "reason": "broker_account_verification_failed",
                }
            )
            return None

        append_runtime_audit(
            {
                "phase": "startup_safety_check",
                "execution_mode": "live",
                "broker_position_verified": True,
                "broker_account_verified": True,
                "startup_allowed": True,
                "positions": [
                    {
                        "symbol": _normalize_ticker(getattr(pos, "ticker", "")),
                        "quantity": int(getattr(pos, "quantity", 0) or 0),
                    }
                    for pos in positions
                    if int(getattr(pos, "quantity", 0) or 0) > 0
                ],
                "equity": float(getattr(account, "equity", 0.0) or 0.0),
            }
        )
        return positions, account

    def scan_orphans(self, positions: list[Position] | None = None) -> dict[str, Position]:
        assigned = self._active_assigned_symbols()
        if positions is None:
            positions = self.verify_broker_positions()
        if positions is None:
            return {}
        orphans: dict[str, Position] = {}
        for pos in positions:
            ticker = _normalize_ticker(getattr(pos, "ticker", ""))
            quantity = int(getattr(pos, "quantity", 0) or 0)
            if quantity <= 0 or not ticker or ticker in assigned or not _is_equity_position(pos):
                continue
            orphans[ticker] = pos
        return orphans

    def _fetch_engine_status(self, port: int) -> dict | None:
        """Return the parsed API status payload or None on any failure."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload
            return None
        except Exception:
            return None

    def _is_top_process_active(self, port: int) -> bool:
        payload = self._fetch_engine_status(port)
        return bool(payload and payload.get("running"))

    def _verify_engine_identity(self, port: int, expected: dict[str, str]) -> str:
        """Verify that the engine at *port* matches its configured identity.

        Returns one of: ASSIGNED_ACTIVE, ASSIGNED_UNVERIFIED, ASSIGNED_STALE,
        ORPHAN_CONFIRMED.
        """
        payload = self._fetch_engine_status(port)

        # Port unreachable → unverified (may still be starting)
        if not payload:
            return _ASSIGNED_UNVERIFIED

        if not payload.get("running"):
            return _ASSIGNED_UNVERIFIED

        # ── Identity checks ──────────────────────────────────────────
        mismatches: list[str] = []

        api_ticker = _normalize_ticker(str(payload.get("ticker") or ""))
        expected_ticker = expected.get("ticker", "")
        if expected_ticker and api_ticker != expected_ticker:
            mismatches.append(f"ticker:{api_ticker}!={expected_ticker}")

        api_mode = str(payload.get("execution_mode") or payload.get("mode") or "").strip().lower()
        expected_mode = expected.get("expected_mode", "")
        if expected_mode and api_mode != expected_mode:
            mismatches.append(f"mode:{api_mode}!={expected_mode}")

        api_env = str(payload.get("broker_environment") or "").strip().lower()
        expected_env = expected.get("expected_environment", "")
        if expected_env and api_env != expected_env:
            mismatches.append(f"environment:{api_env}!={expected_env}")

        api_acct = str(payload.get("account_type") or "").strip().lower()
        expected_acct = expected.get("expected_account_type", "")
        if expected_acct and api_acct != expected_acct:
            mismatches.append(f"account_type:{api_acct}!={expected_acct}")

        if mismatches:
            mismatch_detail = "; ".join(mismatches)
            append_runtime_audit(
                {
                    "phase": "orphan_identity_mismatch",
                    "port": port,
                    "expected_ticker": expected_ticker,
                    "mismatches": mismatch_detail,
                }
            )
            logger.warning(
                "Orphan monitor: port %d identity mismatch — %s",
                port,
                mismatch_detail,
            )
            return _ASSIGNED_UNVERIFIED

        return _ASSIGNED_ACTIVE

    def _active_assigned_symbols(self) -> set[str]:
        assignments = _load_configured_assignments()
        # TOP processes are started after the orphan monitor. Avoid takeover
        # during their normal sequential startup window.
        if (time.monotonic() - self._startup_at) < 120:
            return set(info["ticker"] for info in assignments.values())

        active: set[str] = set()
        for port, info in assignments.items():
            status = self._verify_engine_identity(port, info)
            self._identity_status[port] = status
            if status == _ASSIGNED_ACTIVE:
                self._assignment_failures[port] = 0
                active.add(info["ticker"])
                continue
            # Not fully verified — track consecutive failures
            failures = self._assignment_failures.get(port, 0) + 1
            self._assignment_failures[port] = failures
            if failures < 3:
                # Grace period: still treat as assigned to avoid premature orphan takeover
                active.add(info["ticker"])
                continue
            # Consecutive failures threshold reached — confirm as orphan
            self._identity_status[port] = _ORPHAN_CONFIRMED
            append_runtime_audit(
                {
                    "phase": "orphan_confirmed",
                    "port": port,
                    "ticker": info["ticker"],
                    "consecutive_failures": failures,
                }
            )
        return active

    def log_startup_scan(self, orphans: dict[str, Position]) -> None:
        append_runtime_audit(
            {
                "phase": "orphan_position_scan",
                "execution_mode": "live",
                "symbols": [
                    {"symbol": ticker, "quantity": int(getattr(pos, "quantity", 0) or 0)}
                    for ticker, pos in sorted(orphans.items())
                ],
                "orphan_count": len(orphans),
            }
        )
        self._last_orphan_symbols = set(orphans)

    def _log_orphan_change(self, orphans: dict[str, Position]) -> None:
        symbols = set(orphans)
        if self._last_orphan_symbols is None:
            self._last_orphan_symbols = symbols
            return
        if symbols == self._last_orphan_symbols:
            return
        append_runtime_audit(
            {
                "phase": "orphan_assignment_change",
                "execution_mode": "live",
                "symbols": sorted(symbols),
                "added": sorted(symbols - self._last_orphan_symbols),
                "removed": sorted(self._last_orphan_symbols - symbols),
            }
        )
        self._last_orphan_symbols = symbols

    def run(self) -> int:
        positions = self.verify_broker_positions()
        if positions is None:
            logger.error("Orphan monitor aborted: broker positions could not be verified")
            return 1

        orphans = self.scan_orphans(positions)
        self.log_startup_scan(orphans)
        self._running = True

        while self._running:
            try:
                engines = [self._engine_for_symbol(symbol) for symbol in orphans.keys()]
                if engines and not engines[0]._is_trading_hours():
                    time.sleep(30)
                    positions = self.verify_broker_positions()
                    orphans = self.scan_orphans(positions)
                    self._log_orphan_change(orphans)
                    continue

                positions = self.verify_broker_positions()
                if positions is None:
                    time.sleep(15)
                    continue

                orphans = self.scan_orphans(positions)
                self._log_orphan_change(orphans)
                for symbol, pos in list(orphans.items()):
                    try:
                        self._evaluate_symbol(symbol, pos)
                    except Exception as exc:
                        logger.exception("Orphan monitor _evaluate_symbol(%s) crashed: %s", symbol, exc)
                time.sleep(self.poll_interval_seconds)
            except Exception as exc:
                logger.exception("Orphan monitor run loop crashed: %s", exc)
                time.sleep(30)
        return 0

    def stop(self) -> None:
        self._running = False

    def _engine_for_symbol(self, symbol: str) -> TradingEngine:
        ticker = _normalize_ticker(symbol)
        if ticker not in self._engines:
            config = AppConfig(
                ticker=ticker,
                mode="live",
                position=PositionConfig(reduce_only=True),
            )
            engine = TradingEngine(
                config,
                ignore_trading_hours=False,
                startup_role="orphan_monitor",
            )
            engine.broker = self.broker
            self._engines[ticker] = engine
        return self._engines[ticker]

    def _evaluate_symbol(self, symbol: str, pos: Position) -> None:
        ticker = _normalize_ticker(symbol)
        quantity = int(getattr(pos, "quantity", 0) or 0)
        if quantity <= 0:
            return

        # ── B4: Ownership gate — skip if a TOP engine is actively assigned ──
        if ticker in self._active_assigned_symbols():
            append_runtime_audit(
                {
                    "phase": "orphan_exit_skipped",
                    "symbol": ticker,
                    "reason": "active_top_owner",
                }
            )
            return

        avg_cost = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
        current_price = float(getattr(pos, "current_price", 0.0) or avg_cost or 0.0)
        if avg_cost <= 0 or current_price <= 0:
            return

        engine = self._engine_for_symbol(ticker)
        engine._reconcile_pending_order()
        confirmed = engine._apply_position_sync_fence(quantity)
        if confirmed <= 0:
            return

        report_range = None if ticker.split(".")[0].upper() == "SOXS" else _load_report_exit_range(ticker)
        if report_range:
            support = float(report_range["support"])
            resistance = float(report_range["resistance"])
            decision = {
                "should_exit": False,
                "reason": None,
                "trigger_price": None,
                "avg_cost": avg_cost,
                "position_qty": confirmed,
                "symbol": ticker,
                "mode": "orphan_range",
                "range_source": report_range.get("source"),
                "support": support,
                "resistance": resistance,
            }
            if current_price <= support:
                decision["should_exit"] = True
                decision["reason"] = "stop_loss"
                decision["trigger_price"] = support
            elif current_price >= resistance:
                decision["should_exit"] = True
                decision["reason"] = "take_profit"
                decision["trigger_price"] = resistance
        else:
            decision = check_exit_conditions(
                ticker,
                current_price,
                avg_cost,
                confirmed,
                is_inverse_etf=is_inverse_etf_symbol(ticker),
                mode="orphan",
            )
        if not decision["should_exit"]:
            return
        if engine._has_active_sell_protection():
            append_runtime_audit(
                {
                    "phase": "orphan_risk_exit",
                    "symbol": ticker,
                    "ticker": ticker,
                    "avg_cost": avg_cost,
                    "current_price": current_price,
                    "quantity": confirmed,
                    "mode": decision.get("mode") or "orphan",
                    "reason": decision.get("reason"),
                    "trigger_price": decision.get("trigger_price"),
                    "range_source": decision.get("range_source"),
                    "skipped": True,
                    "skip_reason": "sell_protection_active",
                }
            )
            return

        engine._position_shares = confirmed
        engine._entry_price = avg_cost
        engine._submit_reduce_order(
            quantity=confirmed,
            current_price=current_price,
            execution_price=current_price,
            reason=str(decision["reason"] or "stop_loss"),
            mode=str(decision.get("mode") or "orphan"),
            avg_cost=avg_cost,
            broker_position_verified=True,
            trigger_price=decision["trigger_price"],
            audit_phase="orphan_risk_exit",
        )


def should_run_orphan_monitor() -> bool:
    if os.getenv("SOXS_DISABLE_ORPHAN_MONITOR", "0").strip() == "1":
        return False
    for path in TOP_CONFIGS:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if str(data.get("mode") or "").strip().lower() == "live":
            return True
    return False
