"""
Long Bridge (长桥证券) broker implementation for the live trading engine.

This adapter is the main live path used by ``run.py --live``.
It supports:
- API key credentials from env vars or config
- sandbox/prod endpoint override via env/config
- audit logging to ``logs/trades-YYYYMMDD.jsonl``
"""
import dataclasses
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from enum import Enum

import longbridge.openapi as lb

from .base import (
    AccountInfo,
    BrokerBase,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "y", "on"}

DEFAULT_PROD_HTTP_URL = "https://openapi.longbridge.com"
DEFAULT_PROD_QUOTE_WS_URL = "wss://openapi-quote.longbridge.com/v2"
DEFAULT_PROD_TRADE_WS_URL = "wss://openapi-trade.longbridge.com/v2"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _jsonable(value):
    try:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Enum):
            return value.value
        if callable(value):
            return getattr(value, "__name__", repr(value))
        if dataclasses.is_dataclass(value):
            return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(v) for v in value]
        if hasattr(value, "__dict__"):
            try:
                attrs = vars(value)
            except Exception:
                attrs = {}
            return {
                key: _jsonable(val)
                for key, val in attrs.items()
                if not key.startswith("_")
            }
        return str(value)
    except Exception:
        return repr(value)


def _first_item(value):
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _balance_field(value, *names, default=0.0):
    value = _first_item(value)
    for name in names:
        if isinstance(value, dict):
            if name in value and value[name] is not None:
                return value[name]
        elif hasattr(value, name):
            attr = getattr(value, name)
            if attr is not None:
                return attr
    return default


def _quote_field(value, *names, default=0.0):
    value = _first_item(value)
    for name in names:
        if isinstance(value, dict):
            if name in value and value[name] is not None:
                return value[name]
        elif hasattr(value, name):
            attr = getattr(value, name)
            if attr is not None:
                return attr
    return default


def _normalize_base_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().split(".")[0]


def _longbridge_symbol(symbol: str) -> str:
    base = _normalize_base_symbol(symbol)
    if not base:
        return ""
    return base if "." in base else f"{base}.US"


