"""
Long Bridge (长桥证券) broker implementation.

Uses longbridge OpenAPI Python SDK v4.x:
    pip install longbridge

To enable live trading:
1. Get credentials from https://open.longbridge.com/
2. Set in config.yaml broker.longbridge.* fields
3. Or use env vars:
    export LONGBRIDGE_APP_KEY="..."
    export LONGBRIDGE_APP_SECRET="..."
    export LONGBRIDGE_ACCESS_TOKEN="..."
4. Run: python run.py --live
"""
import logging
import os
from typing import Optional

import longbridge.openapi as lb

from .base import (
    BrokerBase, Order, OrderSide, OrderType, OrderStatus,
    Position, AccountInfo,
)

logger = logging.getLogger(__name__)


class LongBridgeBroker(BrokerBase):
    """
    Long Bridge Securities — real trading via OpenAPI.
    """

    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        region: str = "cn",
    ):
        # Env vars take precedence over config file
        self._app_key = os.environ.get("LONGBRIDGE_APP_KEY", app_key)
        self._app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", app_secret)
        self._access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", access_token)
        self._region = region
        self._connected = False
        self._trade_ctx: Optional[lb.TradeContext] = None
        self._quote_ctx: Optional[lb.QuoteContext] = None

    def _build_config(self):
        """Build Config from credentials. Uses env var style (no explicit Config object needed)."""
        # The SDK reads env vars automatically, but we also set them explicitly
        os.environ.setdefault("LONGBRIDGE_APP_KEY", self._app_key)
        os.environ.setdefault("LONGBRIDGE_APP_SECRET", self._app_secret)
        os.environ.setdefault("LONGBRIDGE_ACCESS_TOKEN", self._access_token)

    def connect(self) -> bool:
        """Connect to Long Bridge OpenAPI."""
        if not self._app_key or not self._app_secret or not self._access_token:
            logger.error(
                "Long Bridge credentials not configured. Set in config.yaml "
                "or via LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / "
                "LONGBRIDGE_ACCESS_TOKEN environment variables."
            )
            return False

        try:
            self._build_config()

            self._trade_ctx = lb.TradeContext()
            self._quote_ctx = lb.QuoteContext()
            self._connected = True
            logger.info(f"✅ Long Bridge connected (region: {self._region})")
            return True

        except lb.OpenApiException as e:
            logger.error(f"Long Bridge auth failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Long Bridge connection failed: {e}")
            return False

    def disconnect(self) -> None:
        self._connected = False
        self._trade_ctx = None
        self._quote_ctx = None
        logger.info("Long Bridge disconnected")

    def is_connected(self) -> bool:
        return self._connected and self._trade_ctx is not None

    # ── Order Entry ──────────────────────────────────────

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
        if not self.is_connected():
            return Order(
                order_id="", ticker=ticker, side=side,
                order_type=order_type, quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Not connected to Long Bridge",
            )

        try:
            # Map our enums → Long Bridge enums
            lb_side = lb.OrderSide.Buy if side == OrderSide.BUY else lb.OrderSide.Sell
            lb_type = lb.OrderType.MO if order_type == OrderType.MARKET else lb.OrderType.LO

            # For market orders, use last price for limit price guard
            submit_price = limit_price
            if lb_type == lb.OrderType.MO and not submit_price:
                submit_price = current_ask if side == OrderSide.BUY else current_bid

            logger.info(
                f"🔴 [LIVE] {side.value} {quantity} {ticker} "
                f"@ {'MKT' if order_type == OrderType.MARKET else f'${limit_price:.2f}'}"
            )

            result: lb.SubmitOrderResponse = self._trade_ctx.submit_order(
                symbol=ticker,
                order_type=lb_type,
                side=lb_side,
                submitted_quantity=quantity,
                time_in_force=lb.TimeInForceType.Day,
                submitted_price=submit_price,
            )

            logger.info(f"  → Order ID: {result.order_id}")

            return Order(
                order_id=result.order_id,
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                status=OrderStatus.PENDING,
                notes=f"Live order {result.order_id[:12]}...",
            )

        except lb.OpenApiException as e:
            logger.error(f"Long Bridge order rejected: {e}")
            return Order(
                order_id="", ticker=ticker, side=side,
                order_type=order_type, quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=f"API error: {e}",
            )
        except Exception as e:
            logger.error(f"Long Bridge order error: {e}")
            return Order(
                order_id="", ticker=ticker, side=side,
                order_type=order_type, quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=str(e),
            )

    def cancel_order(self, order_id: str) -> bool:
        if not self.is_connected():
            return False
        try:
            self._trade_ctx.cancel_order(order_id=order_id)
            logger.info(f"Canceled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False

    def get_order(self, order_id: str) -> Optional[Order]:
        if not self.is_connected():
            return None
        try:
            od: lb.OrderDetail = self._trade_ctx.order_detail(order_id=order_id)

            # Map status
            status_map = {
                lb.OrderStatus.Filled: OrderStatus.FILLED,
                lb.OrderStatus.PartialFilled: OrderStatus.PARTIALLY_FILLED,
                lb.OrderStatus.Rejected: OrderStatus.REJECTED,
                lb.OrderStatus.Canceled: OrderStatus.CANCELED,
                lb.OrderStatus.Expired: OrderStatus.CANCELED,
                lb.OrderStatus.New: OrderStatus.PENDING,
                lb.OrderStatus.PendingCancel: OrderStatus.PENDING,
                lb.OrderStatus.WaitToNew: OrderStatus.PENDING,
            }
            mapped_status = status_map.get(od.status, OrderStatus.PENDING)

            return Order(
                order_id=od.order_id,
                ticker=od.symbol,
                side=OrderSide.BUY if od.side == lb.OrderSide.Buy else OrderSide.SELL,
                order_type=OrderType.MARKET if od.order_type == lb.OrderType.MO else OrderType.LIMIT,
                quantity=od.quantity,
                filled_quantity=od.executed_quantity,
                avg_fill_price=od.executed_price,
                status=mapped_status,
                notes=str(od.msg or ""),
            )
        except Exception as e:
            logger.error(f"Get order failed: {e}")
            return None

    # ── Positions ────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        if not self.is_connected():
            return []
        try:
            resp: lb.StockPositionsResponse = self._trade_ctx.stock_positions()
            positions = []
            for channel in resp.channels or []:
                for p in channel.positions or []:
                    positions.append(Position(
                        ticker=p.symbol,
                        quantity=p.quantity,
                        avg_entry_price=p.cost_price,
                        current_price=0.0,
                        market_value=p.quantity * p.cost_price,
                        unrealized_pnl=0.0,
                        unrealized_pnl_pct=0.0,
                    ))
            return positions
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            return []

    def get_position_for_ticker(self, ticker: str) -> Optional[Position]:
        for p in self.get_positions():
            if p.ticker.upper() == ticker.upper():
                return p
        return None

    # ── Account ──────────────────────────────────────────

    def get_account(self) -> AccountInfo:
        if not self.is_connected():
            return AccountInfo(cash=0, equity=0, buying_power=0, positions=[])

        try:
            bal: lb.AccountBalance = self._trade_ctx.account_balance()
            cash = float(bal.total_cash or 0)
            equity = float(bal.net_assets or 0)
            bp = float(bal.buy_power or 0)

            return AccountInfo(
                cash=round(cash, 2),
                equity=round(equity, 2),
                buying_power=round(bp, 2),
                positions=self.get_positions(),
            )
        except Exception as e:
            logger.error(f"Get account failed: {e}")
            return AccountInfo(cash=0, equity=0, buying_power=0, positions=[])

    # ── Quote ────────────────────────────────────────────

    def get_realtime_quote(self, ticker: str):
        """Get real-time quote via Long Bridge (faster than yfinance for live)."""
        if not self.is_connected() or not self._quote_ctx:
            return None
        try:
            resp = self._quote_ctx.quote(symbols=[ticker])
            return resp
        except Exception as e:
            logger.warning(f"Quote fetch failed: {e}")
            return None
