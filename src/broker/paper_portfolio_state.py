"""Shared, read-only friendly PaperBroker portfolio state.

PaperBroker owns writes to this file after confirmed paper fills. Dashboard,
notifications, and research collectors may read it, but should not mutate it.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import AccountInfo, Position


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "paper_portfolio_state.v1"


def default_paper_portfolio_state_path() -> Path:
    configured = os.environ.get("SOXS_PAPER_PORTFOLIO_STATE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    state_dir = Path(os.environ.get("SOXS_STATE_DIR", "").strip() or (PROJECT_DIR / "state"))
    return (state_dir / "paper_portfolio_state.json").expanduser().resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _round(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value or 0.0), digits)
    except Exception:
        return 0.0


def position_to_dict(position: Position) -> dict[str, Any]:
    return {
        "ticker": str(position.ticker or "").strip().upper(),
        "quantity": int(position.quantity or 0),
        "avg_entry_price": _round(position.avg_entry_price),
        "current_price": _round(position.current_price),
        "market_value": _round(position.market_value),
        "unrealized_pnl": _round(position.unrealized_pnl),
        "unrealized_pnl_pct": _round(position.unrealized_pnl_pct),
    }


@dataclass
class PaperPortfolioState:
    account_id: str = "paper-default"
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
    last_fill_id: str | None = None
    last_order_id: str | None = None
    last_event_id: str | None = None
    updated_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_account(
        cls,
        account: AccountInfo,
        *,
        account_id: str = "paper-default",
        realized_pnl: float = 0.0,
        processed_fill_ids: set[str] | list[str] | None = None,
        last_fill_id: str | None = None,
        last_order_id: str | None = None,
        last_event_id: str | None = None,
    ) -> "PaperPortfolioState":
        positions = [position_to_dict(position) for position in (account.positions or [])]
        return cls(
            account_id=account_id,
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
            last_fill_id=last_fill_id,
            last_order_id=last_order_id,
            last_event_id=last_event_id,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PaperPortfolioState":
        return cls(
            account_id=str(payload.get("account_id") or "paper-default"),
            cash=_round(payload.get("cash")),
            equity=_round(payload.get("equity")),
            buying_power=_round(payload.get("buying_power")),
            positions=list(payload.get("positions") or []),
            realized_pnl=_round(payload.get("realized_pnl")),
            total_commission=_round(payload.get("total_commission")),
            total_trades=int(payload.get("total_trades") or 0),
            win_rate=_round(payload.get("win_rate")),
            avg_win=_round(payload.get("avg_win")),
            avg_loss=_round(payload.get("avg_loss")),
            processed_fill_ids=list(payload.get("processed_fill_ids") or []),
            last_fill_id=payload.get("last_fill_id"),
            last_order_id=payload.get("last_order_id"),
            last_event_id=payload.get("last_event_id"),
            updated_at=str(payload.get("updated_at") or _utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        positions = [dict(pos) for pos in self.positions if isinstance(pos, dict)]
        market_value = _round(sum(float(pos.get("market_value") or 0.0) for pos in positions))
        unrealized_pnl = _round(sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions))
        return {
            "schema_version": SCHEMA_VERSION,
            "account_id": self.account_id,
            "execution_mode": "paper",
            "broker": "PaperBroker",
            "updated_at": self.updated_at,
            "cash": _round(self.cash),
            "equity": _round(self.equity),
            "buying_power": _round(self.buying_power),
            "market_value": market_value,
            "realized_pnl": _round(self.realized_pnl),
            "unrealized_pnl": unrealized_pnl,
            "positions_count": len([pos for pos in positions if int(pos.get("quantity") or 0) > 0]),
            "positions": positions,
            "total_trades": int(self.total_trades or 0),
            "win_rate": _round(self.win_rate),
            "avg_win": _round(self.avg_win),
            "avg_loss": _round(self.avg_loss),
            "total_commission": _round(self.total_commission),
            "processed_fill_ids": list(self.processed_fill_ids or []),
            "last_fill_id": self.last_fill_id,
            "last_order_id": self.last_order_id,
            "last_event_id": self.last_event_id,
            "data_stale": False,
            "account_error": False,
            "mode": "paper",
        }


def write_paper_portfolio_state(
    state: PaperPortfolioState | dict[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    payload = state.to_dict() if isinstance(state, PaperPortfolioState) else dict(state)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("execution_mode", "paper")
    payload.setdefault("broker", "PaperBroker")
    payload.setdefault("updated_at", _utc_now())
    target = Path(path).expanduser().resolve() if path is not None else default_paper_portfolio_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, target)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
    return payload


def read_paper_portfolio_state(path: str | Path | None = None) -> dict[str, Any] | None:
    target = Path(path).expanduser().resolve() if path is not None else default_paper_portfolio_state_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION:
        return None
    return PaperPortfolioState.from_dict(payload).to_dict()
