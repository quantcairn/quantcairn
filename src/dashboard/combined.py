"""Combined dashboard aggregating the selected TOP5 trading engines."""
import json, os, subprocess, urllib.request
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, redirect, render_template_string, request
import yaml

from src.ai_selector.settings import load_runtime_settings, save_runtime_settings
from src.reports.trade_audit import summarize_trade_log

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]

TICKERS = [
    {"name": "TOP1", "desc": "AI优选第1名",    "port": 8091, "config": "TOP1.yaml"},
    {"name": "TOP2", "desc": "AI优选第2名",    "port": 8092, "config": "TOP2.yaml"},
    {"name": "TOP3", "desc": "AI优选第3名",    "port": 8093, "config": "TOP3.yaml"},
    {"name": "TOP4", "desc": "AI优选第4名",    "port": 8094, "config": "TOP4.yaml"},
    {"name": "TOP5", "desc": "AI优选第5名",    "port": 8095, "config": "TOP5.yaml"},
]

IGNORED_AUDIT_ACTIONS = {"get_account", "get_positions", "get_realtime_quote"}
_LIVE_ACCOUNT_CACHE = None
_LIVE_ACCOUNT_CACHE_AT = 0.0
_LIVE_ACCOUNT_CACHE_TTL = 30.0


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
    global _LIVE_ACCOUNT_CACHE, _LIVE_ACCOUNT_CACHE_AT
    now = time.time()
    if _LIVE_ACCOUNT_CACHE and (now - _LIVE_ACCOUNT_CACHE_AT) < _LIVE_ACCOUNT_CACHE_TTL:
        return _LIVE_ACCOUNT_CACHE
    if not _has_live_account_env():
        return None
    try:
        from src.broker.longbridge_broker import LongBridgeBroker
    except Exception:
        return None
    summary = None
    broker = None
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
        positions = broker.get_positions()
        account = broker.get_account()
        summary = {
            "cash": float(getattr(account, "cash", 0.0) or 0.0),
            "equity": float(getattr(account, "equity", 0.0) or 0.0),
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0),
            "positions_count": len(positions or []),
            "mode": "live",
        }
    except Exception:
        return None
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                pass
    if summary is not None:
        _LIVE_ACCOUNT_CACHE = summary
        _LIVE_ACCOUNT_CACHE_AT = now
    return summary


def _load_ai_selection_report():
    path = PROJECT_DIR / "reports" / "ai_selection_latest.json"
    if not path.exists():
        return {"timestamp": None, "report": [], "top5": [], "top3": [], "top10": [], "settings": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"timestamp": None, "report": [], "top5": [], "top3": [], "top10": [], "settings": {}}
        rows = data.get("report") if isinstance(data.get("report"), list) else []
        return {
            "timestamp": data.get("timestamp"),
            "report": rows,
            "top5": data.get("top5") if isinstance(data.get("top5"), list) else [],
            "top3": data.get("top3") if isinstance(data.get("top3"), list) else [],
            "top10": data.get("top10") if isinstance(data.get("top10"), list) else [],
            "settings": data.get("settings") if isinstance(data.get("settings"), dict) else {},
        }
    except Exception:
        return {"timestamp": None, "report": [], "top5": [], "top3": [], "top10": [], "settings": {}}


def _desired_audit_mode() -> str:
    value = _env("SOXS_AUDIT_MODE", "live").strip().lower()
    return value if value in {"live", "paper"} else "live"


def _load_top_modes() -> list[str]:
    modes: list[str] = []
    for item in TICKERS:
        cfg_path = PROJECT_DIR / "configs" / item["config"]
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        mode = str(data.get("mode", "paper")).strip().lower() or "paper"
        modes.append(mode)
    return modes


def _resolve_dashboard_execution_mode(trade_audit: dict) -> str:
    explicit = str(trade_audit.get("execution_mode") or "").strip().lower()
    if explicit == "live" and int(trade_audit.get("execution_count", 0) or 0) > 0:
        return "live"

    top_modes = _load_top_modes()
    if top_modes and all(mode == "live" for mode in top_modes):
        return "live"
    if top_modes and any(mode == "live" for mode in top_modes):
        return "mixed"
    return explicit or "paper"


