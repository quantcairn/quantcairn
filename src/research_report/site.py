from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .generator import DEFAULT_REPORTS_DIR, DEFAULT_SITE_DIR, PROJECT_DIR


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _report_digest(payload: dict[str, Any]) -> dict[str, Any]:
    top_cards = payload.get("top_cards") if isinstance(payload.get("top_cards"), list) else []
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    selection_sync = payload.get("selection_sync") if isinstance(payload.get("selection_sync"), dict) else {}
    trade_activity = payload.get("trade_activity") if isinstance(payload.get("trade_activity"), dict) else {}
    summary = trade_activity.get("summary") if isinstance(trade_activity.get("summary"), dict) else {}
    return {
        "date": str(payload.get("date") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "mode": str(payload.get("mode") or "unknown"),
        "top_symbols": [str(item.get("ticker") or "") for item in top_cards if str(item.get("ticker") or "").strip()],
        "entry_ready": int(quality.get("entry_ready_count", 0) or 0),
        "observation_only": int(quality.get("observation_only_count", 0) or 0),
        "selection_sync_ok": bool(selection_sync.get("ok", False)),
        "selection_sync_reason": str(selection_sync.get("reason") or ""),
        "fallback_used": bool(quality.get("fallback_used", False)),
        "execution_count": int(summary.get("execution_count", 0) or 0),
        "buy_count": int(summary.get("buy_count", 0) or 0),
        "sell_count": int(summary.get("sell_count", 0) or 0),
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
    }


def _render_index(latest: dict[str, Any] | None, reports: list[dict[str, Any]]) -> str:
    latest = latest or {}
    top_symbols = latest.get("top_symbols") or []
    warnings = latest.get("warnings") or []
    report_rows = []
    for item in reports:
        top_line = ", ".join(item.get("top_symbols") or []) or "暂无"
        report_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('date') or ''))}</td>"
            f"<td>{html.escape(str(item.get('mode') or 'unknown'))}</td>"
            f"<td>{html.escape(top_line)}</td>"
            f"<td>{html.escape(str(item.get('entry_ready') or 0))}</td>"
            f"<td>{html.escape(str(item.get('observation_only') or 0))}</td>"
            f"<td>{html.escape('通过' if item.get('selection_sync_ok') else '不一致')}</td>"
            f"<td>{html.escape('是' if item.get('fallback_used') else '否')}</td>"
            f"<td>{html.escape(str(item.get('execution_count') or 0))}</td>"
            f"<td><a href='../../reports/research/daily-paper-report-{html.escape(str(item.get('date') or ''))}.html'>HTML</a> | "
            f"<a href='../../reports/research/daily-paper-report-{html.escape(str(item.get('date') or ''))}.md'>MD</a> | "
            f"<a href='../../reports/research/daily-paper-report-{html.escape(str(item.get('date') or ''))}.json'>JSON</a></td>"
            "</tr>"
        )
    latest_summary = (
        f"Latest report {html.escape(str(latest.get('date') or 'N/A'))} · "
        f"TOP: {html.escape(', '.join(top_symbols) or '暂无')} · "
        f"同步: {html.escape('通过' if latest.get('selection_sync_ok') else '不一致')} · "
        f"成交: {html.escape(str(latest.get('execution_count') or 0))}"
    )
    warnings_html = ""
    if warnings:
        warnings_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in warnings)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>研究报告首页</title>
  <style>
    body {{ margin: 0; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background: #0f172a; color: #e5e7eb; }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 28px 20px 48px; }}
    .hero {{ background: linear-gradient(180deg, rgba(17,24,39,.96), rgba(15,23,42,.96)); border: 1px solid #334155; border-radius: 20px; padding: 24px; }}
    h1 {{ margin: 0 0 10px; font-size: 30px; }}
    .muted {{ color: #94a3b8; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 18px; }}
    .card {{ background: rgba(15,23,42,.82); border: 1px solid #334155; border-radius: 16px; padding: 14px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 18px; background: rgba(17,24,39,.94); border-radius: 16px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid rgba(51,65,85,.8); padding: 10px 12px; text-align: left; }}
    th {{ color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    a {{ color: #38bdf8; }}
    ul {{ margin: 8px 0 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>研究报告首页</h1>
      <p class="muted">只读研究副系统，汇总每日纸面交易报告，不参与交易执行。</p>
      <div class="cards">
        <div class="card"><div class="muted">最新摘要</div><strong>{latest_summary}</strong></div>
        <div class="card"><div class="muted">报告数量</div><strong>{len(reports)}</strong></div>
        <div class="card"><div class="muted">最后生成时间</div><strong>{html.escape(str(latest.get('generated_at') or 'N/A'))}</strong></div>
      </div>
      {f'<div class="card" style="margin-top:12px;"><div class="muted">警告</div><ul>{warnings_html}</ul></div>' if warnings_html else ''}
    </div>

    <table>
      <thead>
        <tr>
          <th>日期</th>
          <th>模式</th>
          <th>TOP</th>
          <th>可开仓</th>
          <th>观察级</th>
          <th>同步</th>
          <th>fallback</th>
          <th>成交数</th>
          <th>链接</th>
        </tr>
      </thead>
      <tbody>
        {''.join(report_rows) if report_rows else '<tr><td colspan="9">暂无研究报告</td></tr>'}
      </tbody>
    </table>
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
    reports = []
    for path in sorted(reports_path.glob("daily-paper-report-*.json")):
        payload = _load_json(path)
        if payload:
            reports.append(_report_digest(payload))
    latest_path = _latest_report_path(reports_path)
    latest = _report_digest(_load_json(latest_path)) if latest_path else {}
    index_path = site_path / "index.html"
    index_path.write_text(_render_index(latest, reports), encoding="utf-8")
    return index_path

