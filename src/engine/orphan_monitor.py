from __future__ import annotations

import logging
import os
import re
import time
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


def _is_equity_position(position: Position) -> bool:
    return bool(EQUITY_SYMBOL_RE.fullmatch(_normalize_ticker(getattr(position, "ticker", ""))))


class OrphanPositionMonitor:
    def __init__(self, broker, poll_interval_seconds: int = 60):
        self.broker = broker
        self.poll_interval_seconds = max(60, int(poll_interval_seconds or 60))
        self._running = False
        self._engines: dict[str, TradingEngine] = {}

    def verify_broker_positions(self) -> list[Position] | None:
        positions = self.broker.get_positions()
        reliable = getattr(self.broker, "is_positions_snapshot_reliable", lambda: True)()
        if not reliable:
            return None
        return list(positions or [])

    def scan_orphans(self, positions: list[Position] | None = None) -> dict[str, Position]:
        assigned = _load_assigned_symbols()
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

    def run(self) -> int:
        positions = self.verify_broker_positions()
        if positions is None:
            logger.error("Orphan monitor aborted: broker positions could not be verified")
            return 1

        orphans = self.scan_orphans(positions)
        self.log_startup_scan(orphans)
        self._running = True

        while self._running:
            engines = [self._engine_for_symbol(symbol) for symbol in orphans.keys()]
            if engines and not engines[0]._is_trading_hours():
                time.sleep(30)
                positions = self.verify_broker_positions()
                orphans = self.scan_orphans(positions)
                continue

            positions = self.verify_broker_positions()
            if positions is None:
                time.sleep(15)
                continue

            orphans = self.scan_orphans(positions)
            for symbol, pos in list(orphans.items()):
                self._evaluate_symbol(symbol, pos)
            time.sleep(self.poll_interval_seconds)
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
            engine = TradingEngine(config, ignore_trading_hours=False)
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
                    "phase": "orphan_stop_loss",
                    "symbol": ticker,
                    "ticker": ticker,
                    "avg_cost": avg_cost,
                    "current_price": current_price,
                    "quantity": confirmed,
                    "mode": "orphan",
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
            reason="stop_loss",
            mode="orphan",
            avg_cost=avg_cost,
            broker_position_verified=True,
            trigger_price=decision["trigger_price"],
            audit_phase="orphan_stop_loss",
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
