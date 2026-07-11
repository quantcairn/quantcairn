from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass
class TimeStop:
    calculation_version: str = "time_stop_v1"

    def evaluate(
        self,
        *,
        symbol: str,
        entry_time: Any,
        current_time: Any,
        holding_bars: int,
        holding_minutes: int,
        leveraged_etf: bool = False,
        configured_max_bars: int = 20,
        configured_max_minutes: int = 240,
    ) -> dict[str, Any]:
        entry_dt = _coerce_datetime(entry_time)
        current_dt = _coerce_datetime(current_time)
        holding_bars = max(0, int(holding_bars or 0))
        holding_minutes = max(0, int(holding_minutes or 0))
        configured_max_bars = max(1, int(configured_max_bars or 1))
        configured_max_minutes = max(1, int(configured_max_minutes or 1))

        if leveraged_etf:
            configured_max_bars = max(1, int(configured_max_bars * 0.5))
            configured_max_minutes = max(1, int(configured_max_minutes * 0.5))

        duration_minutes = 0
        if entry_dt is not None and current_dt is not None and current_dt >= entry_dt:
            duration_minutes = int((current_dt - entry_dt).total_seconds() // 60)

        triggered = holding_bars >= configured_max_bars or holding_minutes >= configured_max_minutes
        if entry_dt is None or current_dt is None:
            triggered = False

        reason = ""
        if triggered:
            if holding_bars >= configured_max_bars and holding_minutes >= configured_max_minutes:
                reason = "time_stop_bars_and_minutes"
            elif holding_bars >= configured_max_bars:
                reason = "time_stop_bars"
            else:
                reason = "time_stop_minutes"

        return {
            "symbol": str(symbol or "").strip().upper(),
            "triggered": bool(triggered),
            "reason": reason,
            "exit_priority": 3 if triggered else 0,
            "holding_duration_minutes": duration_minutes,
            "holding_bars": holding_bars,
            "configured_max_bars": configured_max_bars,
            "configured_max_minutes": configured_max_minutes,
            "leveraged_etf": bool(leveraged_etf),
            "calculation_version": self.calculation_version,
            "entry_time": entry_dt.isoformat() if entry_dt else None,
            "current_time": current_dt.isoformat() if current_dt else None,
        }
