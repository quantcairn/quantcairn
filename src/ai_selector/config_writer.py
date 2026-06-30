import yaml
import os

BASE = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
TOP_INITIAL_CAPITAL = 700.0


def _auto_refresh_minutes() -> int:
    raw = os.environ.get("AI_SELECTOR_AUTO_REFRESH_MINUTES", "5")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 5
    return max(1, value)


def _default_top_mode() -> str:
    mode = str(os.environ.get("AI_SELECTOR_TOP_MODE", "")).strip().lower()
    if mode in {"live", "paper"}:
        return mode

    has_live_creds = bool(
        os.environ.get("LONGBRIDGE_ACCESS_TOKEN")
        and (os.environ.get("LONGBRIDGE_APP_KEY") or os.environ.get("LONGBRIDGE_API_KEY"))
        and (os.environ.get("LONGBRIDGE_APP_SECRET") or os.environ.get("LONGBRIDGE_API_SECRET"))
    )
    return "live" if has_live_creds else "paper"


def _load_existing_mode(index: int, fallback: str) -> str:
    path = os.path.join(BASE, f"configs/TOP{index}.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        mode = str(cfg.get("mode", "")).strip().lower()
        if mode in {"live", "paper"}:
            return mode
    except Exception:
        pass
    return fallback

def write_top_configs(top_items):
    default_mode = _default_top_mode()
    for i, item in enumerate(top_items, start=1):
        support = float(item["range_low"])
        resistance = float(item["range_high"])
        if support <= 0 or resistance <= support:
            continue
        initial_capital = TOP_INITIAL_CAPITAL
        estimated_price = (support + resistance) / 2
        fallback_size = max(1, int((initial_capital * 0.8) // estimated_price))
        requested_size = int(item.get("size") or fallback_size)
        size_per_trade = max(1, min(requested_size, fallback_size))
        mode = _load_existing_mode(i, default_mode)
        live_enabled = mode == "live"

        cfg = {
            "ticker": item["ticker"],
            "mode": mode,
            "range": {
                "mode": "auto",
                "support_price": None,
                "resistance_price": None,
                "auto_lookback": 78,
                "auto_refresh_minutes": _auto_refresh_minutes(),
                "trend_filter": {
                    "enabled": True,
                    "ma_period": 20,
                    "min_trend_strength": 0.3,
                },
                "tolerance_pct": 0.8,
                "min_profit_per_trade": 1.0,
                "min_range_width_pct": 0.8,
                "quick_stop_pct": 3.0,
            },
            "position": {
                "size_per_trade": size_per_trade,
                "max_position": 9999,
                "cool_down_seconds": 30,
                "initial_capital": initial_capital,
            },
            "risk": {
                "stop_loss_pct": float(item.get("risk", {}).get("stop_loss_pct", 1.5)),
                "take_profit_pct": None,
                "daily_loss_limit": 60.0,
                "max_consecutive_losses": 3,
                "max_drawdown_pct": 8.0,
            },
            "trading_hours": {
                "timezone": "America/New_York",
                "start": "09:30",
                "end": "16:00",
                "early_close": "13:00",
            },
            "data": {
                "provider": "yfinance",
                "poll_interval_seconds": 10,
            },
            "notifications": {
                "console": True,
                "macos_notification": True,
                "webhook_url": None,
                "trade_summary_interval": 3,
            },
            "broker": {
                "longbridge": {
                    "app_key": "",
                    "app_secret": "",
                    "access_token": "",
                    "region": "cn",
                    "enabled": live_enabled,
                },
            },
        }
        path = os.path.join(BASE, f"configs/TOP{i}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
