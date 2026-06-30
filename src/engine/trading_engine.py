"""
Trading Engine: main loop that orchestrates all components.

Flow:
  1. Fetch price → 2. Feed strategy → 3. Generate signal
  → 4. Risk check → 5. Place order → 6. Notify → 7. Log

Runs in paper or live mode.
"""
import logging
import time
from datetime import datetime, date
from typing import Optional

import pytz

from ..config.loader import AppConfig
from ..data.fetcher import PriceFetcher
from ..strategy.range_detector import RangeDetector, SignalType
from ..risk.manager import RiskManager, TradeRecord
from ..broker.base import BrokerBase, OrderSide, OrderType
from ..broker.paper_broker import PaperBroker
from ..notifier.alerts import Notifier
from .position_sizing import determine_buy_quantity

logger = logging.getLogger(__name__)

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
            quick_stop_pct=config.range.quick_stop_pct,
        )

        self.risk = RiskManager(
            stop_loss_pct=config.risk.stop_loss_pct,
            daily_loss_limit=config.risk.daily_loss_limit,
            max_consecutive_losses=config.risk.max_consecutive_losses,
            max_position=config.position.max_position,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            cool_down_seconds=config.position.cool_down_seconds,
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
        else:
            logger.warning("Could not seed auto range — waiting for live data")

    # ---- Main Loop ----

    def run(self) -> None:
        """Start the main trading loop. Blocks until interrupted."""
        self._running = True
        self._start_time = datetime.now()

        # Connect broker
        if not self.broker.connect():
            self.notifier.alert("Failed to connect to broker", "error")
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
                self.risk.update_equity(acct.equity)

                # 6b. Update broker price (for P&L calc)
                if isinstance(self.broker, PaperBroker):
                    self.broker.update_price(self.ticker, current_price)

                # 7. Evaluate strategy
                signal = self.strategy.evaluate(current_price, has_position)
                self._last_signal_type = signal.type

                # 8. Act on signal — skip non-critical signals during halt
                is_halted = self.risk._halted

                if signal.type == SignalType.BUY and not has_position:
                    if not is_halted:
                        self._handle_buy_signal(signal, current_price, quote.ask)

                elif signal.type == SignalType.SELL and has_position:
                    if is_halted:
                        pass
                    else:
                        self._handle_sell_signal(signal, current_price, quote.bid)

                elif signal.type == SignalType.STOP_LOSS and has_position:
                    self._handle_stop_loss(signal, current_price, quote.bid)  # Stop loss always fires

                # 9. Risk: check stop loss (always fires, even during halt)
                if has_position and self._entry_price:
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
        acct = self.broker.get_account()
        available_cash = acct.cash
        shares = determine_buy_quantity(
            current_price=current_price,
            available_cash=available_cash,
            configured_size=self.config.position.size_per_trade,
            max_position=self.config.position.max_position,
            execution_price=ask if ask > 0 else current_price,
        )

        entry_check = self.risk.check_entry(
            current_price, shares, self._position_shares
        )

        if not entry_check.allowed:
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

        if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
            self._entry_price = order.avg_fill_price
            self._position_shares += order.filled_quantity
            self.strategy.record_entry(order.avg_fill_price)  # Quick stop tracking
            self.notifier.trade(
                self.ticker, "BUY", order.filled_quantity, order.avg_fill_price
            )

    def _handle_sell_signal(self, signal, current_price: float, bid: float) -> None:
        """Handle a SELL signal (take profit at resistance)."""
        order = self.broker.place_order(
            ticker=self.ticker,
            side=OrderSide.SELL,
            quantity=self._position_shares,
            order_type=OrderType.MARKET,
            current_bid=bid,
            current_ask=current_price,
        )

        if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
            pnl = self._calculate_pnl(order.avg_fill_price)
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

            self._entry_price = None
            self._position_shares = 0
            self.strategy.clear_entry()

    def _handle_stop_loss(self, signal, current_price: float, bid: float) -> None:
        """Handle stop loss: exit position immediately."""
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

        if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
            pnl = self._calculate_pnl(order.avg_fill_price)
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
            self._entry_price = None
            self._position_shares = 0
            self.strategy.clear_entry()

    # ---- Helpers ----

    def _calculate_pnl(self, exit_price: float) -> Optional[float]:
        """Calculate realized P&L for current position."""
        if self._entry_price is None:
            return None
        return (exit_price - self._entry_price) * self._position_shares

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
