"""
Risk manager: enforces all risk control rules.

Rules (in priority order):
1. Stop Loss — if price breaks below support by stop_loss_pct
2. Daily Loss Limit — cumulative P&L for the day
3. Max Consecutive Losses — pause after N losing trades
4. Position Size Limit — never exceed max_position
5. Cool-down — minimum time between trades
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str
    rule_triggered: Optional[str] = None


@dataclass
class TradeRecord:
    """Record of a completed trade."""
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    side: str  # "LONG" or "SHORT" (for SOXS we only do LONG)


class RiskManager:
    """
    Central risk controller. Every trade must pass through check_trade()
    before execution. No exceptions.
    """

    def __init__(
        self,
        stop_loss_pct: float = 2.0,
        daily_loss_limit: float = 500.0,
        max_consecutive_losses: int = 3,
        max_position: int = 300,
        max_drawdown_pct: float = 10.0,
        cool_down_seconds: int = 30,
        state_path: Optional[Path] = None,
    ):
        self.stop_loss_pct = stop_loss_pct
        self.daily_loss_limit = daily_loss_limit
        self.max_consecutive_losses = max_consecutive_losses
        self.max_position = max_position
        self.max_drawdown_pct = max_drawdown_pct
        self.cool_down_seconds = cool_down_seconds
        self._state_path = Path(state_path) if state_path else None

        # State tracking
        self._trade_history: list[TradeRecord] = []
        self._consecutive_losses: int = 0
        self._daily_pnl: dict[str, float] = {}  # date_iso → cumulative P&L
        self._daily_peak_equity: dict[str, float] = {}  # date_iso → peak equity
        self._current_equity: float = 0.0
        self._last_trade_time: Optional[datetime] = None
        self._halted: bool = False
        self._halt_reason: str = ""
        self._halt_until: Optional[datetime] = None
        self._last_state_save_at = 0.0
        self._load_state()

    # ---- Public API ----

    def check_entry(
        self, price: float, shares: int, current_position: int
    ) -> RiskCheckResult:
        """
        Check if a new entry (buy) is allowed.

        Args:
            price: Entry price per share
            shares: Number of shares to buy
            current_position: Current position size

        Returns:
            RiskCheckResult with allowed=True/False
        """
        # 1. Check if trading is halted
        if self._halted:
            if self._halt_until and datetime.now() < self._halt_until:
                return RiskCheckResult(
                    False,
                    f"Trading halted: {self._halt_reason}. "
                    f"Resumes at {self._halt_until.strftime('%H:%M:%S')}",
                    "halt",
                )
            else:
                # Halt period expired — clear halt, reset consecutive losses
                # (the halt was the penalty; don't re-halt immediately)
                self._halted = False
                self._halt_reason = ""
                self._halt_until = None
                self._consecutive_losses = 0
                self._save_state(force=True)
                logger.info("Trading halt expired — consecutive losses reset, resuming")

        # 2. Position size limit
        new_position = current_position + shares
        if new_position > self.max_position:
            return RiskCheckResult(
                False,
                f"Position limit: {new_position} > {self.max_position} shares max",
                "max_position",
            )

        # 3. Cool-down
        if self._last_trade_time:
            elapsed = (datetime.now() - self._last_trade_time).total_seconds()
            if elapsed < self.cool_down_seconds:
                remaining = self.cool_down_seconds - elapsed
                return RiskCheckResult(
                    False,
                    f"Cool-down: {remaining:.0f}s remaining before next trade",
                    "cool_down",
                )

        # 4. Daily loss limit
        today = self._trading_date()
        daily_pnl = self._daily_pnl.get(today, 0)
        if daily_pnl <= -self.daily_loss_limit:
            self._halt_trading(
                f"Daily loss limit reached: ${daily_pnl:.2f} (limit: ${self.daily_loss_limit})"
            )
            return RiskCheckResult(
                False,
                f"Daily loss limit: ${-daily_pnl:.2f} lost today (limit: ${self.daily_loss_limit})",
                "daily_loss_limit",
            )

        # 5. Consecutive losses
        if self._consecutive_losses >= self.max_consecutive_losses:
            self._halt_trading(
                f"{self._consecutive_losses} consecutive losses "
                f"(limit: {self.max_consecutive_losses}), pausing 30 min"
            )
            return RiskCheckResult(
                False,
                f"Consecutive losses: {self._consecutive_losses} in a row, paused 30 min",
                "consecutive_losses",
            )

        # 6. Drawdown check
        if self._current_equity > 0:
            peak = self._daily_peak_equity.get(today, self._current_equity)
            drawdown = (peak - self._current_equity) / peak * 100
            if drawdown > self.max_drawdown_pct:
                self._halt_trading(
                    f"Max drawdown reached: {drawdown:.1f}% "
                    f"(limit: {self.max_drawdown_pct}%)"
                )
                return RiskCheckResult(
                    False,
                    f"Drawdown: {drawdown:.1f}% from peak (limit: {self.max_drawdown_pct}%)",
                    "max_drawdown",
                )

        return RiskCheckResult(True, "OK")

    def check_stop_loss(self, current_price: float, entry_price: float, support: float) -> RiskCheckResult:
        """Check if stop loss is triggered."""
        stop_price = support * (1 - self.stop_loss_pct / 100)
        if current_price <= stop_price:
            return RiskCheckResult(
                False,
                f"STOP LOSS: ${current_price:.2f} <= stop ${stop_price:.2f} "
                f"(entry ${entry_price:.2f}, support ${support:.2f})",
                "stop_loss",
            )
        return RiskCheckResult(True, "OK")

    def record_trade(self, trade: TradeRecord) -> None:
        """Record a completed trade and update state."""
        self._trade_history.append(trade)
        self._last_trade_time = datetime.now()

        # Track P&L
        today = self._trading_date()
        if today not in self._daily_pnl:
            self._daily_pnl[today] = 0
        self._daily_pnl[today] += trade.pnl

        # Update equity tracking
        self._current_equity += trade.pnl
        if today not in self._daily_peak_equity:
            self._daily_peak_equity[today] = self._current_equity
        self._daily_peak_equity[today] = max(
            self._daily_peak_equity[today], self._current_equity
        )

        # Track consecutive losses
        if trade.pnl < 0:
            self._consecutive_losses += 1
            logger.warning(
                f"Loss ${trade.pnl:.2f} — {self._consecutive_losses} consecutive losses"
            )
        else:
            self._consecutive_losses = 0
        self._save_state(force=True)

    def _trading_date(self) -> str:
        """Use ET timezone date so trading day doesn't reset at Beijing midnight."""
        try:
            import pytz
            ny = pytz.timezone("America/New_York")
            return datetime.now(ny).date().isoformat()
        except Exception:
            return date.today().isoformat()

    def get_daily_pnl(self) -> float:
        """Get today's cumulative P&L (ET trading day)."""
        return self._daily_pnl.get(self._trading_date(), 0)

    def get_daily_trades(self) -> int:
        """Count trades executed today (ET trading day)."""
        tday = self._trading_date()
        return sum(
            1 for trade in self._trade_history
            if self._datetime_trading_date(trade.entry_time) == tday
        )

    @staticmethod
    def _datetime_trading_date(value: datetime) -> str:
        """Convert a stored local/aware timestamp to its New York trading date."""
        try:
            import pytz

            if value.tzinfo is None:
                value = value.astimezone()
            return value.astimezone(pytz.timezone("America/New_York")).date().isoformat()
        except Exception:
            return value.date().isoformat()

    def get_stats(self) -> dict:
        """Get summary statistics."""
        trades = self._trade_history
        if not trades:
            return {
                "total_trades": 0,
                "daily_trades_today": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_pnl": 0,
                "daily_pnl_today": round(self.get_daily_pnl(), 2),
                "consecutive_losses": self._consecutive_losses,
                "halted": self._halted,
            }

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        total_pnl = sum(t.pnl for t in trades)

        return {
            "total_trades": len(trades),
            "daily_trades_today": self.get_daily_trades(),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
            "best_trade": round(max(t.pnl for t in trades), 2) if trades else 0,
            "worst_trade": round(min(t.pnl for t in trades), 2) if trades else 0,
            "daily_pnl_today": round(self.get_daily_pnl(), 2),
            "consecutive_losses": self._consecutive_losses,
            "halted": self._halted,
        }

    def _halt_trading(self, reason: str) -> None:
        """Halt trading with a reason."""
        self._halted = True
        self._halt_reason = reason
        self._halt_until = datetime.now().replace(second=0, microsecond=0)
        # 30 minute halt
        from datetime import timedelta
        self._halt_until += timedelta(minutes=30)
        self._save_state(force=True)
        logger.critical(f"⚠️  TRADING HALTED: {reason}")

    def update_equity(self, current_equity: float) -> None:
        """Sync equity from broker. Call periodically (every loop iteration)."""
        if self._current_equity <= 0 and current_equity > 0:
            # First sync: seed with actual account value
            self._current_equity = current_equity
            today = self._trading_date()
            self._daily_peak_equity[today] = current_equity
        else:
            self._current_equity = current_equity
        self._save_state()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._consecutive_losses = int(data.get("consecutive_losses", 0) or 0)
            self._daily_pnl = {
                str(k): float(v) for k, v in (data.get("daily_pnl") or {}).items()
            }
            self._daily_peak_equity = {
                str(k): float(v)
                for k, v in (data.get("daily_peak_equity") or {}).items()
            }
            self._current_equity = float(data.get("current_equity", 0.0) or 0.0)
            self._halted = bool(data.get("halted", False))
            self._halt_reason = str(data.get("halt_reason", "") or "")
            if data.get("halt_until"):
                self._halt_until = datetime.fromisoformat(data["halt_until"])
            if data.get("last_trade_time"):
                self._last_trade_time = datetime.fromisoformat(data["last_trade_time"])
            self._trade_history = [
                TradeRecord(
                    entry_time=datetime.fromisoformat(row["entry_time"]),
                    exit_time=datetime.fromisoformat(row["exit_time"]),
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["exit_price"]),
                    shares=int(row["shares"]),
                    pnl=float(row["pnl"]),
                    pnl_pct=float(row["pnl_pct"]),
                    side=str(row["side"]),
                )
                for row in (data.get("trade_history") or [])
            ]
        except Exception as exc:
            logger.error("Risk state is invalid at %s: %s", self._state_path, exc)
            self._halted = True
            self._halt_reason = "risk state invalid"
            self._halt_until = None

    def _save_state(self, force: bool = False) -> None:
        if self._state_path is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_state_save_at) < 30.0:
            return
        payload = {
            "consecutive_losses": self._consecutive_losses,
            "daily_pnl": self._daily_pnl,
            "daily_peak_equity": self._daily_peak_equity,
            "current_equity": self._current_equity,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halt_until": self._halt_until.isoformat() if self._halt_until else None,
            "last_trade_time": (
                self._last_trade_time.isoformat() if self._last_trade_time else None
            ),
            "trade_history": [
                {
                    "entry_time": trade.entry_time.isoformat(),
                    "exit_time": trade.exit_time.isoformat(),
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "shares": trade.shares,
                    "pnl": trade.pnl,
                    "pnl_pct": trade.pnl_pct,
                    "side": trade.side,
                }
                for trade in self._trade_history[-500:]
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._state_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self._state_path)
            self._last_state_save_at = now
        except OSError as exc:
            logger.error("Could not persist risk state at %s: %s", self._state_path, exc)
