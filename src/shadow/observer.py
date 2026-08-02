from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import pandas as pd
from zoneinfo import ZoneInfo

from src.backtest import BacktestPortfolio, StrategyBacktester, compare_versions
from src.backtest.accounting import build_trade_accounting
from src.backtest.benchmarking import validate_benchmark_alignment
from src.backtest.data_feed import infer_bar_frequency
from src.backtest.metrics import compute_backtest_metrics
from src.backtest.models import Bar
from src.dashboard.snapshots import write_dashboard_snapshot
from src.strategy import DynamicRangeCalculator, EntryLayerPlanner, ExitLayerManager, InventoryAwareSizer, TimeStop, TrendGuard
from .config import ShadowRuntimeConfig, ShadowSafetyConfig
from .market_data import ShadowMarketBundle, ShadowMarketDataSource, ShadowMarketDataError
from .models import ShadowBarSnapshot, ShadowOrder, ShadowTrade
from .runtime import ShadowRuntimeStateStore
from .universe import ShadowUniverseConfig


UTC = timezone.utc
US_EASTERN = ZoneInfo("America/New_York")


class ShadowObservationError(RuntimeError):
    pass


def _bar_lookup(bars: Iterable[Bar]) -> dict[datetime, Bar]:
    return {bar.timestamp: bar for bar in bars}


def _round(value: Any, digits: int = 6) -> float | None:
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _to_ohlcv_dict(bar: Bar | None) -> dict[str, Any]:
    if bar is None:
        return {
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
        }
    return {
        "open": _round(bar.open),
        "high": _round(bar.high),
        "low": _round(bar.low),
        "close": _round(bar.close),
        "volume": int(bar.volume),
    }


def _trade_ts(value: dict[str, Any]) -> datetime | None:
    raw = value.get("filled_at") or value.get("timestamp") or value.get("submitted_at")
    if isinstance(raw, datetime):
        return raw.astimezone(UTC) if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _trade_side(trade: dict[str, Any]) -> str:
    return str(trade.get("side") or "").strip().upper()


def _trade_qty(trade: dict[str, Any]) -> int:
    try:
        return int(float(trade.get("filled_quantity") or trade.get("quantity") or 0))
    except Exception:
        return 0


def _trade_price(trade: dict[str, Any]) -> float | None:
    try:
        price = float(trade.get("filled_price") or trade.get("price") or 0.0)
    except Exception:
        return None
    return price if price > 0 else None


def _data_freshness(bar_ts: datetime, now_utc: datetime) -> str:
    age_minutes = max(0.0, (now_utc - bar_ts).total_seconds() / 60.0)
    if age_minutes <= 30.0:
        return "fresh"
    if age_minutes <= 180.0:
        return "stale"
    return "old"


def _gap_status(previous: datetime | None, current: datetime, expected_minutes: int = 15) -> str:
    if previous is None:
        return "ok"
    delta_minutes = (current - previous).total_seconds() / 60.0
    if abs(delta_minutes - expected_minutes) > 0.1:
        return "gap"
    return "ok"


def _build_benchmark_alignment(symbol: str, symbol_bars: list[Bar], benchmark_bars: dict[str, list[Bar]]) -> dict[str, Any]:
    statuses: dict[str, Any] = {}
    valid = True
    for benchmark_symbol, bars in benchmark_bars.items():
        validation = validate_benchmark_alignment(symbol, symbol_bars, bars)
        statuses[benchmark_symbol] = validation.to_dict()
        valid = valid and validation.status == "VALID"
    return {
        "status": "VALID" if valid else "DEGRADED",
        "benchmarks": statuses,
    }


