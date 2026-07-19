"""Durable, auditable PaperBroker portfolio state.

PaperBroker owns writes after confirmed paper fills. Dashboard, notifications,
and research collectors may read this file but must not mutate it.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except Exception:  # pragma: no cover - non-posix fallback
    fcntl = None

from .base import AccountInfo, Position


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "paper_portfolio_state.v2"
ALLOWED_EXECUTION_MODES = {"paper", "sandbox", "test"}
MAX_PROCESSED_FILL_IDS = 5000


class PaperPortfolioStateError(RuntimeError):
    """Raised when durable paper state cannot be safely loaded or written."""


class PaperPortfolioStateCorruptError(PaperPortfolioStateError):
    """Raised when an existing state file is unreadable or malformed."""


class PaperPortfolioValidationError(PaperPortfolioStateError):
    """Raised when state invariants fail before persistence."""


def default_paper_portfolio_state_path(account_id: str = "paper-default") -> Path:
    configured = os.environ.get("SOXS_PAPER_PORTFOLIO_STATE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    state_dir = Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))
    safe_account_id = _safe_account_id(account_id)
    return (state_dir / "paper" / safe_account_id / "portfolio_state.json").expanduser().resolve()


def new_writer_run_id() -> str:
    return f"paper-run-{uuid.uuid4().hex}"


def _safe_account_id(account_id: str) -> str:
    raw = str(account_id or "paper-default").strip()
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw)
    return safe or "paper-default"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _round(value: Any, digits: int = 6) -> float:
    try:
        result = float(value or 0.0)
    except Exception:
        return 0.0
    if not math.isfinite(result):
        return 0.0
    return round(result, digits)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def position_to_dict(position: Position) -> dict[str, Any]:
    symbol = str(position.ticker or "").strip().upper()
    quantity = max(0, int(position.quantity or 0))
    average_cost = _round(position.avg_entry_price)
    market_price = _round(position.current_price)
    market_value = _round(position.market_value)
    unrealized_pnl = _round(position.unrealized_pnl)
    unrealized_pnl_pct = _round(position.unrealized_pnl_pct)
    now = _utc_now()
    return {
        "symbol": symbol,
        "ticker": symbol,
        "quantity": quantity,
        "average_cost": average_cost,
        "avg_entry_price": average_cost,
        "market_price": market_price,
        "current_price": market_price,
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "opened_at": now,
        "updated_at": now,
    }


@dataclass
class PaperPortfolioState:
    account_id: str = "paper-default"
    execution_mode: str = "paper"
    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    realized_pnl: float = 0.0
    total_commission: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    processed_fill_ids: list[str] = field(default_factory=list)
    state_version: int = 0
    fill_sequence: int = 0
    last_fill_id: str | None = None
    last_order_id: str | None = None
    last_event_id: str | None = None
    last_fill_time: str | None = None
    last_update_time: str | None = None
    writer_pid: int | None = None
    writer_port: int | None = None
    writer_mode: str = "paper"
    writer_run_id: str | None = None
    updated_at: str | None = None

    @classmethod
    def initial(
        cls,
        *,
        account_id: str = "paper-default",
        initial_cash: float = 0.0,
        execution_mode: str = "paper",
        writer_mode: str = "paper",
        writer_run_id: str | None = None,
        writer_port: int | None = None,
    ) -> "PaperPortfolioState":
        cash = _round(initial_cash)
        return cls(
            account_id=account_id,
            execution_mode=execution_mode,
            cash=cash,
            equity=cash,
            buying_power=_round(cash * 2),
            writer_mode=writer_mode,
            writer_run_id=writer_run_id,
            writer_port=writer_port,
        )

    @classmethod
    def from_account(
        cls,
        account: AccountInfo,
        *,
        account_id: str = "paper-default",
        realized_pnl: float = 0.0,
        processed_fill_ids: set[str] | list[str] | None = None,
        state_version: int = 0,
        fill_sequence: int = 0,
        last_fill_id: str | None = None,
        last_order_id: str | None = None,
        last_event_id: str | None = None,
        last_fill_time: str | None = None,
        last_update_time: str | None = None,
        writer_pid: int | None = None,
        writer_port: int | None = None,
        writer_mode: str = "paper",
        writer_run_id: str | None = None,
        execution_mode: str = "paper",
    ) -> "PaperPortfolioState":
        positions = [
            position_to_dict(position)
            for position in (account.positions or [])
            if int(position.quantity or 0) > 0
        ]
        return cls(
            account_id=account_id,
            execution_mode=execution_mode,
            cash=_round(account.cash),
            equity=_round(account.equity),
            buying_power=_round(account.buying_power),
            positions=positions,
            realized_pnl=_round(realized_pnl),
            total_commission=_round(account.total_commission),
            total_trades=int(account.total_trades or 0),
            win_rate=_round(account.win_rate),
            avg_win=_round(account.avg_win),
            avg_loss=_round(account.avg_loss),
            processed_fill_ids=sorted(str(item) for item in (processed_fill_ids or []) if str(item)),
            state_version=int(state_version or 0),
            fill_sequence=int(fill_sequence or 0),
            last_fill_id=last_fill_id,
            last_order_id=last_order_id,
            last_event_id=last_event_id,
            last_fill_time=last_fill_time,
            last_update_time=last_update_time,
            writer_pid=writer_pid,
            writer_port=writer_port,
            writer_mode=writer_mode,
            writer_run_id=writer_run_id,
            updated_at=last_update_time,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PaperPortfolioState":
        positions = []
        for pos in payload.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            quantity = max(0, int(pos.get("quantity") or 0))
            if quantity <= 0:
                continue
            symbol = str(pos.get("symbol") or pos.get("ticker") or "").strip().upper()
            if not symbol:
                continue
            average_cost = _round(pos.get("average_cost", pos.get("avg_entry_price")))
            market_price = _round(pos.get("market_price", pos.get("current_price")))
            normalized = {
                **pos,
                "symbol": symbol,
                "ticker": symbol,
                "quantity": quantity,
                "average_cost": average_cost,
                "avg_entry_price": average_cost,
                "market_price": market_price,
                "current_price": market_price,
                "market_value": _round(pos.get("market_value")),
                "unrealized_pnl": _round(pos.get("unrealized_pnl")),
                "unrealized_pnl_pct": _round(pos.get("unrealized_pnl_pct")),
                "opened_at": str(pos.get("opened_at") or payload.get("last_update_time") or _utc_now()),
                "updated_at": str(pos.get("updated_at") or payload.get("last_update_time") or _utc_now()),
            }
            positions.append(normalized)
        return cls(
            account_id=str(payload.get("account_id") or "paper-default"),
            execution_mode=str(payload.get("execution_mode") or payload.get("mode") or "paper").lower(),
            cash=_round(payload.get("cash")),
            equity=_round(payload.get("equity")),
            buying_power=_round(payload.get("buying_power")),
            positions=positions,
            realized_pnl=_round(payload.get("realized_pnl")),
            total_commission=_round(payload.get("total_commission")),
            total_trades=int(payload.get("total_trades") or 0),
            win_rate=_round(payload.get("win_rate")),
            avg_win=_round(payload.get("avg_win")),
            avg_loss=_round(payload.get("avg_loss")),
            processed_fill_ids=[str(item) for item in payload.get("processed_fill_ids") or [] if str(item)],
            state_version=int(payload.get("state_version") or 0),
            fill_sequence=int(payload.get("fill_sequence") or 0),
            last_fill_id=payload.get("last_fill_id"),
            last_order_id=payload.get("last_order_id"),
            last_event_id=payload.get("last_event_id"),
            last_fill_time=payload.get("last_fill_time"),
            last_update_time=payload.get("last_update_time") or payload.get("updated_at"),
            writer_pid=payload.get("writer_pid"),
            writer_port=payload.get("writer_port"),
            writer_mode=str(payload.get("writer_mode") or payload.get("execution_mode") or "paper").lower(),
            writer_run_id=payload.get("writer_run_id"),
            updated_at=payload.get("updated_at") or payload.get("last_update_time"),
        )

    def to_dict(self) -> dict[str, Any]:
        positions = [dict(pos) for pos in self.positions if isinstance(pos, dict) and int(pos.get("quantity") or 0) > 0]
        market_value = _round(sum(float(pos.get("market_value") or 0.0) for pos in positions))
        unrealized_pnl = _round(sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions))
        return {
            "schema_version": SCHEMA_VERSION,
            "state_version": int(self.state_version or 0),
            "account_id": self.account_id,
            "execution_mode": self.execution_mode,
            "broker": "PaperBroker",
            "updated_at": self.last_update_time or self.updated_at,
            "last_update_time": self.last_update_time or self.updated_at,
            "cash": _round(self.cash),
            "equity": _round(self.equity),
            "buying_power": _round(self.buying_power),
            "market_value": market_value,
            "realized_pnl": _round(self.realized_pnl),
            "unrealized_pnl": unrealized_pnl,
            "positions_count": len(positions),
            "positions": positions,
            "total_trades": int(self.total_trades or 0),
            "win_rate": _round(self.win_rate),
            "avg_win": _round(self.avg_win),
            "avg_loss": _round(self.avg_loss),
            "total_commission": _round(self.total_commission),
            "processed_fill_ids": list(self.processed_fill_ids or [])[-MAX_PROCESSED_FILL_IDS:],
            "fill_sequence": int(self.fill_sequence or 0),
            "last_fill_id": self.last_fill_id,
            "last_order_id": self.last_order_id,
            "last_event_id": self.last_event_id,
            "last_fill_time": self.last_fill_time,
            "writer_pid": self.writer_pid,
            "writer_port": self.writer_port,
            "writer_mode": self.writer_mode,
            "writer_run_id": self.writer_run_id,
            "data_stale": False,
            "account_error": False,
            "mode": self.execution_mode,
        }

    def validate(self) -> None:
        if self.execution_mode not in ALLOWED_EXECUTION_MODES:
            raise PaperPortfolioValidationError(f"invalid_execution_mode:{self.execution_mode}")
        if self.writer_mode not in ALLOWED_EXECUTION_MODES:
            raise PaperPortfolioValidationError(f"invalid_writer_mode:{self.writer_mode}")
        for field_name in ("cash", "equity", "buying_power", "realized_pnl"):
            value = float(getattr(self, field_name) or 0.0)
            if not math.isfinite(value):
                raise PaperPortfolioValidationError(f"non_finite_{field_name}")
        if self.cash < -0.000001:
            raise PaperPortfolioValidationError("cash_negative")
        if int(self.state_version or 0) < 0:
            raise PaperPortfolioValidationError("state_version_negative")
        if int(self.fill_sequence or 0) < 0:
            raise PaperPortfolioValidationError("fill_sequence_negative")
        market_value = 0.0
        for pos in self.positions:
            quantity = int((pos or {}).get("quantity") or 0)
            if quantity < 0:
                raise PaperPortfolioValidationError("position_quantity_negative")
            market_value += float((pos or {}).get("market_value") or 0.0)
        expected_equity = _round(self.cash + market_value)
        if abs(_round(self.equity) - expected_equity) > 0.02:
            raise PaperPortfolioValidationError("equity_mismatch")
        last_fill_time = _parse_time(self.last_fill_time)
        last_update_time = _parse_time(self.last_update_time or self.updated_at)
        if last_fill_time and last_update_time and last_fill_time > last_update_time:
            raise PaperPortfolioValidationError("last_fill_time_after_last_update_time")


class PaperPortfolioStateStore:
    def __init__(
        self,
        *,
        path: str | Path | None = None,
        account_id: str = "paper-default",
        writer_port: int | None = None,
        writer_mode: str = "paper",
        writer_run_id: str | None = None,
    ):
        self.account_id = account_id
        self.path = Path(path).expanduser().resolve() if path is not None else default_paper_portfolio_state_path(account_id)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.writer_port = writer_port
        self.writer_mode = writer_mode
        self.writer_run_id = writer_run_id or new_writer_run_id()
        self._lock_depth = 0

    @contextmanager
    def locked(self):
        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load(self) -> PaperPortfolioState | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise PaperPortfolioStateError(f"state_read_failed:{exc}") from exc
        try:
            payload = json.loads(raw)
        except Exception as exc:
            corrupt_path = self.path.with_suffix(self.path.suffix + f".corrupt.{int(datetime.now().timestamp())}")
            try:
                corrupt_path.write_text(raw, encoding="utf-8")
            except Exception:
                pass
            raise PaperPortfolioStateCorruptError(f"state_json_corrupt:{self.path}") from exc
        if not isinstance(payload, dict):
            raise PaperPortfolioStateCorruptError("state_payload_not_object")
        schema = str(payload.get("schema_version") or "")
        if schema and schema not in {SCHEMA_VERSION, "paper_portfolio_state.v1"}:
            raise PaperPortfolioStateCorruptError(f"unsupported_schema:{schema}")
        state = PaperPortfolioState.from_dict(payload)
        state.validate()
        return state

    def load_or_create(self, initial_state: PaperPortfolioState) -> PaperPortfolioState:
        existing = self.load()
        if existing is not None:
            return existing
        return self.save(initial_state)

    def save(
        self,
        state: PaperPortfolioState,
        *,
        last_fill_id: str | None = None,
        last_order_id: str | None = None,
        last_event_id: str | None = None,
        fill_time: str | None = None,
    ) -> PaperPortfolioState:
        previous = self.load()
        processed_fill_ids = list((previous.processed_fill_ids if previous else state.processed_fill_ids) or [])
        fill_sequence = int(previous.fill_sequence if previous else state.fill_sequence or 0)
        last_known_fill_id = previous.last_fill_id if previous else state.last_fill_id
        last_known_order_id = previous.last_order_id if previous else state.last_order_id
        last_known_event_id = previous.last_event_id if previous else state.last_event_id
        last_known_fill_time = previous.last_fill_time if previous else state.last_fill_time

        fill_id = str(last_fill_id or "").strip()
        is_new_fill = bool(fill_id and fill_id not in set(processed_fill_ids))
        if is_new_fill:
            processed_fill_ids.append(fill_id)
            processed_fill_ids = processed_fill_ids[-MAX_PROCESSED_FILL_IDS:]
            fill_sequence += 1
            last_known_fill_id = fill_id
            last_known_order_id = last_order_id or fill_id
            last_known_event_id = last_event_id
            last_known_fill_time = fill_time or _utc_now()

        now = _utc_now()
        next_state = PaperPortfolioState.from_dict(
            {
                **state.to_dict(),
                "account_id": self.account_id,
                "execution_mode": state.execution_mode or "paper",
                "state_version": int(previous.state_version if previous else state.state_version or 0) + 1,
                "fill_sequence": fill_sequence,
                "processed_fill_ids": processed_fill_ids,
                "last_fill_id": last_known_fill_id,
                "last_order_id": last_known_order_id,
                "last_event_id": last_known_event_id,
                "last_fill_time": last_known_fill_time,
                "last_update_time": now,
                "updated_at": now,
                "writer_pid": os.getpid(),
                "writer_port": self.writer_port,
                "writer_mode": self.writer_mode,
                "writer_run_id": self.writer_run_id,
            }
        )
        next_state.validate()
        self._atomic_write(next_state.to_dict())
        return next_state

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any] | None:
        state = self.load()
        return state.to_dict() if state is not None else None


def write_paper_portfolio_state(
    state: PaperPortfolioState | dict[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    payload_state = state if isinstance(state, PaperPortfolioState) else PaperPortfolioState.from_dict(dict(state))
    store = PaperPortfolioStateStore(
        path=path,
        account_id=payload_state.account_id,
        writer_port=payload_state.writer_port,
        writer_mode=payload_state.writer_mode,
        writer_run_id=payload_state.writer_run_id,
    )
    with store.locked():
        saved = store.save(
            payload_state,
            last_fill_id=payload_state.last_fill_id,
            last_order_id=payload_state.last_order_id,
            last_event_id=payload_state.last_event_id,
            fill_time=payload_state.last_fill_time,
        )
    return saved.to_dict()


def read_paper_portfolio_state(path: str | Path | None = None) -> dict[str, Any] | None:
    store = PaperPortfolioStateStore(path=path)
    try:
        state = store.load()
    except PaperPortfolioStateError:
        return None
    return state.to_dict() if state is not None else None
