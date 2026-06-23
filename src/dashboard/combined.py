"""Combined dashboard aggregating all 3 trading engines."""
import json, urllib.request
from datetime import datetime
from flask import Flask

app = Flask(__name__)

TICKERS = [
    {"name": "DRIP", "desc": "2x做空油气",        "port": 8080},
    {"name": "AMC",  "desc": "AMC电影院 (meme)",  "port": 8081},
    {"name": "SMR",  "desc": "NuScale小型核反应堆", "port": 8082},
]

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
    .halted-badge{display:inline-block;background:#ff4757;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px}
    .refresh{text-align:center;color:#333;font-size:10px;margin-top:15px}
</style></head>
<body>
<h1>🎯 Multi-Stock Range Arbitrage</h1>
<div class="sub">{{ update_time }}</div>

<div class="grid">
{% for card in cards %}
<div class="card">
    <h2>
        <span class="ticker">${{ card.name }}</span>
        <span class="desc">{{ card.desc }}</span>
        {% if card.halted %}<span class="halted-badge">HALTED</span>{% endif %}
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
    <div style="margin-top:5px;color:#888">Total Equity: ${{ "%.2f"|format(total_equity) }} | Total Trades: {{ total_trades }}</div>
</div>

<div class="refresh">Auto-refresh every 5s | {{ update_time }}</div>
</body></html>"""


def _fetch_status(port):
    """Fetch /api/status JSON from an engine."""
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2)
        return json.loads(req.read().decode())
    except Exception:
        return None

def _fetch_recent(port):
    """Fetch /api/recent JSON (sparkline data) from an engine."""
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/recent", timeout=2)
        return json.loads(req.read().decode())
    except Exception:
        return {"recent_bars": []}


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
    total_equity = 0.0
    total_trades = 0

    for t in TICKERS:
        d = _fetch_status(t["port"])
        r = _fetch_recent(t["port"])

        if d:
            supp = d.get("support", 0)
            res = d.get("resistance", 0)
            price = d.get("price", 0)
            pos_pct = ((price - supp) / (res - supp) * 100) if res and supp and res != supp else 50
            pos_pct = max(0, min(100, pos_pct))

            sparkline = _build_sparkline(r.get("recent_bars", []), price)

            card = {
                "name": t["name"],
                "desc": t["desc"],
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
                "shares": d.get("position_shares", 0),
                "pnl": d.get("daily_pnl", 0),
                "equity": d.get("equity", 0),
                "trades": d.get("trades_today", 0),
                "halted": d.get("halted", False),
            }
            cards.append(card)
            total_pnl += d.get("daily_pnl", 0) or 0
            total_equity += d.get("equity", 0) or 0
            total_trades += d.get("trades_today", 0) or 0
        else:
            cards.append({
                "name": t["name"], "desc": t["desc"],
                "price": 0, "price_change": 0,
                "day_high": 0, "day_low": 0,
                "bid": 0, "ask": 0, "vol_display": "0",
                "support": 0, "resistance": 0, "spread_pct": 0,
                "pos_pct": 50,
                "sparkline": _build_sparkline([], 0),
                "signal": "OFFLINE", "shares": 0,
                "pnl": 0, "equity": 0, "trades": 0, "halted": False,
            })

    from flask import render_template_string
    return render_template_string(HTML,
        cards=cards,
        total_pnl=round(total_pnl, 2),
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
