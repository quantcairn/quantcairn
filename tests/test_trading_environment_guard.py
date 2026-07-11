from src.config.loader import AppConfig, BrokerConfig, LongBridgeConfig
from src.safety.trading_environment_guard import TradingEnvironmentGuard


def _config(mode: str, **broker_overrides) -> AppConfig:
    broker = LongBridgeConfig(
        enabled=broker_overrides.pop("enabled", False),
        environment=broker_overrides.pop("environment", "prod"),
        account_type=broker_overrides.pop("account_type", ""),
        http_url=broker_overrides.pop("http_url", None),
        quote_ws_url=broker_overrides.pop("quote_ws_url", None),
        trade_ws_url=broker_overrides.pop("trade_ws_url", None),
        allow_live_order=broker_overrides.pop("allow_live_order", False),
    )
    return AppConfig(mode=mode, broker=BrokerConfig(longbridge=broker))


def test_paper_mode_passes_without_affecting_startup():
    verdict = TradingEnvironmentGuard().validate(_config("paper"))
    assert verdict.ok is True
    assert verdict.broker == "PaperBroker"


def test_sandbox_mode_passes_with_official_endpoints():
    verdict = TradingEnvironmentGuard().validate(
        _config(
            "sandbox",
            enabled=True,
            environment="sandbox",
            account_type="paper",
            http_url="https://openapi.longbridge.com",
            quote_ws_url="wss://openapi-quote.longbridge.com/v2",
            trade_ws_url="wss://openapi-trade.longbridge.com/v2",
            allow_live_order=False,
        )
    )
    assert verdict.ok is True
    assert verdict.broker == "Longbridge"
    assert verdict.live_order_enabled is False


def test_sandbox_mode_rejects_non_official_endpoint():
    verdict = TradingEnvironmentGuard().validate(
        _config(
            "sandbox",
            enabled=True,
            environment="sandbox",
            account_type="paper",
            http_url="https://sandbox.example/http",
            quote_ws_url="wss://sandbox.example/quote",
            trade_ws_url="wss://sandbox.example/trade",
            allow_live_order=False,
        )
    )
    assert verdict.ok is False
    assert any("official Longbridge endpoint" in error for error in verdict.errors)


def test_sandbox_mode_requires_paper_or_demo_account_type():
    verdict = TradingEnvironmentGuard().validate(
        _config(
            "sandbox",
            enabled=True,
            environment="sandbox",
            account_type="real",
            http_url="https://openapi.longbridge.com",
            quote_ws_url="wss://openapi-quote.longbridge.com/v2",
            trade_ws_url="wss://openapi-trade.longbridge.com/v2",
            allow_live_order=False,
        )
    )
    assert verdict.ok is False
    assert any("account_type=paper/demo" in error for error in verdict.errors)


def test_live_mode_emits_warning_and_requires_explicit_live_order():
    verdict = TradingEnvironmentGuard().validate(
        _config(
            "live",
            enabled=True,
            environment="prod",
            account_type="real",
            allow_live_order=True,
        )
    )
    assert verdict.ok is True
    assert any("LIVE trading mode selected" in warning for warning in verdict.warnings)
