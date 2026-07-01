"""Combined dashboard aggregating the selected TOP5 trading engines."""
import json, os, subprocess, threading, urllib.request
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, redirect, render_template_string, request
import yaml

from src.ai_selector.settings import load_runtime_settings, save_runtime_settings
from src.reports.trade_audit import summarize_trade_log

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
TRADING_FLAGS_PATH = PROJECT_DIR / "state" / "trading_flags.json"

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
_LIVE_ACCOUNT_CACHE_TTL = float(os.getenv("SOXS_LIVE_ACCOUNT_CACHE_TTL", "15"))
_LIVE_ACCOUNT_LOCK = threading.Lock()
_STATUS_CACHE: dict[int, dict] = {}
_STATUS_FAILURES: dict[int, int] = {}
_STATUS_OFFLINE_THRESHOLD = 3


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
    with _LIVE_ACCOUNT_LOCK:
        if _LIVE_ACCOUNT_CACHE and (now - _LIVE_ACCOUNT_CACHE_AT) < _LIVE_ACCOUNT_CACHE_TTL:
            return _LIVE_ACCOUNT_CACHE
        if not _has_live_account_env():
            return None
        return _refresh_live_account_summary(now)


def _refresh_live_account_summary(now: float):
    """Refresh the account once while the caller holds the process lock."""
    global _LIVE_ACCOUNT_CACHE, _LIVE_ACCOUNT_CACHE_AT
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
            return _LIVE_ACCOUNT_CACHE
        positions = broker.get_positions()
        account = broker.get_account()
        position_rows = []
        for pos in positions or []:
            position_rows.append(
                {
                    "ticker": str(getattr(pos, "ticker", "") or ""),
                    "quantity": int(getattr(pos, "quantity", 0) or 0),
                    "avg_entry_price": float(getattr(pos, "avg_entry_price", 0.0) or 0.0),
                    "current_price": float(getattr(pos, "current_price", 0.0) or 0.0),
                    "market_value": float(getattr(pos, "market_value", 0.0) or 0.0),
                    "unrealized_pnl": float(getattr(pos, "unrealized_pnl", 0.0) or 0.0),
                    "unrealized_pnl_pct": float(getattr(pos, "unrealized_pnl_pct", 0.0) or 0.0),
                }
            )
        summary = {
            "cash": float(getattr(account, "cash", 0.0) or 0.0),
            "equity": float(getattr(account, "equity", 0.0) or 0.0),
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0),
            "positions_count": len(positions or []),
            "positions": position_rows,
            "mode": "live",
        }
    except Exception:
        return _LIVE_ACCOUNT_CACHE
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


def _position_lookup(live_account: dict | None) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    if not isinstance(live_account, dict):
        return lookup
    for pos in live_account.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "").strip().upper()
        if ticker:
            lookup[ticker] = pos
    return lookup


def _filter_live_positions(live_account: dict | None, allowed_tickers: set[str]) -> dict | None:
    if not isinstance(live_account, dict):
        return live_account
    if not allowed_tickers:
        return live_account
    filtered_positions = []
    for pos in live_account.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "").strip().upper()
        if ticker and ticker in allowed_tickers:
            filtered_positions.append(pos)
    return {
        **live_account,
        "positions": filtered_positions,
        "positions_count": len(filtered_positions),
    }


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


