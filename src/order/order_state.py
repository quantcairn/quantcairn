"""
Order state tracking with dedup, cooldown, and failed-order bookkeeping.

Responsibilities:
  - Track per-ticker order status (PENDING/FILLED/REJECTED/CANCELLED)
  - Maintain a blocked-buy list with cooldown timestamps
  - Record failed orders for audit and dashboard display
  - Persist state across restarts (for cooldown persistence)
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from src.config.runtime_paths import resolve_state_dir

logger = logging.getLogger(__name__)
SYNTHETIC_TEST_TICKERS = {"TEST", "MOCK", "FAKE"}

# Default state directory — matches engine convention
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DEFAULT_STATE_DIR = resolve_state_dir(Path(PROJECT_DIR))


class OrderState(Enum):
    """Terminal and non-terminal order states."""
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"


@dataclass
class FailedOrder:
    """Record of a single rejected/failed order for audit and dashboard."""
    ticker: str
    timestamp: datetime
    reason: str
    quantity: int
    price: float
    buying_power: float

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
            "quantity": self.quantity,
            "price": self.price,
            "buying_power": self.buying_power,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FailedOrder":
        return cls(
            ticker=d["ticker"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            reason=d["reason"],
            quantity=d.get("quantity", 0),
            price=d.get("price", 0.0),
            buying_power=d.get("buying_power", 0.0),
        )


@dataclass
class BlockedTicker:
    """A ticker that is temporarily blocked from new BUY orders."""
    ticker: str
    blocked_until: datetime
    reason: str
    buying_power_at_block: float

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, (self.blocked_until - datetime.now()).total_seconds())

    @property
    def is_expired(self) -> bool:
        return self.remaining_seconds <= 0


class OrderStateManager:
    """
    Per-engine order state tracking.

    Lifecycle:
      - Created once per TradingEngine, keyed by ticker.
      - Tracks the last order state to prevent duplicate submissions.
      - Maintains a blocked list when orders are rejected.
      - Persists blocked tickers to disk for cross-restart resilience.
    """

    def __init__(
        self,
        ticker: str,
        mode: str = "paper",
        cooldown_seconds: int = 3600,
        state_dir: Optional[Path] = None,
    ):
        self.ticker = ticker.upper()
        self.mode = str(mode or "paper").strip().lower()
        self.cooldown_seconds = cooldown_seconds
        self._state_dir = Path(state_dir) if state_dir else resolve_state_dir(Path(PROJECT_DIR))
        self.runtime_scope = self._detect_runtime_scope()
        bucket_name = "order_state_test" if self.runtime_scope == "test" else "order_state"
        self._state_path = self._state_dir / bucket_name / f"{self.ticker}.json"

        # Runtime state (not persisted — resets on restart)
        self._active_order_id: Optional[str] = None
        self._active_order_state: Optional[OrderState] = None
        self._active_order_side: Optional[str] = None  # BUY / SELL

        # Failed order history (today only — persisted)
        self._failed_orders_today: list[FailedOrder] = []

        # Blocked ticker (persisted)
        self._blocked: Optional[BlockedTicker] = None

        self._load()

    def _detect_runtime_scope(self) -> str:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return "test"
        if self.ticker in SYNTHETIC_TEST_TICKERS:
            return "test"
        if self.mode not in {"live", "paper", "backtest"}:
            return "test"
        return "runtime"

    # ---- Public API: Order lifecycle ----

    def record_submitted(self, order_id: str, side: str) -> None:
        """Mark an order as submitted (PENDING)."""
        self._active_order_id = order_id
        self._active_order_state = OrderState.ORDER_PENDING
        self._active_order_side = side.upper()

    def record_filled(self, order_id: str) -> None:
        """Mark an order as filled."""
        if self._active_order_id == order_id:
            self._active_order_state = OrderState.ORDER_FILLED
        self._clear_active_order()

    def record_rejected(
        self,
        order_id: str,
        reason: str,
        quantity: int = 0,
        price: float = 0.0,
        buying_power: float = 0.0,
    ) -> None:
        """Record a rejected order and apply cooldown."""
        if self._active_order_id == order_id:
            self._active_order_state = OrderState.ORDER_REJECTED

        fail = FailedOrder(
            ticker=self.ticker,
            timestamp=datetime.now(),
            reason=reason,
            quantity=quantity,
            price=price,
            buying_power=buying_power,
        )
        self._failed_orders_today.append(fail)

        if self.cooldown_seconds > 0:
            self._blocked = BlockedTicker(
                ticker=self.ticker,
                blocked_until=datetime.now().replace(
                    microsecond=0
                ) + self._create_timedelta(self.cooldown_seconds),
                reason=reason,
                buying_power_at_block=buying_power,
            )
            logger.warning(
                "%s: BUY blocked until %s (cooldown %ds): %s",
                self.ticker,
                self._blocked.blocked_until.strftime("%H:%M:%S"),
                self.cooldown_seconds,
                reason,
            )

        self._clear_active_order()
        self._save()

    def record_cancelled(self, order_id: str) -> None:
        """Mark an order as cancelled."""
        if self._active_order_id == order_id:
            self._active_order_state = OrderState.ORDER_CANCELLED
        self._clear_active_order()

    # ---- Public API: Queries ----

    @property
    def has_pending_order(self) -> bool:
        """Is there an outstanding order that hasn't resolved yet?"""
        return self._active_order_state == OrderState.ORDER_PENDING

    @property
    def is_blocked(self) -> bool:
        """Is this ticker currently blocked from new BUY orders?"""
        if self._blocked is None:
            return False
        if self._blocked.is_expired:
            self._clear_block()
            return False
        return True

    @property
    def blocked_until(self) -> Optional[datetime]:
        """When the block expires, or None."""
        return self._blocked.blocked_until if self._blocked else None

    @property
    def blocked_reason(self) -> str:
        """Human-readable reason for the block."""
        if not self._blocked or self._blocked.is_expired:
            return ""
        remaining = int(self._blocked.remaining_seconds)
        return (
            f"买单已暂停：{self._blocked.reason}，"
            f"冷却中，剩余 {remaining // 60}分{remaining % 60}秒"
        )

    @property
    def failed_orders_today(self) -> list[FailedOrder]:
        """All failed orders for today (live objects)."""
        return list(self._failed_orders_today)

    @property
    def failed_count_today(self) -> int:
        """Number of failed orders today."""
        return len(self._failed_orders_today)

    @property
    def pending_order_id(self) -> Optional[str]:
        return self._active_order_id if self.has_pending_order else None

    # ---- Public API: Buying-power check ----

    def check_buying_power(
        self,
        price: float,
        quantity: int,
        available_cash: float,
        fee_buffer: float = 5.0,
    ) -> tuple[bool, str]:
        """
        Pre-trade buying-power check.

        Returns (allowed, reason).
        """
        required = price * quantity + fee_buffer
        if available_cash < required:
            reason = (
                f"BUY_BLOCKED: insufficient buying power — "
                f"需要 ${required:.2f} (${price:.2f}×{quantity}+fee), "
                f"可用 ${available_cash:.2f}"
            )
            return False, reason
        return True, ""

    # ---- Public API: Maintenance ----

    def maybe_clear_block_on_bp_change(self, current_buying_power: float) -> bool:
        """If buying power has changed, lift the block (e.g. after a sell)."""
        if not self._blocked or self._blocked.is_expired:
            return False
        if current_buying_power != self._blocked.buying_power_at_block:
            logger.info(
                "%s: 购买力已变化 $%.2f → $%.2f，解除买入限制",
                self.ticker,
                self._blocked.buying_power_at_block,
                current_buying_power,
            )
            self._clear_block()
            return True
        return False

    def reset_daily(self) -> None:
        """Clear today's failed orders (called at start of new trading day)."""
        self._failed_orders_today.clear()
        if self._blocked and self._blocked.is_expired:
            self._clear_block()

    # ---- Internal ----

    def _clear_active_order(self) -> None:
        self._active_order_id = None
        self._active_order_state = None
        self._active_order_side = None

    def _clear_block(self) -> None:
        self._blocked = None
        self._save()

    def _create_timedelta(self, seconds: int):
        """Create timedelta — extracted for testability."""
        from datetime import timedelta
        return timedelta(seconds=seconds)

    # ---- Persistence ----

    def _save(self) -> None:
        """Persist blocked state and failed orders to disk."""
        if self.mode != "live":
            return  # Only persist live mode to avoid clutter
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "ticker": self.ticker,
                "mode": self.mode,
                "runtime_scope": self.runtime_scope,
                "updated_at": datetime.now().isoformat(),
                "blocked": None,
                "failed_orders_today": [f.to_dict() for f in self._failed_orders_today],
            }
            if self._blocked and not self._blocked.is_expired:
                data["blocked"] = {
                    "blocked_until": self._blocked.blocked_until.isoformat(),
                    "reason": self._blocked.reason,
                    "buying_power_at_block": self._blocked.buying_power_at_block,
                }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.error("Failed to persist order state for %s: %s", self.ticker, exc)

    def _load(self) -> None:
        """Restore blocked state from disk."""
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return

            # Restore failed orders
            for fo in data.get("failed_orders_today", []):
                try:
                    self._failed_orders_today.append(FailedOrder.from_dict(fo))
                except Exception:
                    pass

            # Restore blocked state
            blocked = data.get("blocked")
            if blocked:
                blocked_until = datetime.fromisoformat(blocked["blocked_until"])
                self._blocked = BlockedTicker(
                    ticker=self.ticker,
                    blocked_until=blocked_until,
                    reason=blocked.get("reason", ""),
                    buying_power_at_block=blocked.get("buying_power_at_block", 0.0),
                )
                if self._blocked.is_expired:
                    self._clear_block()
                    logger.info("%s: expired buy block cleared on load", self.ticker)
                else:
                    logger.info(
                        "%s: restored buy block until %s",
                        self.ticker,
                        self._blocked.blocked_until.strftime("%H:%M:%S"),
                    )
        except Exception as exc:
            logger.warning("Failed to load order state for %s: %s", self.ticker, exc)