def _latest_trade_line(trade_audit: dict) -> str | None:
    latest_execution = trade_audit.get("latest_execution") if isinstance(trade_audit, dict) else {}
    latest_record = trade_audit.get("latest_record") if isinstance(trade_audit, dict) else {}
    latest_decision = trade_audit.get("latest_decision") if isinstance(trade_audit, dict) else {}

    candidates = [latest_execution, latest_decision, latest_record]
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate:
            continue
        action = str(candidate.get("action") or "").strip().lower()
        if action in IGNORED_AUDIT_ACTIONS:
            continue
        ticker = candidate.get("ticker") or candidate.get("symbol") or ""
        phase = candidate.get("phase") or action or "record"
        order = candidate.get("order") if isinstance(candidate.get("order"), dict) else {}
        side = order.get("side") or candidate.get("trade_signal", {}).get("action") or ""
        qty = order.get("qty") or ""
        status = ""
        response = candidate.get("response")
        if isinstance(response, dict):
            status = response.get("status") or response.get("body", {}).get("status") or response.get("ok")
        line = " ".join(str(part) for part in [phase, ticker, side, qty, status] if part not in ("", None, False))
        if line:
            return line
    return None


def _nearest_trigger(cards: list[dict], side: str) -> tuple[str, str]:
    best_name = "暂无"
    if side == "buy":
        best_hint = "暂无接近买点的标的"
    else:
        best_hint = "暂无接近卖点的标的"
    best_distance = None
    for card in cards:
        if not card.get("online"):
            continue
        pos_pct = float(card.get("pos_pct", 50) or 50)
        if side == "buy":
            distance = pos_pct
            hint = f"{card['name']} 接近买点"
        else:
            distance = abs(100 - pos_pct)
            hint = f"{card['name']} 接近卖点"
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_name = card["name"]
            best_hint = hint
    return best_name, best_hint

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI区间交易总览</title>
<style>
    :root{
        --bg:#05070d;
        --bg2:#0a1020;
        --panel:rgba(11,17,31,.82);
        --panel-strong:rgba(15,22,40,.94);
        --line:rgba(255,255,255,.08);
        --text:#e6edf8;
        --muted:#8c97ab;
        --accent:#86efac;
        --accent2:#7dd3fc;
        --warn:#fbbf24;
        --down:#fb7185;
        --up:#34d399;
        --shadow:0 24px 80px rgba(0,0,0,.45);
    }
    *{margin:0;padding:0;box-sizing:border-box}
    body{
        min-height:100vh;
        color:var(--text);
        font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
        background:
            radial-gradient(circle at top left, rgba(125,211,252,.14), transparent 28%),
            radial-gradient(circle at top right, rgba(52,211,153,.12), transparent 24%),
            linear-gradient(180deg, #04060b 0%, #060913 44%, #05070d 100%);
        padding:24px;
    }
    .page{max-width:1440px;margin:0 auto}
    .topbar{
        display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
        margin-bottom:20px;padding:20px 22px;border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(16,24,44,.92), rgba(9,13,24,.86));
        border-radius:18px;box-shadow:var(--shadow);backdrop-filter:blur(14px);
    }
    .brand h1{font-size:28px;line-height:1.1;letter-spacing:.01em;font-weight:700}
    .brand p{margin-top:8px;color:var(--muted);font-size:13px}
    .status-row{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}
    .pill{
        display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border-radius:999px;
        background:rgba(255,255,255,.04);border:1px solid var(--line);color:var(--text);
        font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase
    }
    .pill.live{background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.22);color:#b8f5d0}
    .pill.warn{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.24);color:#fde68a}
    .summary{
        display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px;
    }
    .runtime-strip{
        display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px;
    }
    .runtime-item{
        padding:14px 16px;border-radius:16px;background:rgba(255,255,255,.035);border:1px solid var(--line)
    }
    .runtime-label{display:block;color:var(--muted);font-size:11px;letter-spacing:.09em;text-transform:uppercase}
    .runtime-value{display:block;margin-top:8px;font-size:15px;font-weight:700;color:#fff;line-height:1.35}
    .runtime-value.live{color:#b8f5d0}
    .runtime-value.warn{color:#fde68a}
    .metric,.section,.card{
        background:var(--panel);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .metric{padding:16px 18px}
    .metric span{display:block}
    .metric-label{color:var(--muted);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
    .metric-value{margin-top:8px;font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
    .metric-value.small{font-size:19px}
    .sections{
        display:grid;grid-template-columns:1.2fr .8fr;gap:12px;margin-bottom:18px;
    }
    .section{padding:18px}
    .section h2{font-size:15px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#dbe7ff;margin-bottom:12px}
    .account-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
    .audit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
    .selector-table{display:grid;gap:10px}
    .selector-head,.selector-row{
        display:grid;
        grid-template-columns:minmax(56px,.6fr) minmax(78px,.9fr) minmax(74px,.8fr) minmax(74px,.8fr) minmax(74px,.8fr) minmax(90px,1fr) minmax(90px,1fr) minmax(90px,1fr) minmax(90px,1fr) minmax(140px,1.2fr);
        gap:8px;
        align-items:center;
    }
    .selector-head{
        color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
        padding:0 2px 4px;
    }
    .selector-row{
        padding:12px 14px;border-radius:14px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06);
        font-size:13px;
    }
    .selector-row .ticker{font-weight:800;color:#fff}
    .selector-row .num{font-weight:700;font-variant-numeric:tabular-nums}
    .selector-row .sector{color:var(--muted)}
    .selector-empty{
        padding:16px;border-radius:14px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06);
        color:var(--muted);font-size:13px;
    }
    .section-meta{margin-bottom:12px;color:var(--muted);font-size:12px}
    .settings-form{
        display:flex;gap:10px;align-items:end;flex-wrap:wrap;
        margin-bottom:14px;padding:14px;border-radius:14px;
        background:var(--panel-strong);border:1px solid rgba(255,255,255,.06)
    }
    .settings-field{display:flex;flex-direction:column;gap:6px;min-width:180px}
    .settings-field label{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .settings-field input{
        border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:#fff;
        border-radius:10px;padding:10px 12px;font-size:14px;font-weight:600
    }
    .settings-button{
        border:1px solid rgba(52,211,153,.24);background:rgba(52,211,153,.12);color:#b8f5d0;
        border-radius:10px;padding:10px 14px;font-size:13px;font-weight:800;cursor:pointer
    }
    .settings-button.secondary{
        border-color:rgba(125,211,252,.24);background:rgba(125,211,252,.12);color:#d7f0ff
    }
    .settings-note{color:var(--muted);font-size:12px}
    .stat-box{
        padding:14px;border-radius:14px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06)
    }
    .stat-label{display:block;color:var(--muted);font-size:11px;letter-spacing:.09em;text-transform:uppercase}
    .stat-value{
        margin-top:8px;display:block;color:#fff;font-size:17px;font-weight:700;line-height:1.25;
        font-variant-numeric:tabular-nums;word-break:break-word
    }
    .stat-value.muted{color:var(--muted);font-weight:500}
    .cards{
        display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px;align-items:stretch
    }
    .card{padding:18px;min-width:0}
    .card-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:14px}
    .card-title{min-width:0}
    .card-title .ticker{
        display:block;font-size:20px;font-weight:800;letter-spacing:.02em;line-height:1.1
    }
    .card-title .desc{display:block;margin-top:6px;color:var(--muted);font-size:12px;line-height:1.35}
    .badges{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
    .badge{
        display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;font-size:11px;font-weight:700;
        letter-spacing:.08em;text-transform:uppercase;border:1px solid transparent
    }
    .badge.live{background:rgba(52,211,153,.1);color:#b8f5d0;border-color:rgba(52,211,153,.2)}
    .badge.offline{background:rgba(148,163,184,.1);color:#cbd5e1;border-color:rgba(148,163,184,.16)}
    .badge.halted{background:rgba(251,191,36,.1);color:#fde68a;border-color:rgba(251,191,36,.22)}
    .price-row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:8px 0 14px}
    .price{font-size:34px;line-height:1;font-weight:800;font-variant-numeric:tabular-nums}
    .change{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums}
    .green{color:var(--up)} .red{color:var(--down)} .yellow{color:var(--warn)}
    .grid-quote{
        display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:14px
    }
    .quote-item{
        padding:11px 12px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
    }
    .quote-item .label{display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .quote-item .val{
        display:block;margin-top:7px;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums
    }
    .sparkline{
        display:flex;align-items:flex-end;height:54px;gap:2px;padding:10px;border-radius:14px;
        background:linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
        border:1px solid rgba(255,255,255,.06);margin-bottom:14px
    }
    .spark-bar{flex:1;min-width:2px;border-radius:999px;opacity:.95}
    .range-block{margin-bottom:14px}
    .row{display:flex;justify-content:space-between;gap:12px;align-items:center;font-size:13px}
    .row .label{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:11px}
    .row .val{font-weight:700;font-variant-numeric:tabular-nums}
    .range-bar{
        margin-top:8px;height:8px;border-radius:999px;background:rgba(255,255,255,.07);overflow:hidden
    }
    .range-fill{height:100%;border-radius:999px;transition:width .45s ease}
    .signal{
        display:flex;align-items:center;justify-content:center;min-height:44px;margin-bottom:14px;border-radius:14px;
        border:1px solid transparent;font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase
    }
    .sig-buy{background:rgba(52,211,153,.1);color:#b8f5d0;border-color:rgba(52,211,153,.22)}
    .sig-sell{background:rgba(251,113,133,.1);color:#fecdd3;border-color:rgba(251,113,133,.24)}
    .sig-hold{background:rgba(148,163,184,.08);color:#d1d5db;border-color:rgba(148,163,184,.16)}
    .sig-block{background:rgba(251,191,36,.1);color:#fde68a;border-color:rgba(251,191,36,.22)}
    .pnl-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
    .footer{
        margin-top:18px;padding:18px 20px;border-radius:18px;border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(15,22,40,.96), rgba(9,13,24,.92));text-align:center
    }
    .footer h2{font-size:14px;letter-spacing:.08em;text-transform:uppercase;color:#dbe7ff;margin-bottom:10px}
    .footer .total{font-size:34px;font-weight:800;font-variant-numeric:tabular-nums}
    .footer .meta{margin-top:10px;color:var(--muted);font-size:12px}
    .refresh{text-align:right;color:var(--muted);font-size:11px;margin-top:10px}
    .offline{opacity:.72}
    @media (max-width:1180px){
        .summary,.sections,.cards{grid-template-columns:1fr}
        .runtime-strip{grid-template-columns:repeat(2,minmax(0,1fr))}
        .account-grid,.audit-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
        .selector-head,.selector-row{grid-template-columns:repeat(5,minmax(0,1fr))}
    }
    @media (max-width:760px){
        body{padding:14px}
        .topbar{flex-direction:column}
        .account-grid,.audit-grid,.cards,.grid-quote,.pnl-grid,.summary,.runtime-strip{grid-template-columns:1fr}
        .price{font-size:30px}
        .selector-head{display:none}
        .selector-row{grid-template-columns:repeat(2,minmax(0,1fr))}
    }
</style>
</head>
<body>
<div class="page">
    <div class="topbar">
        <div class="brand">
            <h1>AI区间交易总览</h1>
            <p>TOP1 到 TOP5 五路联动监控，每 5 秒自动刷新。</p>
        </div>
        <div class="status-row">
            <span class="pill live">实时监控</span>
            <span class="pill">更新于 {{ update_time }}</span>
            <span class="pill {% if live_account and live_account.mode == 'live' %}live{% else %}warn{% endif %}">
                {% if live_account and live_account.mode == 'live' %}实盘账户{% else %}模拟盘 / 离线{% endif %}
            </span>
        </div>
    </div>

    <div class="summary">
        <div class="metric">
            <span class="metric-label">可用现金</span>
            <span class="metric-value">{% if live_account and live_account.cash is not none %}${{ "%.2f"|format(live_account.cash) }}{% else %}暂无{% endif %}</span>
        </div>
        <div class="metric">
            <span class="metric-label">账户权益</span>
            <span class="metric-value">{% if live_account and live_account.equity is not none %}${{ "%.2f"|format(live_account.equity) }}{% else %}暂无{% endif %}</span>
        </div>
        <div class="metric">
            <span class="metric-label">购买力</span>
            <span class="metric-value">{% if live_account and live_account.buying_power is not none %}${{ "%.2f"|format(live_account.buying_power) }}{% else %}暂无{% endif %}</span>
        </div>
        <div class="metric">
            <span class="metric-label">持仓数量</span>
            <span class="metric-value small">{% if live_account and live_account.positions_count is not none %}{{ live_account.positions_count }}{% else %}暂无{% endif %}</span>
        </div>
    </div>

    <div class="runtime-strip">
        <div class="runtime-item">
            <span class="runtime-label">当前模式</span>
            <span class="runtime-value {% if trade_audit.execution_mode == 'live' %}live{% else %}warn{% endif %}">{{ trade_audit.execution_mode|upper }}</span>
        </div>
        <div class="runtime-item">
            <span class="runtime-label">当前标的</span>
            <span class="runtime-value">{{ active_symbols }}</span>
        </div>
        <div class="runtime-item">
            <span class="runtime-label">新开仓</span>
            <span class="runtime-value {% if trade_audit.new_entries_allowed %}live{% else %}warn{% endif %}">{% if trade_audit.new_entries_allowed %}允许{% else %}暂停{% endif %}</span>
        </div>
        <div class="runtime-item">
            <span class="runtime-label">最近更新</span>
            <span class="runtime-value">{{ update_time }}</span>
        </div>
        <div class="runtime-item">
            <span class="runtime-label">最近触发买点</span>
            <span class="runtime-value live">{{ nearest_buy_trigger }}</span>
        </div>
        <div class="runtime-item">
            <span class="runtime-label">最近触发卖点</span>
            <span class="runtime-value warn">{{ nearest_sell_trigger }}</span>
        </div>
    </div>

    <div class="sections">
        <div class="section">
            <h2>风控与交易审计</h2>
            <div class="audit-grid">
                <div class="stat-box"><span class="stat-label">执行模式</span><span class="stat-value">{% if trade_audit and trade_audit.execution_mode %}{{ trade_audit.execution_mode|upper }}{% else %}PAPER{% endif %}</span></div>
                <div class="stat-box"><span class="stat-label">仅减仓</span><span class="stat-value">{% if trade_audit and trade_audit.reduce_only %}是{% else %}否{% endif %}</span></div>
                <div class="stat-box"><span class="stat-label">新开仓</span><span class="stat-value">{% if trade_audit and trade_audit.new_entries_allowed %}允许{% else %}暂停{% endif %}</span></div>
                <div class="stat-box"><span class="stat-label">暂停原因</span><span class="stat-value {% if not trade_audit or not trade_audit.risk_pause_reason %}muted{% endif %}">{% if trade_audit and trade_audit.risk_pause_reason %}{{ trade_audit.risk_pause_reason }}{% else %}无{% endif %}</span></div>
            </div>
        </div>
        <div class="section">
            <h2>今日统计</h2>
            <div class="account-grid">
                <div class="stat-box"><span class="stat-label">成交次数</span><span class="stat-value">{% if trade_audit %}{{ trade_audit.execution_count }}{% else %}0{% endif %}</span></div>
                <div class="stat-box"><span class="stat-label">决策次数</span><span class="stat-value">{% if trade_audit %}{{ trade_audit.decision_count }}{% else %}0{% endif %}</span></div>
                <div class="stat-box"><span class="stat-label">成交股数</span><span class="stat-value">{% if trade_audit %}{{ trade_audit.order_qty }}{% else %}0{% endif %}</span></div>
                <div class="stat-box"><span class="stat-label">最近交易</span><span class="stat-value {% if not trade_audit or not trade_audit.latest_line %}muted{% endif %}">{% if trade_audit and trade_audit.latest_line %}{{ trade_audit.latest_line }}{% else %}暂无{% endif %}</span></div>
            </div>
        </div>
    </div>

    <div class="section" style="margin-bottom:18px">
        <h2>AI 区间选股</h2>
        <div class="section-meta">
            {% if ai_selection and ai_selection.timestamp %}
                最新选股时间：{{ ai_selection.timestamp }}
                {% if ai_selection.settings %}
                    · 价格上限：${{ "%.2f"|format(ai_selection.settings.max_price or 0) }}
                    · 扫描数量：{{ ai_selection.settings.max_symbols or 0 }}
                    · 数据模式：{{ ai_selection.settings.data_mode or 'unknown' }}
                {% endif %}
            {% else %}
                暂无 AI 选股报告。
            {% endif %}
        </div>
        <form class="settings-form" method="post" action="/ai-selector-settings">
            <div class="settings-field">
                <label for="max_price">价格上限</label>
                <input id="max_price" name="max_price" type="number" min="1" max="500" step="0.01" value="{{ runtime_settings.max_price }}">
            </div>
            <button class="settings-button" type="submit" name="action" value="save">保存设置</button>
            <button class="settings-button secondary" type="submit" name="action" value="rerun">保存并立即重选</button>
            <span class="settings-note">保存后对下一次自动选股生效。</span>
        </form>
        {% if ai_selection and ai_selection.report %}
        <div class="selector-table">
            <div class="selector-head">
                <span>排名</span>
                <span>标的</span>
                <span>总分</span>
                <span>波动</span>
                <span>流动性</span>
                <span>趋势适配</span>
                <span>区间重复</span>
                <span>回撤安全</span>
                <span>相关性扣分</span>
                <span>建议区间</span>
            </div>
            {% for row in ai_selection.report %}
            <div class="selector-row">
                <span class="num">{{ row.rank }}</span>
                <span class="ticker">{{ row.ticker }} <span class="sector">{{ row.sector }}</span></span>
                <span class="num">{{ "%.2f"|format(row.score) }}</span>
                <span class="num">{{ "%.2f"|format(row.volatility) }}</span>
                <span class="num">{{ "%.2f"|format(row.volume) }}</span>
                <span class="num">{{ "%.2f"|format(row.trend_fit) }}</span>
                <span class="num">{{ "%.2f"|format(row.repeatability) }}</span>
                <span class="num">{{ "%.2f"|format(row.drawdown) }}</span>
                <span class="num">{{ "%.2f"|format(row.correlation_penalty) }}</span>
                <span class="num">{{ row.suggested_range }}</span>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="selector-empty">先运行一次 `scripts/run_ai_selector.py`，这里就会显示最新的 AI 区间选股结果。</div>
        {% endif %}
    </div>

    <div class="cards">
    {% for card in cards %}
        <div class="card {% if not card.online %}offline{% endif %}">
            <div class="card-head">
                <div class="card-title">
                    <span class="ticker">{{ card.name }}</span>
                    <span class="desc">{{ card.desc }}</span>
                </div>
                <div class="badges">
                    {% if card.halted %}<span class="badge halted">已暂停</span>{% endif %}
                    {% if card.online %}<span class="badge live">在线</span>{% else %}<span class="badge offline">离线</span>{% endif %}
                </div>
            </div>

            <div class="price-row">
                <span class="price {{ 'green' if card.price_change >= 0 else 'red' }}">${{ "%.2f"|format(card.price) }}</span>
                <span class="change {{ 'green' if card.price_change >= 0 else 'red' }}">
                    {{ '+' if card.price_change >= 0 else '' }}{{ "%.2f"|format(card.price_change) }}%
                </span>
            </div>

            <div class="grid-quote">
                <div class="quote-item"><span class="label">日内高点</span><span class="val green">${{ "%.2f"|format(card.day_high) }}</span></div>
                <div class="quote-item"><span class="label">日内低点</span><span class="val red">${{ "%.2f"|format(card.day_low) }}</span></div>
                <div class="quote-item"><span class="label">买一</span><span class="val">${{ "%.2f"|format(card.bid) }}</span></div>
                <div class="quote-item"><span class="label">卖一</span><span class="val">${{ "%.2f"|format(card.ask) }}</span></div>
            </div>

            <div class="sparkline">
                {% for bar in card.sparkline %}
                <div class="spark-bar" style="height:{{ bar.height }}%;background:{{ bar.color }}"></div>
                {% endfor %}
            </div>

            <div class="range-block">
                <div class="row"><span class="label">区间</span><span class="val">${{ "%.2f"|format(card.support) }} - ${{ "%.2f"|format(card.resistance) }} ({{ "%.1f"|format(card.spread_pct) }}%)</span></div>
                <div class="range-bar">
                    <div class="range-fill" style="width:{{ card.pos_pct }}%;background:{% if card.pos_pct > 70 %}#fb7185{% elif card.pos_pct < 30 %}#34d399{% else %}#fbbf24{% endif %}"></div>
                </div>
                <div class="row" style="margin-top:8px"><span class="label">区间位置</span><span class="val">{{ "%.0f"|format(card.pos_pct) }}%</span></div>
            </div>

            <div class="signal {% if card.signal == 'BUY' %}sig-buy{% elif card.signal == 'SELL' %}sig-sell{% elif 'TREND' in card.signal %}sig-block{% else %}sig-hold{% endif %}">
                {{ card.signal_cn }}
            </div>

            <div class="pnl-grid">
                <div class="quote-item"><span class="label">分配本金</span><span class="val">${{ "%.2f"|format(card.initial_capital) }}</span></div>
                <div class="quote-item"><span class="label">现金</span><span class="val">${{ "%.2f"|format(card.cash) }}</span></div>
                <div class="quote-item"><span class="label">持股</span><span class="val">{{ card.shares }}</span></div>
                <div class="quote-item"><span class="label">权益</span><span class="val">${{ "%.2f"|format(card.equity) }}</span></div>
                <div class="quote-item"><span class="label">当日盈亏</span><span class="val {{ 'green' if card.pnl >= 0 else 'red' }}">${{ "%+.2f"|format(card.pnl) }}</span></div>
                <div class="quote-item"><span class="label">成交笔数</span><span class="val">{{ card.trades }}</span></div>
            </div>
        </div>
    {% endfor %}
    </div>

    <div class="footer">
        <h2>组合盈亏</h2>
        <div class="total {{ 'green' if total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(total_pnl) }}</div>
        <div class="meta">总本金：${{ "%.2f"|format(total_capital) }} · 总权益：${{ "%.2f"|format(total_equity) }} · 总成交：{{ total_trades }}</div>
    </div>

    <div class="refresh">每 5 秒自动刷新 · {{ update_time }}</div>
</div>
</body>
</html>"""


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


def _signal_cn(signal: str) -> str:
    normalized = str(signal or "").strip().upper()
    mapping = {
        "BUY": "买入",
        "SELL": "卖出",
        "HOLD": "观察",
        "OFFLINE": "离线",
    }
    if "TREND" in normalized:
        return "趋势过滤"
    return mapping.get(normalized, normalized or "暂无")


@app.route("/")
def index():
    cards = []
    total_pnl = 0.0
    total_capital = 0.0
    total_equity = 0.0
    total_trades = 0
    runtime_settings = load_runtime_settings()
    live_account = _fetch_live_account_summary()
    ai_selection = _load_ai_selection_report()
    if not isinstance(ai_selection, dict):
        ai_selection = {"timestamp": None, "report": [], "top5": [], "top3": [], "top10": [], "settings": {}}
    trade_audit = summarize_trade_log(PROJECT_DIR / "logs", mode=_desired_audit_mode())
    latest_line = _latest_trade_line(trade_audit)
    trade_audit = {
        **trade_audit,
        "latest_line": latest_line,
        "execution_mode": _resolve_dashboard_execution_mode(trade_audit),
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
                "signal_cn": _signal_cn(d.get("last_signal", "N/A")),
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
                "signal": "OFFLINE", "signal_cn": _signal_cn("OFFLINE"), "shares": 0,
                "initial_capital": initial_capital, "cash": initial_capital,
                "pnl": 0, "equity": initial_capital, "trades": 0, "halted": False,
            })
            total_capital += initial_capital
            total_equity += initial_capital

    if live_account and live_account.get("mode") == "live":
        total_capital = float(live_account.get("cash") or 0.0)
        total_equity = float(live_account.get("equity") or 0.0)

    active_symbols = " / ".join(
        card["name"].split("·", 1)[-1].strip() if "·" in card["name"] else str(card["name"]).strip()
        for card in cards
    ) or "N/A"
    nearest_buy_trigger_name, nearest_buy_trigger = _nearest_trigger(cards, "buy")
    nearest_sell_trigger_name, nearest_sell_trigger = _nearest_trigger(cards, "sell")

    return render_template_string(HTML,
        cards=cards,
        live_account=live_account,
        ai_selection=ai_selection,
        runtime_settings={
            "max_price": float(runtime_settings.get("max_price", ai_selection.get("settings", {}).get("max_price", 50.0)) or 50.0),
        },
        active_symbols=active_symbols,
        nearest_buy_trigger_name=nearest_buy_trigger_name,
        nearest_buy_trigger=nearest_buy_trigger,
        nearest_sell_trigger_name=nearest_sell_trigger_name,
        nearest_sell_trigger=nearest_sell_trigger,
        trade_audit=trade_audit,
        total_pnl=round(total_pnl, 2),
        total_capital=round(total_capital, 2),
        total_equity=round(total_equity, 2),
        total_trades=total_trades,
        update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _run_ai_selector_now() -> None:
    project_dir = str(PROJECT_DIR)
    env = os.environ.copy()
    env.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    env.setdefault("AI_SELECTOR_MAX_SYMBOLS", "50")
    python_bin = PROJECT_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(os.environ.get("PYTHON", "")) if os.environ.get("PYTHON") else Path("python3")

    subprocess.run(
        [str(python_bin), "scripts/run_ai_selector.py"],
        cwd=project_dir,
        env=env,
        check=False,
    )
    subprocess.run(
        ["/bin/bash", str(PROJECT_DIR / "multi_launch.sh"), "restart-top"],
        cwd=project_dir,
        env=env,
        check=False,
    )


@app.route("/ai-selector-settings", methods=["POST"])
def update_ai_selector_settings():
    raw_max_price = str(request.form.get("max_price", "")).strip()
    action = str(request.form.get("action", "save")).strip().lower()
    settings = load_runtime_settings()
    try:
        max_price = float(raw_max_price)
    except (TypeError, ValueError):
        max_price = float(settings.get("max_price", 50.0) or 50.0)
    max_price = min(500.0, max(1.0, max_price))
    settings["max_price"] = round(max_price, 2)
    save_runtime_settings(settings)
    if action == "rerun":
        _run_ai_selector_now()
    return redirect("/")


def start_combined(port=8090):
    """Start combined dashboard in background thread."""
    from threading import Thread
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False), daemon=True)
    t.start()
    return t