class LongBridgeBroker(BrokerBase):
    """Long Bridge Securities live broker with sandbox support and audits."""

    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        region: str = "cn",
        environment: str = "prod",
        http_url: str | None = None,
        quote_ws_url: str | None = None,
        trade_ws_url: str | None = None,
        log_path: str | None = None,
        audit_dir: str | None = None,
    ):
        self._app_key = (
            os.environ.get("LONGBRIDGE_APP_KEY")
            or os.environ.get("LONGBRIDGE_API_KEY")
            or app_key
        )
        self._app_secret = (
            os.environ.get("LONGBRIDGE_APP_SECRET")
            or os.environ.get("LONGBRIDGE_API_SECRET")
            or app_secret
        )
        self._access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", access_token)
        self._region = os.environ.get("LONGBRIDGE_REGION", region)
        self._environment = os.environ.get("LONGBRIDGE_ENV", environment).strip().lower()

        self._http_url = os.environ.get("LONGBRIDGE_HTTP_URL", http_url or "") or None
        self._quote_ws_url = os.environ.get("LONGBRIDGE_QUOTE_WS_URL", quote_ws_url or "") or None
        self._trade_ws_url = os.environ.get("LONGBRIDGE_TRADE_WS_URL", trade_ws_url or "") or None

        if self._environment == "sandbox":
            self._http_url = self._http_url or os.environ.get("LONGBRIDGE_SANDBOX_HTTP_URL")
            self._quote_ws_url = self._quote_ws_url or os.environ.get("LONGBRIDGE_SANDBOX_QUOTE_WS_URL")
            self._trade_ws_url = self._trade_ws_url or os.environ.get("LONGBRIDGE_SANDBOX_TRADE_WS_URL")

        self._sdk_log_path = os.environ.get("LONGBRIDGE_LOG_PATH", log_path or "") or None

        audit_dir_value = os.environ.get("LONGBRIDGE_AUDIT_DIR", audit_dir or "") or None
        if audit_dir_value:
            self._audit_dir = Path(audit_dir_value)
        else:
            self._audit_dir = Path.cwd() / "logs"
        try:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = Path(tempfile.gettempdir()) / "soxs-range-arbitrage" / "logs"
            fallback.mkdir(parents=True, exist_ok=True)
            self._audit_dir = fallback
        self._connected = False
        self._sdk_config: Optional[lb.Config] = None
        self._trade_ctx: Optional[lb.TradeContext] = None
        self._quote_ctx: Optional[lb.QuoteContext] = None
        self._account_cache = AccountInfo(cash=0, equity=0, buying_power=0, positions=[])
        self._positions_cache: list[Position] = []
        self._account_cache_fetched_at = 0.0
        self._positions_cache_fetched_at = 0.0
        self._account_retry_not_before = 0.0
        self._positions_retry_not_before = 0.0
        ttl_env = os.environ.get("LONGBRIDGE_CACHE_TTL_SECONDS")
        ttl_seconds = float(ttl_env) if ttl_env else 60.0
        self._account_cache_ttl_seconds = max(5.0, ttl_seconds)
        self._positions_cache_ttl_seconds = max(5.0, ttl_seconds)
        self._cache_retry_backoff_seconds = max(5.0, min(self._account_cache_ttl_seconds, self._positions_cache_ttl_seconds))

    def _audit_path(self) -> Path:
        return self._audit_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"

    def _write_audit(self, action: str, request: dict, response: dict, *, ok: bool, error: str | None = None) -> None:
        try:
            record = {
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "broker": "longbridge",
                "environment": self._environment,
                "region": self._region,
                "action": action,
                "ok": ok,
                "request": _jsonable(request),
                "response": _jsonable(response),
            }
            if error:
                record["error"] = error

            with self._audit_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as audit_error:
            logger.warning("Long Bridge audit write skipped for %s: %s", action, audit_error)

    def _build_config(self) -> lb.Config:
        """Build a LongBridge SDK config from credentials and endpoint overrides."""
        return lb.Config.from_apikey(
            self._app_key,
            self._app_secret,
            self._access_token,
            http_url=self._http_url,
            quote_ws_url=self._quote_ws_url,
            trade_ws_url=self._trade_ws_url,
            log_path=self._sdk_log_path,
        )

    def connect(self) -> bool:
        if not self._app_key or not self._app_secret or not self._access_token:
            logger.error(
                "Long Bridge credentials not configured. Set config.yaml fields "
                "or LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN."
            )
            return False

        if self._environment == "sandbox" and not (self._http_url and self._quote_ws_url and self._trade_ws_url):
            logger.warning(
                "Long Bridge sandbox selected but endpoint URLs are incomplete. "
                "Set LONGBRIDGE_SANDBOX_HTTP_URL / LONGBRIDGE_SANDBOX_QUOTE_WS_URL / "
                "LONGBRIDGE_SANDBOX_TRADE_WS_URL, or provide direct URL overrides."
            )

        try:
            self._sdk_config = self._build_config()
            self._trade_ctx = lb.TradeContext(self._sdk_config)
            self._quote_ctx = lb.QuoteContext(self._sdk_config)
            self._connected = True
            logger.info(
                "✅ Long Bridge connected (environment: %s, region: %s)",
                self._environment,
                self._region,
            )
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": True},
                ok=True,
            )
            return True
        except lb.OpenApiException as e:
            logger.error(f"Long Bridge auth failed: {e}")
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": False},
                ok=False,
                error=str(e),
            )
            return False
        except Exception as e:
            logger.error(f"Long Bridge connection failed: {e}")
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": False},
                ok=False,
                error=str(e),
            )
            return False

    def disconnect(self) -> None:
        self._connected = False
        self._trade_ctx = None
        self._quote_ctx = None
        self._sdk_config = None
        logger.info("Long Bridge disconnected")

    def is_connected(self) -> bool:
        return self._connected and self._trade_ctx is not None

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        current_bid: float = 0.0,
        current_ask: float = 0.0,
        notes: str = "",
    ) -> Order:
        request = {
            "ticker": ticker,
            "side": side.value,
            "quantity": quantity,
            "order_type": order_type.value,
            "limit_price": limit_price,
            "current_bid": current_bid,
            "current_ask": current_ask,
            "notes": notes,
        }
        if not self.is_connected():
            response = {"status": "rejected", "reason": "not connected"}
            self._write_audit("place_order", request, response, ok=False, error="Not connected to Long Bridge")
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Not connected to Long Bridge",
            )

        try:
            lb_side = lb.OrderSide.Buy if side == OrderSide.BUY else lb.OrderSide.Sell
            lb_type = lb.OrderType.MO if order_type == OrderType.MARKET else lb.OrderType.LO
            submit_price = limit_price
            if lb_type == lb.OrderType.MO and not submit_price:
                submit_price = current_ask if side == OrderSide.BUY else current_bid
            lb_symbol = _longbridge_symbol(ticker)

            logger.info(
                "🔴 [LIVE/%s] %s %s %s @ %s",
                self._environment,
                side.value,
                quantity,
                ticker,
                "MKT" if order_type == OrderType.MARKET else f"${limit_price:.2f}",
            )

            result: lb.SubmitOrderResponse = self._trade_ctx.submit_order(
                symbol=lb_symbol,
                order_type=lb_type,
                side=lb_side,
                submitted_quantity=quantity,
                time_in_force=lb.TimeInForceType.Day,
                submitted_price=submit_price,
            )

            response = {
                "order_id": str(getattr(result, "order_id", "") or ""),
                "status": str(getattr(result, "status", "submitted") or "submitted"),
                "raw": _jsonable(result),
            }
            self._write_audit("place_order", request, response, ok=True)
            logger.info("  → Order ID: %s", response["order_id"])
            return Order(
                order_id=response["order_id"],
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                status=OrderStatus.PENDING,
                notes=f"Live order {response['order_id'][:12]}...",
            )
        except lb.OpenApiException as e:
            logger.error(f"Long Bridge order rejected: {e}")
            self._write_audit("place_order", request, {"error": str(e)}, ok=False, error=str(e))
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=f"API error: {e}",
            )
        except Exception as e:
            logger.error(f"Long Bridge order error: {e}")
            self._write_audit("place_order", request, {"error": str(e)}, ok=False, error=str(e))
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=str(e),
            )

    def cancel_order(self, order_id: str) -> bool:
        request = {"order_id": order_id}
        if not self.is_connected():
            self._write_audit("cancel_order", request, {"status": "rejected"}, ok=False, error="Not connected")
            return False
        try:
            result = self._trade_ctx.cancel_order(order_id=order_id)
            self._write_audit("cancel_order", request, {"result": _jsonable(result)}, ok=True)
            logger.info("Canceled order %s", order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            self._write_audit("cancel_order", request, {"error": str(e)}, ok=False, error=str(e))
            return False

    def get_order(self, order_id: str) -> Optional[Order]:
        request = {"order_id": order_id}
        if not self.is_connected():
            self._write_audit("get_order", request, {"status": "rejected"}, ok=False, error="Not connected")
            return None
        try:
            # The SDK declares order_id as positional-only. Passing it by keyword
            # causes every reconciliation request to fail after a live submission.
            od: lb.OrderDetail = self._trade_ctx.order_detail(order_id)
            status_map = {
                lb.OrderStatus.Filled: OrderStatus.FILLED,
                lb.OrderStatus.PartialFilled: OrderStatus.PARTIALLY_FILLED,
                lb.OrderStatus.Rejected: OrderStatus.REJECTED,
                lb.OrderStatus.Canceled: OrderStatus.CANCELLED,
                lb.OrderStatus.Expired: OrderStatus.CANCELLED,
                lb.OrderStatus.New: OrderStatus.PENDING,
                lb.OrderStatus.PendingCancel: OrderStatus.PENDING,
                lb.OrderStatus.WaitToNew: OrderStatus.PENDING,
            }
            mapped_status = status_map.get(od.status, OrderStatus.PENDING)
            response = {"order": _jsonable(od), "mapped_status": mapped_status.value}
            self._write_audit("get_order", request, response, ok=True)
            return Order(
                order_id=od.order_id,
                ticker=od.symbol,
                side=OrderSide.BUY if od.side == lb.OrderSide.Buy else OrderSide.SELL,
                order_type=OrderType.MARKET if od.order_type == lb.OrderType.MO else OrderType.LIMIT,
                quantity=int(od.quantity or 0),
                filled_quantity=int(od.executed_quantity or 0),
                avg_fill_price=float(od.executed_price or 0.0),
                status=mapped_status,
                notes=str(od.msg or ""),
            )
        except Exception as e:
            logger.error(f"Get order failed: {e}")
            self._write_audit("get_order", request, {"error": str(e)}, ok=False, error=str(e))
            return None

    def get_active_orders(self, ticker: str) -> Optional[list[Order]]:
        """Return unresolved orders for a symbol so engine startup can fail closed."""
        if not self.is_connected():
            return []
        try:
            details = self._trade_ctx.today_orders(_longbridge_symbol(ticker))
            active_statuses = {
                lb.OrderStatus.New,
                lb.OrderStatus.PartialFilled,
                lb.OrderStatus.PendingCancel,
                lb.OrderStatus.WaitToNew,
                lb.OrderStatus.NotReported,
                lb.OrderStatus.ProtectedNotReported,
                lb.OrderStatus.VarietiesNotReported,
            }
            orders = []
            for od in details or []:
                if od.status not in active_statuses:
                    continue
                orders.append(Order(
                    order_id=od.order_id,
                    ticker=od.symbol,
                    side=OrderSide.BUY if od.side == lb.OrderSide.Buy else OrderSide.SELL,
                    order_type=OrderType.MARKET if od.order_type == lb.OrderType.MO else OrderType.LIMIT,
                    quantity=int(od.quantity or 0),
                    filled_quantity=int(od.executed_quantity or 0),
                    avg_fill_price=float(od.executed_price or 0.0),
                    status=(
                        OrderStatus.PARTIALLY_FILLED
                        if od.status == lb.OrderStatus.PartialFilled
                        else OrderStatus.PENDING
                    ),
                    notes=str(od.msg or ""),
                ))
            self._write_audit(
                "get_active_orders",
                {"ticker": ticker},
                {"count": len(orders), "order_ids": [o.order_id for o in orders]},
                ok=True,
            )
            return orders
        except Exception as exc:
            logger.error("Get active orders failed: %s", exc)
            self._write_audit(
                "get_active_orders",
                {"ticker": ticker},
                {"error": str(exc)},
                ok=False,
                error=str(exc),
            )
            # None distinguishes an API failure from a confirmed empty result.
            return None

    def get_positions(self) -> list[Position]:
        request = {}
        now = time.time()
        if now < self._positions_retry_not_before:
            return list(self._positions_cache)
        if (
            self._positions_cache_fetched_at > 0
            and (now - self._positions_cache_fetched_at) < self._positions_cache_ttl_seconds
        ):
            return list(self._positions_cache)
        if not self.is_connected():
            self._write_audit("get_positions", request, {"positions": []}, ok=False, error="Not connected")
            return list(self._positions_cache)
        try:
            resp: lb.StockPositionsResponse = self._trade_ctx.stock_positions()
            positions = []
            quote_map: dict[str, float] = {}
            raw_positions = []
            for channel in resp.channels or []:
                for p in channel.positions or []:
                    raw_positions.append(p)

            if raw_positions and self._quote_ctx:
                try:
                    symbols = [_longbridge_symbol(p.symbol) for p in raw_positions if _longbridge_symbol(p.symbol)]
                    if symbols:
                        quote_resp = self._quote_ctx.quote(symbols=symbols)
                        quote_items = quote_resp if isinstance(quote_resp, (list, tuple)) else [quote_resp]
                        for item in quote_items:
                            symbol = _normalize_base_symbol(
                                _quote_field(item, "symbol", "code", "ticker", default="")
                            )
                            price = float(_quote_field(item, "last_done", "price", "last_price", default=0.0) or 0.0)
                            if symbol and price > 0:
                                quote_map[symbol] = price
                except Exception as e:
                    logger.warning(f"Quote enrichment for positions failed: {e}")

            for p in raw_positions:
                ticker = _normalize_base_symbol(p.symbol)
                quantity = int(float(getattr(p, "quantity", 0) or 0))
                avg_entry_price = float(getattr(p, "cost_price", 0.0) or 0.0)
                current_price = float(quote_map.get(ticker, 0.0) or 0.0)
                if current_price <= 0:
                    current_price = avg_entry_price
                market_value = round(quantity * current_price, 3)
                cost_value = quantity * avg_entry_price
                unrealized_pnl = round((current_price - avg_entry_price) * quantity, 3)
                unrealized_pnl_pct = round((unrealized_pnl / cost_value * 100.0), 3) if cost_value > 0 else 0.0
                positions.append(
                    Position(
                        ticker=ticker,
                        quantity=quantity,
                        avg_entry_price=avg_entry_price,
                        current_price=round(current_price, 3),
                        market_value=market_value,
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_pct=unrealized_pnl_pct,
                    )
                )
            self._positions_cache = positions
            self._positions_cache_fetched_at = now
            self._positions_retry_not_before = now
            self._write_audit("get_positions", request, {"positions": positions}, ok=True)
            return positions
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            self._positions_retry_not_before = now + self._cache_retry_backoff_seconds
            self._write_audit("get_positions", request, {"error": str(e)}, ok=False, error=str(e))
            return list(self._positions_cache)

    def get_position_for_ticker(self, ticker: str) -> Optional[Position]:
        target = _normalize_base_symbol(ticker)
        for p in self.get_positions():
            if _normalize_base_symbol(p.ticker) == target:
                return p
        return None

    def get_account(self) -> AccountInfo:
        request = {}
        now = time.time()
        if now < self._account_retry_not_before:
            return self._account_cache
        if (
            self._account_cache_fetched_at > 0
            and (now - self._account_cache_fetched_at) < self._account_cache_ttl_seconds
        ):
            return self._account_cache
        if not self.is_connected():
            self._write_audit(
                "get_account",
                request,
                {"cash": 0, "equity": 0, "buying_power": 0},
                ok=False,
                error="Not connected",
            )
            return self._account_cache

        try:
            if not self._positions_cache:
                self.get_positions()
            bal = self._trade_ctx.account_balance()
            cash = float(_balance_field(bal, "total_cash", "cash", "cash_balance", "available_cash", default=0) or 0)
            equity = float(_balance_field(bal, "net_assets", "equity", "net_liquidation", "total_equity", default=0) or 0)
            bp = float(_balance_field(bal, "buy_power", "buying_power", "available_buying_power", default=0) or 0)

            account = AccountInfo(
                cash=round(cash, 2),
                equity=round(equity, 2),
                buying_power=round(bp, 2),
                positions=list(self._positions_cache),
            )
            self._account_cache = account
            self._account_cache_fetched_at = now
            self._account_retry_not_before = now
            self._write_audit("get_account", request, account, ok=True)
            return self._account_cache
        except Exception as e:
            logger.error(f"Get account failed: {e}")
            self._account_retry_not_before = now + self._cache_retry_backoff_seconds
            self._write_audit("get_account", request, {"error": str(e)}, ok=False, error=str(e))
            return self._account_cache

    def get_realtime_quote(self, ticker: str):
        request = {"ticker": ticker}
        if not self.is_connected() or not self._quote_ctx:
            self._write_audit("get_realtime_quote", request, {"quote": None}, ok=False, error="Not connected")
            return None
        try:
            resp = self._quote_ctx.quote(symbols=[_longbridge_symbol(ticker)])
            self._write_audit("get_realtime_quote", request, {"quote": resp}, ok=True)
            return resp
        except Exception as e:
            logger.warning(f"Quote fetch failed: {e}")
            self._write_audit("get_realtime_quote", request, {"error": str(e)}, ok=False, error=str(e))
            return None
