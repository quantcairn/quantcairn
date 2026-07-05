"""
Trading Engine: main loop that orchestrates all components.

Flow:
  1. Fetch price → 2. Feed strategy → 3. Generate signal
  → 4. Risk check → 5. Place order → 6. Notify → 7. Log

Runs in paper or live mode.
"""
import json
import logging
import os
import time
import calendar
from dataclasses import dataclass
from datetime import datetime, date, timedelta
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
from ..ai_selector.config import load_runtime_config as load_ai_selector_runtime_config
from ..ai_selector.integration import AISelector

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))
INVERSE_ETF_SYMBOLS = {"SOXS", "SQQQ", "SPXU", "SDOW", "FAZ"}

# Try to import pytz, fall back if not available
try:
    import pytz as _pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False


@dataclass
class AISelectionDecision:
    enabled: bool = False
    active: bool = False
    top10: list[dict] | None = None
    top3: list[dict] | None = None
    signal_for_ticker: Optional[dict] = None
    regime: str = "DISABLED"
    strategy: str = "range_detector"
    risk_approved: bool = True
    allocation_weight: float = 0.0
    fallback_reason: str = ""


def _audit_log_path() -> Path:
    configured_dir = os.environ.get("SOXS_RUNTIME_AUDIT_DIR", "").strip()
    log_dir = Path(configured_dir) if configured_dir else PROJECT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"


def append_runtime_audit(record: dict) -> None:
    payload = dict(record or {})
    payload.setdefault(
        "timestamp",
        datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
    )
    with _audit_log_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def is_inverse_etf_symbol(symbol: str) -> bool:
    return str(symbol or "").strip().upper().split(".")[0] in INVERSE_ETF_SYMBOLS


