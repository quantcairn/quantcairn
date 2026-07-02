#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.broker.longbridge_broker import LongBridgeBroker
from src.dashboard.combined import _env, _has_live_account_env
from src.engine.orphan_monitor import OrphanPositionMonitor, should_run_orphan_monitor


def _build_broker() -> LongBridgeBroker:
    return LongBridgeBroker(
        app_key=_env("LONGBRIDGE_APP_KEY") or _env("LONGBRIDGE_API_KEY"),
        app_secret=_env("LONGBRIDGE_APP_SECRET") or _env("LONGBRIDGE_API_SECRET"),
        access_token=_env("LONGBRIDGE_ACCESS_TOKEN"),
        region=_env("LONGBRIDGE_REGION", "cn"),
        environment=_env("LONGBRIDGE_ENV", "prod"),
        http_url=_env("LONGBRIDGE_HTTP_URL") or _env("LONGBRIDGE_BASE_URL"),
        quote_ws_url=_env("LONGBRIDGE_QUOTE_WS_URL"),
        trade_ws_url=_env("LONGBRIDGE_TRADE_WS_URL"),
        log_path=_env("LONGBRIDGE_LOG_PATH"),
    )


def main() -> int:
    if not should_run_orphan_monitor() or not _has_live_account_env():
        return 0

    broker = _build_broker()
    if not broker.connect():
        return 1
    try:
        monitor = OrphanPositionMonitor(broker=broker, poll_interval_seconds=60)
        if "--verify-only" in sys.argv:
            return 0 if monitor.verify_broker_positions() is not None else 1
        return monitor.run()
    finally:
        broker.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