def _replay_timeline(
    *,
    symbol: str,
    bars: list[Bar],
    trades: list[dict[str, Any]],
    initial_cash: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    trades_sorted = sorted(trades, key=lambda item: (_trade_ts(item) or datetime.min.replace(tzinfo=UTC), str(item.get("order_id") or "")))
    trade_index = 0
    open_lots: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    replay_trades: list[dict[str, Any]] = []

    for bar_index, bar in enumerate(bars):
        while trade_index < len(trades_sorted):
            trade = trades_sorted[trade_index]
            trade_ts = _trade_ts(trade)
            if trade_ts is None or trade_ts > bar.timestamp:
                break
            replay_trades.append(trade)
            portfolio.apply_fill(
                {
                    "order_id": trade.get("order_id"),
                    "symbol": trade.get("symbol") or symbol,
                    "side": trade.get("side"),
                    "filled_quantity": trade.get("filled_quantity") or trade.get("quantity"),
                    "filled_price": trade.get("filled_price") or trade.get("price"),
                    "commission": trade.get("commission"),
                    "fees": trade.get("fees"),
                    "submitted_at": trade.get("submitted_at"),
                    "filled_at": trade.get("filled_at"),
                }
            )
            side = _trade_side(trade)
            qty = _trade_qty(trade)
            price = _trade_price(trade) or 0.0
            if side == "BUY" and qty > 0 and price > 0:
                open_lots.append(
                    {
                        "quantity": qty,
                        "price": price,
                        "entry_time": trade_ts or bar.timestamp,
                        "entry_index": bar_index,
                        "order_id": trade.get("order_id"),
                    }
                )
            elif side == "SELL" and qty > 0 and price > 0:
                remaining = qty
                while remaining > 0 and open_lots:
                    lot = open_lots[0]
                    matched = min(remaining, int(lot.get("quantity") or 0))
                    lot["quantity"] = int(lot.get("quantity") or 0) - matched
                    remaining -= matched
                    if lot["quantity"] <= 0:
                        open_lots.pop(0)
                    else:
                        open_lots[0] = lot
            trade_index += 1
        portfolio.mark_to_market({symbol: bar.close}, timestamp=bar.timestamp)
        position = portfolio.get_position(symbol)
        timeline.append(
            {
                "timestamp_utc": bar.timestamp.isoformat(),
                "timestamp_et": bar.timestamp.astimezone(US_EASTERN).isoformat(),
                "cash": round(float(portfolio.available_cash or 0.0), 6),
                "equity": round(float(portfolio.total_equity or 0.0), 6),
                "position_quantity": int(position.quantity),
                "average_cost": _round(position.average_cost),
                "market_value": _round(position.market_value),
                "unrealized_pnl": _round(position.unrealized_pnl),
                "open_layer_count": len(open_lots),
                "open_quantity": sum(int(lot.get("quantity") or 0) for lot in open_lots),
            }
        )
    return timeline, replay_trades


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str))
            handle.write("\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False)


def _dashboard_shadow_datetime(value: Any) -> datetime | None:
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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _dashboard_shadow_blocked_top5(blocked_reason_counts: Counter[str]) -> list[dict[str, Any]]:
    rows = [{"reason": reason, "count": count} for reason, count in blocked_reason_counts.items() if reason]
    rows.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("reason") or "")))
    return rows[:5]


