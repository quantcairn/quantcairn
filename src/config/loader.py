"""
Configuration loader: reads config.yaml and returns typed config objects.
"""
import json
import os
import yaml
from .runtime_values import load_private_longbridge_config
from dataclasses import dataclass, field
from typing import Optional, Literal


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
STATE_DIR = os.environ.get("SOXS_STATE_DIR") or os.path.join(PROJECT_DIR, "state")
TRADING_FLAGS_PATH = os.path.join(STATE_DIR, "trading_flags.json")


@dataclass
class RangeConfig:
    mode: Literal["manual", "auto"] = "manual"
    support_price: Optional[float] = None
    resistance_price: Optional[float] = None
    auto_lookback: int = 50
    auto_refresh_minutes: int = 15
    tolerance_pct: float = 0.3
    min_profit_per_trade: float = 1.0
    min_range_width_pct: float = 0.8
    quick_stop_pct: float = 3.0


@dataclass
class PositionConfig:
    size_per_trade: int = 0
    max_position: int = 300
    cool_down_seconds: int = 30
    initial_capital: float = 10000.0
    reduce_only: bool = False


@dataclass
class RiskConfig:
    stop_loss_pct: float = 2.0
    take_profit_pct: Optional[float] = None
    daily_loss_limit: float = 500.0
    max_consecutive_losses: int = 3
    max_drawdown_pct: float = 10.0


@dataclass
class TradingHoursConfig:
    timezone: str = "America/New_York"
    start: str = "09:30"
    end: str = "16:00"
    early_close: str = "13:00"


@dataclass
class DataConfig:
    provider: str = "yfinance"
    poll_interval_seconds: int = 15


@dataclass
class TrendFilterConfig:
    enabled: bool = True
    ma_period: int = 20
    min_trend_strength: float = 0.5  # % distance from MA to trigger filter


@dataclass
class NotificationConfig:
    console: bool = True
    macos_notification: bool = True
    webhook_url: Optional[str] = None
    trade_summary_interval: int = 5


@dataclass
class LongBridgeConfig:
    app_key: str = ""
    app_secret: str = ""
    access_token: str = ""
    region: str = "cn"
    enabled: bool = False
    environment: str = "prod"
    http_url: Optional[str] = None
    quote_ws_url: Optional[str] = None
    trade_ws_url: Optional[str] = None
    log_path: Optional[str] = None


@dataclass
class BrokerConfig:
    longbridge: LongBridgeConfig = field(default_factory=LongBridgeConfig)