def check_exit_conditions(
    symbol,
    current_price,
    avg_cost,
    position_qty,
    is_inverse_etf=False,
    mode="normal",
):
    decision = {
        "should_exit": False,
        "reason": None,
        "trigger_price": None,
        "avg_cost": float(avg_cost or 0.0),
        "position_qty": int(position_qty or 0),
        "symbol": str(symbol or "").strip().upper(),
        "mode": mode,
    }
    qty = decision["position_qty"]
    cost = decision["avg_cost"]
    price = float(current_price or 0.0)
    if qty <= 0 or cost <= 0 or price <= 0:
        return decision

    stop_trigger = round(cost * 0.95, 6)
    take_trigger = round(cost * 1.10, 6)
    # The system only holds cash long positions, including inverse ETFs such as SOXS.
    # Realized P&L for a held position still follows long-position math:
    # price down from cost is a loss, price up from cost is a gain.
    if mode == "orphan":
        take_trigger = None
        stop_trigger = round(cost * 0.92, 6)
        if price <= stop_trigger:
            decision["should_exit"] = True
            decision["reason"] = "stop_loss"
            decision["trigger_price"] = stop_trigger
        return decision

    if price <= stop_trigger:
        decision["should_exit"] = True
        decision["reason"] = "stop_loss"
        decision["trigger_price"] = stop_trigger
    elif price >= take_trigger:
        decision["should_exit"] = True
        decision["reason"] = "take_profit"
        decision["trigger_price"] = take_trigger
    return decision


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
            state_path=STATE_DIR / "risk" / f"{self.ticker.upper()}.json",
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
        self._last_exit_check_at = 0.0
        self._pending_order_state_path = (
            STATE_DIR / "pending_orders" / f"{self.ticker.upper()}.json"
        )
        self._position_sync_state_path = (
            STATE_DIR / "position_sync" / f"{self.ticker.upper()}.json"
        )
        self._sell_lock_path = (
            STATE_DIR / "sell_locks" / f"{self.ticker.upper()}.lock"
        )
        self._position_sync_fence: Optional[dict] = None
        self._load_pending_order()
        self._load_position_sync_fence()
        self._reduce_only = bool(getattr(config.position, "reduce_only", False))
        if self.mode == "live" and (
            self._position_sync_fence
            or (
                self._pending_order
                and str(self._pending_order.get("side") or "").upper() == "SELL"
            )
        ):
            self._acquire_sell_lock("restored_sell_protection")

        # NY timezone
        self._ny_tz = _pytz.timezone("America/New_York") if HAS_PYTZ else None
        self._ai_selection = AISelectionDecision()

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

        if self.mode == "live" and not self._verify_live_startup_safety():
            self.broker.disconnect()
            return

        if self.mode == "live" and not self._adopt_active_live_order():
            self.broker.disconnect()
            return

        self._initialize_ai_selector()

        # Seed auto range from historical data (so it's ready immediately)
        self._seed_auto_range()

        self._print_header()

        try:
            while self._running:
                loop_start = time.time()

                # 1. Check trading hours
                if not self._is_trading_hours():
                    self._refresh_broker_snapshots(outside_trading_hours=True)
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
                positions_reliable = getattr(
                    self.broker, "is_positions_snapshot_reliable", lambda: True
                )()
                if self.mode == "live" and not positions_reliable:
                    self._last_signal_reason = "券商持仓状态无法确认，已暂停本轮交易"
                    logger.error("%s: %s", self.ticker, self._last_signal_reason)
                    self._sleep_with_status(15, self._last_signal_reason)
                    continue
                observed_shares = pos.quantity if pos else 0
                self._position_shares = self._apply_position_sync_fence(observed_shares)
                has_position = self._position_shares > 0

                # 6. Sync equity for risk calculations
                acct = self.broker.get_account()
                self._latest_position = pos
                self._latest_account = acct
                self._latest_snapshot_at = datetime.now()
                self.risk.update_equity(acct.equity)

                if pos:
                    self._position_shares = self._apply_position_sync_fence(pos.quantity)
                    if self._position_shares > 0 and pos.avg_entry_price > 0:
                        self._entry_price = pos.avg_entry_price
                elif not self._pending_order and not self._position_sync_fence:
                    self._position_shares = 0
                    self._entry_price = None

                # 6b. Update broker price (for P&L calc)
                if isinstance(self.broker, PaperBroker):
                    self.broker.update_price(self.ticker, current_price)

                # 6c. Reconcile pending live orders before evaluating new signals.
                self._reconcile_pending_order()
                has_position = self._position_shares > 0

                # 6d. Dynamic exit checks run independently from the range signal.
                if self._should_run_exit_check(loop_start):
                    self._last_exit_check_at = loop_start
                    self._run_dynamic_exit_check(current_price, quote.bid)
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
                elif self._position_sync_fence:
                    # Do not place any order while the broker still reports a
                    # pre-fill position snapshot.
                    pass
                elif signal.type == SignalType.BUY and not has_position:
                    if self._reduce_only:
                        self._last_signal_reason = "仅减仓模式：今晚不新开仓"
                    elif not self._ai_entry_allowed():
                        self._last_signal_reason = self._blocked_ai_reason()
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

    def _refresh_broker_snapshots(self, outside_trading_hours: bool = False) -> None:
        """Refresh cached position/account snapshots without evaluating signals."""
        try:
            pos = self.broker.get_position_for_ticker(self.ticker)
            positions_reliable = getattr(
                self.broker, "is_positions_snapshot_reliable", lambda: True
            )()
            if self.mode == "live" and not positions_reliable:
                self._last_signal_reason = "券商持仓状态无法确认，已暂停仓位同步"
                logger.error("%s: %s", self.ticker, self._last_signal_reason)
                return

            acct = self.broker.get_account()
            account_reliable = getattr(
                self.broker, "is_account_snapshot_reliable", lambda: True
            )()
            if self.mode == "live" and not account_reliable:
                self._last_signal_reason = "券商账户状态无法确认，已暂停账户同步"
                logger.error("%s: %s", self.ticker, self._last_signal_reason)
                return

            observed_shares = pos.quantity if pos else 0
            self._position_shares = self._apply_position_sync_fence(observed_shares)
            self._latest_position = pos
            self._latest_account = acct
            self._latest_snapshot_at = datetime.now()
            self.risk.update_equity(acct.equity)

            if pos:
                self._position_shares = self._apply_position_sync_fence(pos.quantity)
                if self._position_shares > 0 and pos.avg_entry_price > 0:
                    self._entry_price = pos.avg_entry_price
            elif not self._pending_order and not self._position_sync_fence:
                self._position_shares = 0
                self._entry_price = None

            if outside_trading_hours and self.mode == "live" and self._position_shares > 0:
                self._last_signal_reason = "盘后仅同步真实持仓，不执行新交易"
        except Exception as exc:
            logger.exception("Failed to refresh broker snapshots for %s: %s", self.ticker, exc)

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
            available_cash = self._cap_ai_available_cash(available_cash, acct)
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
        if not self._acquire_sell_lock("range_sell"):
            self._last_signal_reason = "卖出已跳过：同标的卖出锁已生效"
            return
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
                self._set_position_sync_fence(self._position_shares)
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
            if order.status == OrderStatus.REJECTED:
                self._release_sell_lock("order_rejected")
        finally:
            self._trade_in_progress = False

    def _handle_stop_loss(self, signal, current_price: float, bid: float) -> None:
        """Handle stop loss: exit position immediately."""
        if not self._acquire_sell_lock("strategy_stop_loss"):
            self._last_signal_reason = "止损卖出已跳过：同标的卖出锁已生效"
            return
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
                self._set_position_sync_fence(self._position_shares)
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
            if order.status == OrderStatus.REJECTED:
                self._release_sell_lock("order_rejected")
        finally:
            self._trade_in_progress = False

    # ---- Helpers ----

    def _should_run_exit_check(self, now_ts: float) -> bool:
        return (now_ts - self._last_exit_check_at) >= 60.0

    def _initialize_ai_selector(self) -> None:
        runtime = load_ai_selector_runtime_config()
        self._ai_selection = AISelectionDecision(enabled=runtime.enabled)
        logger.info("AI selector enabled %s", str(runtime.enabled).lower())
        self._write_runtime_audit(
            "ai_selector_status",
            ai_selector_enabled=runtime.enabled,
            universe=runtime.universe,
        )
        if not runtime.enabled:
            return
        try:
            selector = AISelector(config=runtime)
            top3 = selector.get_signals()
            top10 = selector.get_top10()
        except Exception as exc:
            logger.exception("AI selector failed, fallback to original config: %s", exc)
            self._ai_selection.fallback_reason = "ai_selector_exception"
            return
        if not top3:
            logger.warning("AI selector returned no signals, fallback to original config")
            self._ai_selection.fallback_reason = "empty_ai_signals"
            return
        signal_for_ticker = next(
            (item for item in top3 if str(item.get("ticker") or "").upper() == self.ticker.upper()),
            None,
        )
        regime = self._detect_market_regime(top3, signal_for_ticker)
        strategy = self._route_strategy(regime, signal_for_ticker)
        allocation_weight = self._allocate_portfolio_weight(top3, signal_for_ticker)
        risk_approved = self._preapprove_ai_risk(regime, allocation_weight, signal_for_ticker)
        self._ai_selection = AISelectionDecision(
            enabled=True,
            active=True,
            top10=top10,
            top3=top3,
            signal_for_ticker=signal_for_ticker,
            regime=regime,
            strategy=strategy,
            risk_approved=risk_approved,
            allocation_weight=allocation_weight,
        )
        logger.info("AI selector top10 candidates: %s", [item.get("ticker") for item in top10])
        logger.info("AI selector selected top3: %s", [item.get("ticker") for item in top3])
        logger.info(
            "AI selector ticker=%s final_score=%s reason=%s regime=%s strategy=%s risk_approved=%s",
            self.ticker,
            signal_for_ticker.get("score") if signal_for_ticker else None,
            signal_for_ticker.get("reason") if signal_for_ticker else "not_selected",
            regime,
            strategy,
            risk_approved,
        )
        self._write_runtime_audit(
            "ai_selector_decision",
            ai_selector_enabled=True,
            top10_candidates=top10,
            selected_top3=top3,
            final_score=signal_for_ticker.get("score") if signal_for_ticker else None,
            ai_reason=signal_for_ticker.get("reason") if signal_for_ticker else "not_selected",
            regime=regime,
            strategy=strategy,
            risk_approved=risk_approved,
        )

    def _detect_market_regime(self, top3: list[dict], signal_for_ticker: Optional[dict]) -> str:
        if signal_for_ticker is None:
            return "NO_SELECTION"
        avg_confidence = sum(float(item.get("confidence") or 0.0) for item in top3) / max(1, len(top3))
        if avg_confidence < 0.30:
            return "EVENT"
        return "NORMAL"

    def _route_strategy(self, regime: str, signal_for_ticker: Optional[dict]) -> str:
        if regime == "EVENT":
            return "blocked"
        if signal_for_ticker is None:
            return "watch_only"
        return "range_detector"

    def _allocate_portfolio_weight(self, top3: list[dict], signal_for_ticker: Optional[dict]) -> float:
        if signal_for_ticker is None or not top3:
            return 0.0
        return min(0.30, 1.0 / len(top3))

    def _preapprove_ai_risk(
        self,
        regime: str,
        allocation_weight: float,
        signal_for_ticker: Optional[dict],
    ) -> bool:
        if signal_for_ticker is None:
            return False
        if regime == "EVENT":
            return False
        return 0.0 < allocation_weight <= 0.30

    def _ai_entry_allowed(self) -> bool:
        if not self._ai_selection.enabled:
            return True
        if not self._ai_selection.active:
            return True
        if self._ai_selection.regime == "EVENT":
            return False
        return bool(self._ai_selection.signal_for_ticker and self._ai_selection.risk_approved)

    def _blocked_ai_reason(self) -> str:
        if not self._ai_selection.enabled:
            return "AI 选股未启用"
        if not self._ai_selection.active:
            return f"AI 选股回退原配置：{self._ai_selection.fallback_reason or 'unknown'}"
        if self._ai_selection.regime == "EVENT":
            return "AI 市场状态为 EVENT，禁止开新仓"
        if not self._ai_selection.signal_for_ticker:
            return "当前标的不在 AI Top3，跳过新开仓"
        if not self._ai_selection.risk_approved:
            return "AI 风控预检未通过，跳过新开仓"
        return "AI 选股未批准新开仓"

    def _cap_ai_available_cash(self, available_cash: float, acct) -> float:
        if not self._ai_selection.enabled or not self._ai_selection.active:
            return available_cash
        if not self._ai_selection.signal_for_ticker:
            return 0.0
        equity = float(getattr(acct, "equity", 0.0) or 0.0)
        if equity <= 0 or self._ai_selection.allocation_weight <= 0:
            return available_cash
        allocation_cap = equity * min(0.30, self._ai_selection.allocation_weight)
        return max(0.0, min(available_cash, allocation_cap))

    def _verify_live_startup_safety(self) -> bool:
        """Fail closed unless live startup is reduce-only with verified broker data."""
        if not self._reduce_only:
            self._last_signal_reason = "实盘启动已阻止：全局只减仓未启用"
            self._write_runtime_audit(
                "startup_safety_check",
                broker_position_verified=False,
                broker_account_verified=False,
                startup_allowed=False,
                reason="reduce_only_disabled",
            )
            self.notifier.alert(self._last_signal_reason, "error")
            return False

        invalidate = getattr(self.broker, "invalidate_cache", None)
        if callable(invalidate):
            invalidate()
        positions = self.broker.get_positions()
        positions_reliable = getattr(
            self.broker, "is_positions_snapshot_reliable", lambda: True
        )()
        if not positions_reliable:
            self._last_signal_reason = "实盘启动已阻止：券商持仓无法确认"
            self._write_runtime_audit(
                "startup_safety_check",
                broker_position_verified=False,
                broker_account_verified=False,
                startup_allowed=False,
                reason="broker_position_verification_failed",
            )
            self.notifier.alert(self._last_signal_reason, "error")
            return False

        account = self.broker.get_account()
        account_reliable = getattr(
            self.broker, "is_account_snapshot_reliable", lambda: True
        )()
        if not account_reliable:
            self._last_signal_reason = "实盘启动已阻止：券商账户快照无法确认"
            self._write_runtime_audit(
                "startup_safety_check",
                broker_position_verified=True,
                broker_account_verified=False,
                startup_allowed=False,
                reason="broker_account_verification_failed",
            )
            self.notifier.alert(self._last_signal_reason, "error")
            return False

        self._write_runtime_audit(
            "startup_safety_check",
            broker_position_verified=True,
            broker_account_verified=True,
            startup_allowed=True,
            positions=[
                {
                    "symbol": str(getattr(pos, "ticker", "") or "").split(".")[0].upper(),
                    "quantity": int(getattr(pos, "quantity", 0) or 0),
                }
                for pos in positions or []
                if int(getattr(pos, "quantity", 0) or 0) > 0
            ],
            equity=float(getattr(account, "equity", 0.0) or 0.0),
        )
        return True

    def _has_active_sell_protection(self) -> bool:
        if self.mode == "live" and self._sell_lock_path.exists():
            return True
        if self._position_sync_fence:
            return True
        if self._pending_order and str(self._pending_order.get("side") or "").upper() == "SELL":
            return True
        return False

    def _write_runtime_audit(self, phase: str, **fields) -> None:
        append_runtime_audit(
            {
                "phase": phase,
                "ticker": self.ticker,
                "symbol": self.ticker,
                "execution_mode": self.mode,
                "reduce_only": self._reduce_only,
                **fields,
            }
        )

    def _run_dynamic_exit_check(self, current_price: float, bid: float) -> None:
        pos = self.broker.get_position_for_ticker(self.ticker)
        positions_reliable = getattr(
            self.broker, "is_positions_snapshot_reliable", lambda: True
        )()
        if self.mode == "live" and not positions_reliable:
            self._last_signal_reason = "动态退出跳过：券商持仓未确认"
            return
        if pos is None:
            return
        confirmed_qty = self._apply_position_sync_fence(pos.quantity)
        if confirmed_qty <= 0:
            return
        avg_cost = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
        decision = check_exit_conditions(
            self.ticker,
            current_price,
            avg_cost,
            confirmed_qty,
            is_inverse_etf=is_inverse_etf_symbol(self.ticker),
            mode="normal",
        )
        if not decision["should_exit"]:
            return
        if self._has_active_sell_protection():
            self._last_signal_reason = f"动态退出待执行：{decision['reason']}，但卖出保护已生效"
            self._write_runtime_audit(
                "risk_exit_trigger",
                reason=decision["reason"],
                current_price=current_price,
                avg_cost=avg_cost,
                quantity=confirmed_qty,
                mode="normal",
                trigger_price=decision["trigger_price"],
                broker_position_verified=positions_reliable,
                skipped=True,
                skip_reason="sell_protection_active",
            )
            return
        self._submit_reduce_order(
            quantity=confirmed_qty,
            current_price=current_price,
            execution_price=bid if bid > 0 else current_price,
            reason=str(decision["reason"]),
            mode="normal",
            avg_cost=avg_cost,
            broker_position_verified=positions_reliable,
            trigger_price=decision["trigger_price"],
        )

    def _submit_reduce_order(
        self,
        *,
        quantity: int,
        current_price: float,
        execution_price: float,
        reason: str,
        mode: str,
        avg_cost: Optional[float] = None,
        broker_position_verified: bool = True,
        trigger_price: Optional[float] = None,
        audit_phase: str = "risk_exit_trigger",
    ) -> Optional[str]:
        if quantity <= 0:
            return None
        if not self._acquire_sell_lock(f"{mode}:{reason}"):
            self._last_signal_reason = "动态退出已跳过：同标的卖出锁已生效"
            self._write_runtime_audit(
                audit_phase,
                reason=reason,
                quantity=quantity,
                mode=mode,
                skipped=True,
                skip_reason="cross_process_sell_lock_active",
            )
            return None
        self._trade_in_progress = True
        order_id = None
        try:
            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.SELL,
                quantity=quantity,
                order_type=OrderType.MARKET,
                current_bid=execution_price,
                current_ask=current_price,
                notes=f"{mode}:{reason}",
            )
            order_id = str(getattr(order, "order_id", "") or "")
            self._write_runtime_audit(
                audit_phase,
                reason=reason,
                current_price=current_price,
                avg_cost=avg_cost if avg_cost is not None else self._entry_price,
                quantity=quantity,
                mode=mode,
                trigger_price=trigger_price if trigger_price is not None else current_price,
                broker_position_verified=broker_position_verified,
                order_id=order_id or None,
                order={
                    "side": "sell",
                    "qty": quantity,
                    "type": "market",
                },
                response={"status": str(order.status.value).lower()},
            )

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="SELL",
                    signal_type=reason.upper(),
                )
                self.notifier.order_submitted(
                    self.ticker, "SELL", order.quantity, order.order_id
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                filled_qty = int(order.filled_quantity or 0)
                pnl = self._calculate_pnl(order.avg_fill_price, filled_qty)
                self.notifier.trade(
                    self.ticker, "SELL", filled_qty, order.avg_fill_price, pnl
                )
                if self._entry_price and order.avg_fill_price:
                    self.risk.record_trade(TradeRecord(
                        entry_time=datetime.now(),
                        exit_time=datetime.now(),
                        entry_price=self._entry_price,
                        exit_price=order.avg_fill_price,
                        shares=filled_qty,
                        pnl=pnl or 0.0,
                        pnl_pct=((order.avg_fill_price - self._entry_price) / self._entry_price * 100),
                        side="LONG",
                    ))
                self._position_shares = max(0, self._position_shares - filled_qty)
                self._set_position_sync_fence(self._position_shares)
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
                self._last_signal_reason = f"动态退出已执行：{reason}"
            if order.status == OrderStatus.REJECTED:
                self._release_sell_lock("order_rejected")
            return order_id or None
        finally:
            self._trade_in_progress = False

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
        retry_count = 3
        retry_delay = 6
        for idx in range(retry_count):
            active_orders = get_active_orders(self.ticker)
            if active_orders is not None:
                break
            if idx < (retry_count - 1):
                self._last_signal_reason = (
                    f"启动自检限流，{self.ticker} 活动订单核对重试 {idx + 1}/{retry_count - 1}"
                )
                time.sleep(retry_delay * (idx + 1))
        else:
            active_orders = None
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
            if order.side == OrderSide.SELL:
                self._acquire_sell_lock("recovered_active_sell")
            else:
                self._release_sell_lock("active_order_is_buy")
            self._remember_pending_order(
                order,
                "BUY" if order.side == OrderSide.BUY else "SELL",
                "RECOVERED",
            )
            self._last_signal_reason = (
                f"已接管券商活动订单：{self._pending_order['side']} "
                f"{str(order.order_id)[:12]}"
            )
        elif not self._position_sync_fence:
            self._release_sell_lock("startup_no_active_sell")
        return True

    def _acquire_sell_lock(self, reason: str) -> bool:
        """Atomically reserve this symbol's live SELL path across processes."""
        if self.mode != "live":
            return True
        path = self._sell_lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": self.ticker.upper(),
            "pid": os.getpid(),
            "reason": reason,
            "created_at": datetime.now().isoformat(),
        }
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self._write_runtime_audit(
                "sell_lock_blocked",
                lock_path=str(path),
                reason=reason,
            )
            return False
        try:
            os.write(descriptor, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(descriptor)
        self._write_runtime_audit(
            "sell_lock_acquired",
            lock_path=str(path),
            reason=reason,
        )
        return True

    def _release_sell_lock(self, reason: str) -> None:
        if self.mode != "live":
            return
        try:
            existed = self._sell_lock_path.exists()
            self._sell_lock_path.unlink(missing_ok=True)
            if existed:
                self._write_runtime_audit(
                    "sell_lock_released",
                    lock_path=str(self._sell_lock_path),
                    reason=reason,
                )
        except OSError as exc:
            logger.error("Could not release sell lock for %s: %s", self.ticker, exc)

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

    def _load_position_sync_fence(self) -> None:
        """Restore the post-sell fence that protects against stale broker positions."""
        path = self._position_sync_state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expected = int(payload["expected_max_quantity"])
            if expected < 0:
                raise ValueError("expected quantity cannot be negative")
            self._position_sync_fence = payload
        except Exception as exc:
            logger.error("Position-sync state is invalid for %s: %s", self.ticker, exc)
            # A corrupt fence must fail closed instead of permitting another sell.
            self._position_sync_fence = {
                "expected_max_quantity": 0,
                "created_at": datetime.now().isoformat(),
                "state_error": True,
            }

    def _set_position_sync_fence(self, expected_max_quantity: int) -> None:
        """Block another sell until the broker confirms the post-fill quantity."""
        if self.mode != "live":
            return
        self._position_sync_fence = {
            "expected_max_quantity": max(0, int(expected_max_quantity)),
            "created_at": datetime.now().isoformat(),
        }
        path = self._position_sync_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._position_sync_fence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _clear_position_sync_fence(self) -> None:
        self._position_sync_fence = None
        try:
            self._position_sync_state_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Could not clear position-sync state for %s: %s", self.ticker, exc)
        if not (
            self._pending_order
            and str(self._pending_order.get("side") or "").upper() == "SELL"
        ):
            self._release_sell_lock("broker_position_confirmed")

    def _apply_position_sync_fence(self, observed_quantity: int) -> int:
        """Accept a broker position only after it reflects the latest sell fill."""
        observed = max(0, int(observed_quantity or 0))
        if self.mode != "live" or not self._position_sync_fence:
            return observed
        expected = int(self._position_sync_fence.get("expected_max_quantity", 0) or 0)
        if observed <= expected:
            self._clear_position_sync_fence()
            return observed
        self._last_signal_reason = (
            f"等待券商确认卖出后仓位：预期不超过 {expected} 股，当前仍显示 {observed} 股"
        )
        return expected

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
            if not self._position_sync_fence:
                self._release_sell_lock("order_cancelled")
            return

        if order.status == OrderStatus.REJECTED:
            self._last_signal_reason = f"订单被拒绝：{order.notes or pending['order_id'][:12]}"
            self.notifier.alert(self._last_signal_reason, "warning")
            self._clear_pending_order()
            if not self._position_sync_fence:
                self._release_sell_lock("order_rejected")

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
        self._set_position_sync_fence(remaining)
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
        # The anytime override is for paper simulations only. Live execution
        # must always remain inside the verified regular-market session.
        if self._ignore_trading_hours and self.mode != "live":
            return True

        th = self.config.trading_hours

        if not HAS_PYTZ or self._ny_tz is None:
            # Live trading must fail closed when its configured timezone is unavailable.
            return self.mode != "live"

        now_ny = datetime.now(self._ny_tz)
        session_end = self._market_session_end(now_ny.date())
        if session_end is None:
            return False

        # Parse time strings
        start_h, start_m = map(int, th.start.split(":"))
        end_h, end_m = map(int, session_end.split(":"))

        current_minutes = now_ny.hour * 60 + now_ny.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        return start_minutes <= current_minutes <= end_minutes

    def _market_session_end(self, session_date: date) -> Optional[str]:
        """Return the NYSE close time, or None for weekends and major holidays."""
        if session_date.weekday() >= 5 or session_date in self._market_holidays(session_date.year):
            return None
        if session_date in self._market_early_closes(session_date.year):
            return self.config.trading_hours.early_close
        return self.config.trading_hours.end

    @classmethod
    def _market_holidays(cls, year: int) -> set[date]:
        def nth_weekday(month: int, weekday: int, n: int) -> date:
            first = date(year, month, 1)
            day = 1 + ((weekday - first.weekday()) % 7) + (n - 1) * 7
            return date(year, month, day)

        def last_weekday(month: int, weekday: int) -> date:
            day = calendar.monthrange(year, month)[1]
            value = date(year, month, day)
            return value - timedelta(days=(value.weekday() - weekday) % 7)

        def observed(value: date) -> date:
            if value.weekday() == 5:
                return value - timedelta(days=1)
            if value.weekday() == 6:
                return value + timedelta(days=1)
            return value

        easter = cls._easter_sunday(year)
        holidays = {
            observed(date(year, 1, 1)),
            nth_weekday(1, 0, 3),
            nth_weekday(2, 0, 3),
            easter - timedelta(days=2),
            last_weekday(5, 0),
            observed(date(year, 6, 19)),
            observed(date(year, 7, 4)),
            nth_weekday(9, 0, 1),
            nth_weekday(11, 3, 4),
            observed(date(year, 12, 25)),
        }
        # A Monday-observed New Year can belong to the following calendar year.
        next_new_year = observed(date(year + 1, 1, 1))
        if next_new_year.year == year:
            holidays.add(next_new_year)
        return holidays

    @classmethod
    def _market_early_closes(cls, year: int) -> set[date]:
        holidays = cls._market_holidays(year)

        def previous_session(value: date) -> date:
            value -= timedelta(days=1)
            while value.weekday() >= 5 or value in holidays:
                value -= timedelta(days=1)
            return value

        thanksgiving = next(
            value for value in holidays if value.month == 11 and value.weekday() == 3
        )
        closes = {
            previous_session(date(year, 7, 4)),
            thanksgiving + timedelta(days=1),
        }
        christmas_eve = date(year, 12, 24)
        if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
            closes.add(christmas_eve)
        return closes

    @staticmethod
    def _easter_sunday(year: int) -> date:
        """Gregorian Easter date used to derive the Good Friday closure."""
        a = year % 19
        b, c = divmod(year, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        month_seed = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * month_seed) // 451
        month = (h + month_seed - 7 * m + 114) // 31
        day = (h + month_seed - 7 * m + 114) % 31 + 1
        return date(year, month, day)

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
