"""Combined dashboard aggregating the selected TOP3 trading engines."""
import json, os, subprocess, urllib.request
from datetime import datetime
from pathlib import Path
from flask import Flask
import yaml

from src.reports.trade_audit import summarize_trade_log

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]

TICKERS = [
    {"name": "TOP1", "desc": "AI Top Pick #1",    "port": 8091, "config": "TOP1.yaml"},
    {"name": "TOP2", "desc": "AI Top Pick #2",    "port": 8092, "config": "TOP2.yaml"},
    {"name": "TOP3", "desc": "AI Top Pick #3",    "port": 8093, "config": "TOP3.yaml"},
]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None and str(value).strip():
        return str(value).strip()
    try:
        proc = subprocess.run(
            ["launchctl", "getenv", name],
            capture_output=True,
            text=True,
            check=False,
        )
        value = (proc.stdout or "").strip()
        if value:
            return value
    except Exception:
        pass
    return default.strip()


def _has_live_account_env() -> bool:
    return bool(
        _env("LONGBRIDGE_ACCESS_TOKEN")
        and (
            (_env("LONGBRIDGE_APP_KEY") or _env("LONGBRIDGE_API_KEY"))
            and (_env("LONGBRIDGE_APP_SECRET") or _env("LONGBRIDGE_API_SECRET"))
        )
    )


def _fetch_live_account_summary():
    """Read live buying power from LongBridge if credentials are present."""
    if not _has_live_account_env():
        return None
    try:
        from src.broker.longbridge_broker import LongBridgeBroker
    except Exception:
        return None

    try:
        broker = LongBridgeBroker(
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
        if not broker.connect():
            return None
        account = broker.get_account()
        return {
            "cash": float(getattr(account, "cash", 0.0) or 0.0),
            "equity": float(getattr(account, "equity", 0.0) or 0.0),
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0),
            "positions_count": len(getattr(account, "positions", []) or []),
            "mode": "live",
        }
    except Exception:
        return None

HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>📊 Multi-Stock Range Arbitrage</title>
<style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#0a0a1a;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,monospace;padding:20px}
    h1{text-align:center;color:#00d4aa;margin-bottom:5px;font-size:22px}
    .sub{text-align:center;color:#555;font-size:11px;margin-bottom:20px}
    .grid{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}
    .card{background:#111133;border-radius:10px;padding:14px;width:340px;border:1px solid #222}
    .card h2{margin-bottom:6px;font-size:15px;display:flex;align-items:center;gap:6px}
    .ticker{color:#00d4aa}.desc{color:#666;font-size:12px}
    .price-row{display:flex;align-items:baseline;gap:10px;margin:6px 0}
    .price{font-size:30px;font-weight:bold}
    .change{font-size:14px;font-weight:bold}
    .price-detail{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;font-size:11px;background:#0d0f25;padding:8px;border-radius:6px;margin:8px 0}
    .pd-label{color:#666}.pd-val{color:#ddd;text-align:right}
    .sparkline{display:flex;align-items:flex-end;height:40px;gap:1px;background:#0d0f25;border-radius:4px;padding:4px 2px;margin:6px 0}
    .spark-bar{flex:1;min-width:2px;border-radius:1px;opacity:0.85}
    .vol-bar{height:3px;background:#1a1f3a;border-radius:2px;margin:3px 0;overflow:hidden}
    .vol-fill{height:100%;background:#334;border-radius:2px}
    .stat{display:flex;justify-content:space-between;padding:2px 0;font-size:12px}
    .label{color:#888}.val{color:#fff}
    .green{color:#00d4aa}.red{color:#ff4757}.yellow{color:#ffa502}
    .range-bar{height:6px;background:#1a1f3a;border-radius:3px;margin:6px 0;overflow:hidden}
    .range-fill{height:100%;border-radius:3px;transition:width .5s}
    .signal{text-align:center;padding:5px;border-radius:5px;font-weight:bold;margin:6px 0;font-size:13px}
    .sig-buy{background:#00d4aa22;color:#00d4aa;border:1px solid #00d4aa44}
    .sig-sell{background:#ff475722;color:#ff4757;border:1px solid #ff475744}
    .sig-hold{background:#55555522;color:#888;border:1px solid #55555544}
    .sig-block{background:#ffa50222;color:#ffa502;border:1px solid #ffa50244}
    .pnl-row{display:grid;grid-template-columns:1fr 1fr;gap:3px 8px;margin-top:4px}
    .total{background:#111138;border-radius:10px;padding:15px;margin-top:20px;text-align:center;border:1px solid #333}
    .total h2{color:#ffa502}
    .total .price{font-size:36px}
    .account{background:#10162d;border-radius:10px;padding:14px;margin:12px 0 20px;border:1px solid #26314d}
    .account h2{font-size:16px;margin-bottom:8px;color:#7dd3fc}
    .account-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
    .account-stat{background:#0d0f25;border-radius:8px;padding:10px}
    .account-label{display:block;color:#8a8fa3;font-size:11px;margin-bottom:4px}
    .account-value{display:block;color:#fff;font-size:18px;font-weight:700}
    .audit{background:#10162d;border-radius:10px;padding:14px;margin:12px 0 20px;border:1px solid #26314d}
    .audit h2{font-size:16px;margin-bottom:8px;color:#fbbf24}
    .audit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
    .audit-stat{background:#0d0f25;border-radius:8px;padding:10px}
    .audit-label{display:block;color:#8a8fa3;font-size:11px;margin-bottom:4px}
    .audit-value{display:block;color:#fff;font-size:15px;font-weight:700;line-height:1.3;word-break:break-word}
    .audit-value.muted{color:#8a8fa3;font-weight:500}
    .halted-badge{display:inline-block;background:#ff4757;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px}
    .offline-badge{display:inline-block;background:#555;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px}
    .offline{opacity:.72}
    .refresh{text-align:center;color:#333;font-size:10px;margin-top:15px}
</style></head>
<body>
<h1>🎯 Multi-Stock Range Arbitrage</h1>
<div class="sub">{{ update_time }}</div>

<div class="account">
    <h2>💼 Live Account</h2>
    <div class="account-grid">
        <div class="account-stat"><span class="account-label">Cash</span><span class="account-value">{% if live_account and live_account.cash is not none %}${{ "%.2f"|format(live_account.cash) }}{% else %}N/A{% endif %}</span></div>
        <div class="account-stat"><span class="account-label">Equity</span><span class="account-value">{% if live_account and live_account.equity is not none %}${{ "%.2f"|format(live_account.equity) }}{% else %}N/A{% endif %}</span></div>
        <div class="account-stat"><span class="account-label">Buying Power</span><span class="account-value">{% if live_account and live_account.buying_power is not none %}${{ "%.2f"|format(live_account.buying_power) }}{% else %}N/A{% endif %}</span></div>
        <div class="account-stat"><span class="account-label">Positions</span><span class="account-value">{% if live_account and live_account.positions_count is not none %}{{ live_account.positions_count }}{% else %}N/A{% endif %}</span></div>
    </div>
</div>

<div class="audit">
    <h2>🧭 Session Risk & Trade Audit</h2>
    <div class="audit-grid">
        <div class="audit-stat"><span class="audit-label">Execution Mode</span><span class="audit-value">{% if trade_audit and trade_audit.execution_mode %}{{ trade_audit.execution_mode|upper }}{% else %}PAPER{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">Reduce Only</span><span class="audit-value">{% if trade_audit and trade_audit.reduce_only %}YES{% else %}NO{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">New Entries</span><span class="audit-value">{% if trade_audit and trade_audit.new_entries_allowed %}ALLOWED{% else %}PAUSED{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">Pause Reason</span><span class="audit-value {% if not trade_audit or not trade_audit.risk_pause_reason %}muted{% endif %}">{% if trade_audit and trade_audit.risk_pause_reason %}{{ trade_audit.risk_pause_reason }}{% else %}None{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">Today Executions</span><span class="audit-value">{% if trade_audit %}{{ trade_audit.execution_count }}{% else %}0{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">Today Decisions</span><span class="audit-value">{% if trade_audit %}{{ trade_audit.decision_count }}{% else %}0{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">Today Qty</span><span class="audit-value">{% if trade_audit %}{{ trade_audit.order_qty }}{% else %}0{% endif %}</span></div>
        <div class="audit-stat"><span class="audit-label">Latest Trade</span><span class="audit-value {% if not trade_audit or not trade_audit.latest_line %}muted{% endif %}">{% if trade_audit and trade_audit.latest_line %}{{ trade_audit.latest_line }}{% else %}None{% endif %}</span></div>
    </div>
</div>

<div class="grid">
{% for card in cards %}
<div class="card {% if not card.online %}offline{% endif %}">
    <h2>
        <span class="ticker">${{ card.name }}</span>
        <span class="desc">{{ card.desc }}</span>
        {% if card.halted %}<span class="halted-badge">HALTED</span>{% endif %}
        {% if not card.online %}<span class="offline-badge">OFFLINE</span>{% endif %}
    </h2>

    <!-- Price -->
    <div class="price-row">
        <span class="price" style="color:{% if card.price_change >= 0 %}#00d4aa{% else %}#ff4757{% endif %}">
            ${{ "%.2f"|format(card.price) }}
        </span>
        <span class="change {{ 'green' if card.price_change >= 0 else 'red' }}">
            {{ '+' if card.price_change >= 0 else '' }}{{ "%.2f"|format(card.price_change) }}%
        </span>
    </div>

    <!-- Real-time data grid -->
    <div class="price-detail">
        <span class="pd-label">Day High</span><span class="pd-val green">${{ "%.2f"|format(card.day_high) }}</span>
        <span class="pd-label">Day Low</span><span class="pd-val red">${{ "%.2f"|format(card.day_low) }}</span>
        <span class="pd-label">Bid</span><span class="pd-val">${{ "%.2f"|format(card.bid) }}</span>
        <span class="pd-label">Ask</span><span class="pd-val">${{ "%.2f"|format(card.ask) }}</span>
        <span class="pd-label">Volume</span><span class="pd-val">{{ card.vol_display }}</span>
    </div>

    <!-- Sparkline - last 30 prices -->
    <div class="sparkline">
        {% for bar in card.sparkline %}
        <div class="spark-bar" style="height:{{ bar.height }}%;background:{{ bar.color }}"></div>
        {% endfor %}
    </div>

    <!-- Range -->
    <div class="stat"><span class="label">Range</span><span class="val">${{ "%.2f"|format(card.support) }} – ${{ "%.2f"|format(card.resistance) }} ({{ "%.1f"|format(card.spread_pct) }}%)</span></div>
    <div class="range-bar">
        <div class="range-fill" style="width:{{ card.pos_pct }}%;background:{% if card.pos_pct > 70 %}#ff4757{% elif card.pos_pct < 30 %}#00d4aa{% else %}#ffa502{% endif %}"></div>
    </div>
    <div class="stat"><span class="label">In Range</span><span class="val">{{ "%.0f"|format(card.pos_pct) }}%</span></div>

    <!-- Signal -->
    <div class="signal {% if card.signal == 'BUY' %}sig-buy{% elif card.signal == 'SELL' %}sig-sell{% elif 'TREND' in card.signal %}sig-block{% else %}sig-hold{% endif %}">
        {{ card.signal }}
    </div>

    <!-- P&L Row -->
    <div class="pnl-row">
        <div class="stat"><span class="label">Capital</span><span class="val">${{ "%.2f"|format(card.initial_capital) }}</span></div>
        <div class="stat"><span class="label">Cash</span><span class="val">${{ "%.2f"|format(card.cash) }}</span></div>
        <div class="stat"><span class="label">Shares</span><span class="val">{{ card.shares }}</span></div>
        <div class="stat"><span class="label">Equity</span><span class="val">${{ "%.2f"|format(card.equity) }}</span></div>
        <div class="stat"><span class="label">Daily P&L</span><span class="val {{ 'green' if card.pnl >= 0 else 'red' }}">${{ "%+.2f"|format(card.pnl) }}</span></div>
        <div class="stat"><span class="label">Trades</span><span class="val">{{ card.trades }}</span></div>
    </div>
</div>
{% endfor %}
</div>

<div class="total">
    <h2>💰 Combined P&L</h2>
    <div class="price {{ 'green' if total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(total_pnl) }}</div>
    <div style="margin-top:5px;color:#888">Total Capital: ${{ "%.2f"|format(total_capital) }} | Total Equity: ${{ "%.2f"|format(total_equity) }} | Total Trades: {{ total_trades }}</div>
</div>

<div class="refresh">Auto-refresh every 5s | {{ update_time }}</div>
</body></html>"""


def _fetch_status(port):
    """Fetch /api/status JSON from an engine."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1)
        return json.loads(req.read().decode())
    except Exception:
        return None


def _load_config_defaults(config_name):
    """Read display defaults from YAML so offline engines do not show $0 capital."""
    cfg_path = PROJECT_DIR / "configs" / config_name
    defaults = {
        "ticker": config_name.replace(".yaml", ""),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
    }
    try:
        data = yaml.safe_load(cfg_path.read_text()) or {}
        defaults["ticker"] = data.get("ticker") or defaults["ticker"]
        position = data.get("position") or {}
        defaults["initial_capital"] = float(position.get("initial_capital") or 0.0)
        range_cfg = data.get("range") or {}
        defaults["support"] = float(range_cfg.get("support_price") or 0.0)
        defaults["resistance"] = float(range_cfg.get("resistance_price") or 0.0)
    except Exception:
        pass
    return defaults


def _build_sparkline(prices, current_price):
    """Turn price list into sparkline bar dicts."""
    bars = []
    if not prices or len(prices) < 2:
        # Empty sparkline
        for _ in range(30):
            bars.append({"height": 50, "color": "#222"})
        return bars

    valid = [p for p in prices if p > 0]
    if len(valid) < 2:
        for _ in range(30):
            bars.append({"height": 50, "color": "#222"})
        return bars

    lo = min(valid)
    hi = max(valid)
    rng = hi - lo if hi != lo else 1

    prev = valid[0]
    for p in valid[-30:]:
        h = (p - lo) / rng * 100
        if h < 0: h = 0
        if h > 100: h = 100
        color = "#00d4aa" if p >= prev else "#ff4757"
        bars.append({"height": round(h, 1), "color": color})
        prev = p

    # Pad to 30
    while len(bars) < 30:
        bars.insert(0, {"height": 50, "color": "#222"})

    return bars


def _fmt_vol(v):
    if v >= 1_000_000_000:
        return f"{v/1e9:.1f}B"
    if v >= 1_000_000:
        return f"{v/1e6:.1f}M"
    if v >= 1_000:
        return f"{v/1e3:.1f}K"
    return str(int(v))


@app.route("/")
def index():
    cards = []
    total_pnl = 0.0
    total_capital = 0.0
    total_equity = 0.0
    total_trades = 0
    live_account = _fetch_live_account_summary()
    trade_audit = summarize_trade_log(PROJECT_DIR / "logs")
    latest_execution = trade_audit.get("latest_execution") if isinstance(trade_audit, dict) else {}
    latest_record = trade_audit.get("latest_record") if isinstance(trade_audit, dict) else {}
    latest_line = None
    latest_source = latest_execution if latest_execution else latest_record
    if isinstance(latest_source, dict) and latest_source:
        ticker = latest_source.get("ticker") or latest_source.get("symbol") or ""
        phase = latest_source.get("phase") or latest_source.get("action") or "record"
        order = latest_source.get("order") if isinstance(latest_source.get("order"), dict) else {}
        side = order.get("side") or latest_source.get("trade_signal", {}).get("action") or ""
        qty = order.get("qty") or ""
        status = ""
        response = latest_source.get("response")
        if isinstance(response, dict):
            status = response.get("status") or response.get("body", {}).get("status") or response.get("ok")
        latest_line = " ".join(str(part) for part in [phase, ticker, side, qty, status] if part not in ("", None, False))
    trade_audit = {
        **trade_audit,
        "latest_line": latest_line,
        "new_entries_allowed": bool(trade_audit.get("new_entries_allowed", True)),
        "reduce_only": bool(trade_audit.get("reduce_only", False)),
    }

    for t in TICKERS:
        d = _fetch_status(t["port"])
        defaults = _load_config_defaults(t["config"])

        if d:
            supp = d.get("support", 0)
            res = d.get("resistance", 0)
            price = d.get("price", 0)
            pos_pct = ((price - supp) / (res - supp) * 100) if res and supp and res != supp else 50
            pos_pct = max(0, min(100, pos_pct))

            sparkline = _build_sparkline([price], price)

            card = {
                "name": f"{t['name']} · {defaults['ticker']}" if t["name"].startswith("TOP") else t["name"],
                "desc": t["desc"],
                "online": True,
                "price": price,
                "price_change": d.get("change", 0),
                "day_high": d.get("high_1m", price),
                "day_low": d.get("low_1m", price),
                "bid": d.get("bid", price),
                "ask": d.get("ask", price),
                "vol_display": _fmt_vol(d.get("volume", 0)),
                "support": supp,
                "resistance": res,
                "spread_pct": d.get("spread_pct", 0),
                "pos_pct": pos_pct,
                "sparkline": sparkline,
                "signal": d.get("last_signal", "N/A"),
                "initial_capital": d.get("initial_capital", 0),
                "cash": d.get("cash", 0),
                "shares": d.get("position_shares", 0),
                "pnl": d.get("daily_pnl", 0),
                "equity": d.get("equity", 0),
                "trades": d.get("trades_today", 0),
                "halted": d.get("halted", False),
            }
            cards.append(card)
            total_pnl += d.get("daily_pnl", 0) or 0
            total_capital += d.get("initial_capital", 0) or 0
            total_equity += d.get("equity", 0) or 0
            total_trades += d.get("trades_today", 0) or 0
        else:
            initial_capital = defaults["initial_capital"]
            cards.append({
                "name": defaults["ticker"], "desc": t["desc"],
                "online": False,
                "price": 0, "price_change": 0,
                "day_high": 0, "day_low": 0,
                "bid": 0, "ask": 0, "vol_display": "0",
                "support": defaults["support"], "resistance": defaults["resistance"], "spread_pct": 0,
                "pos_pct": 50,
                "sparkline": _build_sparkline([], 0),
                "signal": "OFFLINE", "shares": 0,
                "initial_capital": initial_capital, "cash": initial_capital,
                "pnl": 0, "equity": initial_capital, "trades": 0, "halted": False,
            })
            total_capital += initial_capital
            total_equity += initial_capital

    from flask import render_template_string
    return render_template_string(HTML,
        cards=cards,
        live_account=live_account,
        trade_audit=trade_audit,
        total_pnl=round(total_pnl, 2),
        total_capital=round(total_capital, 2),
        total_equity=round(total_equity, 2),
        total_trades=total_trades,
        update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def start_combined(port=8090):
    """Start combined dashboard in background thread."""
    from threading import Thread
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False), daemon=True)
    t.start()
    return t