@dataclass
class AppConfig:
    ticker: str = "SOXS"
    mode: Literal["paper", "live", "backtest"] = "paper"
    range: RangeConfig = field(default_factory=RangeConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trend_filter: TrendFilterConfig = field(default_factory=TrendFilterConfig)
    trading_hours: TradingHoursConfig = field(default_factory=TradingHoursConfig)
    data: DataConfig = field(default_factory=DataConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)


def load_config(config_path: str = None) -> AppConfig:
    """Load configuration from YAML file, with env var overrides."""
    if config_path is None:
        config_path = os.environ.get(
            "SOXS_CONFIG",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.yaml")
        )

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    return _parse_config(raw)


def _bool_env(key: str, default: bool = False) -> bool:
    """Read a boolean-like env var with a default."""
    val = os.environ.get(key)
    if val is None:
        return bool(default)
    return val.strip().lower() in TRUE_VALUES


def _load_trading_flags() -> dict:
    """Load shared runtime trading flags from the state directory."""
    try:
        if not os.path.exists(TRADING_FLAGS_PATH):
            return {}
        with open(TRADING_FLAGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_config(raw: dict) -> AppConfig:
    """Parse raw dict into AppConfig, applying env var overrides."""
    config = AppConfig()
    trading_flags = _load_trading_flags()

    if "ticker" in raw:
        config.ticker = os.environ.get("SOXS_TICKER", raw["ticker"])
    if "mode" in raw:
        config.mode = os.environ.get("SOXS_MODE", raw["mode"])

    # Range
    r = raw.get("range", {})
    config.range = RangeConfig(
        mode=r.get("mode", "manual"),
        support_price=_float_env("SOXS_SUPPORT", r.get("support_price")),
        resistance_price=_float_env("SOXS_RESISTANCE", r.get("resistance_price")),
        auto_lookback=r.get("auto_lookback", 50),
        auto_refresh_minutes=r.get("auto_refresh_minutes", 15),
        tolerance_pct=r.get("tolerance_pct", 0.3),
        min_profit_per_trade=r.get("min_profit_per_trade", 1.0),
        min_range_width_pct=r.get("min_range_width_pct", 0.8),
        quick_stop_pct=r.get("quick_stop_pct", 3.0),
    )

    # Position
    p = raw.get("position", {})
    reduce_only = (
        _bool_env("SOXS_REDUCE_ONLY_ALL", False)
        or _bool_env("SOXS_REDUCE_ONLY", p.get("reduce_only", False))
        or bool(trading_flags.get("reduce_only_all", False))
    )
    config.position = PositionConfig(
        size_per_trade=int(os.environ.get("SOXS_SIZE", p.get("size_per_trade", 0))),
        max_position=int(os.environ.get("SOXS_MAX_POS", p.get("max_position", 300))),
        cool_down_seconds=p.get("cool_down_seconds", 30),
        initial_capital=float(os.environ.get("SOXS_CAPITAL", p.get("initial_capital", 10000))),
        reduce_only=reduce_only,
    )

    # Risk
    rk = raw.get("risk", {})
    config.risk = RiskConfig(
        stop_loss_pct=rk.get("stop_loss_pct", 2.0),
        take_profit_pct=rk.get("take_profit_pct"),
        daily_loss_limit=rk.get("daily_loss_limit", 500.0),
        max_consecutive_losses=rk.get("max_consecutive_losses", 3),
        max_drawdown_pct=rk.get("max_drawdown_pct", 10.0),
    )

    # Trading hours
    th = raw.get("trading_hours", {})
    config.trading_hours = TradingHoursConfig(
        timezone=th.get("timezone", "America/New_York"),
        start=th.get("start", "09:30"),
        end=th.get("end", "16:00"),
        early_close=th.get("early_close", "13:00"),
    )

    # Data
    d = raw.get("data", {})
    config.data = DataConfig(
        provider=d.get("provider", "yfinance"),
        poll_interval_seconds=d.get("poll_interval_seconds", 15),
    )

    # Trend filter
    tf = raw.get("range", {}).get("trend_filter", {})
    config.trend_filter = TrendFilterConfig(
        enabled=tf.get("enabled", True),
        ma_period=tf.get("ma_period", 20),
        min_trend_strength=tf.get("min_trend_strength", 0.5),
    )

    # Notifications
    n = raw.get("notifications", {})
    config.notifications = NotificationConfig(
        console=n.get("console", True),
        macos_notification=n.get("macos_notification", True),
        webhook_url=n.get("webhook_url"),
        trade_summary_interval=n.get("trade_summary_interval", 5),
    )

    # Broker
    lb = raw.get("broker", {}).get("longbridge", {})
    private_lb = load_private_longbridge_config()
    app_key = (
        os.environ.get("LONGBRIDGE_APP_KEY")
        or os.environ.get("LONGBRIDGE_API_KEY")
        or lb.get("app_key", "")
        or private_lb.get("app_key", "")
    )
    app_secret = (
        os.environ.get("LONGBRIDGE_APP_SECRET")
        or os.environ.get("LONGBRIDGE_API_SECRET")
        or lb.get("app_secret", "")
        or private_lb.get("app_secret", "")
    )
    environment = os.environ.get("LONGBRIDGE_ENV", lb.get("environment") or private_lb.get("environment", "prod"))
    http_url = os.environ.get("LONGBRIDGE_HTTP_URL", lb.get("http_url") or private_lb.get("http_url"))
    quote_ws_url = os.environ.get("LONGBRIDGE_QUOTE_WS_URL", lb.get("quote_ws_url") or private_lb.get("quote_ws_url"))
    trade_ws_url = os.environ.get("LONGBRIDGE_TRADE_WS_URL", lb.get("trade_ws_url") or private_lb.get("trade_ws_url"))
    if environment.strip().lower() == "sandbox":
        http_url = http_url or os.environ.get("LONGBRIDGE_SANDBOX_HTTP_URL")
        quote_ws_url = quote_ws_url or os.environ.get("LONGBRIDGE_SANDBOX_QUOTE_WS_URL")
        trade_ws_url = trade_ws_url or os.environ.get("LONGBRIDGE_SANDBOX_TRADE_WS_URL")
    config.broker = BrokerConfig(
        longbridge=LongBridgeConfig(
            app_key=app_key,
            app_secret=app_secret,
            access_token=os.environ.get("LONGBRIDGE_ACCESS_TOKEN", lb.get("access_token") or private_lb.get("access_token", "")),
            region=os.environ.get("LONGBRIDGE_REGION", lb.get("region") or private_lb.get("region", "cn")),
            enabled=_bool_env("LONGBRIDGE_ENABLED", lb.get("enabled", private_lb.get("enabled", False))),
            environment=environment,
            http_url=http_url,
            quote_ws_url=quote_ws_url,
            trade_ws_url=trade_ws_url,
            log_path=os.environ.get("LONGBRIDGE_LOG_PATH", lb.get("log_path") or private_lb.get("log_path")),
        )
    )

    return config


def _float_env(key: str, default) -> Optional[float]:
    """Get float from env var or return default."""
    val = os.environ.get(key)
    if val is not None:
        return float(val)
    if default is not None:
        return float(default)
    return None


def validate_config(config: AppConfig) -> list[str]:
    """Validate configuration and return list of warnings/errors."""
    issues = []

    if config.range.mode == "manual":
        if config.range.support_price is None:
            issues.append("[ERROR] manual mode requires support_price")
        if config.range.resistance_price is None:
            issues.append("[ERROR] manual mode requires resistance_price")
        if (
            config.range.support_price is not None
            and config.range.resistance_price is not None
            and config.range.support_price >= config.range.resistance_price
        ):
            issues.append("[ERROR] support_price must be < resistance_price")

    if config.mode == "live":
        if not config.broker.longbridge.enabled:
            issues.append("[ERROR] live mode selected but longbridge broker is disabled")
        if config.broker.longbridge.enabled:
            if not config.broker.longbridge.app_key:
                issues.append("[ERROR] live mode requires longbridge app_key")
            if not config.broker.longbridge.app_secret:
                issues.append("[ERROR] live mode requires longbridge app_secret")
            if not config.broker.longbridge.access_token:
                issues.append("[ERROR] live mode requires longbridge access_token")
            if config.broker.longbridge.environment == "sandbox":
                if not config.broker.longbridge.http_url:
                    issues.append("[WARNING] sandbox mode selected but longbridge http_url is not set")
                if not config.broker.longbridge.quote_ws_url:
                    issues.append("[WARNING] sandbox mode selected but longbridge quote_ws_url is not set")
                if not config.broker.longbridge.trade_ws_url:
                    issues.append("[WARNING] sandbox mode selected but longbridge trade_ws_url is not set")

    spread_pct = (
        (config.range.resistance_price - config.range.support_price)
        / config.range.support_price
        * 100
        if config.range.support_price and config.range.resistance_price
        else 0
    )
    if spread_pct > 10:
        issues.append(f"[WARN] Range spread is {spread_pct:.1f}% — unusually wide")

    return issues
