from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.backtest.models import Bar
from src.shadow import ShadowMarketDataSource, ShadowObserver, ShadowObservationError, ShadowRuntimeConfig, ShadowRuntimeStateStore, ShadowSafetyConfig
import src.shadow.universe as shadow_universe


def _bar(symbol: str, ts: datetime, base: float, index: int) -> Bar:
    price = base + index * 0.1
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=round(price, 4),
        high=round(price + 0.2, 4),
        low=round(price - 0.2, 4),
        close=round(price + 0.05, 4),
        volume=1000 + index,
        source="shadow_test",
    )


class FakeQuoteContext:
    def __init__(self, config, pages):
        self.config = config
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def history_candlesticks_by_offset(self, **kwargs):
        self.calls.append(dict(kwargs))
        symbol = str(kwargs.get("symbol") or "").upper()
        period = str(kwargs.get("period") or "").lower()
        offset = int(kwargs.get("offset") or 0)
        return {"candlesticks": self.pages.get((symbol, period, offset), [])}


def _install_fake_longbridge(monkeypatch, pages):
    fake_module = types.SimpleNamespace()
    fake_module.AdjustType = types.SimpleNamespace(ForwardAdjust="ForwardAdjust", NoAdjust="NoAdjust")
    fake_module.TradeSessions = types.SimpleNamespace(All="All", Intraday="Intraday")
    fake_module.TradeSession = types.SimpleNamespace(Normal="Normal")

    class _Config:
        @classmethod
        def from_apikey(cls, *args, **kwargs):
            return {"args": args, "kwargs": kwargs}

    fake_module.Config = _Config
    fake_module.QuoteContext = lambda config: FakeQuoteContext(config, pages)

    class _TradeContext:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("TradeContext must not be created in shadow mode")

    fake_module.TradeContext = _TradeContext
    monkeypatch.setitem(sys.modules, "longbridge", types.SimpleNamespace(openapi=fake_module))
    monkeypatch.setitem(sys.modules, "longbridge.openapi", fake_module)
    return fake_module


def _prepare_pages() -> dict[tuple[str, str, int], list[dict[str, object]]]:
    pages: dict[tuple[str, str, int], list[dict[str, object]]] = {}
    symbols = ["SOXS.US", "SOXX.US", "SMH.US"]
    start = datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=15)
    for symbol in symbols:
        page0 = []
        page1 = []
        for idx in range(4):
            page0.append(
                {
                    "symbol": symbol,
                    "timestamp": (start + step * idx).isoformat(),
                    "open": 10 + idx,
                    "high": 10.2 + idx,
                    "low": 9.8 + idx,
                    "close": 10.05 + idx,
                    "volume": 1000 + idx,
                    "turnover": 10000 + idx,
                }
            )
            page1.append(
                {
                    "symbol": symbol,
                    "timestamp": (start + step * (idx + 4)).isoformat(),
                    "open": 14 + idx,
                    "high": 14.2 + idx,
                    "low": 13.8 + idx,
                    "close": 14.05 + idx,
                    "volume": 1004 + idx,
                    "turnover": 12000 + idx,
                }
            )
        pages[(symbol, "15m", 0)] = list(reversed(page0))
        pages[(symbol, "15m", 4)] = list(reversed(page1))
    return pages


def _shadow_env(monkeypatch):
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("ORDER_SUBMISSION_ENABLED", "false")
    monkeypatch.setenv("BROKER_WRITE_ENABLED", "false")
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "key")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "secret")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "token")

def _shadow_sandbox_root(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    shadow_root = project_dir / "artifacts" / "shadow"
    shadow_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(shadow_universe, "PROJECT_DIR", project_dir)
    monkeypatch.setattr(shadow_universe, "SHADOW_ROOT_DIR", shadow_root)
    return project_dir, shadow_root


def test_shadow_safety_config_fail_closed(monkeypatch):
    monkeypatch.delenv("SHADOW_MODE", raising=False)
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("ORDER_SUBMISSION_ENABLED", "false")
    monkeypatch.setenv("BROKER_WRITE_ENABLED", "false")
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "key")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "secret")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "token")
    config = ShadowSafetyConfig.from_env()
    errors = config.validate()
    assert errors and "SHADOW_MODE" in errors[0]