def _dashboard_shadow_status_payload(
    *,
    audit: dict[str, Any],
    comparison_summary: dict[str, Any],
    runtime_state: dict[str, Any],
    signal_rows: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    equity_rows: list[dict[str, Any]],
    blocked_reason_counts: Counter[str],
    output_dir: Path,
) -> dict[str, Any]:
    symbol = str(runtime_state.get("symbol") or comparison_summary.get("symbol") or "SOXS.US").strip().upper() or "SOXS.US"
    timeframe = str(runtime_state.get("timeframe") or comparison_summary.get("timeframe") or runtime_state.get("frequency") or comparison_summary.get("frequency") or "15m").strip().lower() or "15m"
    benchmark_status = str((comparison_summary.get("benchmark_alignment") or {}).get("status") or "unavailable")
    latest_processed_bar_utc = ""
    latest_processed_bar_et = None
    data_freshness = "unavailable"
    latest_ts = _dashboard_shadow_datetime(runtime_state.get("last_processed_timestamp_utc"))
    if latest_ts is not None:
        latest_processed_bar_utc = latest_ts.isoformat()
        latest_processed_bar_et = latest_ts.astimezone(US_EASTERN).isoformat()
        age_minutes = max(0.0, (datetime.now(UTC) - latest_ts).total_seconds() / 60.0)
        if age_minutes <= 45.0:
            data_freshness = "fresh"
        elif age_minutes <= 240.0:
            data_freshness = "stale"
        else:
            data_freshness = "old"

    summary_metrics = list(comparison_summary.get("strategy_metrics") or [])
    eligible_rank = list(comparison_summary.get("eligible_ranking") or [])
    strategy_rank = list(comparison_summary.get("strategy_ranking") or [])
    best_metric = eligible_rank[0] if eligible_rank else (strategy_rank[0] if strategy_rank else (summary_metrics[0] if summary_metrics else {}))
    benchmark_symbol = None
    simulated_return = None
    simulated_drawdown = None
    simulated_equity = None
    open_simulated_positions = None
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
            version_equity_rows = [row for row in equity_rows if str(row.get("version") or "") == best_version]
            if version_equity_rows:
                try:
                    simulated_equity = float(version_equity_rows[-1].get("equity") or 0.0)
                except Exception:
                    simulated_equity = None

    safety_ok = bool(audit.get("ok"))
    quote_api_only = bool(audit.get("quote_api_only"))
    trade_api_used = bool(audit.get("trade_api_used"))
    trade_context_initialized = bool(audit.get("trade_context_initialized"))
    if not safety_ok or not quote_api_only or trade_api_used or trade_context_initialized:
        state = "UNSAFE"
        detail = "安全审计失败"
    elif benchmark_status not in {"VALID", "valid"}:
        state = "UNSAFE"
        detail = "benchmark alignment invalid"
    elif data_freshness in {"stale", "old"}:
        state = "STALE"
        detail = f"shadow data {data_freshness}"
    else:
        state = "SAFE"
        detail = "read-only shadow observer healthy"

    return {
        "ok": state != "UNSAFE",
        "state": state,
        "status_label": state,
        "detail": detail,
        "mode": "READ-ONLY SHADOW",
        "title": str(runtime_state.get("shadow_title") or comparison_summary.get("shadow_title") or f"{symbol.split('.')[0]} Shadow Observer"),
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_family": str(runtime_state.get("strategy_family") or comparison_summary.get("strategy_family") or ""),
        "strategy_version": str(runtime_state.get("strategy_version") or comparison_summary.get("strategy_version") or ""),
        "symbol_class": str(runtime_state.get("symbol_class") or comparison_summary.get("symbol_class") or ""),
        "regular_session_only": bool(runtime_state.get("regular_session_only", comparison_summary.get("regular_session_only", True))),
        "shadow_enabled": bool(runtime_state.get("shadow_enabled", comparison_summary.get("shadow_enabled", True))),
        "trading_enabled": bool(runtime_state.get("trading_enabled", comparison_summary.get("trading_enabled", False))),
        "benchmark_symbols": list(runtime_state.get("benchmark_symbols") or []),
        "output_directory": str(comparison_summary.get("output_dir") or output_dir.name),
        "quote_api_only": quote_api_only,
        "trade_api_used": trade_api_used,
        "trade_context_initialized": trade_context_initialized,
        "last_run_at": str(runtime_state.get("last_run_at") or audit.get("generated_at") or ""),
        "latest_processed_bar_utc": latest_processed_bar_utc,
        "latest_processed_bar_et": latest_processed_bar_et,
        "data_freshness": data_freshness,
        "benchmark_status": benchmark_status,
        "alignment_status": benchmark_status,
        "signals_generated": len(signal_rows),
        "simulated_orders": len(orders),
        "simulated_trades": len(trades),
        "open_simulated_positions": open_simulated_positions if open_simulated_positions is not None else sum(
            1 for row in positions if str(row.get("quantity") or "0").strip() not in {"", "0", "0.0"}
        ),
        "simulated_equity": simulated_equity,
        "simulated_return": simulated_return,
        "simulated_drawdown": simulated_drawdown,
        "blocked_reason_top5": _dashboard_shadow_blocked_top5(blocked_reason_counts),
        "benchmark_sensitive": bool(comparison_summary.get("benchmark_sensitive")),
        "benchmark_symbol": benchmark_symbol,
        "available": True,
        "processed_bar_count": int(runtime_state.get("processed_bar_count") or len(signal_rows)),
    }


