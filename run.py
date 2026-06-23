#!/usr/bin/env python3
"""
SOXS Range Arbitrage — Entry Point

Usage:
    python run.py                        # Paper trading (default)
    python run.py --paper                # Paper trading mode
    python run.py --backtest             # Backtest with historical data
    python run.py --live                 # Live trading (requires Long Bridge config)
    python run.py --dashboard            # Run with web dashboard on port 8080
    python run.py --paper --dashboard    # Paper trading + dashboard

Configuration:
    Edit config.yaml to set support/resistance prices and risk parameters.
    Environment variables override config:
        SOXS_SUPPORT=28.50  SOXS_RESISTANCE=30.50  SOXS_SIZE=100
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config.loader import load_config, validate_config
from src.engine.trading_engine import TradingEngine
from src.dashboard.server import start_dashboard


def setup_logging(level=logging.INFO):
    """Configure logging with timestamps and colors."""
    fmt = "%(asctime)s [%(levelname)-7s] %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=datefmt,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Quiet down noisy libraries
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(
        description="🎯 SOXS Range Arbitrage Trading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py                        # Paper trading
  python run.py --dashboard            # Paper + web dashboard
  python run.py --backtest             # Backtest last 30 days
  python run.py --live                 # LIVE trading (be careful!)

Environment variables:
  SOXS_SUPPORT=28.50       Override support price
  SOXS_RESISTANCE=30.50    Override resistance price
  SOXS_SIZE=100            Override position size
  SOXS_MODE=paper          Override trading mode
        """,
    )
    parser.add_argument("--paper", action="store_true", help="Paper trading mode (default)")
    parser.add_argument("--live", action="store_true", help="LIVE trading with Long Bridge")
    parser.add_argument("--backtest", action="store_true", help="Run backtest")
    parser.add_argument("--dashboard", action="store_true", help="Start web dashboard (port 8080)")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port (default: 8080)")
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument("--anytime", action="store_true", help="Ignore trading hours (run 24/7)")
    parser.add_argument("--init-position", type=float, default=0, metavar="AMOUNT",
                        help="Seed an initial virtual position (e.g. --init-position 1000 = $1,000 worth of shares)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and exit")

    args = parser.parse_args()

    # Setup logging
    setup_logging(level=logging.DEBUG if args.debug else logging.INFO)

    # Load configuration
    config_path = args.config or os.path.join(os.path.dirname(__file__), "config.yaml")

    if not os.path.exists(config_path):
        print(f"\n❌ Config file not found: {config_path}")
        print("   Copy and edit config.yaml to set your range parameters.\n")
        sys.exit(1)

    config = load_config(config_path)

    # Override mode from CLI
    if args.live:
        config.mode = "live"
    elif args.backtest:
        config.mode = "backtest"
    elif args.paper:
        config.mode = "paper"

    # Validate config
    issues = validate_config(config)
    has_errors = any(i.startswith("[ERROR]") for i in issues)
    for issue in issues:
        level = logging.ERROR if issue.startswith("[ERROR]") else logging.WARNING
        logging.log(level, issue)

    if has_errors:
        print("\n⚠️  Configuration has errors. Please fix them in config.yaml")
        print("   Set support_price and resistance_price to begin.\n")
        if not args.dry_run:
            sys.exit(1)

    # Dry run: validate only
    if args.dry_run:
        print("\n✅ Configuration is valid.")
        print(f"   Mode: {config.mode}")
        print(f"   Ticker: {config.ticker}")
        print(f"   Range: ${config.range.support_price} – ${config.range.resistance_price}")
        print(f"   Position: {config.position.size_per_trade} shares/trade")
        print(f"   Stop Loss: {config.risk.stop_loss_pct}%")
        return

    # Warnings for live mode
    if config.mode == "live":
        print("\n" + "⚠️ " * 20)
        print("   LIVE TRADING MODE — REAL MONEY WILL BE USED")
        print("   Ensure you have tested in paper mode first!")
        print("⚠️ " * 20 + "\n")
        time.sleep(2)

    # Create engine
    engine = TradingEngine(config, ignore_trading_hours=args.anytime)

    # Seed initial virtual position if requested
    if args.init_position > 0 and config.mode == "paper":
        engine.broker.connect()
        quote = engine.fetcher.get_quote()
        price = quote.price if quote else 6.50
        shares = int(args.init_position / price)
        engine.broker.seed_position(config.ticker, shares, price)
        logging.info(f"💰 Seeded virtual position: {shares} shares of {config.ticker} @ ${price:.2f} (≈${args.init_position:,.0f})")

    # Start dashboard if requested
    dashboard_thread = None
    if args.dashboard:
        dashboard_thread = start_dashboard(engine, port=args.port)

    # Handle backtest separately
    if config.mode == "backtest":
        print("Backtest mode — running simulation on historical data...")
        run_backtest(engine, config)
        return

    # Run the engine (blocks until Ctrl+C)
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n\n👋 Shutdown complete.")
    finally:
        if dashboard_thread:
            dashboard_thread.join(timeout=2)


