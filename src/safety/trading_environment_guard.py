"""Startup guard for trading environment mode/broker consistency."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..config.loader import AppConfig
from ..broker.longbridge_broker import (
    DEFAULT_PROD_HTTP_URL,
    DEFAULT_PROD_QUOTE_WS_URL,
    DEFAULT_PROD_TRADE_WS_URL,
)


@dataclass
class TradingEnvironmentVerdict:
    ok: bool
    mode: str
    broker: str
    live_order_enabled: bool
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TradingEnvironmentGuard:
    """Validate mode/broker/endpoint combinations before engine startup."""

    def validate(self, config: AppConfig) -> TradingEnvironmentVerdict:
        mode = str(getattr(config, "mode", "") or "").strip().lower()
        broker = getattr(config, "broker", None)
        broker_cfg = getattr(broker, "longbridge", None)
        if broker_cfg is None:
            broker_cfg = type(
                "_MissingLongbridgeConfig",
                (),
                {
                    "enabled": False,
                    "environment": None,
                    "account_type": None,
                    "allow_live_order": False,
                    "http_url": None,
                    "quote_ws_url": None,
                    "trade_ws_url": None,
                },
            )()
        broker_name = "PaperBroker" if mode == "paper" else "Longbridge"
        errors: list[str] = []
        warnings: list[str] = []

        summary = {
            "mode": mode.upper() if mode else "UNKNOWN",
            "broker": broker_name,
            "environment": str(getattr(broker_cfg, "environment", "") or "").strip().lower() or None,
            "live_order": bool(getattr(broker_cfg, "allow_live_order", False)),
            "account_type": str(getattr(broker_cfg, "account_type", "") or "").strip().lower() or None,
            "http_url": getattr(broker_cfg, "http_url", None),
            "quote_ws_url": getattr(broker_cfg, "quote_ws_url", None),
            "trade_ws_url": getattr(broker_cfg, "trade_ws_url", None),
        }

        if mode == "paper":
            broker_name = "PaperBroker"
            summary["broker"] = broker_name
            if bool(getattr(broker_cfg, "allow_live_order", False)):
                warnings.append("paper mode ignores longbridge allow_live_order")

        elif mode == "sandbox":
            summary["broker"] = "Longbridge"
            if not bool(getattr(broker_cfg, "enabled", False)):
                errors.append("sandbox mode requires longbridge broker enabled")
            if str(getattr(broker_cfg, "environment", "") or "").strip().lower() != "sandbox":
                errors.append("sandbox mode requires longbridge environment=sandbox")
            account_type = str(getattr(broker_cfg, "account_type", "") or "").strip().lower()
            if account_type not in {"paper", "demo"}:
                errors.append("sandbox mode requires longbridge account_type=paper/demo")
            if bool(getattr(broker_cfg, "allow_live_order", False)):
                errors.append("sandbox mode requires allow_live_order=false")
            sandbox_errors = self._validate_official_endpoints(
                getattr(broker_cfg, "http_url", None),
                getattr(broker_cfg, "quote_ws_url", None),
                getattr(broker_cfg, "trade_ws_url", None),
            )
            errors.extend(sandbox_errors)

        elif mode == "live":
            summary["broker"] = "Longbridge"
            warnings.append("LIVE trading mode selected")
            if not bool(getattr(broker_cfg, "enabled", False)):
                errors.append("live mode requires longbridge broker enabled")
            if str(getattr(broker_cfg, "environment", "") or "").strip().lower() == "sandbox":
                errors.append("live mode cannot use sandbox environment")
            if str(getattr(broker_cfg, "account_type", "") or "").strip().lower() in {"paper", "demo"}:
                errors.append("live mode cannot use paper/demo account_type")
            if not bool(getattr(broker_cfg, "allow_live_order", False)):
                errors.append("live mode requires allow_live_order=true")

        elif mode == "backtest":
            summary["broker"] = "PaperBroker"
        else:
            errors.append(f"unsupported trading mode: {mode or 'missing'}")

        return TradingEnvironmentVerdict(
            ok=not errors,
            mode=mode,
            broker=summary["broker"],
            live_order_enabled=bool(summary["live_order"]),
            summary=summary,
            errors=errors,
            warnings=warnings,
        )

    def format_report(self, verdict: TradingEnvironmentVerdict) -> str:
        lines = [
            "================================",
            "Trading Environment",
            "",
            f"Mode: {verdict.summary.get('mode', 'UNKNOWN')}",
            f"Broker: {verdict.summary.get('broker', 'UNKNOWN')}",
            f"Live Order: {'ENABLED' if verdict.live_order_enabled else 'DISABLED'}",
        ]
        environment = verdict.summary.get("environment")
        account_type = verdict.summary.get("account_type")
        if environment and verdict.mode in {"sandbox", "live"}:
            lines.append(f"Longbridge Env: {str(environment).upper()}")
        if account_type and verdict.mode in {"sandbox", "live"}:
            lines.append(f"Account Type: {str(account_type).upper()}")
        if verdict.mode == "sandbox":
            lines.append(f"HTTP: {verdict.summary.get('http_url') or 'missing'}")
        if verdict.warnings:
            lines.append("")
            for warning in verdict.warnings:
                lines.append(f"WARNING: {warning}")
        if verdict.errors:
            lines.append("")
            for error in verdict.errors:
                lines.append(f"ERROR: {error}")
        lines.append("")
        lines.append("================================")
        return "\n".join(lines)

    def _validate_official_endpoints(
        self,
        http_url: str | None,
        quote_ws_url: str | None,
        trade_ws_url: str | None,
    ) -> list[str]:
        errors: list[str] = []
        endpoints = {
            "http_url": http_url,
            "quote_ws_url": quote_ws_url,
            "trade_ws_url": trade_ws_url,
        }
        endpoint_defaults = {
            "http_url": DEFAULT_PROD_HTTP_URL,
            "quote_ws_url": DEFAULT_PROD_QUOTE_WS_URL,
            "trade_ws_url": DEFAULT_PROD_TRADE_WS_URL,
        }
        for name, value in endpoints.items():
            if not value:
                errors.append(f"sandbox mode requires {name}")
                continue
            parsed = urlparse(str(value))
            if parsed.scheme not in {"http", "https", "ws", "wss"}:
                errors.append(f"sandbox {name} has invalid scheme: {value}")
                continue
            normalized = str(value).strip().rstrip("/")
            if normalized != endpoint_defaults[name]:
                errors.append(
                    f"sandbox {name} must use the official Longbridge endpoint: {endpoint_defaults[name]}"
                )
        return errors
