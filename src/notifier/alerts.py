"""
Notification system: sends alerts via console, macOS notifications, and webhooks.
"""
import json
import logging
import subprocess
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class Notifier:
    """Multi-channel notification system for trading signals and events."""

    def __init__(
        self,
        console: bool = True,
        macos_notification: bool = True,
        webhook_url: Optional[str] = None,
        trade_summary_interval: int = 5,
    ):
        self.console_enabled = console
        self.macos_enabled = macos_notification
        self.webhook_url = webhook_url
        self.trade_summary_interval = trade_summary_interval

        self._trade_count_since_summary = 0
        self._last_trades: list[str] = []

    # ---- Public API ----

    def signal(self, ticker: str, signal_type: str, price: float, reason: str) -> None:
        """Send a trading signal notification."""
        title = f"📊 {ticker} — {signal_type}"
        body = f"${price:.2f} | {reason}"

        self._send(title, body, "signal")

    def trade(self, ticker: str, side: str, quantity: int, price: float, pnl: Optional[float] = None) -> None:
        """Notify about an executed trade."""
        pnl_str = f" | P&L: ${pnl:+.2f}" if pnl is not None else ""
        title = f"💱 {side} {quantity} {ticker}"
        body = f"@ ${price:.2f}{pnl_str}"

        self._send(title, body, "trade")

        # Track for summary
        self._trade_count_since_summary += 1
        self._last_trades.append(body)

    def alert(self, message: str, level: str = "info") -> None:
        """General alert (errors, warnings, halts)."""
        emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🚨", "halt": "🛑"}
        title = f"{emoji.get(level, 'ℹ️')} Trading Alert"
        self._send(title, message, level)

    def summary(self, stats: dict) -> None:
        """Send a trading summary."""
        title = "📈 Trading Summary"
        body = (
            f"Trades: {stats.get('total_trades', 0)} | "
            f"Win Rate: {stats.get('win_rate', 0)}% | "
            f"P&L: ${stats.get('total_pnl', 0):+.2f} | "
            f"Today: ${stats.get('daily_pnl_today', 0):+.2f}"
        )
        self._send(title, body, "summary")

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

    def _send(self, title: str, body: str, category: str) -> None:
        """Send notification through all enabled channels."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        # Console
        if self.console_enabled:
            self._console_out(timestamp, title, body, category)

        # macOS Notification Center
        if self.macos_enabled:
            self._macos_notify(title, body)

        # Webhook (Discord/Slack/WeCom)
        if self.webhook_url:
            self._webhook_send(title, body, category)

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
            script = f'''
            display notification "{body}" with title "{title}" sound name "Glass"
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

    @staticmethod
    def _make_bar(pct: float, width: int = 20) -> str:
        """Make a visual progress bar for price position in range."""
        filled = max(0, min(width, int(pct / 100 * width)))
        empty = width - filled
        return "█" * filled + "░" * empty