def run_backtest(engine, config):
    """Simple backtest using recent historical data."""
    from src.data.fetcher import PriceFetcher

    print(f"\n🔬 Backtesting {config.ticker} over last 10 trading days...")

    # Fetch 5-minute bars for last 10 days
    fetcher = PriceFetcher(config.ticker)
    candles = fetcher.get_ohlcv(period="10d", interval="5m")

    if not candles:
        print("❌ No historical data available for backtest.")
        return

    print(f"   Loaded {len(candles)} bars from {candles[0].timestamp} to {candles[-1].timestamp}")

    # Simulate strategy
    position = 0
    entry_price = 0
    trades = []
    cash = 10000.0

    for i, c in enumerate(candles):
        engine.strategy.feed_price(c.close)

        if config.range.mode == "auto" and engine.strategy.needs_auto_refresh():
            engine.strategy.update_auto_range()

        has_pos = position > 0
        signal = engine.strategy.evaluate(c.close, has_pos)

        if signal.type.value == "BUY" and not has_pos:
            entry_price = c.close
            position = config.position.size_per_trade
            cost = entry_price * position
            cash -= cost
            trades.append({
                "type": "BUY", "price": entry_price, "shares": position,
                "time": c.timestamp, "cash": cash,
            })

        elif signal.type.value == "SELL" and has_pos:
            exit_price = c.close
            pnl = (exit_price - entry_price) * position
            cash += exit_price * position
            trades.append({
                "type": "SELL", "price": exit_price, "shares": position,
                "time": c.timestamp, "cash": cash, "pnl": pnl,
            })
            position = 0
            entry_price = 0

        elif signal.type.value == "STOP_LOSS" and has_pos:
            exit_price = c.close
            pnl = (exit_price - entry_price) * position
            cash += exit_price * position
            trades.append({
                "type": "STOP_LOSS", "price": exit_price, "shares": position,
                "time": c.timestamp, "cash": cash, "pnl": pnl,
            })
            position = 0
            entry_price = 0

    # Close any open position at last price
    if position > 0:
        exit_price = candles[-1].close
        pnl = (exit_price - entry_price) * position
        cash += exit_price * position
        trades.append({
            "type": "SELL (close)", "price": exit_price, "shares": position,
            "time": candles[-1].timestamp, "cash": cash, "pnl": pnl,
        })

    # Results
    total_pnl = cash - 10000.0
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]

    print(f"\n{'='*50}")
    print(f"  📊 BACKTEST RESULTS")
    print(f"{'='*50}")
    print(f"  Initial Cash:      $10,000.00")
    print(f"  Final Cash:        ${cash:,.2f}")
    print(f"  Total P&L:         ${total_pnl:+,.2f} ({total_pnl/10000*100:+.2f}%)")
    print(f"  Total Trades:      {len(trades)}")
    print(f"  Wins:              {len(wins)}")
    print(f"  Losses:            {len(losses)}")
    print(f"  Win Rate:          {len(wins)/len(trades)*100:.1f}%" if trades else "  Win Rate: N/A")
    if wins:
        print(f"  Best Trade:        ${max(t.get('pnl', 0) for t in wins):+.2f}")
    if losses:
        print(f"  Worst Trade:       ${min(t.get('pnl', 0) for t in losses):+.2f}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
