"""
Configuration loader: reads config.yaml and returns typed config objects.
"""
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional, Literal


@dataclass
class RangeConfig:
    mode: Literal["manual", "auto"] = "manual"
    support_price: Optional[float] = None
    resistance_price: Optional[float] = None
    auto_lookback: int = 50
    auto_refresh_minutes: int = 15
    tolerance_pct: float = 0.3
    min_profit_per_trade: float = 1.0
    quick_stop_pct: float = 3.0


@dataclass
class PositionConfig:
    size_per_trade: int = 100
    max_position: int = 300
    cool_down_seconds: int = 30
    initial_capital: float = 10000.0


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


def _parse_config(raw: dict) -> AppConfig:
    """Parse raw dict into AppConfig, applying env var overrides."""
    config = AppConfig()

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
        quick_stop_pct=r.get("quick_stop_pct", 3.0),
    )

    # Position
    p = raw.get("position", {})
    config.position = PositionConfig(
        size_per_trade=int(os.environ.get("SOXS_SIZE", p.get("size_per_trade", 100))),
        max_position=int(os.environ.get("SOXS_MAX_POS", p.get("max_position", 300))),
        cool_down_seconds=p.get("cool_down_seconds", 30),
        initial_capital=float(os.environ.get("SOXS_CAPITAL", p.get("initial_capital", 10000))),
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
    config.broker = BrokerConfig(
        longbridge=LongBridgeConfig(
            app_key=lb.get("app_key", ""),
            app_secret=lb.get("app_secret", ""),
            access_token=lb.get("access_token", ""),
            region=lb.get("region", "cn"),
            enabled=lb.get("enabled", False),
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

    if config.mode == "live" and not config.broker.longbridge.enabled:
        issues.append("[WARN] live mode selected but longbridge broker is disabled")

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
