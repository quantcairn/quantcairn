from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .generator import DEFAULT_REPORTS_DIR, DEFAULT_SITE_DIR, PROJECT_DIR


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _latest_report_path(reports_dir: Path) -> Path | None:
    candidates = sorted(reports_dir.glob("daily-paper-report-*.json"))
    return candidates[-1] if candidates else None


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_money(value: Any) -> str:
    number = _coerce_float(value)
    if number is None:
        return "N/A"
    return f"${number:,.2f}"


def _fmt_pct(value: Any) -> str:
    number = _coerce_float(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}%"


def _normalize_ticker(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return raw.split(".")[0] if raw else ""


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _report_digest(payload: dict[str, Any]) -> dict[str, Any]:
    top_cards = payload.get("top_cards") if isinstance(payload.get("top_cards"), list) else []
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    strategy_review = payload.get("strategy_review") if isinstance(payload.get("strategy_review"), dict) else {}
    selection_sync = payload.get("selection_sync") if isinstance(payload.get("selection_sync"), dict) else {}
    trade_activity = payload.get("trade_activity") if isinstance(payload.get("trade_activity"), dict) else {}
    summary = trade_activity.get("summary") if isinstance(trade_activity.get("summary"), dict) else {}
    top_symbols = [_normalize_ticker(item.get("ticker")) for item in top_cards if _normalize_ticker(item.get("ticker"))]
    entry_ready_symbols = [str(item.get("ticker") or "") for item in top_cards if item.get("entry_ready")]
    observation_only_symbols = [str(item.get("ticker") or "") for item in top_cards if not item.get("entry_ready")]
    price_band = quality.get("price_band") if isinstance(quality.get("price_band"), dict) else {}

    return {
        "date": str(payload.get("date") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "mode": str(payload.get("mode") or "unknown"),
        "top_symbols": top_symbols,
        "top_line": " / ".join(top_symbols) if top_symbols else "暂无",
        "entry_ready": int(quality.get("entry_ready_count", 0) or 0),
        "observation_only": int(quality.get("observation_only_count", 0) or 0),
        "entry_ready_symbols": entry_ready_symbols,
        "observation_only_symbols": observation_only_symbols,
        "selection_sync_ok": bool(selection_sync.get("ok", False)),
        "selection_sync_reason": str(selection_sync.get("reason") or ""),
        "selection_sync_mismatch_reason": str(selection_sync.get("mismatch_reason") or ""),
        "fallback_used": bool(quality.get("fallback_used", False)),
        "provider_fallback_used": bool(quality.get("provider_fallback_used", False)),
        "strategy_success_count": int(strategy_review.get("success_count", 0) or 0),
        "strategy_observation_correct_count": int(strategy_review.get("observation_correct_count", 0) or 0),
        "strategy_failure_count": int(strategy_review.get("failure_count", 0) or 0),
        "strategy_review_rows": strategy_review.get("rows") if isinstance(strategy_review.get("rows"), list) else [],
        "execution_count": int(summary.get("execution_count", 0) or 0),
        "buy_count": int(summary.get("buy_count", 0) or 0),
        "sell_count": int(summary.get("sell_count", 0) or 0),
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
        "price_band_min": _coerce_float(price_band.get("min"), None),
        "price_band_max": _coerce_float(price_band.get("max"), None),
        "no_trade_reason": str(payload.get("no_trade_reason") or ""),
    }


def _brief_sentence(item: dict[str, Any]) -> str:
    top_symbols = item.get("top_symbols") or []
    if not top_symbols:
        return "暂无 TOP 结果。"
    parts = [
        f"TOP：{', '.join(top_symbols)}",
        f"可开仓 {int(item.get('entry_ready', 0) or 0)}",
        f"观察级 {int(item.get('observation_only', 0) or 0)}",
        f"成交 {int(item.get('execution_count', 0) or 0)}",
    ]
    if any(int(item.get(key, 0) or 0) for key in ("strategy_success_count", "strategy_observation_correct_count", "strategy_failure_count")):
        parts.append(
            "策略复盘 "
            f"成功 {int(item.get('strategy_success_count', 0) or 0)} / "
            f"观察正确 {int(item.get('strategy_observation_correct_count', 0) or 0)} / "
            f"失败 {int(item.get('strategy_failure_count', 0) or 0)}"
        )
    if item.get("selection_sync_ok"):
        parts.append("同步正常")
    else:
        parts.append(f"同步异常：{item.get('selection_sync_mismatch_reason') or item.get('selection_sync_reason') or 'unknown'}")
    if item.get("fallback_used"):
        parts.append("含 fallback/mock")
    return " · ".join(parts)


def _render_report_card(item: dict[str, Any], latest_date: str) -> str:
    warning_badges = []
    if item.get("selection_sync_ok"):
        warning_badges.append('<span class="chip good">同步正常</span>')
    else:
        warning_badges.append('<span class="chip warn">同步不一致</span>')
    warning_badges.append(f'<span class="chip">模式 {html.escape(str(item.get("mode") or "unknown"))}</span>')
    if item.get("fallback_used"):
        warning_badges.append('<span class="chip warn">fallback</span>')
    if item.get("provider_fallback_used"):
        warning_badges.append('<span class="chip warn">provider fallback</span>')

    bullets = [
        f"可开仓：{int(item.get('entry_ready', 0) or 0)}",
        f"观察级：{int(item.get('observation_only', 0) or 0)}",
        f"买单：{int(item.get('buy_count', 0) or 0)}",
        f"卖单：{int(item.get('sell_count', 0) or 0)}",
    ]
    if any(int(item.get(key, 0) or 0) for key in ("strategy_success_count", "strategy_observation_correct_count", "strategy_failure_count")):
        bullets.append(
            "策略复盘："
            f"{int(item.get('strategy_success_count', 0) or 0)} / "
            f"{int(item.get('strategy_observation_correct_count', 0) or 0)} / "
            f"{int(item.get('strategy_failure_count', 0) or 0)}"
        )
    if item.get("warnings"):
        bullets.append(f"警告：{len(item.get('warnings') or [])} 条")

    links = []
    for suffix, label in (("html", "HTML"), ("md", "MD"), ("json", "JSON")):
        links.append(
            f"<a class=\"doc-link\" href=\"../../reports/research/daily-paper-report-{html.escape(str(item.get('date') or ''))}.{suffix}\" target=\"_blank\" rel=\"noopener\">{label}</a>"
        )

    warning_html = ""
    if item.get("warnings"):
        warning_html = "<ul class=\"warning-list\">" + "".join(
            f"<li>{html.escape(str(w))}</li>" for w in item.get("warnings") or []
        ) + "</ul>"

    is_latest = item.get("date") == latest_date
    return f"""
    <article class="report-card{' latest' if is_latest else ''}">
      <div class="card-top">
        <div>
          <div class="report-date">{html.escape(str(item.get("date") or ""))}{' · 今日最新' if is_latest else ''}</div>
          <div class="report-title">{html.escape(item.get("top_line") or '暂无')}</div>
        </div>
        <div class="chip-row">
          {''.join(warning_badges)}
        </div>
      </div>
      <div class="report-summary">{html.escape(_brief_sentence(item))}</div>
      <div class="report-metrics">
        <div><span>可开仓</span><strong>{int(item.get('entry_ready', 0) or 0)}</strong></div>
        <div><span>观察级</span><strong>{int(item.get('observation_only', 0) or 0)}</strong></div>
        <div><span>成交</span><strong>{int(item.get('execution_count', 0) or 0)}</strong></div>
        <div><span>价格带</span><strong>{html.escape(_fmt_money(item.get('price_band_min')))} - {html.escape(_fmt_money(item.get('price_band_max')))}</strong></div>
      </div>
      <div class="report-notes">
        <div class="note-title">当日报告要点</div>
        <div class="note-list">
          <span>TOP：{html.escape(item.get('top_line') or '暂无')}</span>
          <span>同步：{'通过' if item.get('selection_sync_ok') else '不一致'}</span>
          <span>fallback：{'是' if item.get('fallback_used') else '否'}</span>
          <span>provider fallback：{'是' if item.get('provider_fallback_used') else '否'}</span>
          <span>策略复盘：成功 {int(item.get('strategy_success_count', 0) or 0)} · 观察正确 {int(item.get('strategy_observation_correct_count', 0) or 0)} · 失败 {int(item.get('strategy_failure_count', 0) or 0)}</span>
        </div>
      </div>
      {warning_html}
      <div class="card-footer">
        <div class="report-time">生成时间 {html.escape(str(item.get("generated_at") or ""))}</div>
        <div class="doc-links">{''.join(links)}</div>
      </div>
    </article>
    """


def _render_index(latest: dict[str, Any] | None, reports: list[dict[str, Any]]) -> str:
    latest = latest or {}
    reports = sorted(reports, key=lambda item: str(item.get("date") or ""), reverse=True)
    latest_summary = _brief_sentence(latest) if latest else "暂无最新报告"
    latest_warn = ""
    if latest.get("warnings"):
        latest_warn = "<ul class=\"warning-list\">" + "".join(
            f"<li>{html.escape(str(w))}</li>" for w in latest.get("warnings") or []
        ) + "</ul>"

    cards_html = "".join(_render_report_card(item, latest.get("date") or "") for item in reports)
    if not cards_html:
        cards_html = """
        <div class="empty-state">
          <div class="empty-title">暂无研究报告</div>
          <div class="empty-copy">先运行 <code>.venv/bin/python scripts/generate_daily_research_report.py</code> 生成第一份日报。</div>
        </div>
        """

    latest_top_line = html.escape(str(latest.get("top_line") or "暂无"))
    latest_mode = html.escape(str(latest.get("mode") or "unknown"))
    latest_date = html.escape(str(latest.get("date") or "N/A"))
    latest_generated = html.escape(str(latest.get("generated_at") or "N/A"))
    entry_ready = int(latest.get("entry_ready", 0) or 0)
    observation_only = int(latest.get("observation_only", 0) or 0)
    execution_count = int(latest.get("execution_count", 0) or 0)
    total_reports = len(reports)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>研究报告首页</title>
  <style>
    :root {{
      --bg: #07111f;
      --panel: rgba(10, 18, 32, 0.92);
      --panel-2: rgba(13, 22, 40, 0.88);
      --line: rgba(148, 163, 184, 0.16);
      --text: #e5eefc;
      --muted: #91a4be;
      --accent: #38bdf8;
      --good: #34d399;
      --warn: #fbbf24;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(56,189,248,.12), transparent 34%),
        radial-gradient(circle at top right, rgba(52,211,153,.10), transparent 28%),
        linear-gradient(180deg, #050b15 0%, #07111f 100%);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .wrap {{ max-width: 1360px; margin: 0 auto; padding: 28px 18px 48px; }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, .8fr);
      gap: 18px;
      padding: 24px;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(15, 23, 42, .96), rgba(8, 15, 28, .96));
      box-shadow: 0 18px 46px rgba(0, 0, 0, .28);
    }}
    h1 {{ margin: 0 0 8px; font-size: 32px; line-height: 1.1; }}
    .lead {{ margin: 0; color: var(--muted); line-height: 1.6; }}
    .hero-right {{
      display: grid;
      gap: 10px;
      align-content: start;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(15, 23, 42, .75);
      padding: 14px 16px;
    }}
    .stat span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 8px;
    }}
    .stat strong {{
      font-size: 18px;
      line-height: 1.3;
    }}
    .summary-box {{
      border: 1px solid rgba(56, 189, 248, .24);
      border-radius: 16px;
      background: rgba(56, 189, 248, .06);
      padding: 14px 16px;
    }}
    .summary-title {{
      font-size: 12px;
      color: #bae6fd;
      text-transform: uppercase;
      letter-spacing: .1em;
      margin-bottom: 8px;
    }}
    .summary-copy {{ line-height: 1.65; color: #dbeafe; }}
    .warning-list {{
      margin: 10px 0 0;
      padding-left: 18px;
      color: #fde68a;
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin: 22px 2px 14px;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 20px;
    }}
    .section-head .muted {{
      color: var(--muted);
      font-size: 13px;
    }}
    .brief-list {{
      display: grid;
      gap: 14px;
    }}
    .report-card {{
      border: 1px solid var(--line);
      border-radius: 20px;
      background: var(--panel);
      padding: 18px 18px 16px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, .16);
    }}
    .report-card.latest {{
      border-color: rgba(56, 189, 248, .34);
      box-shadow: 0 16px 38px rgba(2, 132, 199, .14);
    }}
    .card-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .report-date {{
      color: #c7d2fe;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .12em;
      margin-bottom: 6px;
    }}
    .report-title {{
      font-size: 22px;
      font-weight: 780;
      letter-spacing: -.02em;
    }}
    .chip-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      background: rgba(15, 23, 42, .76);
      color: var(--text);
      font-size: 12px;
    }}
    .chip.good {{
      border-color: rgba(52, 211, 153, .30);
      background: rgba(52, 211, 153, .08);
      color: #b8f5d0;
    }}
    .chip.warn {{
      border-color: rgba(251, 191, 36, .30);
      background: rgba(251, 191, 36, .08);
      color: #fde68a;
    }}
    .report-summary {{
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(15, 23, 42, .78);
      border: 1px solid rgba(148, 163, 184, .10);
      color: #dce7f7;
      line-height: 1.65;
    }}
    .report-metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .report-metrics div {{
      border: 1px solid rgba(148, 163, 184, .12);
      border-radius: 14px;
      background: rgba(12, 18, 33, .8);
      padding: 12px 14px;
    }}
    .report-metrics span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }}
    .report-metrics strong {{
      font-size: 16px;
      line-height: 1.3;
    }}
    .report-notes {{
      margin-top: 14px;
      border-top: 1px dashed rgba(148, 163, 184, .18);
      padding-top: 12px;
    }}
    .note-title {{
      color: #cbd5e1;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .1em;
      margin-bottom: 10px;
    }}
    .note-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .note-list span {{
      display: inline-flex;
      align-items: center;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .04);
      border: 1px solid rgba(148, 163, 184, .12);
      color: #dbeafe;
      font-size: 12px;
    }}
    .card-footer {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid rgba(148, 163, 184, .12);
    }}
    .report-time {{ color: var(--muted); font-size: 12px; }}
    .doc-links {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .doc-link {{
      color: #bfdbfe;
      text-decoration: none;
      border: 1px solid rgba(96, 165, 250, .25);
      background: rgba(37, 99, 235, .08);
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
    }}
    .doc-link:hover {{ border-color: rgba(96, 165, 250, .5); }}
    .empty-state {{
      border: 1px dashed rgba(148, 163, 184, .24);
      border-radius: 20px;
      background: rgba(15, 23, 42, .7);
      padding: 28px;
      text-align: center;
      color: var(--muted);
    }}
    .empty-title {{
      color: var(--text);
      font-size: 20px;
      margin-bottom: 8px;
    }}
    .empty-copy code {{
      display: inline-block;
      margin-top: 6px;
      padding: 4px 8px;
      border-radius: 8px;
      background: rgba(15, 23, 42, .8);
      color: #93c5fd;
      border: 1px solid rgba(148, 163, 184, .12);
    }}
    @media (max-width: 1024px) {{
      .hero {{
        grid-template-columns: 1fr;
      }}
      .report-metrics {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
    @media (max-width: 720px) {{
      .wrap {{ padding: 18px 12px 32px; }}
      .report-metrics {{
        grid-template-columns: 1fr;
      }}
      .card-top {{
        flex-direction: column;
      }}
      .chip-row {{
        justify-content: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>
        <h1>每日简报列表</h1>
        <p class="lead">只读研究副系统，聚焦每天的 paper trading 复盘、TOP 观察池、选股同步和成交概览，不参与交易执行。</p>
      </div>
      <div class="hero-right">
        <div class="stat-grid">
          <div class="stat"><span>报告数量</span><strong>{total_reports}</strong></div>
          <div class="stat"><span>最新日期</span><strong>{latest_date}</strong></div>
          <div class="stat"><span>可开仓</span><strong>{entry_ready}</strong></div>
          <div class="stat"><span>观察级</span><strong>{observation_only}</strong></div>
        </div>
        <div class="summary-box">
          <div class="summary-title">最新简报</div>
          <div class="summary-copy">{html.escape(latest_summary)}</div>
          <div class="summary-copy" style="margin-top:8px;color:#bfdbfe">生成时间：{latest_generated} · 模式：{latest_mode} · 成交：{execution_count}</div>
          {latest_warn}
        </div>
      </div>
    </section>

    <div class="section-head">
      <h2>近日报告</h2>
      <div class="muted">按日期倒序展示，点击可打开 HTML / Markdown / JSON 三种版本</div>
    </div>

    <section class="brief-list">
      {cards_html}
    </section>
  </div>
</body>
</html>
"""


def build_research_site(
    *,
    project_dir: Path | None = None,
    reports_dir: Path | None = None,
    site_dir: Path | None = None,
) -> Path:
    project_root = Path(project_dir or PROJECT_DIR)
    reports_path = Path(reports_dir or DEFAULT_REPORTS_DIR)
    site_path = Path(site_dir or DEFAULT_SITE_DIR)
    site_path.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    for path in sorted(reports_path.glob("daily-paper-report-*.json")):
        payload = _load_json(path)
        if payload:
            reports.append(_report_digest(payload))
    latest_path = _latest_report_path(reports_path)
    latest = _report_digest(_load_json(latest_path)) if latest_path else {}
    index_path = site_path / "index.html"
    index_path.write_text(_render_index(latest, reports), encoding="utf-8")
    return index_path
