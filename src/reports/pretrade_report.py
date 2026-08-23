"""
Pretrade Dry Run Report: snapshot of system state before a live trading
session begins.

Generated once at TradingEngine startup (live mode) and written to
``reports/pretrade_check_YYYYMMDD.json``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from src.openalpha.selection_report import load_latest_ai_selection_state
from src.config.runtime_paths import resolve_reports_dir

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
REPORTS_DIR = resolve_reports_dir(PROJECT_DIR)


@dataclass
class PretradeReport:
    """Aggregate pre-trade snapshot that is serialised to JSON."""

    today: str = ""
    mode: str = ""
    account_equity: float = 0.0
    account_cash: float = 0.0
    top_configs: list[dict[str, Any]] = field(default_factory=list)
    ai_selection_date: str = ""
    protected_positions: list[dict[str, Any]] = field(default_factory=list)
    orphan_positions: list[dict[str, Any]] = field(default_factory=list)
    reduce_only_symbols: list[str] = field(default_factory=list)
    max_position_by_symbol: dict[str, int] = field(default_factory=dict)
    max_loss_by_symbol: dict[str, float] = field(default_factory=dict)
    allowed_to_open_new_positions: bool = False
    allowed_reduce_only: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def generate(cls, context: dict[str, Any] | None = None) -> PretradeReport:
        """Build a PretradeReport from the current system state.

        *context* may contain:
            - ``mode``: trading mode string
            - ``broker``: broker instance
            - ``live_guard_verdict``: result of LiveGuard.validate_live_start()
            - ``reduce_only_symbols``: list of tickers forced to reduce-only
        """
        ctx = dict(context or {})
        now = datetime.now()

        report = cls(today=now.date().isoformat(), mode=str(ctx.get("mode", "") or ""))

        # Account
        broker = ctx.get("broker")
        if broker is not None:
            try:
                acct = broker.get_account()
                if acct is not None:
                    report.account_equity = float(getattr(acct, "equity", 0.0) or 0.0)
                    report.account_cash = float(getattr(acct, "cash", 0.0) or 0.0)
            except Exception as exc:
                report.errors.append(f"account_fetch:{exc}")

        # TOP configs & max position / loss
        top_dir = PROJECT_DIR / "configs"
        for idx in range(1, 6):
            path = top_dir / f"TOP{idx}.yaml"
            if not path.exists():
                continue
            try:
                import yaml
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            ticker = str(data.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            sel = data.get("selection") or {}
            rk = data.get("risk") or {}
            pos_cfg = data.get("position") or {}
            report.top_configs.append({
                "rank": idx,
                "ticker": ticker,
                "selection_date": str(sel.get("selection_date") or ""),
                "score": sel.get("score"),
                "mode": str(data.get("mode") or ""),
                "size_per_trade": pos_cfg.get("size_per_trade"),
                "max_position": pos_cfg.get("max_position"),
                "stop_loss_pct": rk.get("stop_loss_pct"),
                "daily_loss_limit": rk.get("daily_loss_limit"),
            })
            report.max_position_by_symbol[ticker] = int(pos_cfg.get("max_position", 0) or 0)
            report.max_loss_by_symbol[ticker] = float(rk.get("daily_loss_limit", 0.0) or 0.0)

        # AI selection date
        ai_data = load_latest_ai_selection_state(PROJECT_DIR)
        if isinstance(ai_data, dict):
            report.ai_selection_date = str(ai_data.get("selection_date") or "")
            for item in ai_data.get("protected_positions") or []:
                if isinstance(item, dict):
                    report.protected_positions.append({
                        "ticker": str(item.get("ticker") or "").upper(),
                        "reduce_only": bool(item.get("reduce_only", False)),
                        "range_low": item.get("range_low"),
                        "range_high": item.get("range_high"),
                    })

        # Orphan positions from broker
        if broker is not None:
            try:
                positions = broker.get_positions()
                if positions:
                    top_symbols = {c["ticker"] for c in report.top_configs}
                    for pos in positions:
                        ticker = str(getattr(pos, "ticker", "") or "").strip().upper()
                        qty = int(getattr(pos, "quantity", 0) or 0)
                        if ticker and qty > 0 and ticker not in top_symbols:
                            report.orphan_positions.append({
                                "ticker": ticker,
                                "quantity": qty,
                                "avg_entry_price": float(getattr(pos, "avg_entry_price", 0.0) or 0.0),
                            })
            except Exception:
                pass

        # Reduce-only symbols
        report.reduce_only_symbols = list(ctx.get("reduce_only_symbols") or [])

        # LiveGuard verdict
        verdict = ctx.get("live_guard_verdict")
        if isinstance(verdict, dict):
            report.allowed_to_open_new_positions = bool(verdict.get("allowed_to_open_new_positions", False))
            report.allowed_reduce_only = bool(verdict.get("allowed_reduce_only", True))
            report.errors.extend(list(verdict.get("errors") or []))
            report.warnings.extend(list(verdict.get("warnings") or []))

        return report

    def write(self, reports_dir: Path | None = None) -> Path:
        """Write the report to disk (JSON)."""
        target = (reports_dir or REPORTS_DIR) / f"pretrade_check_{self.today}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Pretrade report written to %s", target)
        return target
