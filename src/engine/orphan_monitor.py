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
AI_SELECTION_REPORT = PROJECT_DIR / "reports" / "ai_selection_latest.json"


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


def _load_configured_assignments() -> dict[int, str]:
    assignments: dict[int, str] = {}
    for path, port in zip(TOP_CONFIGS, TOP_PORTS):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ticker = _normalize_ticker(data.get("ticker"))
        if ticker and EQUITY_SYMBOL_RE.fullmatch(ticker):
            assignments[port] = ticker
    return assignments


def _is_equity_position(position: Position) -> bool:
    return bool(EQUITY_SYMBOL_RE.fullmatch(_normalize_ticker(getattr(position, "ticker", ""))))


def _load_report_exit_range(symbol: str) -> dict | None:
    if not AI_SELECTION_REPORT.exists():
        return None
    try:
        payload = json.loads(AI_SELECTION_REPORT.read_text(encoding="utf-8"))
    except Exception:
        return None
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

    def _is_top_process_active(self, port: int) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(isinstance(payload, dict) and payload.get("running"))
        except Exception:
            return False

    def _active_assigned_symbols(self) -> set[str]:
        assignments = _load_configured_assignments()
        # TOP processes are started after the orphan monitor. Avoid takeover
        # during their normal sequential startup window.
        if (time.monotonic() - self._startup_at) < 120:
            return set(assignments.values())

        active: set[str] = set()
        for port, ticker in assignments.items():
            if self._is_top_process_active(port):
                self._assignment_failures[port] = 0
                active.add(ticker)
                continue
            failures = self._assignment_failures.get(port, 0) + 1
            self._assignment_failures[port] = failures
            if failures < 3:
                active.add(ticker)
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
        avg_cost = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
        current_price = float(getattr(pos, "current_price", 0.0) or avg_cost or 0.0)
        if avg_cost <= 0 or current_price <= 0:
            return

        engine = self._engine_for_symbol(ticker)
        engine._reconcile_pending_order()
        confirmed = engine._apply_position_sync_fence(quantity)
        if confirmed <= 0:
            return

        report_range = _load_report_exit_range(ticker)
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
