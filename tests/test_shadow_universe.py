from __future__ import annotations

from pathlib import Path

from src.shadow.universe import (
    ShadowUniverseConfig,
    default_benchmarks_for,
    default_shadow_output_directory,
    shadow_title_for,
)


def test_shadow_universe_defaults_preserve_soxs_behavior():
    config = ShadowUniverseConfig.for_symbol("SOXS.US")
    assert config.symbol == "SOXS.US"
    assert config.benchmarks == ("SOXX.US", "SMH.US")
    assert config.timeframe == "15m"
    assert config.symbol_class == "inverse_etf"
    assert config.strategy_family == "range_etf"
    assert config.output_directory == Path("artifacts/shadow/soxs_15m")
    assert config.display_name == "SOXS Shadow Observer"
    assert config.validate() == []


def test_shadow_universe_supports_other_symbols_with_default_catalog():
    config = ShadowUniverseConfig.for_symbol("AAPL.US")
    assert config.symbol == "AAPL.US"
    assert config.benchmarks == ("QQQ.US", "SPY.US")
    assert config.symbol_class == "common_stock"
    assert config.strategy_family == "equity_mean_reversion"
    assert config.display_name == "AAPL Shadow Observer"
    assert config.validate() == []


def test_shadow_universe_uses_generic_benchmark_fallback_for_unknown_common_stock():
    assert default_benchmarks_for("SOFI.US") == ("QQQ.US", "SPY.US")
    config = ShadowUniverseConfig.for_symbol(
        "SOFI.US",
        strategy_family="mean_reversion",
        risk_profile="balanced",
    )
    assert config.benchmarks == ("QQQ.US", "SPY.US")
    assert config.symbol_class == "common_stock"
    assert config.validate() == []


def test_shadow_universe_classifies_leveraged_etf():
    config = ShadowUniverseConfig.for_symbol("YINN.US")
    assert config.symbol_class == "leveraged_etf"
    assert config.strategy_family == "leveraged_range"
    assert config.validate() == []


def test_shadow_universe_rejects_missing_risk_profile_for_leveraged_etf():
    config = ShadowUniverseConfig.for_symbol("YINN.US", risk_profile="")
    config = ShadowUniverseConfig(
        symbol=config.symbol,
        benchmarks=config.benchmarks,
        timeframe=config.timeframe,
        strategy_version=config.strategy_version,
        strategy_family=config.strategy_family,
        risk_profile="",
        regular_session_only=config.regular_session_only,
        output_directory=config.output_directory,
        shadow_enabled=config.shadow_enabled,
        trading_enabled=config.trading_enabled,
        symbol_class=config.symbol_class,
    )
    errors = config.validate()
    assert "missing_risk_profile" in errors


def test_shadow_universe_fails_closed_without_benchmarks_or_family():
    config = ShadowUniverseConfig(
        symbol="XYZ.US",
        benchmarks=(),
        timeframe="15m",
        strategy_version="baseline",
        strategy_family="",
        regular_session_only=True,
        output_directory=Path("artifacts/shadow/xyz_15m"),
        shadow_enabled=True,
        trading_enabled=False,
        symbol_class="common_stock",
    )
    errors = config.validate()
    assert "benchmarks missing" in errors
    assert "strategy_family missing" in errors


def test_shadow_universe_rejects_incompatible_strategy_family():
    config = ShadowUniverseConfig.for_symbol(
        "SOXS.US",
        strategy_family="equity_mean_reversion",
        benchmarks=("SOXX.US", "SMH.US"),
    )
    errors = config.validate()
    assert any("incompatible_strategy_family" in error for error in errors)


def test_shadow_universe_rejects_path_injection_and_bad_timeframe():
    config = ShadowUniverseConfig.for_symbol(
        "../SOXS",
        benchmarks=("SOXX.US", "SMH.US"),
        timeframe="15m/../../tmp",
        output_directory=Path("/tmp/escape"),
        strategy_family="range_etf",
        risk_profile="strict",
    )
    errors = config.validate()
    assert "invalid_symbol" in errors
    assert "invalid_timeframe" in errors
    assert "unsafe_output_directory" in errors


def test_shadow_universe_rejects_output_directory_escape():
    config = ShadowUniverseConfig.for_symbol(
        "SOXS.US",
        benchmarks=("SOXX.US", "SMH.US"),
        output_directory=Path("../outside"),
    )
    errors = config.validate()
    assert "unsafe_output_directory" in errors


def test_shadow_universe_rejects_shadow_disabled_or_trading_enabled():
    shadow_disabled = ShadowUniverseConfig.for_symbol(
        "SOXS.US",
        benchmarks=("SOXX.US", "SMH.US"),
        shadow_enabled=False,
    )
    trading_enabled = ShadowUniverseConfig.for_symbol(
        "SOXS.US",
        benchmarks=("SOXX.US", "SMH.US"),
        trading_enabled=True,
    )
    assert "shadow_enabled must be true" in shadow_disabled.validate()
    assert "trading_enabled must be false" in trading_enabled.validate()


def test_shadow_universe_helpers_keep_generic_output_paths():
    assert default_benchmarks_for("SOXS.US") == ("SOXX.US", "SMH.US")
    assert default_shadow_output_directory("SOXS.US", "15m") == Path("artifacts/shadow/soxs_15m")
    assert default_shadow_output_directory("AAPL.US", "15m") == Path("artifacts/shadow/aapl_15m")
    assert shadow_title_for("AAPL.US", "15m") == "AAPL Shadow Observer"
