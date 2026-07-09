"""
Notification system: sends alerts via console, macOS notifications, webhooks, and Telegram.
"""
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]

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
        prefix = "实盘" if mode == "live" else "模拟"
        side_cn = "买入" if side.upper() == "BUY" else "卖出"
        emoji = "🟢" if side.upper() == "BUY" else "🔴"
        trade_value = price * quantity
        sign = "+" if side.upper() == "BUY" else "-"

        pnl_str = f" | 盈亏 ${pnl:+.2f}" if pnl is not None else ""
        title = f"{emoji} {prefix}{side_cn} {ticker} {quantity}股"
        body = f"{sign}${trade_value:,.2f} @ ${price:.2f}{pnl_str}"

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
    filter_text = "通过" if filter_passed else "未通过"
    kind = "杠杆/反向ETF" if leveraged else "普通标的"
    fallback_text = "是" if fallback_used else "否"
    return (
        f"TOP{rank}：{ticker}\n"
        f"分数：final {final_score} / AI {ai_score} / Range {range_score}\n"
        f"类型：{kind}\n"
        f"仓位：${target_capital:.0f} / {target_shares}股\n"
        f"过滤：{filter_text}\n"
        f"fallback：{fallback_text}\n"
        f"理由：{reason or '无'}"
    )


def _build_ai_selection_message(selection_report: dict, top_configs: list | None = None) -> tuple[str, str]:
    report = dict(selection_report or {})
    date_str = str(report.get("selection_date") or report.get("date") or datetime.now().date().isoformat())
    raw_top_items = list(top_configs or report.get("top3") or report.get("top5") or [])
    top_items = [
        _merge_top_item_with_report(dict(item or {}), report, rank)
        for rank, item in enumerate(raw_top_items, start=1)
    ]
    target_top_n = int(report.get("target_top_n") or 3)
    selection_count = int(report.get("selection_count") or len(top_items))
    fallback_used = bool(report.get("fallback_used", False))
    providers_used = ", ".join(report.get("providers_used") or []) or "无"
    providers_disabled = ", ".join(report.get("providers_disabled") or []) or "无"
    warnings = list(report.get("warnings") or [])
    quality_report = dict(report.get("quality_filter_report") or {})
    warnings.extend(quality_report.get("warnings") or [])
    warnings.extend((report.get("composition_filter") or {}).get("warnings") or [])
    warnings = [str(item) for item in warnings if str(item).strip()]
    status = "成功" if top_items else "失败"
    fallback_text = "true" if fallback_used else "false"
    lines = [
        f"日期：{date_str}",
        f"状态：{status}",
        f"TOP数量：{selection_count}/{target_top_n}",
        f"fallback：{fallback_text}",
        "",
    ]

    for rank in range(1, target_top_n + 1):
        if rank <= len(top_items):
            lines.append(_ticker_line(dict(top_items[rank - 1] or {}), rank))
        else:
            reason = "top_n_not_filled" if selection_count < target_top_n else "未生成"
            lines.append(f"TOP{rank}：未生成 / disabled\n原因：{reason}")
        lines.append("")

    lines.extend(
        [
            f"Provider：使用：{providers_used}",
            f"禁用：{providers_disabled}",
        ]
    )
    if warnings:
        lines.append("警告：")
        lines.extend([f"- {item}" for item in warnings[:6]])
    if fallback_used:
        lines.append("注意：本次包含 fallback/mock 数据，仅建议 paper 验证，不建议直接 live。")
    return "【AI 选股完成】", "\n".join(lines).strip()


def notify_ai_selection_result(selection_report: dict, top_configs: list | None = None) -> None:
    notification_cfg = _load_notification_config()
    webhook_url = os.environ.get("AI_SELECTOR_WEBHOOK") or notification_cfg.get("webhook_url")
    notifier = Notifier(
        console=False,
        macos_notification=False,
        webhook_url=webhook_url,
        trade_summary_interval=int(notification_cfg.get("trade_summary_interval", 5) or 5),
        telegram_bot_token=(
            os.environ.get("SOXS_TELEGRAM_BOT_TOKEN")
            or notification_cfg.get("telegram_bot_token", "")
        ),
        telegram_chat_id=(
            os.environ.get("SOXS_TELEGRAM_CHAT_ID")
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
