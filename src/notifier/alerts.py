"""
Notification system: sends alerts via console, macOS notifications, webhooks, and Telegram.
"""
import json
import logging
import math
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import yaml

from src.ai_selector.selection_bundle import load_committed_selection_bundle
from src.ai_selector.selection_report import provider_audit_sections
from src.broker.paper_portfolio_state import read_paper_portfolio_state

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]
US_EASTERN = ZoneInfo("America/New_York")
ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")

# Rate limiting for Telegram: max 1 msg/sec
_telegram_last_send = 0.0


def _telegram_rate_limit():
    """Ensure at least 1 second between Telegram messages."""
    global _telegram_last_send
    elapsed = time.time() - _telegram_last_send
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _telegram_last_send = time.time()


class Notifier:
    """Multi-channel notification system for trading signals and events."""

    def __init__(
        self,
        console: bool = True,
        macos_notification: bool = True,
        webhook_url: Optional[str] = None,
        trade_summary_interval: int = 5,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
    ):
        self.console_enabled = console
        self.macos_enabled = macos_notification
        self.webhook_url = webhook_url
        self.trade_summary_interval = trade_summary_interval
        self._telegram_bot_token = telegram_bot_token or os.environ.get("SOXS_TELEGRAM_BOT_TOKEN", "")
        self._telegram_chat_id = telegram_chat_id or os.environ.get("SOXS_TELEGRAM_CHAT_ID", "")
        self._telegram_enabled = bool(self._telegram_bot_token and self._telegram_chat_id)

        self._trade_count_since_summary = 0
        self._last_trades: list[str] = []

    # ---- Public API ----

    def signal(self, ticker: str, signal_type: str, price: float, reason: str) -> None:
        """Send a trading signal notification."""
        title = f"📊 {ticker} — {signal_type}"
        body = f"${price:.2f} | {reason}"

        self._send(title, body, "signal", macos=False, remote=False)

    def order_submitted(self, ticker: str, side: str, quantity: int, order_id: str = "", mode: str = "paper") -> None:
        """Notify when a live order has been accepted by the broker."""
        suffix = f" | {order_id[:12]}" if order_id else ""
        prefix = "实盘" if mode == "live" else "模拟"
        side_cn = "买入" if side.upper() == "BUY" else "卖出"
        title = f"🟦 {prefix}{side_cn} {quantity}股 {ticker}"
        body = f"已提交{suffix}"
        self._send(title, body, "trade", macos=False, remote=False)

    def trade(self, ticker: str, side: str, quantity: int, price: float, pnl: Optional[float] = None, mode: str = "paper") -> None:
        """Notify about an executed trade."""
        side_upper = str(side or "").upper()
        try:
            quantity_int = int(quantity)
            price_float = float(price)
        except (TypeError, ValueError):
            logger.warning("Rejected trade notification with malformed fill: %s %s qty=%r price=%r", ticker, side, quantity, price)
            return
        if quantity_int <= 0 or price_float <= 0 or not math.isfinite(price_float):
            logger.warning("Rejected trade notification with invalid fill: %s %s qty=%r price=%r", ticker, side, quantity, price)
            return

        prefix = "实盘" if mode == "live" else "模拟"
        side_cn = "买入" if side_upper == "BUY" else "卖出"
        emoji = "🟢" if side_upper == "BUY" else "🔴"
        trade_value = price_float * quantity_int
        sign = "+" if side_upper == "BUY" else "-"

        pnl_str = f" | 盈亏 ${pnl:+.2f}" if pnl is not None else ""
        title = f"{emoji} {prefix}{side_cn} {ticker} {quantity_int}股"
        body = f"{sign}${trade_value:,.2f} @ ${price_float:.2f}{pnl_str}"
        if str(mode or "").strip().lower() == "paper":
            portfolio_state = read_paper_portfolio_state()
            if isinstance(portfolio_state, dict):
                body += (
                    f" | 现金 ${float(portfolio_state.get('cash') or 0.0):,.2f}"
                    f" | 权益 ${float(portfolio_state.get('equity') or 0.0):,.2f}"
                )

        # External notifications are intentionally limited to filled trades
        # so the desktop / Telegram channels stay quiet during scans, signals,
        # rejects, and submitted-but-unfilled orders.
        self._send(title, body, "trade", macos=True, remote=True)

        # Track for summary
        self._trade_count_since_summary += 1
        self._last_trades.append(body)

    def alert(self, message: str, level: str = "info") -> None:
        """General alert (errors, warnings, halts)."""
        emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🚨", "halt": "🛑"}
        title = f"{emoji.get(level, 'ℹ️')} Trading Alert"
        self._send(title, message, level, macos=False, remote=False)

    def summary(self, stats: dict) -> None:
        """Send a trading summary."""
        title = "📈 Trading Summary"
        body = (
            f"Trades: {stats.get('total_trades', 0)} | "
            f"Win Rate: {stats.get('win_rate', 0)}% | "
            f"P&L: ${stats.get('total_pnl', 0):+.2f} | "
            f"Today: ${stats.get('daily_pnl_today', 0):+.2f}"
        )
        self._send(title, body, "summary", macos=False, remote=False)

    def heartbeat(self, ticker: str, price: float, range_state, trend_info: dict = None, halted: bool = False) -> None:
        """Lightweight periodic status (not a full notification)."""
        if not self.console_enabled:
            return
        supp = range_state.support
        res = range_state.resistance
        pos_pct = ((price - supp) / (res - supp) * 100) if (res and supp and res != supp) else 50
        bar = self._make_bar(pos_pct)

        trend_str = ""
        if trend_info and trend_info.get("active") and trend_info.get("direction") != "neutral":
            arrow = {"up": "↗", "down": "↘"}.get(trend_info["direction"], "→")
            trend_str = f" | Trend: {arrow} {trend_info['direction']} ({trend_info.get('pct_from_ma', 0):+.1f}% vs MA)"

        conf = getattr(range_state, 'support_confidence', 0) if hasattr(range_state, 'support_confidence') else 0
        conf_str = f" | S-conf:{conf:.0%}" if conf > 0 else ""
        halt_str = " ⛔ HALTED" if halted else ""

        logger.info(f"  {ticker} ${price:.2f} [{bar}] {pos_pct:.0f}% | Supp=${supp:.2f} Res=${res:.2f}{trend_str}{conf_str}{halt_str}")

    # ---- Internal ----

    def _send(
        self,
        title: str,
        body: str,
        category: str,
        macos: bool = False,
        remote: bool = False,
    ) -> None:
        """Send notification through all enabled channels."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        # Console
        if self.console_enabled:
            self._console_out(timestamp, title, body, category)

        # macOS Notification Center
        if self.macos_enabled and macos:
            self._macos_notify(title, body)

        # Webhook (Discord/Slack/WeCom)
        if self.webhook_url and remote:
            self._webhook_send(title, body, category)

        # Telegram
        if self._telegram_enabled and remote:
            self._telegram_send(title, body)

    def _console_out(self, timestamp: str, title: str, body: str, category: str) -> None:
        """Rich console output with colors."""
        colors = {
            "signal": "\033[96m",   # Cyan
            "trade": "\033[92m",    # Green
            "error": "\033[91m",    # Red
            "warning": "\033[93m",  # Yellow
            "halt": "\033[91m\033[1m",  # Bold Red
            "summary": "\033[95m",  # Magenta
        }
        reset = "\033[0m"
        color = colors.get(category, "")

        print(f"{color}[{timestamp}] {title}{reset}")
        print(f"{color}         {body}{reset}")

    def _macos_notify(self, title: str, body: str) -> None:
        """Send macOS native notification via osascript."""
        try:
            safe_body = body.replace('"', '\\"')
            safe_title = title.replace('"', '\\"')
            script = f'''
            display notification "{safe_body}" with title "{safe_title}" sound name "Glass"
            '''
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass  # Non-critical, don't crash

    def _webhook_send(self, title: str, body: str, category: str) -> None:
        """Send webhook notification (Discord/Slack format)."""
        try:
            payload = {
                "embeds": [{
                    "title": title,
                    "description": body,
                    "color": {
                        "signal": 3447003,   # Blue
                        "trade": 3066993,    # Green
                        "error": 15158332,   # Red
                        "warning": 16776960, # Yellow
                        "summary": 10181046, # Purple
                    }.get(category, 0),
                    "timestamp": datetime.now().isoformat(),
                }]
            }
            requests.post(
                self.webhook_url,
                json=payload,
                timeout=5,
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            pass  # Non-critical

    def _telegram_send(self, title: str, body: str) -> None:
        """Send notification via Telegram Bot API using HTML formatting."""
        try:
            _telegram_rate_limit()
            # Escape HTML special chars to avoid formatting errors
            safe_title = (title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_body = (body or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            text = f"<b>{safe_title}</b>\n{safe_body}"
            resp = requests.post(
                f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self._telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=8,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
                # Retry with plain text
                try:
                    _telegram_rate_limit()
                    resp2 = requests.post(
                        f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage",
                        json={
                            "chat_id": self._telegram_chat_id,
                            "text": f"{title}\n{body}",
                            "disable_web_page_preview": True,
                        },
                        timeout=8,
                    )
                    if not resp2.ok:
                        logger.warning("Telegram plain retry also failed: %s %s", resp2.status_code, resp2.text[:200])
                except Exception as e2:
                    logger.warning("Telegram plain retry error: %s", e2)
        except Exception as e:
            logger.warning("Telegram send error: %s", e)

    @staticmethod
    def _make_bar(pct: float, width: int = 20) -> str:
        """Make a visual progress bar for price position in range."""
        filled = max(0, min(width, int(pct / 100 * width)))
        empty = width - filled
        return "█" * filled + "░" * empty


def _load_notification_config() -> dict:
    notifications: dict = {}
    for path in (
        PROJECT_DIR / "config.yaml",
        PROJECT_DIR / "config.local.yaml",
    ):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        section = payload.get("notifications")
        if isinstance(section, dict):
            notifications.update(section)
    return notifications


def _load_ai_selector_notification_config() -> dict:
    notifications = _load_notification_config()
    ai_section = notifications.get("ai_selector")
    if isinstance(ai_section, dict):
        merged = dict(notifications)
        merged.update(
            {
                "ai_selector_webhook_url": ai_section.get("webhook_url", merged.get("ai_selector_webhook_url")),
                "ai_selector_telegram_bot_token": ai_section.get("telegram_bot_token", merged.get("ai_selector_telegram_bot_token")),
                "ai_selector_telegram_chat_id": ai_section.get("telegram_chat_id", merged.get("ai_selector_telegram_chat_id")),
            }
        )
        return merged
    return notifications


def _truncate_reason(value: object, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _first_non_empty(*values: object, default: object = "") -> object:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        return value
    return default


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ASIA_SHANGHAI)
    return dt


def _format_datetime_in_timezone(value: object, tz: ZoneInfo, *, suffix: str) -> str:
    dt = _parse_datetime(value)
    if dt is None:
        return "未知"
    try:
        return dt.astimezone(tz).strftime(f"%Y-%m-%d %H:%M {suffix}")
    except Exception:
        return "未知"


def _current_notification_sent_at() -> datetime:
    return datetime.now(tz=ASIA_SHANGHAI)


_SHORTFALL_REASON_LABELS = {
    "low_dollar_volume": "低成交额",
    "price_out_of_range": "价格超出范围",
    "quote_missing": "报价缺失",
    "ohlcv_missing": "历史行情缺失",
    "benchmark_invalid": "基准数据无效",
    "freshness_invalid": "数据时效无效",
    "history_insufficient": "历史数据不足",
    "entry_quality_too_low": "入场质量不足",
    "leveraged_etf_limit_exceeded": "杠杆ETF数量限制",
    "composition_limit": "组合约束",
    "top_n_not_filled": "正式候选不足",
    "unknown": "其他原因",
}


def _selection_shortfall_reasons(report: dict, *, limit: int = 3) -> list[str]:
    counts = report.get("rejection_reason_counts")
    if not isinstance(counts, dict):
        counts = dict((report.get("quality_filter_report") or {}).get("rejection_reason_counts") or {})
    items: list[tuple[str, int]] = []
    for raw_code, raw_count in counts.items():
        code = str(raw_code or "").strip().lower()
        if not code or code == "top_n_not_filled":
            continue
        try:
            count = int(raw_count or 0)
        except Exception:
            continue
        if count <= 0:
            continue
        items.append((code, count))
    items.sort(key=lambda item: (-item[1], item[0]))
    summary: list[str] = []
    for code, count in items[:limit]:
        label = _SHORTFALL_REASON_LABELS.get(code, code)
        summary.append(f"{label}：{count}")
    return summary


def _resolve_manifest_first_selection_payload(
    selection_report: dict,
    top_configs: list | None,
) -> tuple[dict, list[dict], str]:
    report = dict(selection_report or {})
    source = "missing"
    committed = None
    try:
        committed = load_committed_selection_bundle(PROJECT_DIR)
    except Exception:
        committed = None

    if isinstance(committed, dict):
        committed_report = committed.get("report")
        if isinstance(committed_report, dict):
            report = dict(committed_report)
            source = "selection_bundle" if report.get("selection_date") else "missing"

    if source == "missing":
        if report.get("selection_date"):
            source = "report_payload"
        elif report.get("date"):
            source = "legacy_date"

    raw_top_items = list(report.get("top3") or report.get("top5") or [])
    if not raw_top_items:
        raw_top_items = list(top_configs or [])
    top_items = [_merge_top_item_with_report(dict(item or {}), report, rank) for rank, item in enumerate(raw_top_items, start=1)]
    return report, top_items, source


def build_provider_audit_sections(
    provider_audit: dict[str, dict] | None,
    provider_outputs: dict[str, dict] | None,
) -> dict[str, str]:
    return provider_audit_sections(provider_audit, provider_outputs)


def build_research_admission_notice(
    execution_status: str | None,
    result_quality: str | None,
    research_admission: str | None,
    mock_used: bool,
    data_status: str | None,
) -> str:
    execution = str(execution_status or "").strip().upper() or "COMPLETED"
    quality = str(result_quality or "").strip().upper() or "COMPLETE"
    admission = str(research_admission or "").strip().upper() or "RESEARCH_READY"
    if admission == "BLOCKED" or quality == "INVALID":
        return "本次流程已完成，但结果数据无效或包含不可接受的降级数据。当前仅允许排障和重新补数。不得进入 Backtest、Walk-Forward、Paper 或 Live。"
    if admission == "RESEARCH_ONLY" or quality == "DEGRADED" or mock_used:
        return "本次结果仅供研究，必须通过独立真实数据验证后，才可进入 Backtest 或 Paper。不得直接进入 Live。"
    if admission == "RESEARCH_READY":
        return "候选可进入独立数据验证，不代表具备交易资格。"
    if execution == "FAILED":
        return "本次流程已完成，但执行失败或结果不可用。当前仅允许排障和重新补数。不得进入 Backtest、Walk-Forward、Paper 或 Live。"
    return "候选可进入独立数据验证，不代表具备交易资格。"


def _selection_report_top_items(selection_report: dict) -> list[dict]:
    report = dict(selection_report or {})
    items = list(report.get("top3") or report.get("top5") or [])
    return [dict(item) for item in items if isinstance(item, dict)]


def _merge_top_item_with_report(top_item: dict, selection_report: dict, rank: int) -> dict:
    merged = dict(top_item or {})
    report_items = _selection_report_top_items(selection_report)
    report_item = None
    ticker = str(merged.get("ticker") or "").strip().upper()
    if ticker:
        for item in report_items:
            if str(item.get("ticker") or "").strip().upper() == ticker:
                report_item = item
                break
    if report_item is None and 0 <= rank - 1 < len(report_items):
        report_item = report_items[rank - 1]

    if not report_item:
        return merged

    merged.setdefault("ticker", report_item.get("ticker"))
    for key in (
        "ai_score",
        "range_score",
        "final_score",
        "score",
        "confidence",
        "reason",
        "source",
        "leveraged_etf",
        "trade_filter_passed",
        "fallback_used",
        "candidate_fallback",
        "fallback_sources",
        "mock_used",
        "mock_sources",
        "degraded",
        "degradation_reasons",
        "data_mode",
        "data_freshness",
        "data_status",
        "scoring_eligible",
        "scoring_block_reason",
        "current_validation_status",
        "trade_admission_status",
        "current_price",
        "size",
        "selection_penalty_reason",
        "reject_reason",
        "composition_filter_passed",
        "composition_reject_reason",
        "final_rank",
    ):
        value = _first_non_empty(merged.get(key), report_item.get(key), default=None)
        if value is not None:
            merged[key] = value

    merged_selection = dict(merged.get("selection") or {})
    report_selection = dict(report_item.get("selection") or {})
    for key in (
        "selection_date",
        "ai_score",
        "range_score",
        "final_score",
        "score",
        "confidence",
        "reason",
        "source",
        "leveraged_etf",
        "trade_filter_passed",
        "fallback_used",
        "candidate_fallback",
        "fallback_sources",
        "mock_used",
        "mock_sources",
        "degraded",
        "degradation_reasons",
        "data_mode",
        "data_freshness",
        "data_status",
        "scoring_eligible",
        "scoring_block_reason",
        "current_validation_status",
        "trade_admission_status",
        "reject_reason",
        "composition_filter_passed",
        "composition_reject_reason",
        "final_rank",
    ):
        value = _first_non_empty(merged_selection.get(key), report_selection.get(key), merged.get(key), report_item.get(key), default=None)
        if value is not None:
            merged_selection[key] = value
    if merged_selection:
        merged["selection"] = merged_selection

    merged_allocation = dict(merged.get("allocation") or {})
    report_allocation = dict(report_item.get("allocation") or {})
    for key in ("target_capital", "target_shares", "weight", "atr_pct", "risk_pct", "reason"):
        value = _first_non_empty(merged_allocation.get(key), report_allocation.get(key), default=None)
        if value is not None:
            merged_allocation[key] = value
    if merged_allocation:
        merged["allocation"] = merged_allocation

    return merged


def _ticker_line(top_config: dict, rank: int) -> str:
    selection = dict(top_config.get("selection") or {})
    allocation = dict(top_config.get("allocation") or {})
    ticker = str(top_config.get("ticker") or f"TOP{rank}")
    final_score = _first_non_empty(selection.get("final_score"), selection.get("score"), top_config.get("final_score"), top_config.get("score"), default="-")
    ai_score = _first_non_empty(selection.get("ai_score"), top_config.get("ai_score"), default="-")
    range_score = _first_non_empty(selection.get("range_score"), top_config.get("range_score"), default="-")
    leveraged = bool(_first_non_empty(selection.get("leveraged_etf"), top_config.get("leveraged_etf"), default=False))
    filter_passed = bool(_first_non_empty(selection.get("trade_filter_passed"), top_config.get("trade_filter_passed"), default=False))
    fallback_used = bool(_first_non_empty(selection.get("fallback_used"), top_config.get("fallback_used"), default=False))
    candidate_fallback = bool(_first_non_empty(selection.get("candidate_fallback"), top_config.get("candidate_fallback"), default=False))
    mock_used = bool(_first_non_empty(selection.get("mock_used"), top_config.get("mock_used"), default=False))
    data_status = str(_first_non_empty(selection.get("data_status"), top_config.get("data_status"), default="")).strip().upper()
    validation_status = str(_first_non_empty(selection.get("current_validation_status"), top_config.get("current_validation_status"), top_config.get("validation_status"), default="")).strip().upper()
    trade_admission_status = str(_first_non_empty(selection.get("trade_admission_status"), top_config.get("trade_admission_status"), default="")).strip().upper()
    fallback_sources = _first_non_empty(selection.get("fallback_sources"), top_config.get("fallback_sources"), default=[])
    mock_sources = _first_non_empty(selection.get("mock_sources"), top_config.get("mock_sources"), default=[])
    reason = _truncate_reason(
        _first_non_empty(
            selection.get("reason"),
            top_config.get("reason"),
            top_config.get("selection_penalty_reason"),
            top_config.get("fallback_reason"),
            default="",
        )
    )
    current_price = float(_first_non_empty(top_config.get("current_price"), top_config.get("price"), top_config.get("price_midpoint_hint"), default=0.0) or 0.0)
    target_shares = int(_first_non_empty(allocation.get("target_shares"), top_config.get("target_shares"), top_config.get("size_per_trade"), top_config.get("size"), default=0) or 0)
    target_capital = float(_first_non_empty(allocation.get("target_capital"), top_config.get("target_capital"), default=0.0) or 0.0)
    if target_capital <= 0 and current_price > 0 and target_shares > 0:
        target_capital = current_price * target_shares
    universe_filter_text = "通过" if filter_passed else "拒绝"
    data_sufficiency_text = "通过" if data_status == "VALID" else "失败"
    scoring_eligible_text = "是" if bool(_first_non_empty(selection.get("scoring_eligible"), top_config.get("scoring_eligible"), default=False)) else "否"
    kind = "杠杆/反向ETF" if leveraged else "普通标的"
    fallback_text = "是" if candidate_fallback else "否"
    fallback_source_text = " / ".join(str(item).strip().upper() for item in (fallback_sources or []) if str(item).strip())
    mock_source_text = " / ".join(str(item).strip().upper() for item in (mock_sources or []) if str(item).strip())
    fallback_scope = str(_first_non_empty(selection.get("fallback_scope"), top_config.get("fallback_scope"), default="")).strip().upper()
    fallback_severity = str(_first_non_empty(selection.get("fallback_severity"), top_config.get("fallback_severity"), default="")).strip().upper()
    affected_fields = _first_non_empty(selection.get("affected_fields"), top_config.get("affected_fields"), default=[])
    lines = [
        f"TOP{rank}：{ticker}",
        f"分数：final {final_score} / AI {ai_score} / Range {range_score}",
        f"类型：{kind}",
        f"仓位：${target_capital:.0f} / {target_shares}股",
        f"Universe Filter：{universe_filter_text}",
        f"Data Sufficiency：{data_sufficiency_text}",
        f"Scoring Eligible：{scoring_eligible_text}",
        f"Trade Admission：{trade_admission_status or 'NOT_TRADABLE'}",
        f"fallback：{fallback_text}",
        f"状态：{validation_status or 'AI_CANDIDATE'}",
        f"数据：{data_status or 'UNKNOWN'} · candidate_fallback={'是' if candidate_fallback else '否'} · mock={'是' if mock_used else '否'}",
    ]
    if fallback_source_text:
        lines.append(f"fallback来源：{fallback_source_text}")
    if mock_source_text:
        lines.append(f"mock来源：{mock_source_text}")
    if fallback_scope:
        lines.append(f"fallback范围：{fallback_scope}")
    if fallback_severity:
        lines.append(f"fallback级别：{fallback_severity}")
    if affected_fields:
        fields = ", ".join(str(field) for field in affected_fields if str(field).strip())
        if fields:
            lines.append(f"fallback影响：{fields}")
    lines.append(f"理由：{reason or '无'}")
    return "\n".join(lines)


def _build_ai_selection_message(selection_report: dict, top_configs: list | None = None) -> tuple[str, str]:
    report, top_items, selection_date_source = _resolve_manifest_first_selection_payload(selection_report, top_configs)
    selection_date = str(report.get("selection_date") or "").strip()
    legacy_date = str(report.get("date") or "").strip()
    selection_date_display = selection_date or legacy_date or "未知"
    notification_sent_at = _current_notification_sent_at()
    generated_at_text = _format_datetime_in_timezone(report.get("generated_at"), US_EASTERN, suffix="ET")
    notification_sent_at_text = notification_sent_at.strftime("%Y-%m-%d %H:%M 北京时间")
    selection_stage = str(report.get("selection_stage") or report.get("market_selection_stage") or report.get("settings", {}).get("selection_stage") or "").strip().upper()
    last_completed_session = str(report.get("last_completed_session") or report.get("previous_completed_session") or "")
    daily_data_as_of = str(report.get("daily_data_as_of") or "")
    premarket_snapshot_at = str(report.get("premarket_snapshot_at") or "")
    freshness_status = str(report.get("freshness_status") or "").strip().upper()
    stale_reason = str(report.get("stale_reason") or "").strip()
    requested_top_n = int(_first_non_empty(report.get("requested_top_n"), report.get("target_top_n"), 3, default=3) or 3)
    selected_top_n = int(_first_non_empty(report.get("selected_top_n"), report.get("selection_count"), len(top_items), default=len(top_items)) or 0)
    missing_count = max(0, requested_top_n - selected_top_n)
    missing_slots = [f"TOP{i}" for i in range(selected_top_n + 1, requested_top_n + 1)] if missing_count > 0 else []
    fallback_used = bool(report.get("fallback_used", False))
    execution_status = str(report.get("execution_status") or "").strip().upper()
    result_quality = str(report.get("result_quality") or "").strip().upper()
    research_admission = str(report.get("research_admission") or "").strip().upper()
    provider_audit_sections = build_provider_audit_sections(
        dict(report.get("provider_audit") or {}),
        dict(report.get("provider_outputs") or {}),
    )
    warnings = list(report.get("warnings") or [])
    quality_report = dict(report.get("quality_filter_report") or {})
    warnings.extend(quality_report.get("warnings") or [])
    warnings.extend((report.get("composition_filter") or {}).get("warnings") or [])
    warnings = list(dict.fromkeys(str(item) for item in warnings if str(item).strip()))
    if not execution_status:
        execution_status = "COMPLETED" if top_items else "FAILED"
    if not result_quality:
        # Legacy payloads without explicit quality semantics are not proof of a
        # complete result. Keep fallback-only reports usable for research, but
        # fail closed for otherwise unclassified payloads.
        result_quality = "DEGRADED" if fallback_used else "INVALID"
        warnings.append("result_quality_missing")
    if not research_admission:
        research_admission = "RESEARCH_ONLY" if result_quality == "DEGRADED" else ("BLOCKED" if result_quality == "INVALID" else "RESEARCH_READY")
        warnings.append("research_admission_missing")
    if not selection_date and selection_date_source == "missing":
        warnings.insert(0, "selection_date_missing")
    structured_warnings = list(report.get("warnings_structured") or quality_report.get("warnings_structured") or [])
    if selected_top_n < requested_top_n and not any(
        str(item.get("warning_code") or item.get("code") or "").strip().lower() == "top_n_not_filled"
        for item in structured_warnings
        if isinstance(item, dict)
    ):
        structured_warnings.append(
            {
                "warning_code": "top_n_not_filled",
                "stage": "FINALIZED",
                "requested_count": requested_top_n,
                "selected_count": selected_top_n,
                "missing_count": missing_count,
                "selected_symbols": [str(item.get("ticker") or "").strip().upper() for item in top_items if str(item.get("ticker") or "").strip()],
                "missing_slots": list(missing_slots),
                "details": "final TOP still below requested count",
            }
        )
    if structured_warnings:
        unique_warnings = []
        seen = set()
        for item in structured_warnings:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("warning_code") or item.get("code") or "warning"),
                str(item.get("stage") or "").strip().upper(),
                item.get("requested_count"),
                item.get("selected_count"),
                item.get("missing_count"),
                tuple(item.get("selected_symbols") or item.get("symbols") or []),
                tuple(item.get("missing_slots") or []),
                str(item.get("details") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            unique_warnings.append(item)
        warnings = []
        for item in unique_warnings:
            parts = [str(item.get("warning_code") or item.get("code") or "warning")]
            stage = str(item.get("stage") or "").strip().upper() or "FINALIZED"
            parts.append(f"stage={stage}")
            if item.get("requested_count") is not None:
                parts.append(f"requested={item.get('requested_count')}")
            if item.get("selected_count") is not None:
                parts.append(f"selected={item.get('selected_count')}")
            if item.get("missing_count") is not None:
                parts.append(f"missing={item.get('missing_count')}")
            selected_symbols = item.get("selected_symbols") or item.get("symbols") or []
            if selected_symbols:
                parts.append(f"selected_symbols={','.join(str(sym).strip().upper() for sym in selected_symbols if str(sym).strip())}")
            missing_slots = item.get("missing_slots") or []
            if not missing_slots and item.get("warning_code") == "top_n_not_filled":
                try:
                    selected_count = int(item.get("selected_count") or 0)
                    requested_count = int(item.get("requested_count") or 0)
                except Exception:
                    selected_count = 0
                    requested_count = 0
                if requested_count > selected_count >= 0:
                    missing_slots = [f"TOP{i}" for i in range(selected_count + 1, requested_count + 1)]
            if missing_slots:
                parts.append(f"missing_slots={','.join(str(slot).strip().upper() for slot in missing_slots if str(slot).strip())}")
            details = str(item.get("details") or "").strip()
            if details:
                parts.append(details)
            warnings.append(" | ".join(parts))
    if selection_date_source == "missing" and "selection_date_missing" not in warnings:
        warnings.insert(0, "selection_date_missing")
    execution_text = "已完成" if execution_status != "FAILED" else "已失败"
    result_text = {"COMPLETE": "完整", "DEGRADED": "降级", "INVALID": "无效"}.get(result_quality, result_quality or "未知")
    admission_text = {"RESEARCH_READY": "已就绪", "RESEARCH_ONLY": "仅研究", "BLOCKED": "已阻止"}.get(research_admission, research_admission or "未知")
    notice = build_research_admission_notice(
        execution_status,
        result_quality,
        research_admission,
        bool(report.get("mock_used", False)),
        report.get("data_status") or (top_items[0].get("data_status") if top_items else ""),
    )
    shortfall_reasons = _selection_shortfall_reasons(report)
    lines = [
        f"选股数据日：{selection_date_display}{'（美东交易日）' if selection_date_display != '未知' else ''}",
        f"选股日期来源：{selection_date_source}",
        f"结果生成：{generated_at_text}",
        f"通知发送：{notification_sent_at_text}",
        f"流程：{selection_stage or 'UNKNOWN'}",
        f"正式TOP：{selected_top_n}/{requested_top_n}",
        f"缺失槽位：{missing_count}{'（' + ', '.join(missing_slots) + '）' if missing_slots else ''}",
        f"执行状态：{execution_text} ({execution_status or 'COMPLETED'})",
        f"结果质量：{result_text} ({result_quality or ('DEGRADED' if fallback_used else 'COMPLETE')})",
        f"研究准入：{admission_text} ({research_admission or ('RESEARCH_ONLY' if fallback_used else 'RESEARCH_READY')})",
        f"交易含义：{notice}",
        "",
    ]
    if last_completed_session:
        lines.append(f"上一完整交易日：{last_completed_session}")
    if daily_data_as_of:
        lines.append(f"日线截至：{daily_data_as_of}")
    if premarket_snapshot_at:
        lines.append(f"盘前快照：{premarket_snapshot_at}")
    if freshness_status:
        lines.append(f"新鲜度：{freshness_status}")
    if stale_reason:
        lines.append(f"原因：{stale_reason}")
    if shortfall_reasons and missing_count > 0:
        lines.append("候选不足主要原因：")
        lines.extend([f"- {item}" for item in shortfall_reasons[:3]])
    if last_completed_session or daily_data_as_of or premarket_snapshot_at or freshness_status or stale_reason:
        lines.append("")

    for rank in range(1, requested_top_n + 1):
        if rank <= len(top_items):
            lines.append(_ticker_line(dict(top_items[rank - 1] or {}), rank))
        else:
            reason = "top_n_not_filled" if selected_top_n < requested_top_n else "未生成"
            lines.append(f"TOP{rank}：未生成 / disabled\n原因：{reason}")
        lines.append("")

    lines.extend(
        [
            f"Provider 尝试：{provider_audit_sections['attempted']}",
            f"Provider 成功：{provider_audit_sections['success']}",
            f"Provider 失败：{provider_audit_sections['failure']}",
            f"Provider 超时：{provider_audit_sections['timeout']}",
            f"Provider Fallback：{provider_audit_sections['fallback']}",
            f"Provider Mock：{provider_audit_sections['mock']}",
            f"Provider 实际贡献：{provider_audit_sections['contributor']}",
        ]
    )
    if warnings:
        lines.append("警告：")
        lines.extend([f"- {item}" for item in warnings[:6]])
    lines.append(f"状态解释：{_stage_explanation(selection_stage, result_quality, research_admission)}")
    return "【AI 选股完成】", "\n".join(lines).strip()


def _stage_explanation(selection_stage: str, result_quality: str, research_admission: str) -> str:
    stage = str(selection_stage or "").strip().upper() or "UNKNOWN"
    quality = str(result_quality or "").strip().upper() or "UNKNOWN"
    admission = str(research_admission or "").strip().upper() or "UNKNOWN"
    stage_label = {
        "FINALIZED": "流程已完成",
        "COMPLETE": "核心数据和研究证据完整",
        "DEGRADED": "流程完成，但存在非关键降级或研究证据不足",
        "INVALID": "核心数据无效",
        "RESEARCH_READY": "可进入下一阶段研究",
        "RESEARCH_ONLY": "仅供研究，不代表交易资格",
        "BLOCKED": "阻断后续研究或交易准入",
    }
    parts = [
        f"{stage}={stage_label.get(stage, '状态未知')}",
        f"{quality}={stage_label.get(quality, '状态未知')}",
        f"{admission}={stage_label.get(admission, '状态未知')}",
    ]
    return "；".join(parts)


def notify_ai_selection_result(selection_report: dict, top_configs: list | None = None) -> None:
    notification_cfg = _load_ai_selector_notification_config()
    webhook_url = (
        os.environ.get("SOXS_AI_SELECTOR_WEBHOOK")
        or os.environ.get("AI_SELECTOR_WEBHOOK")
        or notification_cfg.get("ai_selector_webhook_url")
        or notification_cfg.get("webhook_url")
    )
    notifier = Notifier(
        console=False,
        macos_notification=False,
        webhook_url=webhook_url,
        trade_summary_interval=int(notification_cfg.get("trade_summary_interval", 5) or 5),
        telegram_bot_token=(
            notification_cfg.get("ai_selector_telegram_bot_token", "")
            or os.environ.get("SOXS_AI_SELECTOR_TELEGRAM_BOT_TOKEN")
            or os.environ.get("SOXS_TELEGRAM_BOT_TOKEN")
            or notification_cfg.get("telegram_bot_token", "")
        ),
        telegram_chat_id=(
            notification_cfg.get("ai_selector_telegram_chat_id", "")
            or os.environ.get("SOXS_AI_SELECTOR_TELEGRAM_CHAT_ID")
            or os.environ.get("SOXS_TELEGRAM_CHAT_ID")
            or notification_cfg.get("telegram_chat_id", "")
        ),
    )
    title, body = _build_ai_selection_message(selection_report, top_configs)
    if not notifier._telegram_enabled and not notifier.webhook_url:
        logger.info("AI selection notification skipped: Telegram/Webhook not configured")
        return
    try:
        notifier._send(title, body, "summary", macos=False, remote=True)
    except Exception as exc:
        logger.warning("AI selection notification failed: %s", exc)
