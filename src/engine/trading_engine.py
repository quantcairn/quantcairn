"""
Trading Engine: main loop that orchestrates all components.

Flow:
  1. Fetch price → 2. Feed strategy → 3. Generate signal
  → 4. Risk check → 5. Place order → 6. Notify → 7. Log

Runs in paper or live mode.
"""
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pytz

from ..config.loader import AppConfig
from ..data.fetcher import PriceFetcher
from ..strategy.range_detector import RangeDetector, SignalType
from ..risk.manager import RiskManager, TradeRecord
from ..broker.base import BrokerBase, OrderSide, OrderStatus, OrderType
from ..broker.paper_broker import PaperBroker
from ..notifier.alerts import Notifier
from .position_sizing import determine_buy_quantity

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]

# Try to import pytz, fall back if not available
try:
    import pytz as _pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False


class TradingEngine:
    """
    Main trading engine. Orchestrates price fetching, strategy evaluation,
    risk management, and order execution.
    """

    def __init__(self, config: AppConfig, ignore_trading_hours: bool = False):
        self.config = config
        self.ticker = config.ticker
        self.mode = config.mode
        self._ignore_trading_hours = ignore_trading_hours

        # Initialize components
        self.fetcher = PriceFetcher(
            ticker=config.ticker,
            poll_interval=config.data.poll_interval_seconds,
        )

        self.strategy = RangeDetector(
            ticker=config.ticker,
            mode=config.range.mode,
            support_price=config.range.support_price,
            resistance_price=config.range.resistance_price,
            tolerance_pct=config.range.tolerance_pct,
            auto_lookback=config.range.auto_lookback,
            auto_refresh_minutes=config.range.auto_refresh_minutes,
            trend_ma_period=config.trend_filter.ma_period,
            trend_enabled=config.trend_filter.enabled,
            trend_min_strength=config.trend_filter.min_trend_strength,
            min_profit_per_trade=config.range.min_profit_per_trade,
            min_range_width_pct=config.range.min_range_width_pct,
            quick_stop_pct=config.range.quick_stop_pct,
        )

        self.risk = RiskManager(
            stop_loss_pct=config.risk.stop_loss_pct,
            daily_loss_limit=config.risk.daily_loss_limit,
            max_consecutive_losses=config.risk.max_consecutive_losses,
            max_position=config.position.max_position,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            cool_down_seconds=config.position.cool_down_seconds,
            state_path=PROJECT_DIR / "state" / "risk" / f"{self.ticker.upper()}.json",
        )

        # Broker setup
        if config.mode == "live" and config.broker.longbridge.enabled:
            from ..broker.longbridge_broker import LongBridgeBroker
            self.broker: BrokerBase = LongBridgeBroker(
                app_key=config.broker.longbridge.app_key,
                app_secret=config.broker.longbridge.app_secret,
                access_token=config.broker.longbridge.access_token,
                region=config.broker.longbridge.region,
                environment=config.broker.longbridge.environment,
                http_url=config.broker.longbridge.http_url,
                quote_ws_url=config.broker.longbridge.quote_ws_url,
                trade_ws_url=config.broker.longbridge.trade_ws_url,
                log_path=config.broker.longbridge.log_path,
            )
            logger.info("Using Long Bridge (LIVE) broker")
        else:
            self.broker = PaperBroker(initial_cash=config.position.initial_capital)
            logger.info(f"Using Paper Trading broker (initial capital: ${config.position.initial_capital:,.2f})")

        self.notifier = Notifier(
            console=config.notifications.console,
            macos_notification=config.notifications.macos_notification,
            webhook_url=config.notifications.webhook_url,
            trade_summary_interval=config.notifications.trade_summary_interval,
        )

        # State
        self._running = False
        self._entry_price: Optional[float] = None
        self._position_shares: int = 0
        self._last_signal_type: Optional[SignalType] = None
        self._start_time: Optional[datetime] = None
        self._latest_account = None
        self._latest_position = None
        self._latest_snapshot_at: Optional[datetime] = None
        self._trade_in_progress = False
        self._last_signal_reason: str = "暂无"
        self._pending_order: Optional[dict] = None
        self._pending_order_state_path = (
            PROJECT_DIR / "state" / "pending_orders" / f"{self.ticker.upper()}.json"
        )
        self._load_pending_order()
        self._reduce_only = bool(getattr(config.position, "reduce_only", False))

        # NY timezone
        self._ny_tz = _pytz.timezone("America/New_York") if HAS_PYTZ else None

    def _seed_auto_range(self) -> None:
        """Seed auto range from recent OHLCV history so it's ready immediately."""
        if self.config.range.mode != "auto":
            return

        candles = self.fetcher.get_ohlcv(period="5d", interval="5m")
        if candles and self.strategy.seed_from_ohlcv(candles):
            rs = self.strategy.get_range_state()
            logger.info(
                f"Seeded auto range: ${rs.support:.2f} – ${rs.resistance:.2f} "
                f"({rs.spread_pct:.1f}% spread)"
            )
            return

        if self._seed_auto_range_from_report():
            rs = self.strategy.get_range_state()
            logger.warning(
                "Seeded auto range from AI report fallback: $%.2f – $%.2f (%.1f%% spread)",
                rs.support,
                rs.resistance,
                rs.spread_pct,
            )
            return

        logger.warning("Could not seed auto range — waiting for live data")

    def _seed_auto_range_from_report(self) -> bool:
        report_path = PROJECT_DIR / "reports" / "ai_selection_latest.json"
        if not report_path.exists():
            return False
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        candidates = data.get("top5") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            return False
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if str(item.get("ticker") or "").strip().upper() != self.ticker.upper():
                continue
            try:
                support = float(item.get("range_low"))
                resistance = float(item.get("range_high"))
            except (TypeError, ValueError):
                return False
            return self.strategy.apply_auto_range(
                support,
                resistance,
                confidence=0.2,
                source="ai_report_fallback",
            )
        return False

    # ---- Main Loop ----

    def run(self) -> None:
        """Start the main trading loop. Blocks until interrupted."""
        self._running = True
        self._start_time = datetime.now()

        # Connect broker
        if not self.broker.connect():
            self.notifier.alert("Failed to connect to broker", "error")
            return

        if self.mode == "live" and not self._adopt_active_live_order():
            self.broker.disconnect()
            return

        # Seed auto range from historical data (so it's ready immediately)
        self._seed_auto_range()

        self._print_header()

        try:
            while self._running:
                loop_start = time.time()

                # 1. Check trading hours
                if not self._is_trading_hours():
                    self._sleep_with_status(30, "Outside trading hours")
                    continue

                # 2. Fetch price
                quote = self.fetcher.get_quote()
                if quote is None or quote.price <= 0:
                    self._sleep_with_status(5, "Waiting for valid price data")
                    continue

                current_price = quote.price

                # 3. Feed price + volume to strategy
                self.strategy.feed_price(current_price)
                if quote.high_1m is not None and quote.low_1m is not None:
                    self.strategy.feed_volume_bar(
                        high=quote.high_1m,
                        low=quote.low_1m,
                        close=current_price,
                        volume=quote.volume,
                    )

                # 4. Auto-refresh range if needed
                if self.config.range.mode == "auto" and self.strategy.needs_auto_refresh():
                    # Pass recent OHLCV and let volume profile do its work
                    candles = self.fetcher.get_ohlcv(period="1d", interval="5m")
                    if candles:
                        # Seed volume bars from recent history to ensure rich data
                        for c in candles[-self.strategy.auto_lookback:]:
                            self.strategy.feed_volume_bar(
                                high=c.high, low=c.low,
                                close=c.close, volume=c.volume,
                            )
                    self.strategy.update_auto_range()

                # 5. Get current position
                pos = self.broker.get_position_for_ticker(self.ticker)
                self._position_shares = pos.quantity if pos else 0
                has_position = self._position_shares > 0

                # 6. Sync equity for risk calculations
                acct = self.broker.get_account()
                self._latest_position = pos
                self._latest_account = acct
                self._latest_snapshot_at = datetime.now()
                self.risk.update_equity(acct.equity)

                if pos:
                    self._position_shares = pos.quantity
                    if pos.quantity > 0 and pos.avg_entry_price > 0:
                        self._entry_price = pos.avg_entry_price
                elif not self._pending_order:
                    self._position_shares = 0
                    self._entry_price = None

                # 6b. Update broker price (for P&L calc)
                if isinstance(self.broker, PaperBroker):
                    self.broker.update_price(self.ticker, current_price)

                # 6c. Reconcile pending live orders before evaluating new signals.
                self._reconcile_pending_order()
                has_position = self._position_shares > 0

                # 7. Evaluate strategy
                signal = self.strategy.evaluate(current_price, has_position)
                self._last_signal_type = signal.type
                self._last_signal_reason = signal.reason

                # 8. Act on signal — skip non-critical signals during halt
                is_halted = self.risk._halted

                if self._pending_order:
                    self._last_signal_reason = (
                        f"等待订单成交：{self._pending_order['side']} {self._pending_order['order_id'][:12]}"
                    )
                elif signal.type == SignalType.BUY and not has_position:
                    if self._reduce_only:
                        self._last_signal_reason = "仅减仓模式：今晚不新开仓"
                    elif not is_halted:
                        self._handle_buy_signal(signal, current_price, quote.ask)

                elif signal.type == SignalType.SELL and has_position:
                    # A risk halt blocks new exposure, not position reduction.
                    self._handle_sell_signal(signal, current_price, quote.bid)

                elif signal.type == SignalType.STOP_LOSS and has_position:
                    self._handle_stop_loss(signal, current_price, quote.bid)  # Stop loss always fires

                # 9. Risk: check stop loss (always fires, even during halt)
                if has_position and self._entry_price and not self._pending_order:
                    rs = self.strategy.get_range_state()
                    sl_check = self.risk.check_stop_loss(
                        current_price, self._entry_price, rs.support
                    )
                    if not sl_check.allowed:
                        self._handle_stop_loss(signal, current_price, quote.bid)

                # 10. Heartbeat display (always show, with halt indicator)
                rs = self.strategy.get_range_state()
                self.notifier.heartbeat(
                    self.ticker, current_price, rs,
                    trend_info=self.strategy.get_trend_info(),
                    halted=is_halted,
                )

                # 11. Show signal in console — suppress non-critical signals during halt
                if signal.type not in (SignalType.HOLD, SignalType.TREND_BLOCK):
                    if is_halted:
                        pass  # Don't spam BUY/SELL when halted
                    else:
                        self.notifier.signal(
                            self.ticker, signal.type.value, signal.price, signal.reason
                        )

                # 12. Sleep until next poll
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.config.data.poll_interval_seconds - elapsed)
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("\nShutdown requested...")
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Gracefully stop the engine."""
        self._running = False

    # ---- Signal Handlers ----

    def _handle_buy_signal(self, signal, current_price: float, ask: float) -> None:
        """Handle a BUY signal with auto position sizing."""
        if self._reduce_only:
            self._last_signal_reason = "仅减仓模式：今晚不新开仓"
            return
        self._trade_in_progress = True
        try:
            acct = self.broker.get_account()
            cash = float(getattr(acct, "cash", 0.0) or 0.0)
            buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
            # Do not size live orders from margin buying power. The strategy's
            # capital limit is the settled/available cash reported by the broker.
            available_cash = max(0.0, cash)
            shares = determine_buy_quantity(
                current_price=current_price,
                available_cash=available_cash,
                configured_size=self.config.position.size_per_trade,
                max_position=self.config.position.max_position,
                execution_price=ask if ask > 0 else current_price,
            )

            if shares <= 0:
                self._last_signal_reason = (
                    f"买入数量为 0：购买力 ${buying_power:.2f} / 现金 ${cash:.2f} "
                    f"不足以买入 ${max(current_price, ask):.2f} 的标的"
                )
                self.notifier.alert(self._last_signal_reason, "warning")
                return

            entry_check = self.risk.check_entry(
                current_price, shares, self._position_shares
            )

            if not entry_check.allowed:
                self._last_signal_reason = entry_check.reason
                self.notifier.alert(entry_check.reason, "warning")
                return

            # Place order
            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.BUY,
                quantity=shares,
                order_type=OrderType.MARKET,
                current_bid=current_price,
                current_ask=ask,
            )

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="BUY",
                    signal_type=signal.type.value,
                )
                self.notifier.order_submitted(
                    self.ticker, "BUY", order.quantity, order.order_id
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                self._entry_price = order.avg_fill_price
                self._position_shares += order.filled_quantity
                self.strategy.record_entry(order.avg_fill_price)  # Quick stop tracking
                self.notifier.trade(
                    self.ticker, "BUY", order.filled_quantity, order.avg_fill_price
                )
                self._last_signal_reason = (
                    f"已买入 {order.filled_quantity} 股 @ ${order.avg_fill_price:.2f}"
                )
        finally:
            self._trade_in_progress = False

    def _handle_sell_signal(self, signal, current_price: float, bid: float) -> None:
        """Handle a SELL signal (take profit at resistance)."""
        self._trade_in_progress = True
        try:
            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.SELL,
                quantity=self._position_shares,
                order_type=OrderType.MARKET,
                current_bid=bid,
                current_ask=current_price,
            )

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="SELL",
                    signal_type=signal.type.value,
                )
                self.notifier.order_submitted(
                    self.ticker, "SELL", order.quantity, order.order_id
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                pnl = self._calculate_pnl(
                    order.avg_fill_price, int(order.filled_quantity or 0)
                )
                self.notifier.trade(
                    self.ticker, "SELL", order.filled_quantity, order.avg_fill_price, pnl
                )

                # Record the trade
                self.risk.record_trade(TradeRecord(
                    entry_time=datetime.now(),
                    exit_time=datetime.now(),
                    entry_price=self._entry_price or 0,
                    exit_price=order.avg_fill_price,
                    shares=order.filled_quantity,
                    pnl=pnl or 0,
                    pnl_pct=((order.avg_fill_price - (self._entry_price or order.avg_fill_price))
                             / (self._entry_price or 1) * 100),
                    side="LONG",
                ))

                self._position_shares = max(
                    0, self._position_shares - int(order.filled_quantity or 0)
                )
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
        finally:
            self._trade_in_progress = False

    def _handle_stop_loss(self, signal, current_price: float, bid: float) -> None:
        """Handle stop loss: exit position immediately."""
        self._trade_in_progress = True
        try:
            self.notifier.alert(
                f"STOP LOSS triggered! Exiting at ${current_price:.2f}",
                "halt",
            )

            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.SELL,
                quantity=self._position_shares,
                order_type=OrderType.MARKET,
                current_bid=bid,
                current_ask=current_price,
            )

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="SELL",
                    signal_type=signal.type.value,
                )
                self.notifier.order_submitted(
                    self.ticker, "SELL", order.quantity, order.order_id
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                pnl = self._calculate_pnl(
                    order.avg_fill_price, int(order.filled_quantity or 0)
                )
                self.risk.record_trade(TradeRecord(
                    entry_time=datetime.now(),
                    exit_time=datetime.now(),
                    entry_price=self._entry_price or 0,
                    exit_price=order.avg_fill_price,
                    shares=order.filled_quantity,
                    pnl=pnl or 0,
                    pnl_pct=((order.avg_fill_price - (self._entry_price or order.avg_fill_price))
                             / (self._entry_price or 1) * 100),
                    side="LONG",
                ))
                self._position_shares = max(
                    0, self._position_shares - int(order.filled_quantity or 0)
                )
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
        finally:
            self._trade_in_progress = False

    # ---- Helpers ----

    def _calculate_pnl(
        self, exit_price: float, shares: Optional[int] = None
    ) -> Optional[float]:
        """Calculate realized P&L for current position."""
        if self._entry_price is None:
            return None
        quantity = self._position_shares if shares is None else shares
        return (exit_price - self._entry_price) * quantity

    def _remember_pending_order(self, order, side: str, signal_type: str) -> None:
        """Store a submitted live order so later fills can be reconciled."""
        self._pending_order = {
            "order_id": order.order_id,
            "side": side,
            "signal_type": signal_type,
            "requested_quantity": int(order.quantity or 0),
            "acknowledged_filled_quantity": int(order.filled_quantity or 0),
            "entry_price_before": self._entry_price,
            "created_at": datetime.now(),
        }
        self._persist_pending_order()
        self._last_signal_reason = f"订单已提交，等待成交：{side} {order.quantity} 股"

    def _adopt_active_live_order(self) -> bool:
        """Block startup unless broker-side open orders are safely accounted for."""
        get_active_orders = getattr(self.broker, "get_active_orders", None)
        if not callable(get_active_orders):
            return True
        active_orders = get_active_orders(self.ticker)
        if active_orders is None:
            self.notifier.alert(
                f"{self.ticker} 无法核对活动订单，拒绝启动实盘交易",
                "error",
            )
            return False
        if self._pending_order:
            known_id = str(self._pending_order.get("order_id") or "")
            unknown = [order for order in active_orders if str(order.order_id) != known_id]
            if unknown:
                self.notifier.alert(
                    f"{self.ticker} 存在 {len(unknown)} 笔未接管活动订单，拒绝启动",
                    "error",
                )
                return False
            return True
        if len(active_orders) > 1:
            self.notifier.alert(
                f"{self.ticker} 存在 {len(active_orders)} 笔活动订单，拒绝启动",
                "error",
            )
            return False
        if len(active_orders) == 1:
            order = active_orders[0]
            self._remember_pending_order(
                order,
                "BUY" if order.side == OrderSide.BUY else "SELL",
                "RECOVERED",
            )
            self._last_signal_reason = (
                f"已接管券商活动订单：{self._pending_order['side']} "
                f"{str(order.order_id)[:12]}"
            )
        return True

    def _load_pending_order(self) -> None:
        """Restore an unresolved live order so restarts cannot submit duplicates."""
        path = self._pending_order_state_path
        if not path.exists():
            return
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(pending, dict) or not pending.get("order_id"):
                return
            created_at = pending.get("created_at")
            if isinstance(created_at, str):
                pending["created_at"] = datetime.fromisoformat(created_at)
            self._pending_order = pending
            self._last_signal_reason = (
                f"恢复待成交订单：{pending.get('side', 'UNKNOWN')} "
                f"{str(pending['order_id'])[:12]}"
            )
        except Exception as exc:
            logger.error("Pending-order state is invalid for %s: %s", self.ticker, exc)
            # Fail closed: a corrupt order-state file must not permit new orders.
            self._pending_order = {
                "order_id": "STATE_ERROR",
                "side": "UNKNOWN",
                "requested_quantity": 0,
                "acknowledged_filled_quantity": 0,
                "created_at": datetime.now(),
            }

    def _persist_pending_order(self) -> None:
        path = self._pending_order_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self._pending_order or {})
        created_at = payload.get("created_at")
        if isinstance(created_at, datetime):
            payload["created_at"] = created_at.isoformat()
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _clear_pending_order(self) -> None:
        self._pending_order = None
        try:
            self._pending_order_state_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Could not clear pending-order state for %s: %s", self.ticker, exc)

    def _reconcile_pending_order(self) -> None:
        """Pull the latest broker status for any submitted live order."""
        pending = self._pending_order
        if not pending or not pending.get("order_id"):
            return

        order = self.broker.get_order(pending["order_id"])
        if order is None:
            return

        previous_filled = int(pending.get("acknowledged_filled_quantity", 0) or 0)
        current_filled = int(order.filled_quantity or 0)
        delta_filled = max(0, current_filled - previous_filled)

        if delta_filled > 0:
            if pending["side"] == "BUY":
                self._apply_buy_fill(order, delta_filled)
            else:
                self._apply_sell_fill(order, delta_filled, pending)
            pending["acknowledged_filled_quantity"] = current_filled
            self._persist_pending_order()

        if order.status == OrderStatus.PARTIALLY_FILLED:
            self._last_signal_reason = (
                f"订单部分成交：{pending['side']} {current_filled}/{pending['requested_quantity']} 股"
            )
            return

        if order.status == OrderStatus.FILLED:
            self._clear_pending_order()
            return

        if order.status == OrderStatus.CANCELLED:
            self._last_signal_reason = f"订单已取消：{pending['side']} {pending['order_id'][:12]}"
            self._clear_pending_order()
            return

        if order.status == OrderStatus.REJECTED:
            self._last_signal_reason = f"订单被拒绝：{order.notes or pending['order_id'][:12]}"
            self.notifier.alert(self._last_signal_reason, "warning")
            self._clear_pending_order()

    def _apply_buy_fill(self, order, filled_quantity: int) -> None:
        fill_price = float(order.avg_fill_price or self._entry_price or 0.0)
        if fill_price > 0:
            self._entry_price = fill_price
            self.strategy.record_entry(fill_price)
        self._position_shares = max(self._position_shares, int(order.filled_quantity or 0))
        self.notifier.trade(
            self.ticker, "BUY", filled_quantity, fill_price or 0.0
        )
        self._last_signal_reason = (
            f"买单已成交 {filled_quantity} 股 @ ${fill_price:.2f}"
            if fill_price > 0
            else f"买单已成交 {filled_quantity} 股"
        )

    def _apply_sell_fill(self, order, filled_quantity: int, pending: dict) -> None:
        fill_price = float(order.avg_fill_price or 0.0)
        entry_price = float(pending.get("entry_price_before") or self._entry_price or 0.0)
        pnl = ((fill_price - entry_price) * filled_quantity) if entry_price > 0 and fill_price > 0 else None

        self.notifier.trade(
            self.ticker, "SELL", filled_quantity, fill_price or 0.0, pnl
        )

        if entry_price > 0 and fill_price > 0:
            self.risk.record_trade(TradeRecord(
                entry_time=pending.get("created_at") or datetime.now(),
                exit_time=datetime.now(),
                entry_price=entry_price,
                exit_price=fill_price,
                shares=filled_quantity,
                pnl=pnl or 0.0,
                pnl_pct=((fill_price - entry_price) / entry_price * 100),
                side="LONG",
            ))

        remaining = max(0, self._position_shares - filled_quantity)
        self._position_shares = remaining
        if remaining <= 0:
            self._entry_price = None
            self.strategy.clear_entry()
        self._last_signal_reason = (
            f"卖单已成交 {filled_quantity} 股 @ ${fill_price:.2f}"
            if fill_price > 0
            else f"卖单已成交 {filled_quantity} 股"
        )

    def _is_trading_hours(self) -> bool:
        """Check if we're currently in US regular trading hours."""
        if self._ignore_trading_hours:
            return True

        th = self.config.trading_hours

        if not HAS_PYTZ or self._ny_tz is None:
            # Without pytz, assume always trading (for demo/paper)
            return True

        now_ny = datetime.now(self._ny_tz)

        # Weekend check
        if now_ny.weekday() >= 5:
            return False

        # Parse time strings
        start_h, start_m = map(int, th.start.split(":"))
        end_h, end_m = map(int, th.end.split(":"))

        current_minutes = now_ny.hour * 60 + now_ny.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        return start_minutes <= current_minutes <= end_minutes

    def _sleep_with_status(self, seconds: int, reason: str) -> None:
        """Sleep with status message."""
        logger.debug(f"Sleeping {seconds}s: {reason}")
        # Break sleep into 1s chunks to stay responsive
        for _ in range(seconds):
            if not self._running:
                break
            time.sleep(1)

    def _print_header(self) -> None:
        """Print startup banner."""
        rs = self.strategy.get_range_state()
        print("\n" + "=" * 60)
        print(f"  🎯 SOXS Range Arbitrage Engine")
        print(f"  Mode: {self.mode.upper()}{' (24/7)' if self._ignore_trading_hours else ''}")
        print(f"  Ticker: {self.ticker}")
        print(f"  Range: ${rs.support:.2f} – ${rs.resistance:.2f} "
              f"({rs.spread_dollars:.2f} spread, {rs.spread_pct:.1f}%)")
        print(f"  Position: {self.config.position.size_per_trade} shares/trade, "
              f"{self.config.position.max_position} max")
        print(f"  Trend Filter: {'✅ ON' if self.config.trend_filter.enabled else '❌ OFF'} "
              f"(MA{self.config.trend_filter.ma_period}, "
              f"min strength {self.config.trend_filter.min_trend_strength}%)")
        print(f"  Stop Loss: {self.config.risk.stop_loss_pct}%")
        print(f"  Daily Loss Limit: ${self.config.risk.daily_loss_limit}")
        print("=" * 60 + "\n")

    def _shutdown(self) -> None:
        """Clean shutdown."""
        self._running = False

        # Show final summary
        stats = self.risk.get_stats()
        self.notifier.summary(stats)

        # Close broker connection
        self.broker.disconnect()

        run_time = datetime.now() - self._start_time if self._start_time else None
        print(f"\n🏁 Engine stopped. Run time: {run_time}")
        print(f"   Trades: {stats['total_trades']} | "
              f"Win rate: {stats['win_rate']}% | "
              f"Total P&L: ${stats['total_pnl']:+.2f}")