def test_shadow_mode_uses_quote_only_and_writes_outputs(monkeypatch, tmp_path):
    pages = _prepare_pages()
    fake_module = _install_fake_longbridge(monkeypatch, pages)
    _shadow_env(monkeypatch)
    _project_dir, _shadow_root = _shadow_sandbox_root(monkeypatch, tmp_path)
    runtime_output_dir = Path("artifacts/shadow/test_shadow_runtime")
    runtime = ShadowRuntimeConfig(
        output_dir=runtime_output_dir,
        symbol="SOXS.US",
        benchmark_symbols=("SOXX.US", "SMH.US"),
        frequency="15m",
        lookback_days=30,
        initial_cash=10_000.0,
        page_size=4,
        max_retries=3,
        request_interval_seconds=0.0,
        poll_interval_seconds=1.0,
        run_once=True,
    )
    observer = ShadowObserver(
        safety_config=ShadowSafetyConfig.from_env(),
        runtime_config=runtime,
        market_source=ShadowMarketDataSource(page_size=4, max_retries=3, request_interval_seconds=0.0),
        state_store=ShadowRuntimeStateStore(runtime.output_dir / "runtime_state.json"),
    )
    result = observer.run_once()
    assert result["quote_api_only"] is True
    assert result["trade_api_used"] is False
    assert result["deployment_eligible"] is False
    assert result["strategy_metrics"]
    assert (runtime_output_dir / "shadow_events.jsonl").exists()
    assert (runtime_output_dir / "shadow_simulated_orders.csv").exists()
    assert (runtime_output_dir / "shadow_simulated_trades.csv").exists()
    assert (runtime_output_dir / "runtime_state.json").exists()
    audit = json.loads((runtime_output_dir / "safety_audit.json").read_text(encoding="utf-8"))
    assert audit["quote_api_only"] is True
    assert audit["trade_api_used"] is False
    assert fake_module.QuoteContext({"x": 1}).calls == []


def test_shadow_restart_skips_duplicate_bars(monkeypatch, tmp_path):
    pages = _prepare_pages()
    _install_fake_longbridge(monkeypatch, pages)
    _shadow_env(monkeypatch)
    _project_dir, _shadow_root = _shadow_sandbox_root(monkeypatch, tmp_path)
    runtime = ShadowRuntimeConfig(
        output_dir=Path("artifacts/shadow/test_shadow_restart"),
        symbol="SOXS.US",
        benchmark_symbols=("SOXX.US", "SMH.US"),
        frequency="15m",
        lookback_days=30,
        initial_cash=10_000.0,
        page_size=4,
        max_retries=3,
        request_interval_seconds=0.0,
        poll_interval_seconds=1.0,
        run_once=True,
    )
    state_store = ShadowRuntimeStateStore(runtime.output_dir / "runtime_state.json")
    observer = ShadowObserver(
        safety_config=ShadowSafetyConfig.from_env(),
        runtime_config=runtime,
        market_source=ShadowMarketDataSource(page_size=4, max_retries=3, request_interval_seconds=0.0),
        state_store=state_store,
    )
    first = observer.run_once()
    state_after_first = json.loads((runtime.output_dir / "runtime_state.json").read_text(encoding="utf-8"))
    second = observer.run_once()
    assert second["quote_api_only"] is True
    state_after_second = json.loads((runtime.output_dir / "runtime_state.json").read_text(encoding="utf-8"))
    assert state_after_first["processed_bar_count"] == state_after_second["processed_bar_count"]
    assert state_after_first["last_processed_timestamp_utc"] == state_after_second["last_processed_timestamp_utc"]


def test_shadow_config_refuses_invalid_flags(monkeypatch):
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("ORDER_SUBMISSION_ENABLED", "false")
    monkeypatch.setenv("BROKER_WRITE_ENABLED", "false")
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "key")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "secret")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "token")
    config = ShadowSafetyConfig.from_env()
    errors = config.validate()
    assert any("TRADING_ENABLED" in error for error in errors)


def test_shadow_runtime_config_derives_universe_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("SOXS_SHADOW_SYMBOL", "AAPL")
    monkeypatch.setenv("SOXS_SHADOW_BENCHMARKS", "QQQ.US,SPY.US")
    monkeypatch.setenv("SOXS_SHADOW_TIMEFRAME", "15m")
    monkeypatch.setenv("SOXS_SHADOW_STRATEGY_VERSION", "baseline")
    monkeypatch.setenv("SOXS_SHADOW_STRATEGY_FAMILY", "equity_mean_reversion")
    project_dir, shadow_root = _shadow_sandbox_root(monkeypatch, tmp_path)
    monkeypatch.setenv("SOXS_SHADOW_OUTPUT_DIR", "artifacts/shadow/aapl_15m")
    monkeypatch.setenv("SOXS_SHADOW_ENABLED", "true")
    monkeypatch.setenv("SOXS_TRADING_ENABLED", "false")
    monkeypatch.setenv("SOXS_SHADOW_REGULAR_SESSION_ONLY", "true")
    runtime = ShadowRuntimeConfig.from_env()
    universe = runtime.resolve_universe()
    assert runtime.symbol == "AAPL.US"
    assert runtime.benchmark_symbols == ("QQQ.US", "SPY.US")
    assert runtime.shadow_title == "AAPL Shadow Observer"
    assert universe.validate() == []
