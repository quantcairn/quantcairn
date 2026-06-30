"""
Lightweight web dashboard for monitoring the range arbitrage system.
Flask + simple HTML/CSS, auto-refreshing.

Run: python -m src.dashboard.server
"""
import json
import logging
import os
from datetime import datetime
from threading import Thread
from typing import Optional

from flask import Flask, jsonify, render_template_string

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global reference to engine (set by run.py)
_engine = None

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SOXS Range Arbitrage Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, 'SF Mono', monospace; background: #0a0e27; color: #e0e0e0; padding: 20px; }
        .header { text-align: center; padding: 20px 0; border-bottom: 1px solid #1a1f3a; margin-bottom: 20px; }
        .header h1 { font-size: 24px; color: #00d4aa; }
        .header .subtitle { color: #666; font-size: 12px; margin-top: 4px;}
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
        .card { background: #111636; border: 1px solid #1a1f3a; border-radius: 8px; padding: 16px; }
        .card h2 { font-size: 12px; text-transform: uppercase; color: #666; margin-bottom: 12px; letter-spacing: 1px; }
        .price-big { font-size: 36px; font-weight: bold; color: #fff; }
        .change-up { color: #00d4aa; }
        .change-down { color: #ff4757; }
        .stat-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #1a1f3a; font-size: 13px; }
        .stat-row:last-child { border-bottom: none; }
        .stat-label { color: #888; }
        .stat-value { font-weight: bold; }
        .value-green { color: #00d4aa; }
        .value-red { color: #ff4757; }
        .value-yellow { color: #ffa502; }
        .signal { padding: 8px 16px; border-radius: 4px; font-size: 18px; font-weight: bold; text-align: center; margin-top: 8px; }
        .signal-buy { background: rgba(0, 212, 170, 0.15); color: #00d4aa; border: 1px solid #00d4aa; }
        .signal-sell { background: rgba(255, 71, 87, 0.15); color: #ff4757; border: 1px solid #ff4757; }
        .signal-hold { background: rgba(255, 165, 2, 0.1); color: #ffa502; border: 1px solid #ffa502; }
        .bar-container { height: 24px; background: #1a1f3a; border-radius: 4px; overflow: hidden; margin: 8px 0; position: relative; }
        .bar-fill { height: 100%; background: linear-gradient(90deg, #00d4aa, #ffa502, #ff4757); transition: width 1s; }
        .bar-label { position: absolute; width: 100%; text-align: center; line-height: 24px; font-size: 11px; color: #fff; }
        .trades-list { max-height: 200px; overflow-y: auto; font-size: 12px; }
        .trade-item { padding: 4px 0; border-bottom: 1px solid #1a1f3a; }
        .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
        .status-live { background: #00d4aa; animation: pulse 1.5s infinite; }
        .status-halted { background: #ff4757; }
        .status-paper { background: #ffa502; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        .refresh { font-size: 10px; color: #555; text-align: center; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🎯 SOXS Range Arbitrage</h1>
        <div class="subtitle">{{ status_line }}</div>
    </div>

    <div class="grid">
        <!-- Price Card -->
        <div class="card">
            <h2>💰 Price</h2>
            <div class="price-big">${{ "%.2f"|format(price) }}</div>
            <div style="margin-top:4px;">
                <span class="{{ 'change-up' if (change|default(0)) >= 0 else 'change-down' }}">
                    {{ '+' if (change|default(0)) >= 0 else '' }}{{ "%.2f"|format(change|default(0.0)) }}%
                </span>
            </div>
            <div class="stat-row"><span class="stat-label">Bid/Ask</span><span class="stat-value">{{ "%.2f"|format(bid) }} / {{ "%.2f"|format(ask) }}</span></div>
        </div>

        <!-- Range Card -->
        <div class="card">
            <h2>📐 Range</h2>
            <div class="stat-row"><span class="stat-label">Support</span><span class="stat-value value-green">${{ "%.2f"|format(support) }}</span></div>
            <div class="stat-row"><span class="stat-label">Resistance</span><span class="stat-value value-red">${{ "%.2f"|format(resistance) }}</span></div>
            <div class="stat-row"><span class="stat-label">Spread</span><span class="stat-value">${{ "%.2f"|format(spread_dollars) }} ({{ "%.1f"|format(spread_pct) }}%)</span></div>
            <div class="bar-container">
                <div class="bar-fill" style="width: {{ position_in_range }}%;"></div>
                <div class="bar-label">Price: {{ "%.0f"|format(position_in_range) }}% in range</div>
            </div>
            <div class="signal {{ 'signal-buy' if last_signal == 'BUY' else 'signal-sell' if last_signal == 'SELL' else 'signal-hold' }}">
                {{ last_signal }}
            </div>
        </div>

        <!-- Position Card -->
        <div class="card">
            <h2>📊 Position</h2>
            <div class="stat-row"><span class="stat-label">Initial Capital</span><span class="stat-value">${{ "%.2f"|format(initial_capital) }}</span></div>
            <div class="stat-row"><span class="stat-label">Cash</span><span class="stat-value">${{ "%.2f"|format(cash) }}</span></div>
            <div class="stat-row"><span class="stat-label">Shares</span><span class="stat-value">{{ position_shares }}</span></div>
            <div class="stat-row"><span class="stat-label">Entry Price</span><span class="stat-value">{% if entry_price and entry_price > 0 %}${{ "%.2f"|format(entry_price) }}{% else %}N/A{% endif %}</span></div>
            <div class="stat-row"><span class="stat-label">Unrealized P&L</span><span class="stat-value {{ 'value-green' if (unrealized_pnl|default(0)) >= 0 else 'value-red' }}">${{ "%.2f"|format(unrealized_pnl|default(0.0)) }}</span></div>
            <div class="stat-row"><span class="stat-label">Equity</span><span class="stat-value">${{ "%.2f"|format(equity) }}</span></div>
        </div>

        <!-- Risk Card -->
        <div class="card">
            <h2>🛡️ Risk</h2>
            <div class="stat-row"><span class="stat-label">Daily P&L</span><span class="stat-value {{ 'value-green' if daily_pnl is not none and daily_pnl >= 0 else 'value-red' }}">${{ "%.2f"|format(daily_pnl) if daily_pnl is not none else "0.00" }}</span></div>
            <div class="stat-row"><span class="stat-label">Trades Today</span><span class="stat-value">{{ trades_today }}</span></div>
            <div class="stat-row"><span class="stat-label">Consecutive Losses</span><span class="stat-value {{ 'value-red' if (consecutive_losses|default(0)) > 0 else '' }}">{{ consecutive_losses }}</span></div>
            <div class="stat-row"><span class="stat-label">Win Rate</span><span class="stat-value">{{ "%.1f"|format(win_rate) }}%</span></div>
            <div class="stat-row">
                <span class="stat-label">Status</span>
                <span class="stat-value">
                    <span class="status-dot {{ 'status-live' if running and not halted else 'status-halted' if halted else 'status-paper' }}"></span>
                    {{ 'RUNNING' if running and not halted else 'HALTED' if halted else 'STOPPED' }}
                </span>
            </div>
        </div>
    </div>

    <div class="refresh">Auto-refresh every 3s | {{ last_update }}</div>
</body>
<script>
    setTimeout(function() { location.reload(); }, 3000);
</script>
</html>"""


def set_engine(engine):
    """Set the trading engine reference for dashboard data."""
    global _engine
    _engine = engine


@app.route("/")
def index():
    """Main dashboard page."""
    data = get_dashboard_data()
    return render_template_string(HTML_TEMPLATE, **data)


@app.route("/api/recent")
def api_recent():
    """Return last 30 price points for sparkline."""
    if _engine is None:
        return jsonify({"prices": [], "ticker": "N/A"})
    try:
        prices = _engine.strategy._price_history[-60:] if _engine.strategy._price_history else []
        recent_bars = [round(float(p), 4) for p in prices[-30:]]
        return jsonify({
            "ticker": _engine.ticker,
            "prices": prices,
            "recent_bars": recent_bars,
        })
    except Exception as e:
        return jsonify({"prices": [], "ticker": "N/A", "error": str(e)})


@app.route("/api/status")
def api_status():
    """JSON API endpoint."""
    return jsonify(get_dashboard_data())


def get_dashboard_data() -> dict:
    """Gather all data for the dashboard."""
    if _engine is None:
        return _empty_data()

    try:
        # Price: dashboard reads cached engine state only. Fetching live market
        # data here can block /api/status and make the combined view show zero.
        quote = getattr(_engine.fetcher, "_cached_quote", None)
        recent_prices = getattr(_engine.strategy, "_price_history", []) or []
        last_price = float(recent_prices[-1]) if recent_prices else 0.0

        price = float(getattr(quote, "price", 0) or 0.0)
        if price <= 0:
            price = last_price

        change = float(getattr(quote, "change_pct", 0) or 0.0)
        bid = float(getattr(quote, "bid", 0) or 0.0)
        if bid <= 0:
            bid = price
        ask = float(getattr(quote, "ask", 0) or 0.0)
        if ask <= 0:
            ask = price

        high_1m = getattr(quote, "high_1m", None)
        if not high_1m or high_1m <= 0:
            high_1m = price
        low_1m = getattr(quote, "low_1m", None)
        if not low_1m or low_1m <= 0:
            low_1m = price
        volume = int(getattr(quote, "volume", 0) or 0)

        # Range
        rs = _engine.strategy.get_range_state()
        support = rs.support
        resistance = rs.resistance
        spread_dollars = rs.spread_dollars
        spread_pct = rs.spread_pct
        position_in_range = ((price - support) / (resistance - support) * 100) if (resistance and support and resistance != support) else 50
        position_in_range = max(0, min(100, position_in_range))

        # Position/account snapshots are maintained by the engine loop.
        # Do not call back into the broker here: status polling must stay
        # lightweight and never block on trading APIs.
        pos = getattr(_engine, "_latest_position", None)
        position_shares = pos.quantity if pos else 0
        entry_price = pos.avg_entry_price if pos and pos.quantity > 0 else 0.0
        unrealized_pnl = pos.unrealized_pnl if pos else 0.0

        initial_capital = _engine.config.position.initial_capital

        # Risk
        stats = _engine.risk.get_stats()
        daily_pnl = float(stats.get("daily_pnl_today") or 0.0)

        acct = getattr(_engine, "_latest_account", None)

        if _engine.mode == "live":
            initial_capital = float(getattr(acct, "buying_power", 0.0) or 0.0) if acct else 0.0
            if initial_capital <= 0 and acct is not None:
                initial_capital = float(getattr(acct, "cash", 0.0) or 0.0)
            cash = float(getattr(acct, "cash", 0.0) or 0.0) if acct else 0.0
            equity = float(getattr(acct, "equity", 0.0) or 0.0) if acct else 0.0
        else:
            cash = acct.cash if acct else 0.0
            equity = acct.equity if acct else 0.0

        # Signal
        last_signal = (_engine._last_signal_type.value
                       if _engine._last_signal_type else "HOLD")

        # Guarantee no None values escape to the template
        def _nz(v, default=0):
            return v if v is not None else default

        range_ready = bool(
            getattr(rs, "is_valid", bool(support and resistance and resistance > support))
            and support
            and resistance
        )

        return {
            "price": _nz(price, 0.0),
            "change": _nz(change, 0.0),
            "bid": _nz(bid, 0.0),
            "ask": _nz(ask, 0.0),
            "high_1m": high_1m,
            "low_1m": low_1m,
            "volume": volume,
            "support": _nz(support, 0.0),
            "resistance": _nz(resistance, 0.0),
            "spread_dollars": _nz(spread_dollars, 0.0),
            "spread_pct": _nz(spread_pct, 0.0),
            "range_ready": range_ready,
            "range_source": getattr(rs, "source", "unknown"),
            "support_confidence": float(getattr(rs, "support_confidence", 0.0) or 0.0),
            "position_in_range": _nz(position_in_range, 50.0),
            "position_shares": _nz(position_shares, 0),
            "entry_price": _nz(entry_price, 0.0),
            "unrealized_pnl": _nz(unrealized_pnl, 0.0),
            "initial_capital": _nz(initial_capital, 0.0),
            "cash": _nz(round(cash, 2), 0.0),
            "equity": _nz(round(equity, 2), 0.0),
            "daily_pnl": _nz(daily_pnl, 0.0),
            "trades_today": _nz(stats.get("total_trades"), 0),
            "consecutive_losses": _nz(stats.get("consecutive_losses"), 0),
            "win_rate": _nz(stats.get("win_rate"), 0.0),
            "running": _engine._running if _engine._running is not None else False,
            "halted": stats.get("halted", False) or False,
            "last_signal": _nz(last_signal, "HOLD"),
            "last_update": datetime.now().strftime("%H:%M:%S"),
            "status_line": (f"{_engine.ticker} Range Arbitrage | "
                            f"{_engine.mode.upper()} Mode | "
                            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"),
        }
    except Exception as e:
        logger.error(f"Dashboard data error: {e}")
        return _empty_data()


def _empty_data() -> dict:
    return {
        "price": 0, "change": 0, "bid": 0, "ask": 0,
        "high_1m": 0, "low_1m": 0, "volume": 0,
        "support": 0, "resistance": 0, "spread_dollars": 0, "spread_pct": 0,
        "range_ready": False, "range_source": "unknown", "support_confidence": 0.0,
        "position_in_range": 50,
        "position_shares": 0, "entry_price": 0.0, "unrealized_pnl": 0.0,
        "initial_capital": 0, "cash": 0, "equity": 0,
        "daily_pnl": 0, "trades_today": 0,
        "consecutive_losses": 0, "win_rate": 0,
        "running": False, "halted": False, "last_signal": "N/A",
        "last_update": datetime.now().strftime("%H:%M:%S"),
        "status_line": "SOXS Range Arbitrage | Engine not running",
    }


def start_dashboard(engine, host: str = "0.0.0.0", port: int = 8080) -> Thread:
    """Start the dashboard server in a background thread."""
    set_engine(engine)

    # Run single-threaded to limit concurrent thread/socket creation which
    # may exhaust file descriptors in long-running environments.
    thread = Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False, threaded=False), daemon=True)
    thread.start()

    logger.info(f"📊 Dashboard running at http://localhost:{port}")
    return thread
