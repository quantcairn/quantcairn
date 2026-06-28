import json
import os
import re
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .broker import BrokerInterface

try:  # pragma: no cover - exercised in the real app, not in this sandbox
    import longbridge.openapi as lb  # type: ignore
except Exception:  # pragma: no cover - keep module importable without SDK installed
    lb = None


TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclass_isinstance(value):
        return value.__dict__
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def dataclass_isinstance(value: Any) -> bool:
    return hasattr(value, "__dataclass_fields__")


def _call_first(target: Any, candidates: list[str], *args: Any, **kwargs: Any) -> Any:
    last_error: Optional[Exception] = None
    for name in candidates:
        if not hasattr(target, name):
            continue
        method = getattr(target, name)
        try:
            return method(*args, **kwargs)
        except TypeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise AttributeError(f"{target!r} has none of {candidates}")


def _enum_value(enum_cls: Any, name: str) -> Any:
    value = getattr(enum_cls, name, None)
    if value is None:
        raise ValueError(f"{enum_cls.__name__} does not define {name}")
    return value


def _extract_attr(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            attr = getattr(value, name)
            if attr is not None:
                return attr
    return None


def _is_already_cancelled_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == 601011:
        return True
    if code is not None and str(code) == "601011":
        return True
    text = str(exc).lower()
    return "601011" in text and "cancelled" in text


def _normalize_longbridge_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if "." in symbol:
        return symbol
    if re.fullmatch(r"[A-Z0-9]{1,10}", symbol):
        return f"{symbol}.US"
    return symbol


def _account_mode_from_channel(account_channel: Optional[str]) -> tuple[str, str, bool]:
    channel = str(account_channel or "").strip().lower()
    if not channel:
        return "unknown", "未知", False
    if "paper" in channel:
        return "paper", "模拟", True
    if channel.startswith("lb_") or channel == "live":
        return "live", "实盘", False
    return "live", "实盘", False


def _extract_account_channel(positions: Any) -> Optional[str]:
    if isinstance(positions, dict):
        account_channel = positions.get("account_channel")
        if account_channel:
            return str(account_channel)
        channels = positions.get("channels")
        if isinstance(channels, list):
            for channel in channels:
                found = _extract_account_channel(channel)
                if found:
                    return found
        nested_positions = positions.get("positions")
        if nested_positions is not None:
            found = _extract_account_channel(nested_positions)
            if found:
                return found
    elif isinstance(positions, list):
        for item in positions:
            found = _extract_account_channel(item)
            if found:
                return found
    else:
        account_channel = _extract_attr(positions, "account_channel")
        if account_channel:
            return str(account_channel)
        channels = _extract_attr(positions, "channels")
        if channels is not None:
            return _extract_account_channel(channels)
        nested_positions = _extract_attr(positions, "positions")
        if nested_positions is not None:
            return _extract_account_channel(nested_positions)
    return None


class LongBridgeBroker(BrokerInterface):
    """LongBridge broker adapter using the legacy API-key path by default.

    Runtime modes:
      - `dry_run=True`: simulate every request and write audit records only.
      - `auth_mode=apikey` (default): use API key / secret / access token credentials.
      - `auth_mode=oauth`: optional OAuth fallback if you still want SDK-managed login.

    Environment:
      LONGBRIDGE_AUTH_MODE      apikey (default) or oauth.
      LONGBRIDGE_API_KEY       API key.
      LONGBRIDGE_API_SECRET    API secret.
      LONGBRIDGE_ACCESS_TOKEN  Access token.
      LONGBRIDGE_CLIENT_ID      OAuth client id for the SDK.
      LONGBRIDGE_CALLBACK_PORT  Local callback port for OAuth. Default: 60355.
      LONGBRIDGE_HTTP_URL      Optional sandbox/prod REST endpoint override.
      LONGBRIDGE_QUOTE_WS_URL  Optional quote websocket override.
      LONGBRIDGE_TRADE_WS_URL  Optional trade websocket override.
      LONGBRIDGE_LOG_PATH      Optional SDK log file path.
      DRY_RUN                  Defaults to true. Set false/0 only for real submission.

    Every broker call is also mirrored to `logs/trades-YYYYMMDD.jsonl`.
    """

    def __init__(
        self,
        *,
        auth_mode: Optional[str] = None,
        client_id: Optional[str] = None,
        callback_port: Optional[int] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        http_url: Optional[str] = None,
        quote_ws_url: Optional[str] = None,
        trade_ws_url: Optional[str] = None,
        log_path: Optional[str] = None,
        dry_run: Optional[bool] = None,
        log_dir: Optional[str] = None,
    ):
        self.auth_mode = (auth_mode or os.getenv("LONGBRIDGE_AUTH_MODE") or "apikey").strip().lower()
        self.client_id = client_id or os.getenv("LONGBRIDGE_CLIENT_ID") or ""
        callback_port_value = callback_port if callback_port is not None else os.getenv("LONGBRIDGE_CALLBACK_PORT", "60355")
        self.callback_port = int(callback_port_value)
        self.api_key = api_key or os.getenv("LONGBRIDGE_API_KEY") or ""
        self.api_secret = api_secret or os.getenv("LONGBRIDGE_API_SECRET") or ""
        self.access_token = access_token or os.getenv("LONGBRIDGE_ACCESS_TOKEN") or ""
        self.http_url = http_url or os.getenv("LONGBRIDGE_HTTP_URL") or os.getenv("LONGBRIDGE_BASE_URL") or ""
        self.quote_ws_url = quote_ws_url or os.getenv("LONGBRIDGE_QUOTE_WS_URL") or ""
        self.trade_ws_url = trade_ws_url or os.getenv("LONGBRIDGE_TRADE_WS_URL") or ""
        self.sdk_log_path = log_path or os.getenv("LONGBRIDGE_LOG_PATH") or ""
        self.timeout_seconds = 15.0
        self.dry_run = _env_bool("DRY_RUN", True) if dry_run is None else dry_run
        self._sdk_config = None
        self._trade_ctx = None
        self._quote_ctx = None

        root = _project_root()
        self.log_dir = Path(log_dir) if log_dir else root / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _audit_path(self) -> Path:
        return self.log_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"

    def _write_audit(self, record: Dict[str, Any]) -> None:
        record = _jsonable(record)
        record.setdefault("timestamp", _utc_now())
        with self._audit_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _ensure_sdk(self) -> None:
        if lb is None:
            raise RuntimeError(
                "longbridge.openapi is not installed. Install the LongBridge OpenAPI SDK "
                "before using live trading."
            )

    def _open_oauth_url(self, url: str) -> None:
        self._write_audit({
            "action": "oauth_authorize",
            "request": {"url": url},
            "response": {"browser_opened": True},
        })
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception:
            pass

    def _build_sdk_config(self):
        self._ensure_sdk()
        if self._sdk_config is not None:
            return self._sdk_config

        if self.auth_mode == "oauth":
            if not self.client_id:
                raise ValueError("LONGBRIDGE_CLIENT_ID is required when auth_mode=oauth")
            oauth_builder = lb.OAuthBuilder(self.client_id, self.callback_port)
            oauth = oauth_builder.build(self._open_oauth_url)
            self._sdk_config = lb.Config.from_oauth(
                oauth,
                http_url=self.http_url or None,
                quote_ws_url=self.quote_ws_url or None,
                trade_ws_url=self.trade_ws_url or None,
                log_path=self.sdk_log_path or None,
            )
        elif self.auth_mode == "apikey":
            if not (self.api_key and self.api_secret):
                raise ValueError("LONGBRIDGE_API_KEY and LONGBRIDGE_API_SECRET are required for apikey mode")
            from_apikey = lb.Config.from_apikey
            kwargs: Dict[str, Any] = {}
            if self.access_token:
                kwargs["access_token"] = self.access_token
            if self.http_url:
                kwargs["http_url"] = self.http_url
            if self.quote_ws_url:
                kwargs["quote_ws_url"] = self.quote_ws_url
            if self.trade_ws_url:
                kwargs["trade_ws_url"] = self.trade_ws_url
            if self.sdk_log_path:
                kwargs["log_path"] = self.sdk_log_path
            try:
                self._sdk_config = from_apikey(self.api_key, self.api_secret, **kwargs)
            except TypeError:
                # Compatibility with older SDK signatures that only accept the credentials.
                self._sdk_config = from_apikey(self.api_key, self.api_secret)
        else:
            raise ValueError("LONGBRIDGE_AUTH_MODE must be oauth or apikey")

        return self._sdk_config

    def _ensure_contexts(self) -> None:
        if self._trade_ctx is not None and self._quote_ctx is not None:
            return
        config = self._build_sdk_config()
        trade_ctx_cls = getattr(lb, "TradeContext")
        quote_ctx_cls = getattr(lb, "QuoteContext")
        self._trade_ctx = trade_ctx_cls(config)
        self._quote_ctx = quote_ctx_cls(config)

    def ensure_connected(self) -> None:
        self._ensure_contexts()

    @property
    def runtime_mode(self) -> str:
        return "dry_run" if self.dry_run else "live"

    @property
    def trade_context(self):
        self._ensure_contexts()
        return self._trade_ctx

    @property
    def quote_context(self):
        self._ensure_contexts()
        return self._quote_ctx

    def _sdk_order_type(self, order_type: str) -> Any:
        mapping = {
            "market": "MO",
            "limit": "LO",
        }
        try:
            enum_cls = getattr(lb, "OrderType")
        except Exception:
            enum_cls = None
        try:
            code = mapping[order_type]
        except KeyError as exc:
            raise ValueError("order_type must be market or limit") from exc
        if enum_cls is None:
            return code
        return _enum_value(enum_cls, code)

    def _sdk_order_side(self, side: str) -> Any:
        mapping = {
            "buy": "Buy",
            "sell": "Sell",
        }
        try:
            enum_cls = getattr(lb, "OrderSide")
        except Exception:
            enum_cls = None
        try:
            code = mapping[side]
        except KeyError as exc:
            raise ValueError("order side must be buy or sell") from exc
        if enum_cls is None:
            return code
        return _enum_value(enum_cls, code)

    def _sdk_time_in_force(self, tif: str) -> Any:
        mapping = {
            "day": "Day",
            "good_til_canceled": "GoodTilCanceled",
            "gtc": "GoodTilCanceled",
        }
        try:
            enum_cls = getattr(lb, "TimeInForceType")
        except Exception:
            enum_cls = None
        try:
            code = mapping[tif]
        except KeyError as exc:
            raise ValueError("unsupported time_in_force") from exc
        if enum_cls is None:
            return code
        return _enum_value(enum_cls, code)

    def _simulate_response(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        trace_id = f"dryrun-{uuid.uuid4().hex}"
        response = {
            "trace_id": trace_id,
            "status": "simulated",
            "dry_run": True,
            "action": action,
            "request": payload,
        }
        self._write_audit({
            "trace_id": trace_id,
            "action": action,
            "dry_run": True,
            "request": {"payload": payload},
            "response": response,
        })
        return response

    def _sdk_call(self, action: str, payload: Dict[str, Any], method_names: list[str]) -> Dict[str, Any]:
        self._ensure_contexts()
        trace_id = f"lb-{uuid.uuid4().hex}"
        self._write_audit({
            "trace_id": trace_id,
            "action": action,
            "dry_run": False,
            "request": {"payload": payload},
        })
        try:
            result = _call_first(self._trade_ctx, method_names, payload)
        except Exception as exc:
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": action,
                "dry_run": False,
                "response": error,
            })
            raise

        response = {
            "trace_id": trace_id,
            "ok": True,
            "body": _jsonable(result),
        }
        self._write_audit({
            "trace_id": trace_id,
            "action": action,
            "dry_run": False,
            "response": response,
        })
        return response

    def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_order(order)
        if self.dry_run:
            response = self._simulate_response("place_order", normalized)
            response["order_id"] = f"dryrun-{response['trace_id'][:12]}"
            response["status"] = "simulated_submitted"
            return response

        self._ensure_contexts()
        trace_id = f"lb-{uuid.uuid4().hex}"
        self._write_audit({
            "trace_id": trace_id,
            "action": "place_order",
            "dry_run": False,
            "request": {"payload": normalized},
        })
        try:
            result = self._trade_ctx.submit_order(
                normalized["symbol"],
                self._sdk_order_type(normalized["order_type"]),
                self._sdk_order_side(normalized["side"]),
                normalized["quantity"],
                self._sdk_time_in_force(normalized["time_in_force"]),
                submitted_price=normalized.get("price"),
                remark=_jsonable(normalized.get("metadata")) if normalized.get("metadata") else None,
            )
        except Exception as exc:
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": "place_order",
                "dry_run": False,
                "response": error,
            })
            raise

        response = {
            "trace_id": trace_id,
            "ok": True,
            "body": _jsonable(result),
        }
        self._write_audit({
            "trace_id": trace_id,
            "action": "place_order",
            "dry_run": False,
            "response": response,
        })
        body = response.get("body")
        order_id = _extract_attr(body, "order_id", "id")
        if order_id:
            response["order_id"] = str(order_id)
        return response

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        payload = {"order_id": order_id}
        if self.dry_run:
            response = self._simulate_response("cancel_order", payload)
            response["order_id"] = order_id
            response["status"] = "simulated_cancelled"
            return response
        self._ensure_contexts()
        trace_id = f"lb-{uuid.uuid4().hex}"
        self._write_audit({
            "trace_id": trace_id,
            "action": "cancel_order",
            "dry_run": False,
            "request": {"payload": payload},
        })
        try:
            result = self._trade_ctx.cancel_order(order_id)
        except Exception as exc:
            if _is_already_cancelled_error(exc):
                response = {
                    "trace_id": trace_id,
                    "ok": True,
                    "body": {"order_id": order_id, "status": "already_cancelled"},
                    "order_id": order_id,
                }
                self._write_audit({
                    "trace_id": trace_id,
                    "action": "cancel_order",
                    "dry_run": False,
                    "response": response,
                })
                return response
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": "cancel_order",
                "dry_run": False,
                "response": error,
            })
            raise
        response = {
            "trace_id": trace_id,
            "ok": True,
            "body": _jsonable(result),
            "order_id": order_id,
        }
        self._write_audit({
            "trace_id": trace_id,
            "action": "cancel_order",
            "dry_run": False,
            "response": response,
        })
        return response

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        payload = {"order_id": order_id}
        if self.dry_run:
            response = self._simulate_response("get_order_status", payload)
            response["order_id"] = order_id
            response["status"] = "simulated_filled"
            return response
        self._ensure_contexts()
        trace_id = f"lb-{uuid.uuid4().hex}"
        self._write_audit({
            "trace_id": trace_id,
            "action": "get_order_status",
            "dry_run": False,
            "request": {"payload": payload},
        })
        try:
            result = self._trade_ctx.order_detail(order_id)
        except Exception as exc:
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": "get_order_status",
                "dry_run": False,
                "response": error,
            })
            raise
        response = {
            "trace_id": trace_id,
            "ok": True,
            "body": _jsonable(result),
            "order_id": order_id,
        }
        self._write_audit({
            "trace_id": trace_id,
            "action": "get_order_status",
            "dry_run": False,
            "response": response,
        })
        return response

    def get_positions(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.dry_run:
            response = self._simulate_response("get_positions", payload)
            response["positions"] = []
            response["account_channel"] = "lb_papertrading"
            response["account_mode"], response["account_mode_label"], response["is_paper_trading"] = _account_mode_from_channel(
                response["account_channel"]
            )
            return response
        self._ensure_contexts()
        trace_id = f"lb-{uuid.uuid4().hex}"
        self._write_audit({
            "trace_id": trace_id,
            "action": "get_positions",
            "dry_run": False,
            "request": {"payload": payload},
        })
        try:
            result = self._trade_ctx.stock_positions()
        except Exception as exc:
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": "get_positions",
                "dry_run": False,
                "response": error,
            })
            raise
        response = {
            "trace_id": trace_id,
            "ok": True,
            "positions": _jsonable(result),
        }
        account_channel = _extract_account_channel(response["positions"])
        account_mode, account_mode_label, is_paper_trading = _account_mode_from_channel(account_channel)
        response["account_channel"] = account_channel
        response["account_mode"] = account_mode
        response["account_mode_label"] = account_mode_label
        response["is_paper_trading"] = is_paper_trading
        self._write_audit({
            "trace_id": trace_id,
            "action": "get_positions",
            "dry_run": False,
            "response": response,
        })
        return response

    def get_account_balance(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.dry_run:
            response = self._simulate_response("get_account_balance", payload)
            response["account_balance"] = {}
            return response
        self._ensure_contexts()
        trace_id = f"lb-{uuid.uuid4().hex}"
        self._write_audit({
            "trace_id": trace_id,
            "action": "get_account_balance",
            "dry_run": False,
            "request": {"payload": payload},
        })
        try:
            result = _call_first(
                self._trade_ctx,
                ["account_balance", "balance", "asset_balance", "account_summary"],
            )
        except Exception as exc:
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": "get_account_balance",
                "dry_run": False,
                "response": error,
            })
            raise
        response = {
            "trace_id": trace_id,
            "ok": True,
            "account_balance": _jsonable(result),
        }
        self._write_audit({
            "trace_id": trace_id,
            "action": "get_account_balance",
            "dry_run": False,
            "response": response,
        })
        return response

    def _normalize_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        symbol = order.get("symbol") or order.get("ticker")
        side = str(order.get("side", "")).lower()
        quantity = order.get("qty", order.get("quantity"))
        order_type = str(order.get("order_type", order.get("type", "market"))).lower()

        if not symbol:
            raise ValueError("order requires symbol or ticker")
        if side not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise ValueError("order quantity must be an integer")
        if quantity <= 0:
            raise ValueError("order quantity must be positive")
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")

        normalized: Dict[str, Any] = {
            "symbol": _normalize_longbridge_symbol(str(symbol)),
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "client_order_id": order.get("client_order_id") or f"soxs-{uuid.uuid4().hex[:16]}",
        }
        if order.get("price") is not None:
            normalized["price"] = float(order["price"])
        if order.get("time_in_force"):
            normalized["time_in_force"] = order["time_in_force"]
        else:
            normalized["time_in_force"] = "day"
        if order.get("metadata"):
            normalized["metadata"] = order["metadata"]
        return normalized