def _nearest_trigger(
    cards: list[dict],
    side: str,
    *,
    new_entries_allowed: bool = True,
) -> tuple[str, str]:
    if side == "buy" and not new_entries_allowed:
        return "暂停开仓", "当前已暂停新开仓"

    best_name = "暂无"
    best_hint = "暂无接近买点的标的" if side == "buy" else "暂无接近卖点的标的"
    best_rank = None
    buy_blocked_by_position = False

    for card in cards:
        if not card.get("online") or not card.get("range_ready") or card.get("halted"):
            continue
        if side == "buy" and card.get("reduce_only"):
            continue

        price = float(card.get("price", 0) or 0.0)
        support = float(card.get("support", 0) or 0.0)
        resistance = float(card.get("resistance", 0) or 0.0)
        shares = int(card.get("shares", 0) or 0)

        if price <= 0 or support <= 0 or resistance <= support:
            continue

        if side == "buy":
            if shares > 0:
                buy_blocked_by_position = True
                continue
            distance = abs(price - support)
            distance_pct = (distance / support * 100.0) if support > 0 else 0.0
            rank = (distance, distance_pct)
            hint = f"{card['name']} 距买点 ${distance:.2f} ({distance_pct:.1f}%)"
        else:
            distance = abs(resistance - price)
            distance_pct = (distance / resistance * 100.0) if resistance > 0 else 0.0
            rank = (0 if shares > 0 else 1, distance, distance_pct)
            hint = f"{card['name']} 距卖点 ${distance:.2f} ({distance_pct:.1f}%)"

        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_name = card["name"]
            best_hint = hint

    if side == "buy" and best_rank is None and buy_blocked_by_position:
        return "暂无可买", "当前标的都有持仓，暂无新的买点提示"

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
        padding:16px 16px 28px;
        overflow-x:hidden;
        overflow-y:auto;
    }
    .page{
        max-width:1680px;
        margin:0 auto;
        min-height:calc(100vh - 44px);
        display:flex;
        flex-direction:column;
        gap:18px;
    }
    .topbar{
        display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
        padding:16px 18px;border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(16,24,44,.92), rgba(9,13,24,.86));
        border-radius:18px;box-shadow:var(--shadow);backdrop-filter:blur(14px);
        position:sticky;top:10px;z-index:5;
    }
    .brand{display:flex;flex-direction:column;gap:8px}
    .brand h1{font-size:24px;line-height:1.1;letter-spacing:.01em;font-weight:700}
    .brand p{color:var(--muted);font-size:12px}
    .headline-stats{
        display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;min-width:1040px
    }
    .headline-stat{
        padding:10px 12px;border-radius:14px;background:rgba(255,255,255,.04);
        border:1px solid rgba(255,255,255,.07)
    }
    .headline-stat .label{
        display:block;color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase
    }
    .headline-stat .value{
        display:block;margin-top:6px;font-size:16px;font-weight:800;font-variant-numeric:tabular-nums
    }
    .headline-stat .sub{
        display:block;margin-top:4px;color:var(--muted);font-size:11px;line-height:1.3
    }
    .status-row{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
    .pill{
        display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;
        background:rgba(255,255,255,.04);border:1px solid var(--line);color:var(--text);
        font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase
    }
    .pill.live{background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.22);color:#b8f5d0}
    .pill.warn{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.24);color:#fde68a}
    .overview-layout{
        display:grid;grid-template-columns:1fr;gap:14px;
    }
    .control-grid{
        display:grid;
        grid-template-columns:minmax(0,1.08fr) minmax(0,1fr);
        gap:14px;
        align-items:start
    }
    .two-column{
        display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px;
    }
    .overview-panel{
        padding:18px;border-radius:20px;border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(15,22,40,.92), rgba(9,13,24,.88));
        box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .overview-panel.compact{padding:16px}
    .panel-head{
        display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:10px
    }
    .overview-panel.compact .panel-head{margin-bottom:10px}
    .control-grid .overview-panel{
        min-height:100%
    }
    .panel-head h2{
        font-size:13px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:#dbe7ff
    }
    .panel-head .hint{color:var(--muted);font-size:12px}
    .summary{
        display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;
    }
    .account-strip{
        display:flex;flex-direction:column;gap:10px
    }
    .account-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
    .metric,.section,.card{
        background:var(--panel);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .metric{padding:12px 14px}
    .metric span{display:block}
    .metric-label{color:var(--muted);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
    .metric-value{margin-top:6px;font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
    .metric-value.small{font-size:17px}
    .position-list{
        display:grid;gap:8px;max-height:520px;overflow:auto;padding-right:4px
    }
    .position-item{
        display:grid;grid-template-columns:minmax(68px,.8fr) minmax(78px,.8fr) minmax(80px,.8fr) minmax(80px,.8fr) minmax(92px,.95fr) minmax(92px,.95fr);
        gap:10px;align-items:center;padding:10px 12px;border-radius:14px;background:rgba(255,255,255,.035);
        border:1px solid var(--line)
    }
    .position-ticker{font-size:13px;font-weight:800;letter-spacing:.02em}
    .position-cell .label{
        display:block;color:var(--muted);font-size:9px;letter-spacing:.08em;text-transform:uppercase
    }
    .position-cell .val{
        display:block;margin-top:4px;color:#fff;font-size:12px;font-weight:700;font-variant-numeric:tabular-nums
    }
    .position-empty{
        padding:12px 14px;border-radius:14px;background:rgba(255,255,255,.03);
        border:1px solid rgba(255,255,255,.06);color:var(--muted);font-size:12px
    }
    .account-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
    .selector-table{display:grid;gap:6px;max-height:520px;overflow:auto;padding-right:4px}
    .selector-head,.selector-row{
        display:grid;
        grid-template-columns:minmax(40px,.4fr) minmax(72px,.8fr) minmax(58px,.55fr) minmax(64px,.6fr) minmax(90px,.95fr);
        gap:6px;
        align-items:center;
    }
    .selector-head{
        color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase;
        padding:0 2px 2px;
    }
    .selector-row{
        padding:9px 10px;border-radius:12px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06);
        font-size:11px;
    }
    .selector-row .ticker{font-weight:800;color:#fff}
    .selector-row .num{font-weight:700;font-variant-numeric:tabular-nums}
    .selector-row .sector{color:var(--muted)}
    .selector-empty{
        padding:16px;border-radius:14px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06);
        color:var(--muted);font-size:13px;
    }
    .compact .selector-table{gap:5px}
    .compact .selector-row{padding:7px 8px;font-size:11px}
    .compact .selector-head{font-size:10px}
    .compact .settings-form{
        padding:8px 10px;margin-bottom:10px
    }
    .compact .settings-field{
        min-width:130px
    }
    .compact .section-meta{
        font-size:11px
    }
    .compact .selector-head,.compact .selector-row{
        grid-template-columns:minmax(40px,.4fr) minmax(72px,.8fr) minmax(58px,.55fr) minmax(64px,.6fr) minmax(90px,.95fr);
    }
    .section-meta{margin-bottom:8px;color:var(--muted);font-size:11px;line-height:1.35}
    .settings-form{
        display:flex;gap:8px;align-items:end;flex-wrap:wrap;
        margin-bottom:10px;padding:8px 10px;border-radius:12px;
        background:var(--panel-strong);border:1px solid rgba(255,255,255,.06)
    }
    .settings-field{display:flex;flex-direction:column;gap:4px;min-width:125px}
    .settings-field label{color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
    .settings-field input{
        border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:#fff;
        border-radius:10px;padding:7px 9px;font-size:13px;font-weight:600
    }
    .settings-button{
        border:1px solid rgba(52,211,153,.24);background:rgba(52,211,153,.12);color:#b8f5d0;
        border-radius:10px;padding:7px 10px;font-size:12px;font-weight:800;cursor:pointer
    }
    .settings-button.secondary{
        border-color:rgba(125,211,252,.24);background:rgba(125,211,252,.12);color:#d7f0ff
    }
    .settings-note{color:var(--muted);font-size:11px;margin-left:auto}
    .stat-box{
        padding:14px;border-radius:14px;background:var(--panel-strong);border:1px solid rgba(255,255,255,.06)
    }
    .stat-label{display:block;color:var(--muted);font-size:11px;letter-spacing:.09em;text-transform:uppercase}
    .stat-value{
        margin-top:8px;display:block;color:#fff;font-size:17px;font-weight:700;line-height:1.25;
        font-variant-numeric:tabular-nums;word-break:break-word
    }
    .stat-value.muted{color:var(--muted);font-weight:500}
    .cards-section{
        display:flex;
        flex-direction:column;
        gap:12px;
        padding:18px;
        border-radius:20px;
        border:1px solid var(--line);
        background:linear-gradient(180deg, rgba(15,22,40,.92), rgba(9,13,24,.88));
        box-shadow:var(--shadow);
        backdrop-filter:blur(14px)
    }
    .cards-section-head{
        display:flex;justify-content:space-between;gap:12px;align-items:flex-end;margin-bottom:8px
    }
    .cards-section-head h2{
        font-size:15px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#dbe7ff
    }
    .cards-section-head p{color:var(--muted);font-size:12px;line-height:1.35}
    .cards{
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:14px;
        align-items:stretch;
        overflow:visible;
    }
    .card{
        padding:16px;min-width:0;min-height:100%;
        background:
            linear-gradient(180deg, rgba(16,24,44,.96), rgba(9,13,24,.88)),
            radial-gradient(circle at top right, rgba(125,211,252,.06), transparent 34%);
        overflow:hidden;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);backdrop-filter:blur(14px)
    }
    .card.featured-buy{border-color:rgba(52,211,153,.28)}
    .card.featured-sell{border-color:rgba(251,113,133,.28)}
    .card.featured-dual{border-color:rgba(125,211,252,.32)}
    .card-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:10px}
    .card-title{min-width:0}
    .card-title .ticker{display:block;font-size:16px;font-weight:800;letter-spacing:.02em;line-height:1.1}
    .card-title .desc{display:block;margin-top:4px;color:var(--muted);font-size:11px;line-height:1.25}
    .card-spot{
        display:inline-flex;align-items:center;gap:6px;margin-top:6px;padding:4px 8px;border-radius:999px;
        font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase
    }
    .card-spot.buy{background:rgba(52,211,153,.12);color:#b8f5d0}
    .card-spot.sell{background:rgba(251,113,133,.12);color:#fecdd3}
    .card-spot.dual{background:rgba(125,211,252,.12);color:#d7f0ff}
    .badges{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
    .badge{
        display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;font-size:10px;font-weight:700;
        letter-spacing:.08em;text-transform:uppercase;border:1px solid transparent
    }
    .badge.live{background:rgba(52,211,153,.1);color:#b8f5d0;border-color:rgba(52,211,153,.2)}
    .badge.offline{background:rgba(148,163,184,.1);color:#cbd5e1;border-color:rgba(148,163,184,.16)}
    .badge.halted{background:rgba(251,191,36,.1);color:#fde68a;border-color:rgba(251,191,36,.22)}
    .green{color:var(--up)} .red{color:var(--down)} .yellow{color:var(--warn)}
    .price-row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:4px 0 10px}
    .price{font-size:24px;line-height:1;font-weight:800;font-variant-numeric:tabular-nums}
    .change{font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
    .quote-strip{
        display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px
    }
    .strip-box{
        padding:8px 9px;border-radius:10px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
    }
    .strip-box .label{display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .strip-box .val{display:block;margin-top:5px;font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}
    .range-block{margin-bottom:10px}
    .row{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:12px}
    .row .label{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:11px}
    .row .val{font-weight:700;font-variant-numeric:tabular-nums}
    .range-bar{
        margin-top:6px;height:6px;border-radius:999px;background:rgba(255,255,255,.07);overflow:hidden
    }
    .range-fill{height:100%;border-radius:999px;transition:width .45s ease}
    .signal{
        display:flex;align-items:center;justify-content:center;min-height:34px;margin-bottom:8px;border-radius:12px;
        border:1px solid transparent;font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase
    }
    .signal-note{
        margin-top:-1px;margin-bottom:10px;color:var(--muted);font-size:11px;line-height:1.3;
        min-height:30px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden
    }
    .pnl-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
    .grid-quote{
        display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px;margin-bottom:6px
    }
    .quote-item{
        padding:8px 9px;border-radius:10px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
    }
    .quote-item .label{display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .quote-item .val{
        display:block;margin-top:5px;font-size:12px;font-weight:700;font-variant-numeric:tabular-nums
    }
    .sparkline{display:none}
    .spark-bar{flex:1;min-width:2px;border-radius:999px;opacity:.95}
    .sig-buy{background:rgba(52,211,153,.1);color:#b8f5d0;border-color:rgba(52,211,153,.22)}
    .sig-sell{background:rgba(251,113,133,.1);color:#fecdd3;border-color:rgba(251,113,133,.24)}
    .sig-hold{background:rgba(148,163,184,.08);color:#d1d5db;border-color:rgba(148,163,184,.16)}
    .sig-block{background:rgba(251,191,36,.1);color:#fde68a;border-color:rgba(251,191,36,.22)}
    .refresh{text-align:right;color:var(--muted);font-size:11px}
    .offline{opacity:.72}
    @media (max-width:1180px){
        .headline-stats{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0}
        .overview-layout,.control-grid,.two-column,.cards{grid-template-columns:1fr}
        .account-grid,.audit-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
        .selector-head,.selector-row,.position-item{grid-template-columns:repeat(5,minmax(0,1fr))}
    }
    @media (max-width:760px){
        body{padding:14px;overflow:auto}
        .page{min-height:auto}
        .topbar{flex-direction:column;position:static}
        .headline-stats{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0}
        .account-grid,.audit-grid,.cards,.grid-quote,.pnl-grid,.summary,.overview-layout,.control-grid,.two-column{grid-template-columns:1fr}
        .settings-form{align-items:stretch}
        .settings-note{margin-left:0;width:100%}
        .price{font-size:30px}
        .selector-head{display:none}
        .selector-row,.position-item{grid-template-columns:repeat(2,minmax(0,1fr))}
        .position-list,.selector-table{max-height:none}
    }
</style>
</head>
<body>
<div class="page">
    <div class="topbar">
        <div class="brand">
            <h1>AI区间交易总览</h1>
            <p>TOP1 到 TOP5 五路联动监控，每 5 秒自动刷新。</p>
            <div class="headline-stats">
                <div class="headline-stat">
                    <span class="label">今日总收益</span>
                    <span class="value {{ 'green' if today_total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(today_total_pnl) }}</span>
                    <span class="sub">按 5 路策略今日盈亏汇总</span>
                </div>
                <div class="headline-stat">
                    <span class="label">账户浮盈亏</span>
                    <span class="value {{ 'green' if total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(total_pnl) }}</span>
                    <span class="sub">总成交 {{ total_trades }} 笔</span>
                </div>
                <div class="headline-stat">
                    <span class="label">{{ account_labels.footer_capital }}</span>
                    <span class="value">${{ "%.2f"|format(total_capital) }}</span>
                    <span class="sub">{{ account_labels.footer_equity }}：${{ "%.2f"|format(total_equity) }}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">最近买点</span>
                    <span class="value">{{ nearest_buy_trigger_name }}</span>
                    <span class="sub">{{ nearest_buy_trigger }}</span>
                </div>
                <div class="headline-stat">
                    <span class="label">最近卖点</span>
                    <span class="value">{{ nearest_sell_trigger_name }}</span>
                    <span class="sub">{{ nearest_sell_trigger }}</span>
                </div>
            </div>
        </div>
        <div class="status-row">
            <span class="pill live">实时监控</span>
            <span class="pill">更新于 {{ update_time }}</span>
            <span class="pill {% if live_account and live_account.mode == 'live' %}live{% else %}warn{% endif %}">
                {% if live_account and live_account.mode == 'live' %}实盘账户{% else %}模拟盘 / 离线{% endif %}
            </span>
            {% if footer_buying_power is not none %}
            <span class="pill live">{{ account_labels.footer_buying_power }} ${{ "%.2f"|format(footer_buying_power) }}</span>
            {% endif %}
        </div>
    </div>

    <div class="overview-layout">
        <div class="overview-panel">
            <div class="panel-head">
                <h2>控制台总览</h2>
                <span class="hint">第一屏看账户与选股，第二屏看全部标的卡片</span>
            </div>
            <div class="control-grid">
                <div class="overview-panel compact">
                    <div class="panel-head">
                        <h2>账户与持仓</h2>
                        <span class="hint">可用资金与真实仓位</span>
                    </div>
                    <div class="account-strip">
                        <div class="account-summary">
                            <div class="metric">
                                <span class="metric-label">账户现金</span>
                                <span class="metric-value">{% if live_account and live_account.cash is not none %}${{ "%.2f"|format(live_account.cash) }}{% else %}暂无{% endif %}</span>
                            </div>
                            <div class="metric">
                                <span class="metric-label">账户权益</span>
                                <span class="metric-value">{% if live_account and live_account.equity is not none %}${{ "%.2f"|format(live_account.equity) }}{% else %}暂无{% endif %}</span>
                            </div>
                            <div class="metric">
                                <span class="metric-label">可买额度</span>
                                <span class="metric-value">{% if live_account and live_account.buying_power is not none %}${{ "%.2f"|format(live_account.buying_power) }}{% else %}暂无{% endif %}</span>
                            </div>
                            <div class="metric">
                                <span class="metric-label">精选持仓数量</span>
                                <span class="metric-value small">{% if live_account and live_account.positions_count is not none %}{{ live_account.positions_count }}{% else %}暂无{% endif %}</span>
                            </div>
                        </div>
                        <div>
                            <div class="panel-head" style="margin:0 0 8px 0;">
                                <h2>真实仓位</h2>
                                <span class="hint">显示真实账户全部持仓</span>
                            </div>
                            {% if live_account and live_account.positions %}
                            <div class="position-list">
                                {% for pos in live_account.positions %}
                                <div class="position-item">
                                    <div class="position-cell">
                                        <span class="position-ticker">{{ pos.ticker }}</span>
                                        <span class="val">{{ pos.quantity }} 股</span>
                                    </div>
                                    <div class="position-cell"><span class="label">成本</span><span class="val">${{ "%.2f"|format(pos.avg_entry_price) }}</span></div>
                                    <div class="position-cell"><span class="label">现价</span><span class="val">${{ "%.2f"|format(pos.current_price) }}</span></div>
                                    <div class="position-cell"><span class="label">市值</span><span class="val">${{ "%.2f"|format(pos.market_value) }}</span></div>
                                    <div class="position-cell"><span class="label">浮盈亏</span><span class="val {{ 'green' if pos.unrealized_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(pos.unrealized_pnl) }}</span></div>
                                    <div class="position-cell"><span class="label">收益率</span><span class="val {{ 'green' if pos.unrealized_pnl_pct >= 0 else 'red' }}">{{ "%+.2f"|format(pos.unrealized_pnl_pct) }}%</span></div>
                                </div>
                                {% endfor %}
                            </div>
                            {% else %}
                            <div class="position-empty">当前没有持仓。</div>
                            {% endif %}
                        </div>
                    </div>
                </div>

                <div class="overview-panel compact">
                    <div class="panel-head">
                        <h2>AI 区间选股</h2>
                        <span class="hint">最新选股与参数</span>
                    </div>
                    <div class="section-meta">
                        {% if ai_selection and ai_selection.timestamp %}
                            最新选股时间：{{ ai_selection.timestamp }}
                            {% if ai_selection.settings %}
                                · 价格范围：${{ "%.2f"|format(ai_selection.settings.min_price or 0) }} - ${{ "%.2f"|format(ai_selection.settings.max_price or 0) }}
                                · 自动刷新：{{ ai_selection.settings.auto_refresh_minutes or 0 }} 分钟
                                · 扫描数量：{{ ai_selection.settings.max_symbols or 0 }}
                                · 数据模式：{{ ai_selection.settings.data_mode or 'unknown' }}
                                {% if ai_selection.settings.fallback_used %} · 已回退补齐{% endif %}
                            {% endif %}
                        {% else %}
                            暂无 AI 选股报告。
                        {% endif %}
                    </div>
                    <form class="settings-form" method="post" action="/ai-selector-settings">
                        <div class="settings-field">
                            <label for="min_price">价格下限</label>
                            <input id="min_price" name="min_price" type="number" min="1" max="500" step="0.01" value="{{ runtime_settings.min_price }}">
                        </div>
                        <div class="settings-field">
                            <label for="max_price">价格上限</label>
                            <input id="max_price" name="max_price" type="number" min="1" max="500" step="0.01" value="{{ runtime_settings.max_price }}">
                        </div>
                        <div class="settings-field">
                            <label for="auto_refresh_minutes">自动刷新间隔（分钟）</label>
                            <input id="auto_refresh_minutes" name="auto_refresh_minutes" type="number" min="1" max="1440" step="1" value="{{ runtime_settings.auto_refresh_minutes }}">
                        </div>
                        <button class="settings-button" type="submit" name="action" value="save">保存设置</button>
                        <button class="settings-button secondary" type="submit" name="action" value="rerun">立即重选</button>
                    </form>
                    {% if ai_selection and ai_selection.report %}
                    <div class="selector-table">
                        <div class="selector-head">
                            <span>排名</span>
                            <span>标的</span>
                            <span>总分</span>
                            <span>波动</span>
                            <span>区间</span>
                        </div>
                        {% for row in ai_selection.report[:5] %}
                        <div class="selector-row">
                            <span class="num">{{ row.rank }}</span>
                            <span class="ticker">{{ row.ticker }}</span>
                            <span class="num">{{ "%.2f"|format(row.score) }}</span>
                            <span class="num">{{ "%.2f"|format(row.volatility) }}</span>
                            <span class="num">{{ row.suggested_range }}</span>
                        </div>
                        {% endfor %}
                    </div>
                    {% else %}
                    <div class="selector-empty">先运行一次 `scripts/run_ai_selector.py`，这里就会显示最新的 AI 区间选股结果。</div>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

        <div class="cards-section">
            <div class="cards-section-head">
                <div>
                    <h2>全部标的</h2>
                <p>第二屏重点看每只票的区间位置、当前信号、持仓和盈亏，距离近的会自动高亮。</p>
                </div>
            </div>
        <div class="cards">
    {% for card in cards %}
            <div class="card {% if not card.online %}offline{% endif %} {{ card.featured_class }}">
                <div class="card-head">
                    <div class="card-title">
                        <span class="ticker">{{ card.name }}</span>
                        <span class="desc">{{ card.desc }}</span>
                        {% if card.featured_label %}<span class="card-spot {{ card.featured_class }}">{{ card.featured_label }}</span>{% endif %}
                    </div>
                    <div class="badges">
                        {% if card.halted %}<span class="badge halted">已暂停</span>{% endif %}
                        {% if card.trade_in_progress %}<span class="badge live">交易中</span>{% endif %}
                        {% if card.reduce_only %}<span class="badge halted">仅减仓</span>{% endif %}
                        {% if card.range_ready %}<span class="badge live">区间就绪</span>{% else %}<span class="badge halted">区间未就绪</span>{% endif %}
                        {% if card.online %}<span class="badge live">在线</span>{% else %}<span class="badge offline">离线</span>{% endif %}
                    </div>
                </div>

                <div class="price-row">
                    <span class="price {{ 'green' if card.price_change >= 0 else 'red' }}">${{ "%.2f"|format(card.price) }}</span>
                    <span class="change {{ 'green' if card.price_change >= 0 else 'red' }}">
                        {{ '+' if card.price_change >= 0 else '' }}{{ "%.2f"|format(card.price_change) }}%
                    </span>
                </div>

                <div class="quote-strip">
                    <div class="strip-box">
                        <span class="label">高 / 低</span>
                        <span class="val">${{ "%.2f"|format(card.day_high) }} / ${{ "%.2f"|format(card.day_low) }}</span>
                    </div>
                    <div class="strip-box">
                        <span class="label">买 / 卖</span>
                        <span class="val">${{ "%.2f"|format(card.bid) }} / ${{ "%.2f"|format(card.ask) }}</span>
                    </div>
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
                <div class="signal-note">{{ card.signal_reason }}</div>

                <div class="pnl-grid">
                    <div class="quote-item"><span class="label">持股</span><span class="val">{{ card.shares }}</span></div>
                    <div class="quote-item"><span class="label">持仓来源</span><span class="val">{{ card.hold_source }}</span></div>
                    <div class="quote-item"><span class="label">成交</span><span class="val">{{ card.trades }}</span></div>
                    <div class="quote-item"><span class="label">盈亏</span><span class="val {{ 'green' if card.pnl >= 0 else 'red' }}">${{ "%+.2f"|format(card.pnl) }}</span></div>
                    <div class="quote-item"><span class="label">区间源</span><span class="val {{ 'muted' if not card.range_ready else '' }}">{{ card.range_source }}</span></div>
                </div>
            </div>
    {% endfor %}
        </div>
    </div>

    <div class="refresh">每 5 秒自动刷新 · {{ update_time }}</div>
</div>
</body>
</html>"""


def _fetch_status(port):
    """Fetch /api/status JSON from an engine."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1)
        data = json.loads(req.read().decode())
        if isinstance(data, dict):
            _STATUS_CACHE[port] = data
        _STATUS_FAILURES[port] = 0
        return data
    except Exception:
        failures = _STATUS_FAILURES.get(port, 0) + 1
        _STATUS_FAILURES[port] = failures
        if failures < _STATUS_OFFLINE_THRESHOLD and port in _STATUS_CACHE:
            return _STATUS_CACHE[port]
        return None


def _load_config_defaults(config_name):
    """Read display defaults from YAML so offline engines do not show $0 capital."""
    cfg_path = PROJECT_DIR / "configs" / config_name
    defaults = {
        "ticker": config_name.replace(".yaml", ""),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
        "reduce_only": False,
    }
    try:
        flags = json.loads(TRADING_FLAGS_PATH.read_text(encoding="utf-8")) if TRADING_FLAGS_PATH.exists() else {}
        if not isinstance(flags, dict):
            flags = {}
        data = yaml.safe_load(cfg_path.read_text()) or {}
        defaults["ticker"] = data.get("ticker") or defaults["ticker"]
        position = data.get("position") or {}
        defaults["initial_capital"] = float(position.get("initial_capital") or 0.0)
        defaults["reduce_only"] = bool(position.get("reduce_only", False)) or bool(flags.get("reduce_only_all", False))
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
    today_total_pnl = 0.0
    total_capital = 0.0
    total_equity = 0.0
    total_trades = 0
    runtime_settings = load_runtime_settings()
    live_account = _fetch_live_account_summary()
    account_positions = _position_lookup(live_account)
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
    selected_tickers: set[str] = set()

    for t in TICKERS:
        d = _fetch_status(t["port"])
        defaults = _load_config_defaults(t["config"])
        selected_tickers.add(str(defaults["ticker"]).strip().upper())

        if d:
            supp = d.get("support", 0)
            res = d.get("resistance", 0)
            price = d.get("price", 0)
            pos_pct = ((price - supp) / (res - supp) * 100) if res and supp and res != supp else 50
            pos_pct = max(0, min(100, pos_pct))

            sparkline = _build_sparkline([price], price)
            selected_ticker = str(defaults["ticker"]).strip().upper()
            account_pos = account_positions.get(selected_ticker)
            account_shares = int((account_pos or {}).get("quantity", 0) or 0)
            account_pnl = float((account_pos or {}).get("unrealized_pnl", 0.0) or 0.0)
            account_pnl_pct = float((account_pos or {}).get("unrealized_pnl_pct", 0.0) or 0.0)
            hold_source = "真实账户" if account_pos else "引擎状态"

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
                "range_ready": bool(d.get("range_ready")),
                "range_source": d.get("range_source", "unknown"),
                "pos_pct": pos_pct,
                "sparkline": sparkline,
                "signal": d.get("last_signal", "N/A"),
                "signal_cn": _signal_cn(d.get("last_signal", "N/A")),
                "signal_reason": d.get("last_signal_reason", "暂无"),
                "initial_capital": d.get("initial_capital", 0),
                "cash": d.get("cash", 0),
                "shares": account_shares if account_pos else int(d.get("position_shares", 0) or 0),
                "pnl": account_pnl if account_pos else float(d.get("daily_pnl", 0) or 0.0),
                "pnl_pct": account_pnl_pct if account_pos else 0.0,
                "hold_source": hold_source,
                "reduce_only": defaults.get("reduce_only", False),
                "equity": d.get("equity", 0),
                "trades": d.get("trades_today", 0),
                "halted": d.get("halted", False),
                "trade_in_progress": bool(d.get("trade_in_progress", False)),
            }
            cards.append(card)
            day_pnl = float(d.get("daily_pnl", 0) or 0.0)
            today_total_pnl += day_pnl
            total_pnl += day_pnl
            total_capital += d.get("initial_capital", 0) or 0
            total_equity += d.get("equity", 0) or 0
            total_trades += d.get("trades_today", 0) or 0
        else:
            initial_capital = defaults["initial_capital"]
            selected_ticker = str(defaults["ticker"]).strip().upper()
            account_pos = account_positions.get(selected_ticker)
            account_shares = int((account_pos or {}).get("quantity", 0) or 0)
            account_pnl = float((account_pos or {}).get("unrealized_pnl", 0.0) or 0.0)
            account_pnl_pct = float((account_pos or {}).get("unrealized_pnl_pct", 0.0) or 0.0)
            account_price = float((account_pos or {}).get("current_price", 0.0) or 0.0)
            cards.append({
                "name": defaults["ticker"], "desc": t["desc"],
                "online": False,
                "price": account_price,
                "price_change": 0,
                "day_high": account_price,
                "day_low": account_price,
                "bid": account_price,
                "ask": account_price,
                "vol_display": "0",
                "support": defaults["support"], "resistance": defaults["resistance"], "spread_pct": 0,
                "range_ready": False, "range_source": "offline",
                "pos_pct": 50,
                "sparkline": _build_sparkline([], 0),
                "signal": "OFFLINE",
                "signal_cn": _signal_cn("OFFLINE"),
                "signal_reason": "引擎离线，仓位仍按真实账户显示" if account_pos else "暂无",
                "shares": account_shares,
                "initial_capital": initial_capital, "cash": initial_capital,
                "pnl": account_pnl,
                "pnl_pct": account_pnl_pct,
                "hold_source": "真实账户" if account_pos else "离线",
                "reduce_only": defaults.get("reduce_only", False), "equity": initial_capital, "trades": 0, "halted": False,
                "trade_in_progress": False,
            })
            total_capital += initial_capital
            total_equity += initial_capital

    display_live_account = live_account

    if live_account and live_account.get("mode") == "live":
        total_pnl = sum(float((pos or {}).get("unrealized_pnl", 0.0) or 0.0) for pos in (live_account.get("positions") or []))
        total_capital = float(live_account.get("cash") or 0.0)
        total_equity = float(live_account.get("equity") or 0.0)
        footer_buying_power = float(live_account.get("buying_power") or 0.0)
        account_labels = {
            "footer_capital": "账户现金",
            "footer_equity": "账户权益",
            "footer_buying_power": "可买额度",
        }
    else:
        footer_buying_power = None
        account_labels = {
            "footer_capital": "总本金",
            "footer_equity": "总权益",
            "footer_buying_power": "可买额度",
        }

    active_symbols = " / ".join(
        card["name"].split("·", 1)[-1].strip() if "·" in card["name"] else str(card["name"]).strip()
        for card in cards
    ) or "N/A"
    nearest_buy_trigger_name, nearest_buy_trigger = _nearest_trigger(
        cards,
        "buy",
        new_entries_allowed=trade_audit["new_entries_allowed"],
    )
    nearest_sell_trigger_name, nearest_sell_trigger = _nearest_trigger(cards, "sell")

    highlight_names = []
    if nearest_buy_trigger_name and nearest_buy_trigger_name not in {"暂无", "暂无可买", "暂停开仓"}:
        highlight_names.append(nearest_buy_trigger_name)
    if (
        nearest_sell_trigger_name
        and nearest_sell_trigger_name not in {"暂无", "暂无可买", "暂停开仓"}
        and nearest_sell_trigger_name not in highlight_names
    ):
        highlight_names.append(nearest_sell_trigger_name)

    featured_cards = []
    featured_set = set()
    for name in highlight_names:
        for card in cards:
            if card["name"] != name or name in featured_set:
                continue
            labels = []
            classes = []
            if name == nearest_buy_trigger_name:
                labels.append("最接近买点")
                classes.append("featured-buy")
            if name == nearest_sell_trigger_name:
                labels.append("最接近卖点")
                classes.append("featured-sell")
            if len(labels) > 1:
                labels = ["买卖都接近"]
                classes = ["featured-dual"]
            card["featured_label"] = " / ".join(labels)
            card["featured_class"] = " ".join(classes)
            featured_cards.append(card)
            featured_set.add(name)
            break

    for card in cards:
        if card["name"] in featured_set:
            continue
        card["featured_label"] = ""
        card["featured_class"] = ""

    other_cards = [card for card in cards if card["name"] not in featured_set]

    return render_template_string(HTML,
        cards=cards,
        featured_cards=featured_cards,
        other_cards=other_cards,
        account_labels=account_labels,
        footer_buying_power=footer_buying_power,
        live_account=display_live_account or live_account,
        ai_selection=ai_selection,
        runtime_settings={
            "min_price": float(runtime_settings.get("min_price", ai_selection.get("settings", {}).get("min_price", 10.0)) or 10.0),
            "max_price": float(runtime_settings.get("max_price", ai_selection.get("settings", {}).get("max_price", 200.0)) or 200.0),
            "auto_refresh_minutes": int(runtime_settings.get("auto_refresh_minutes", ai_selection.get("settings", {}).get("auto_refresh_minutes", 5)) or 5),
        },
        active_symbols=active_symbols,
        nearest_buy_trigger_name=nearest_buy_trigger_name,
        nearest_buy_trigger=nearest_buy_trigger,
        nearest_sell_trigger_name=nearest_sell_trigger_name,
        nearest_sell_trigger=nearest_sell_trigger,
        trade_audit=trade_audit,
        total_pnl=round(total_pnl, 2),
        today_total_pnl=round(today_total_pnl, 2),
        total_capital=round(total_capital, 2),
        total_equity=round(total_equity, 2),
        total_trades=total_trades,
        update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _run_ai_selector_now() -> None:
    project_dir = str(PROJECT_DIR)
    env = os.environ.copy()
    settings = load_runtime_settings()
    env.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    env.setdefault("AI_SELECTOR_MAX_SYMBOLS", "50")
    env.setdefault("AI_SELECTOR_MIN_PRICE", str(settings.get("min_price", 10.0)))
    env.setdefault("AI_SELECTOR_MAX_PRICE", str(settings.get("max_price", 200.0)))
    env.setdefault("AI_SELECTOR_AUTO_REFRESH_MINUTES", str(settings.get("auto_refresh_minutes", 5)))
    python_bin = PROJECT_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(os.environ.get("PYTHON", "")) if os.environ.get("PYTHON") else Path("python3")

    subprocess.run(
        [str(python_bin), "scripts/run_ai_selector.py"],
        cwd=project_dir,
        env=env,
        check=False,
    )


@app.route("/ai-selector-settings", methods=["POST"])
def update_ai_selector_settings():
    raw_min_price = str(request.form.get("min_price", "")).strip()
    raw_max_price = str(request.form.get("max_price", "")).strip()
    raw_auto_refresh_minutes = str(request.form.get("auto_refresh_minutes", "")).strip()
    action = str(request.form.get("action", "save")).strip().lower()
    settings = load_runtime_settings()
    try:
        min_price = float(raw_min_price)
    except (TypeError, ValueError):
        min_price = float(settings.get("min_price", 10.0) or 10.0)
    try:
        max_price = float(raw_max_price)
    except (TypeError, ValueError):
        max_price = float(settings.get("max_price", 200.0) or 200.0)
    try:
        auto_refresh_minutes = int(raw_auto_refresh_minutes)
    except (TypeError, ValueError):
        auto_refresh_minutes = int(settings.get("auto_refresh_minutes", 5) or 5)
    min_price = min(500.0, max(1.0, min_price))
    max_price = min(500.0, max(1.0, max_price))
    if min_price > max_price:
        min_price, max_price = max_price, min_price
    min_price = round(min_price, 2)
    auto_refresh_minutes = max(1, min(1440, auto_refresh_minutes))
    settings["max_price"] = round(max_price, 2)
    settings["min_price"] = min_price
    settings["auto_refresh_minutes"] = auto_refresh_minutes
    save_runtime_settings(settings)
    if action == "rerun":
        _run_ai_selector_now()
    return redirect("/")


def start_combined(port=8090):
    """Start combined dashboard as a foreground Flask server."""
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
