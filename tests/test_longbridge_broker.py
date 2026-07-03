import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.config.loader import load_config
from src.engine.trading_engine import TradingEngine


def test_longbridge_env_aliases_and_sandbox_config(tmp_path, monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def __init__(self):
                self._env = {}

            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

            def setenv(self, key, value):
                import os

                if key not in self._env:
                    self._env[key] = os.environ.get(key)
                os.environ[key] = value

            def delenv(self, key, raising=True):
                import os

                if key not in self._env and raising:
                    raise KeyError(key)
                if key not in self._env:
                    self._env[key] = os.environ.get(key)
                os.environ.pop(key, None)

            def restore(self):
                import os

                for key, value in self._env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        monkeypatch = SimpleMonkeyPatch()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode: live
broker:
  longbridge:
    enabled: true
    app_key: ""
    app_secret: ""
    access_token: "token-from-yaml"
    environment: sandbox
""".strip()
        + "\n",
        encoding="utf-8",
    )

    try:
        monkeypatch.delenv("LONGBRIDGE_APP_KEY", raising=False)
        monkeypatch.delenv("LONGBRIDGE_APP_SECRET", raising=False)
        monkeypatch.setenv("LONGBRIDGE_API_KEY", "alias-key")
        monkeypatch.setenv("LONGBRIDGE_API_SECRET", "alias-secret")
        monkeypatch.setenv("LONGBRIDGE_SANDBOX_HTTP_URL", "https://sandbox.example/http")
        monkeypatch.setenv("LONGBRIDGE_SANDBOX_QUOTE_WS_URL", "wss://sandbox.example/quote")
        monkeypatch.setenv("LONGBRIDGE_SANDBOX_TRADE_WS_URL", "wss://sandbox.example/trade")
        monkeypatch.setenv("SOXS_CONFIG", str(config_path))

        config = load_config(str(config_path))
        assert config.broker.longbridge.app_key == "alias-key"
        assert config.broker.longbridge.app_secret == "alias-secret"
        assert config.broker.longbridge.environment == "sandbox"
        assert config.broker.longbridge.http_url == "https://sandbox.example/http"
        assert config.broker.longbridge.quote_ws_url == "wss://sandbox.example/quote"
        assert config.broker.longbridge.trade_ws_url == "wss://sandbox.example/trade"
    finally:
        monkeypatch.restore()


def test_trading_engine_passes_longbridge_fields(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def __init__(self):
                self._originals = {}

            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                key = (module_name, attr_name)
                if key not in self._originals:
                    self._originals[key] = getattr(module, attr_name)
                setattr(module, attr_name, value)

            def restore(self):
                for (module_name, attr_name), original in self._originals.items():
                    module = __import__(module_name, fromlist=[attr_name])
                    setattr(module, attr_name, original)

        monkeypatch = SimpleMonkeyPatch()

    captured = {}

    class FakeLongBridgeBroker:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def connect(self):
            return False

    try:
        monkeypatch.setattr("src.broker.longbridge_broker.LongBridgeBroker", FakeLongBridgeBroker)

        from src.config.loader import (
            AppConfig,
            BrokerConfig,
            LongBridgeConfig,
            RangeConfig,
        )

        config = AppConfig(
            mode="live",
            range=RangeConfig(mode="auto"),
            broker=BrokerConfig(
                longbridge=LongBridgeConfig(
                    enabled=True,
                    app_key="k",
                    app_secret="s",
                    access_token="t",
                    region="us",
                    environment="sandbox",
                    http_url="https://sandbox.example/http",
                    quote_ws_url="wss://sandbox.example/quote",
                    trade_ws_url="wss://sandbox.example/trade",
                    log_path="logs/sdk",
                )
            )
        )

        TradingEngine(config)

        assert captured["environment"] == "sandbox"
        assert captured["http_url"] == "https://sandbox.example/http"
        assert captured["quote_ws_url"] == "wss://sandbox.example/quote"
        assert captured["trade_ws_url"] == "wss://sandbox.example/trade"
        assert captured["log_path"] == "logs/sdk"
    finally:
        monkeypatch.restore()


def test_longbridge_broker_audit_log_records_trade(tmp_path, monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    from src.broker import longbridge_broker as module

    class FakeConfig:
        @staticmethod
        def from_apikey(*args, **kwargs):
            return SimpleNamespace(args=args, kwargs=kwargs)

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config
            self.submit_kwargs = None

        def submit_order(self, **kwargs):
            self.submit_kwargs = kwargs
            return SimpleNamespace(order_id="LB-12345")

        def cancel_order(self, **kwargs):
            return SimpleNamespace(ok=True)

        def order_detail(self, order_id):
            assert order_id == "LB-12345"
            return SimpleNamespace(
                order_id="LB-12345",
                symbol="AAPL",
                side=module.lb.OrderSide.Buy,
                order_type=module.lb.OrderType.MO,
                quantity=1,
                executed_quantity=1,
                executed_price=100.5,
                status=module.lb.OrderStatus.Filled,
                msg="filled",
            )

        def stock_positions(self):
            return SimpleNamespace(channels=[])

        def account_balance(self):
            return SimpleNamespace(total_cash=1000, net_assets=1000, buy_power=1000)

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config
            self.quote_calls = 0

        def quote(self, symbols):
            self.quote_calls += 1
            return [
                SimpleNamespace(symbol="AAPL.US", last_done=12.5, price=12.5, last_price=12.5)
            ]

    fake_lb = SimpleNamespace(
        Config=FakeConfig,
        TradeContext=FakeTradeContext,
        QuoteContext=FakeQuoteContext,
        OrderSide=SimpleNamespace(Buy="Buy", Sell="Sell"),
        OrderType=SimpleNamespace(MO="MO", LO="LO"),
        TimeInForceType=SimpleNamespace(Day="Day"),
        OpenApiException=RuntimeError,
        OrderStatus=SimpleNamespace(
            Filled="Filled",
            PartialFilled="PartialFilled",
            Rejected="Rejected",
            Canceled="Canceled",
            Expired="Expired",
            New="New",
            PendingCancel="PendingCancel",
            WaitToNew="WaitToNew",
        ),
    )

    monkeypatch.setattr("src.broker.longbridge_broker.lb", fake_lb)

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        environment="sandbox",
        http_url="https://sandbox.example/http",
        quote_ws_url="wss://sandbox.example/quote",
        trade_ws_url="wss://sandbox.example/trade",
        audit_dir=str(tmp_path / "logs"),
    )

    log_path = tmp_path / "logs" / f"trades-{module.datetime.now().strftime('%Y%m%d')}.jsonl"
    if log_path.exists():
        log_path.unlink()

    assert broker.connect() is True
    order = broker.place_order("AAPL", module.OrderSide.SELL, 1)

    assert order.order_id == "LB-12345"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    records = [json.loads(line) for line in lines]
    assert records[0]["action"] == "connect"


def test_get_active_orders_accepts_unhashable_status_objects(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    from src.broker import longbridge_broker as module

    class FakeStatus:
        __hash__ = None

        def __init__(self, value):
            self.value = value

        def __eq__(self, other):
            return isinstance(other, FakeStatus) and self.value == other.value

        def __str__(self):
            return self.value

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config

        def today_orders(self, symbol):
            return [
                SimpleNamespace(
                    order_id="LB-ACTIVE-1",
                    symbol=symbol,
                    side="Buy",
                    order_type="MO",
                    quantity=3,
                    executed_quantity=1,
                    executed_price=12.34,
                    status=FakeStatus("PartialFilled"),
                    msg="partial",
                ),
                SimpleNamespace(
                    order_id="LB-DONE-1",
                    symbol=symbol,
                    side="Sell",
                    order_type="MO",
                    quantity=2,
                    executed_quantity=2,
                    executed_price=12.55,
                    status=FakeStatus("Filled"),
                    msg="filled",
                ),
            ]

    fake_lb = SimpleNamespace(
        Config=SimpleNamespace(from_apikey=lambda *args, **kwargs: SimpleNamespace()),
        TradeContext=FakeTradeContext,
        QuoteContext=lambda config: SimpleNamespace(),
        OrderSide=SimpleNamespace(Buy="Buy", Sell="Sell"),
        OrderType=SimpleNamespace(MO="MO", LO="LO"),
        TimeInForceType=SimpleNamespace(Day="Day"),
        OpenApiException=RuntimeError,
        OrderStatus=SimpleNamespace(
            Filled=FakeStatus("Filled"),
            PartialFilled=FakeStatus("PartialFilled"),
            Rejected=FakeStatus("Rejected"),
            Canceled=FakeStatus("Canceled"),
            Expired=FakeStatus("Expired"),
            New=FakeStatus("New"),
            PendingCancel=FakeStatus("PendingCancel"),
            WaitToNew=FakeStatus("WaitToNew"),
            NotReported=FakeStatus("NotReported"),
            ProtectedNotReported=FakeStatus("ProtectedNotReported"),
            VarietiesNotReported=FakeStatus("VarietiesNotReported"),
        ),
    )

    monkeypatch.setattr("src.broker.longbridge_broker.lb", fake_lb)

    broker = module.LongBridgeBroker(app_key="k", app_secret="s", access_token="t")
    assert broker.connect() is True

    orders = broker.get_active_orders("PLTR")

    assert orders is not None
    assert len(orders) == 1
    assert orders[0].order_id == "LB-ACTIVE-1"
    assert orders[0].status == module.OrderStatus.PARTIALLY_FILLED
    assert records[0]["environment"] == "sandbox"
    assert records[1]["action"] == "place_order"
    assert records[1]["request"]["ticker"] == "AAPL"
    assert records[1]["response"]["order_id"] == "LB-12345"
    assert broker._trade_ctx.submit_kwargs["symbol"] == "AAPL.US"


def test_longbridge_broker_account_balance_handles_list_response(tmp_path, monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    from src.broker import longbridge_broker as module

    class FakeConfig:
        @staticmethod
        def from_apikey(*args, **kwargs):
            return SimpleNamespace(args=args, kwargs=kwargs)

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config

        def account_balance(self):
            return [
                SimpleNamespace(total_cash=1234.56, net_assets=2345.67, buy_power=3456.78),
                SimpleNamespace(total_cash=1, net_assets=1, buy_power=1),
            ]

        def stock_positions(self):
            return SimpleNamespace(channels=[])

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config
            self.quote_calls = 0

        def quote(self, symbols):
            self.quote_calls += 1
            return []

    fake_lb = SimpleNamespace(
        Config=FakeConfig,
        TradeContext=FakeTradeContext,
        QuoteContext=FakeQuoteContext,
        OrderSide=SimpleNamespace(Buy="Buy", Sell="Sell"),
        OrderType=SimpleNamespace(MO="MO", LO="LO"),
        TimeInForceType=SimpleNamespace(Day="Day"),
        OpenApiException=RuntimeError,
        OrderStatus=SimpleNamespace(
            Filled="Filled",
            PartialFilled="PartialFilled",
            Rejected="Rejected",
            Canceled="Canceled",
            Expired="Expired",
            New="New",
            PendingCancel="PendingCancel",
            WaitToNew="WaitToNew",
        ),
    )

    monkeypatch.setattr("src.broker.longbridge_broker.lb", fake_lb)

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        environment="sandbox",
        http_url="https://sandbox.example/http",
        quote_ws_url="wss://sandbox.example/quote",
        trade_ws_url="wss://sandbox.example/trade",
        audit_dir=str(tmp_path / "logs"),
    )

    assert broker.connect() is True
    account = broker.get_account()
    assert account.cash == 1234.56
    assert account.equity == 2345.67
    assert account.buying_power == 3456.78


def test_longbridge_broker_reuses_cached_positions_and_account(tmp_path, monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    from src.broker import longbridge_broker as module

    class FakeConfig:
        @staticmethod
        def from_apikey(*args, **kwargs):
            return SimpleNamespace(args=args, kwargs=kwargs)

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config
            self.positions_calls = 0
            self.balance_calls = 0

        def stock_positions(self):
            self.positions_calls += 1
            return SimpleNamespace(
                channels=[
                    SimpleNamespace(
                        positions=[
                            SimpleNamespace(symbol="AAPL.US", quantity=2, cost_price=10.0)
                        ]
                    )
                ]
            )

        def account_balance(self):
            self.balance_calls += 1
            return SimpleNamespace(total_cash=100.0, net_assets=120.0, buy_power=150.0)

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config
            self.quote_calls = 0

        def quote(self, symbols):
            self.quote_calls += 1
            return []

    fake_lb = SimpleNamespace(
        Config=FakeConfig,
        TradeContext=FakeTradeContext,
        QuoteContext=FakeQuoteContext,
        OrderSide=SimpleNamespace(Buy="Buy", Sell="Sell"),
        OrderType=SimpleNamespace(MO="MO", LO="LO"),
        TimeInForceType=SimpleNamespace(Day="Day"),
        OpenApiException=RuntimeError,
        OrderStatus=SimpleNamespace(
            Filled="Filled",
            PartialFilled="PartialFilled",
            Rejected="Rejected",
            Canceled="Canceled",
            Expired="Expired",
            New="New",
            PendingCancel="PendingCancel",
            WaitToNew="WaitToNew",
        ),
    )

    monkeypatch.setattr("src.broker.longbridge_broker.lb", fake_lb)

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        environment="sandbox",
        http_url="https://sandbox.example/http",
        quote_ws_url="wss://sandbox.example/quote",
        trade_ws_url="wss://sandbox.example/trade",
        audit_dir=str(tmp_path / "logs"),
    )

    assert broker.connect() is True

    first_positions = broker.get_positions()
    first_account = broker.get_account()
    second_positions = broker.get_positions()
    second_account = broker.get_account()

    assert len(first_positions) == 1
    assert first_positions[0].ticker == "AAPL"
    assert first_positions[0].market_value > 0
    assert first_positions[0].unrealized_pnl >= 0
    assert first_account.cash == 100.0
    assert second_positions[0].ticker == "AAPL"
    assert second_account.cash == 100.0
    assert broker.get_position_for_ticker("AAPL").quantity == 2
    assert broker._trade_ctx.positions_calls == 1
    assert broker._trade_ctx.balance_calls == 1
    assert broker._quote_ctx.quote_calls == 1


def test_longbridge_broker_uses_safer_default_cache_and_backoff(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def __init__(self):
                self._env = {}
                self._originals = {}

            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                key = (module_name, attr_name)
                if key not in self._originals:
                    self._originals[key] = getattr(module, attr_name)
                setattr(module, attr_name, value)

            def delenv(self, key, raising=True):
                import os
                if key not in self._env:
                    self._env[key] = os.environ.get(key)
                if raising and key not in os.environ:
                    raise KeyError(key)
                os.environ.pop(key, None)

            def restore(self):
                import os
                for key, value in self._env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                for (module_name, attr_name), original in self._originals.items():
                    module = __import__(module_name, fromlist=[attr_name])
                    setattr(module, attr_name, original)

        monkeypatch = SimpleMonkeyPatch()

    from src.broker import longbridge_broker as module

    fake_lb = SimpleNamespace(
        Config=SimpleNamespace(from_apikey=lambda *args, **kwargs: SimpleNamespace()),
        TradeContext=lambda config: SimpleNamespace(),
        QuoteContext=lambda config: SimpleNamespace(),
        OrderSide=SimpleNamespace(Buy="Buy", Sell="Sell"),
        OrderType=SimpleNamespace(MO="MO", LO="LO"),
        TimeInForceType=SimpleNamespace(Day="Day"),
        OpenApiException=RuntimeError,
        OrderStatus=SimpleNamespace(
            Filled="Filled",
            PartialFilled="PartialFilled",
            Rejected="Rejected",
            Canceled="Canceled",
            Expired="Expired",
            New="New",
            PendingCancel="PendingCancel",
            WaitToNew="WaitToNew",
        ),
    )

    try:
        monkeypatch.delenv("LONGBRIDGE_CACHE_TTL_SECONDS", raising=False)
        monkeypatch.delenv("LONGBRIDGE_RETRY_BACKOFF_SECONDS", raising=False)
        monkeypatch.setattr("src.broker.longbridge_broker.lb", fake_lb)

        broker = module.LongBridgeBroker(app_key="k", app_secret="s", access_token="t")

        assert broker._account_cache_ttl_seconds == 180.0
        assert broker._positions_cache_ttl_seconds == 180.0
        assert broker._cache_retry_backoff_seconds == 45.0
    finally:
        monkeypatch.restore()


def test_longbridge_broker_place_order_survives_unserializable_sdk_response(tmp_path, monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit(".", 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    from src.broker import longbridge_broker as module

    class FakeConfig:
        @staticmethod
        def from_apikey(*args, **kwargs):
            return SimpleNamespace(args=args, kwargs=kwargs)

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config
            self.submit_kwargs = None

        def submit_order(self, **kwargs):
            self.submit_kwargs = kwargs
            return SimpleNamespace(
                order_id="LB-99999",
                payload=dict.items,
                nested=SimpleNamespace(fn=dict.items),
            )

        def stock_positions(self):
            return SimpleNamespace(channels=[])

        def account_balance(self):
            return SimpleNamespace(total_cash=1000, net_assets=1000, buy_power=1000)

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config
            self.quote_calls = 0

        def quote(self, symbols):
            self.quote_calls += 1
            return []

    fake_lb = SimpleNamespace(
        Config=FakeConfig,
        TradeContext=FakeTradeContext,
        QuoteContext=FakeQuoteContext,
        OrderSide=SimpleNamespace(Buy="Buy", Sell="Sell"),
        OrderType=SimpleNamespace(MO="MO", LO="LO"),
        TimeInForceType=SimpleNamespace(Day="Day"),
        OpenApiException=RuntimeError,
        OrderStatus=SimpleNamespace(
            Filled="Filled",
            PartialFilled="PartialFilled",
            Rejected="Rejected",
            Canceled="Canceled",
            Expired="Expired",
            New="New",
            PendingCancel="PendingCancel",
            WaitToNew="WaitToNew",
        ),
    )

    monkeypatch.setattr("src.broker.longbridge_broker.lb", fake_lb)

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        environment="sandbox",
        http_url="https://sandbox.example/http",
        quote_ws_url="wss://sandbox.example/quote",
        trade_ws_url="wss://sandbox.example/trade",
        audit_dir=str(tmp_path / "logs"),
    )

    assert broker.connect() is True
    order = broker.place_order("AAPL", module.OrderSide.SELL, 1)

    assert order.order_id == "LB-99999"
    assert order.status == module.OrderStatus.PENDING
    log_path = tmp_path / "logs" / f"trades-{module.datetime.now().strftime('%Y%m%d')}.jsonl"
    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["action"] == "place_order"
    assert records[-1]["response"]["order_id"] == "LB-99999"


def test_positions_failure_marks_snapshot_unreliable(tmp_path):
    from src.broker import longbridge_broker as module

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        audit_dir=str(tmp_path / "logs"),
    )
    broker._connected = True
    broker._trade_ctx = SimpleNamespace(
        stock_positions=lambda: (_ for _ in ()).throw(RuntimeError("rate limited"))
    )

    assert broker.get_positions() == []
    assert broker.is_positions_snapshot_reliable() is False


def test_rate_limited_positions_reuse_recent_cache(tmp_path):
    from src.broker import longbridge_broker as module

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        audit_dir=str(tmp_path / "logs"),
    )
    broker._connected = True
    broker._positions_cache = [
        module.Position(
            ticker="SOFI",
            quantity=30,
            avg_entry_price=18.095,
            current_price=18.24,
            market_value=547.2,
            unrealized_pnl=4.35,
            unrealized_pnl_pct=0.801,
        )
    ]
    broker._positions_cache_fetched_at = module.time.time()
    broker._positions_snapshot_reliable = True
    broker._trade_ctx = SimpleNamespace(
        stock_positions=lambda: (_ for _ in ()).throw(RuntimeError("rate limited"))
    )

    positions = broker.get_positions()

    assert positions == broker._positions_cache
    assert broker.is_positions_snapshot_reliable() is True


def test_rate_limited_account_reuses_recent_cache(tmp_path):
    from src.broker import longbridge_broker as module

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        audit_dir=str(tmp_path / "logs"),
    )
    broker._connected = True
    broker._account_cache = module.AccountInfo(
        cash=829.93,
        equity=1619.88,
        buying_power=1177.85,
        positions=[],
    )
    broker._account_cache_fetched_at = module.time.time()
    broker._account_snapshot_reliable = True
    broker._trade_ctx = SimpleNamespace(
        account_balance=lambda: (_ for _ in ()).throw(RuntimeError("Too many requests"))
    )

    account = broker.get_account()

    assert account == broker._account_cache
    assert broker.is_account_snapshot_reliable() is True


def test_primary_live_broker_blocks_buy_in_global_reduce_only(tmp_path):
    from src.broker import longbridge_broker as module

    broker = module.LongBridgeBroker(
        app_key="k",
        app_secret="s",
        access_token="t",
        audit_dir=str(tmp_path / "logs"),
    )
    broker._connected = True
    broker._trade_ctx = SimpleNamespace()

    order = broker.place_order("AAPL", module.OrderSide.BUY, 1)

    assert order.status == module.OrderStatus.REJECTED
    assert order.order_id == ""
    assert "reduce-only" in order.notes


def run_test_direct():
    tmp_root = Path(tempfile.mkdtemp(prefix="longbridge-broker-test-"))
    test_longbridge_env_aliases_and_sandbox_config(tmp_root)
    test_trading_engine_passes_longbridge_fields()
    test_longbridge_broker_audit_log_records_trade(tmp_root)
    test_longbridge_broker_account_balance_handles_list_response(tmp_root)
    test_longbridge_broker_reuses_cached_positions_and_account(tmp_root)
    test_longbridge_broker_place_order_survives_unserializable_sdk_response(tmp_root)
    test_primary_live_broker_blocks_buy_in_global_reduce_only(tmp_root)
    test_positions_failure_marks_snapshot_unreliable(tmp_root)