class ShadowObserver:
    def __init__(
        self,
        *,
        safety_config: ShadowSafetyConfig,
        runtime_config: ShadowRuntimeConfig,
        market_source: ShadowMarketDataSource | None = None,
        state_store: ShadowRuntimeStateStore | None = None,
    ) -> None:
        self.safety_config = safety_config
        self.runtime_config = runtime_config
        self.market_source = market_source or ShadowMarketDataSource(
            page_size=runtime_config.page_size,
            max_retries=runtime_config.max_retries,
            request_interval_seconds=runtime_config.request_interval_seconds,
            regular_session_only=True,
        )
        self.state_store = state_store or ShadowRuntimeStateStore(runtime_config.output_dir / "runtime_state.json")
        self.dynamic_range = DynamicRangeCalculator()
        self.trend_guard = TrendGuard()
        self.inventory_sizer = InventoryAwareSizer()
        self.entry_planner = EntryLayerPlanner()
        self.exit_manager = ExitLayerManager()
        self.time_stop = TimeStop()

    def _validate_or_raise(self) -> dict[str, Any]:
        universe = self.runtime_config.resolve_universe()
        universe_errors = universe.validate()
        errors = self.safety_config.validate()
        errors.extend(universe_errors)
        audit = {
            "ok": not errors,
            "mode": "shadow",
            "quote_api_only": True,
            "trade_api_used": False,
            "trade_context_initialized": False,
            "symbol": self.runtime_config.symbol,
            "benchmark_symbols": list(self.runtime_config.benchmark_symbols),
            "frequency": self.runtime_config.frequency,
            "output_dir": str(universe.output_dir),
            "shadow_title": universe.display_name,
            "symbol_class": universe.symbol_class,
            "strategy_family": universe.strategy_family,
            "strategy_version": universe.strategy_version,
            "risk_profile": universe.risk_profile,
            "regular_session_only": universe.regular_session_only,
            "shadow_enabled": universe.shadow_enabled,
            "trading_enabled": universe.trading_enabled,
            "safety": self.safety_config.to_audit_dict(),
            "errors": errors,
            "universe": universe.to_dict(),
        }
        if errors:
            raise ShadowObservationError(", ".join(errors))
        return audit

    def run_once(self) -> dict[str, Any]:
        universe = self.runtime_config.resolve_universe()
        audit = self._validate_or_raise()
        output_dir = universe.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle = self.market_source.fetch_bundle(
            symbol=universe.symbol,
            benchmark_symbols=universe.benchmark_symbols,
            frequency=universe.frequency,
            lookback_days=self.runtime_config.lookback_days,
        )
        if not bundle.symbol_bars:
            audit["errors"] = ["symbol_data_missing"]
            audit["ok"] = False
            raise ShadowObservationError("symbol_data_missing")

        benchmark_alignment = _build_benchmark_alignment(bundle.symbol, bundle.symbol_bars, bundle.benchmark_bars)
        if benchmark_alignment["status"] != "VALID":
            audit["errors"] = ["benchmark_alignment_invalid"]
            audit["ok"] = False
            raise ShadowObservationError("benchmark_alignment_invalid")

        benchmark_symbols = list(bundle.benchmark_symbols)
        soxx_symbol = benchmark_symbols[0] if benchmark_symbols else ""
        smh_symbol = benchmark_symbols[1] if len(benchmark_symbols) > 1 else ""

        comparison_soxx = compare_versions(
            bundle.symbol_bars,
            symbol=bundle.symbol,
            benchmark_bars=bundle.benchmark_bars.get(soxx_symbol),
            versions=("baseline", "a", "b", "c"),
            initial_cash=self.runtime_config.initial_cash,
            parameter_set={},
            output_dir=None,
        )
        comparison_smh = compare_versions(
            bundle.symbol_bars,
            symbol=bundle.symbol,
            benchmark_bars=bundle.benchmark_bars.get(smh_symbol),
            versions=("c",),
            initial_cash=self.runtime_config.initial_cash,
            parameter_set={},
            output_dir=None,
        )

        version_rows: list[dict[str, Any]] = []
        comparison_rows: list[dict[str, Any]] = []
        for row in comparison_soxx.comparison:
            row = dict(row)
            row["version"] = {
                "baseline": "baseline",
                "a": "version_a",
                "b": "version_b",
                "c": "version_c_soxx",
            }.get(str(row.get("version") or ""), str(row.get("version") or ""))
            row["benchmark_symbol"] = soxx_symbol
            version_rows.append({k: v for k, v in row.items() if k not in {"trades", "orders", "equity_curve", "drawdown_curve", "rejected_signals"}})
            comparison_rows.append(row)
        for row in comparison_smh.comparison:
            row = dict(row)
            row["version"] = "version_c_smh"
            row["benchmark_symbol"] = smh_symbol
            version_rows.append({k: v for k, v in row.items() if k not in {"trades", "orders", "equity_curve", "drawdown_curve", "rejected_signals"}})
            comparison_rows.append(row)

        version_lookup = {row["version"]: row for row in comparison_rows}
        market_history = bundle.symbol_bars
        benchmark_histories = bundle.benchmark_bars
        benchmarks_by_ts = {symbol: _bar_lookup(bars) for symbol, bars in benchmark_histories.items()}
        events: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        signal_rows: list[dict[str, Any]] = []
        blocked_reason_counts: Counter[str] = Counter()
        blocked_reason_by_strategy: Counter[tuple[str, str]] = Counter()
        daily_summary: dict[tuple[str, str], dict[str, Any]] = {}
        replay_trades_by_version: dict[str, list[dict[str, Any]]] = {}

        for version_name, row in version_lookup.items():
            raw_orders = [dict(item) for item in (row.get("orders") or [])]
            raw_trades = [dict(item) for item in (row.get("trades") or [])]
            for order in raw_orders:
                ts = _trade_ts(order)
                if ts is not None and self.state_store.already_processed(
                    {
                        "timestamp_utc": ts.isoformat(),
                        "strategy_version": version_name,
                    }
                ):
                    continue
                order["simulated"] = True
                order["strategy_version"] = version_name
                orders.append(order)
            for trade in raw_trades:
                ts = _trade_ts(trade)
                if ts is not None and self.state_store.already_processed(
                    {
                        "timestamp_utc": ts.isoformat(),
                        "strategy_version": version_name,
                    }
                ):
                    continue
                trade["simulated"] = True
                trade["strategy_version"] = version_name
                trades.append(trade)
            replay_timeline, replay_trades = _replay_timeline(
                symbol=bundle.symbol,
                bars=market_history,
                trades=raw_trades,
                initial_cash=self.runtime_config.initial_cash,
            )
            replay_trades_by_version[version_name] = replay_trades
            trade_lookup: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            order_lookup: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            reject_lookup: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            for trade in raw_trades:
                ts = _trade_ts(trade)
                if ts is not None:
                    trade_lookup[ts].append(trade)
            for order in raw_orders:
                ts = _trade_ts(order)
                if ts is not None:
                    order_lookup[ts].append(order)
            for reject in row.get("rejected_signals") or []:
                raw_ts = reject.get("timestamp")
                if isinstance(raw_ts, datetime):
                    ts = raw_ts.astimezone(UTC) if raw_ts.tzinfo else raw_ts.replace(tzinfo=UTC)
                else:
                    text = str(raw_ts or "").strip()
                    try:
                        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
                        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
                    except Exception:
                        ts = None
                if ts is not None:
                    reject_lookup[ts].append(reject)
                    reason = str(reject.get("reason") or reject.get("reject_reason") or "unknown")
                    blocked_reason_counts[reason] += 1
                    blocked_reason_by_strategy[(version_name, reason)] += 1

            benchmark_key = soxx_symbol if version_name != "version_c_smh" else smh_symbol
            benchmark_lookup = benchmarks_by_ts.get(benchmark_key, {})
            benchmark_validation = benchmark_alignment["benchmarks"].get(benchmark_key, {})
            previous_ts: datetime | None = None
            now_utc = datetime.now(timezone.utc)
            open_lot_count = 0
            for idx, bar in enumerate(market_history):
                if self.state_store.already_processed(
                    {
                        "timestamp_utc": bar.timestamp.isoformat(),
                        "strategy_version": version_name,
                    }
                ):
                    continue
                bar_benchmark = benchmark_lookup.get(bar.timestamp)
                range_snapshot = self.dynamic_range.calculate(
                    timestamp=bar.timestamp,
                    current_price=bar.close,
                    highs=[item.high for item in market_history[: idx + 1]],
                    lows=[item.low for item in market_history[: idx + 1]],
                    closes=[item.close for item in market_history[: idx + 1]],
                )
                benchmark_series = [item.close for item in benchmark_histories.get(benchmark_key, []) if item.timestamp <= bar.timestamp]
                trend_result = self.trend_guard.evaluate(
                    timestamp=bar.timestamp,
                    current_price=bar.close,
                    closes=[item.close for item in market_history[: idx + 1]],
                    highs=[item.high for item in market_history[: idx + 1]],
                    lows=[item.low for item in market_history[: idx + 1]],
                    benchmark_closes=benchmark_series,
                    symbol=bundle.symbol,
                )
                replay_point = replay_timeline[idx] if idx < len(replay_timeline) else {
                    "cash": self.runtime_config.initial_cash,
                    "equity": self.runtime_config.initial_cash,
                    "position_quantity": 0,
                    "open_layer_count": 0,
                    "open_quantity": 0,
                }
                orders_at_ts = order_lookup.get(bar.timestamp, [])
                trades_at_ts = trade_lookup.get(bar.timestamp, [])
                rejects_at_ts = reject_lookup.get(bar.timestamp, [])
                if orders_at_ts:
                    signal = str(orders_at_ts[0].get("side") or "HOLD").upper()
                    intended_action = signal
                    intended_quantity = int(orders_at_ts[0].get("quantity") or orders_at_ts[0].get("requested_quantity") or 0)
                    fill_price = _trade_price(trades_at_ts[0]) if trades_at_ts else None
                elif rejects_at_ts:
                    signal = "BLOCKED"
                    intended_action = "BLOCKED"
                    intended_quantity = 0
                    fill_price = _trade_price(trades_at_ts[0]) if trades_at_ts else None
                else:
                    signal = "HOLD"
                    intended_action = "HOLD"
                    intended_quantity = 0
                    fill_price = None
                blocked_reason = ";".join(
                    str(item.get("reason") or item.get("reject_reason") or "unknown") for item in rejects_at_ts
                )
                time_stop_state = "inactive"
                if version_name.startswith("version_c") and int(replay_point.get("position_quantity") or 0) > 0:
                    first_trade = next((trade for trade in raw_trades if _trade_side(trade) == "BUY"), None)
                    if first_trade is not None:
                        entry_ts = _trade_ts(first_trade)
                        if entry_ts is not None:
                            time_stop_result = self.time_stop.evaluate(
                                symbol=bundle.symbol,
                                entry_time=entry_ts,
                                current_time=bar.timestamp,
                                holding_bars=idx,
                                holding_minutes=int((bar.timestamp - entry_ts).total_seconds() // 60),
                                leveraged_etf=bundle.symbol.startswith(("SOXS", "SOXL", "LABD", "DRIP", "YINN")),
                            )
                            time_stop_state = "triggered" if time_stop_result.get("triggered") else "armed"
                row_snapshot = ShadowBarSnapshot(
                    strategy_version=version_name,
                    timestamp_utc=bar.timestamp.isoformat(),
                    timestamp_et=bar.timestamp.astimezone(US_EASTERN).isoformat(),
                    symbol=bundle.symbol,
                    symbol_ohlcv=_to_ohlcv_dict(bar),
                    benchmarks={
                        key: _to_ohlcv_dict(bench_lookup.get(bar.timestamp))
                        for key, bench_lookup in benchmarks_by_ts.items()
                    },
                    benchmark_alignment_status=benchmark_validation.get("status", "UNKNOWN"),
                    signal=signal,
                    intended_action=intended_action,
                    intended_quantity=intended_quantity,
                    simulated_fill_price=fill_price,
                    simulated_position=int(replay_point.get("position_quantity") or 0),
                    simulated_cash=float(replay_point.get("cash") or 0.0),
                    simulated_equity=float(replay_point.get("equity") or 0.0),
                    blocked_reason=blocked_reason,
                    range_width=_round(range_snapshot.get("range_width_pct")),
                    layer_count=int(replay_point.get("open_layer_count") or 0),
                    trend_regime=str(trend_result.get("regime") or "UNKNOWN"),
                    time_stop_state=time_stop_state,
                    data_freshness=_data_freshness(bar.timestamp, now_utc),
                    data_gap_status=_gap_status(previous_ts, bar.timestamp),
                )
                payload = row_snapshot.to_dict()
                payload["benchmark_alignment"] = benchmark_validation
                payload["trade_count"] = int(row.get("trade_count") or 0)
                payload["closed_trade_count"] = int(row.get("closed_trade_count") or 0)
                payload["profitability_status"] = row.get("profitability_status")
                payload["deployment_status"] = row.get("deployment_status")
                payload["evidence_status"] = row.get("evidence_status")
                events.append(payload)
                signal_rows.append(
                    {
                        "timestamp_utc": bar.timestamp.isoformat(),
                        "timestamp_et": bar.timestamp.astimezone(US_EASTERN).isoformat(),
                        "version": version_name,
                        "signal": signal,
                        "intended_action": intended_action,
                        "intended_quantity": intended_quantity,
                        "blocked_reason": blocked_reason,
                        "trend_regime": row_snapshot.trend_regime,
                        "range_width": row_snapshot.range_width,
                        "time_stop_state": time_stop_state,
                        "benchmark_alignment_status": row_snapshot.benchmark_alignment_status,
                    }
                )
                positions.append(
                    {
                        "timestamp_utc": bar.timestamp.isoformat(),
                        "timestamp_et": bar.timestamp.astimezone(US_EASTERN).isoformat(),
                        "version": version_name,
                        "symbol": bundle.symbol,
                        "quantity": int(replay_point.get("position_quantity") or 0),
                        "average_cost": replay_point.get("average_cost"),
                        "market_value": replay_point.get("market_value"),
                        "unrealized_pnl": replay_point.get("unrealized_pnl"),
                        "cash": replay_point.get("cash"),
                        "equity": replay_point.get("equity"),
                    }
                )
                equity_rows.append(
                    {
                        "timestamp_utc": bar.timestamp.isoformat(),
                        "timestamp_et": bar.timestamp.astimezone(US_EASTERN).isoformat(),
                        "version": version_name,
                        "symbol": bundle.symbol,
                        "cash": replay_point.get("cash"),
                        "equity": replay_point.get("equity"),
                        "position_quantity": replay_point.get("position_quantity"),
                        "open_layer_count": replay_point.get("open_layer_count"),
                    }
                )
                if previous_ts is not None:
                    delta = bar.timestamp - previous_ts
                    if abs(delta.total_seconds() - 15 * 60) > 1:
                        key = (bar.timestamp.date().isoformat(), version_name)
                        daily_summary.setdefault(
                            key,
                            {
                                "date": bar.timestamp.date().isoformat(),
                                "version": version_name,
                                "bars_received": 0,
                                "missing_bars": 0,
                                "stale_data_events": 0,
                                "benchmark_mismatch_count": 0,
                                "signals_generated": 0,
                                "simulated_orders": 0,
                                "simulated_fills": 0,
                                "closed_trades": 0,
                                "open_positions": 0,
                                "simulated_return": 0.0,
                                "simulated_drawdown": 0.0,
                            },
                        )
                        daily_summary[key]["missing_bars"] += 1
                previous_ts = bar.timestamp
                key = (bar.timestamp.date().isoformat(), version_name)
                summary_row = daily_summary.setdefault(
                    key,
                    {
                        "date": bar.timestamp.date().isoformat(),
                        "version": version_name,
                        "bars_received": 0,
                        "missing_bars": 0,
                        "stale_data_events": 0,
                        "benchmark_mismatch_count": 0,
                        "signals_generated": 0,
                        "simulated_orders": 0,
                        "simulated_fills": 0,
                        "closed_trades": 0,
                        "open_positions": 0,
                        "simulated_return": 0.0,
                        "simulated_drawdown": 0.0,
                    },
                )
                summary_row["bars_received"] += 1
                summary_row["stale_data_events"] += 1 if row_snapshot.data_freshness != "fresh" else 0
                summary_row["benchmark_mismatch_count"] += 0 if row_snapshot.benchmark_alignment_status == "VALID" else 1
                summary_row["signals_generated"] += 1 if signal != "HOLD" else 0
                summary_row["simulated_orders"] += len(orders_at_ts)
                summary_row["simulated_fills"] += len(trades_at_ts)
                summary_row["closed_trades"] += 1 if any(str(t.get("status") or "").upper() == "FILLED" for t in trades_at_ts) else 0
                summary_row["open_positions"] = int(replay_point.get("position_quantity") or 0)
                summary_row["simulated_return"] = round((float(replay_point.get("equity") or self.runtime_config.initial_cash) - self.runtime_config.initial_cash) / self.runtime_config.initial_cash, 6)
                summary_row["simulated_drawdown"] = round(max(0.0, self.runtime_config.initial_cash - float(replay_point.get("equity") or self.runtime_config.initial_cash)), 6)

        version_metrics: list[dict[str, Any]] = []
        for version_name, row in version_lookup.items():
            metrics = dict(row)
            metrics["version"] = version_name
            metrics["benchmark_symbol"] = soxx_symbol if version_name != "version_c_smh" else smh_symbol
            metrics["simulated"] = True
            version_metrics.append(metrics)

        c_soxx = next((row for row in version_metrics if row["version"] == "version_c_soxx"), None)
        c_smh = next((row for row in version_metrics if row["version"] == "version_c_smh"), None)
        benchmark_sensitive = False
        if c_soxx and c_smh:
            benchmark_sensitive = any(
                [
                    abs(float(c_soxx.get("total_return") or 0.0) - float(c_smh.get("total_return") or 0.0)) > 0.02,
                    abs(float(c_soxx.get("max_drawdown") or 0.0) - float(c_smh.get("max_drawdown") or 0.0)) > 0.02,
                    int(c_soxx.get("trade_count") or 0) != int(c_smh.get("trade_count") or 0),
                    (float(c_soxx.get("total_return") or 0.0) > 0) != (float(c_smh.get("total_return") or 0.0) > 0),
                ]
            )

        comparison_summary = {
            "symbol": bundle.symbol,
            "frequency": bundle.frequency,
            "timeframe": universe.timeframe,
            "shadow_title": universe.display_name,
            "symbol_class": universe.symbol_class,
            "strategy_family": universe.strategy_family,
            "strategy_version": universe.strategy_version,
            "regular_session_only": universe.regular_session_only,
            "shadow_enabled": universe.shadow_enabled,
            "trading_enabled": universe.trading_enabled,
            "shadow_universe": universe.to_dict(),
            "lookback_days": self.runtime_config.lookback_days,
            "output_dir": output_dir.name,
            "output_dir_path": str(output_dir),
            "market_bundle": bundle.to_dict(),
            "benchmark_alignment": benchmark_alignment,
            "safety_audit": audit,
            "strategy_metrics": version_metrics,
            "benchmark_sensitive": benchmark_sensitive,
            "quote_api_only": True,
            "trade_api_used": False,
            "deployment_eligible": False,
        }

        ranking_rows = sorted(
            version_metrics,
            key=lambda item: (
                -float(item.get("risk_adjusted_score") or 0.0),
                str(item.get("version") or ""),
            ),
        )
        comparison_summary["strategy_ranking"] = ranking_rows
        comparison_summary["eligible_ranking"] = [
            row for row in ranking_rows if str(row.get("evidence_status") or "").upper() == "ELIGIBLE"
        ]
        comparison_summary["insufficient_evidence"] = [
            row for row in ranking_rows if str(row.get("evidence_status") or "").upper() != "ELIGIBLE"
        ]

        _write_jsonl(output_dir / "shadow_events.jsonl", events)
        _write_csv(output_dir / "shadow_signals.csv", signal_rows)
        _write_csv(output_dir / "shadow_simulated_orders.csv", [dict(item, simulated=True) for item in orders])
        _write_csv(output_dir / "shadow_simulated_trades.csv", [dict(item, simulated=True) for item in trades])
        _write_csv(output_dir / "shadow_positions.csv", positions)
        _write_csv(output_dir / "shadow_equity.csv", equity_rows)
        _write_csv(
            output_dir / "blocked_reason_counts.csv",
            [{"reason": reason, "count": count} for reason, count in sorted(blocked_reason_counts.items(), key=lambda item: (-item[1], item[0]))],
        )
        _write_csv(
            output_dir / "blocked_reason_by_strategy.csv",
            [
                {"version": version, "reason": reason, "count": count}
                for (version, reason), count in sorted(blocked_reason_by_strategy.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
            ],
        )
        _write_csv(output_dir / "daily_summary.csv", list(daily_summary.values()))
        (output_dir / "comparison_summary.json").write_text(
            json.dumps(comparison_summary, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        (output_dir / "safety_audit.json").write_text(
            json.dumps(
                {
                    **audit,
                    "benchmark_alignment": benchmark_alignment,
                    "output_files": {
                        "shadow_events": "shadow_events.jsonl",
                        "shadow_signals": "shadow_signals.csv",
                        "shadow_simulated_orders": "shadow_simulated_orders.csv",
                        "shadow_simulated_trades": "shadow_simulated_trades.csv",
                        "shadow_positions": "shadow_positions.csv",
                        "shadow_equity": "shadow_equity.csv",
                        "blocked_reason_counts": "blocked_reason_counts.csv",
                        "blocked_reason_by_strategy": "blocked_reason_by_strategy.csv",
                        "daily_summary": "daily_summary.csv",
                    },
                    "trade_api_used": False,
                    "quote_api_only": True,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        runtime_state = self.state_store.mark_processed(
            {
                "timestamp_utc": row["timestamp_utc"],
                "strategy_version": row["strategy_version"],
            }
            for row in events
        )
        runtime_state.update(
            {
                "mode": "shadow",
                "symbol": bundle.symbol,
                "frequency": bundle.frequency,
                "timeframe": universe.timeframe,
                "shadow_title": universe.display_name,
                "symbol_class": universe.symbol_class,
                "strategy_family": universe.strategy_family,
                "strategy_version": universe.strategy_version,
                "risk_profile": universe.risk_profile,
                "regular_session_only": universe.regular_session_only,
                "shadow_enabled": universe.shadow_enabled,
                "trading_enabled": universe.trading_enabled,
                "benchmark_symbols": list(bundle.benchmark_symbols),
                "last_bundle": bundle.to_dict(),
                "benchmark_sensitive": benchmark_sensitive,
                "strategy_metrics_count": len(version_metrics),
                "event_count": len(events),
            }
        )
        self.state_store.save(runtime_state)
        (output_dir / "runtime_state.json").write_text(
            json.dumps(runtime_state, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        try:
            write_dashboard_snapshot(
                "shadow_status",
                _dashboard_shadow_status_payload(
                    audit=audit,
                    comparison_summary=comparison_summary,
                    runtime_state=runtime_state,
                    signal_rows=signal_rows,
                    orders=orders,
                    trades=trades,
                    positions=positions,
                    equity_rows=equity_rows,
                    blocked_reason_counts=blocked_reason_counts,
                    output_dir=output_dir,
                ),
                source_run_id=str(runtime_state.get("last_processed_timestamp_utc") or ""),
                generated_at=runtime_state.get("last_run_at"),
            )
        except Exception as exc:
            print(f"Warning: failed to write shadow status dashboard snapshot: {exc}")
        return comparison_summary
