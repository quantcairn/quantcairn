"""
LiveGuard: pre-flight checks that must pass before live trading is allowed.

Every check that fails appends an entry to ``errors`` or ``warnings`` and
sets the corresponding capability flag to *False*.

Usage::

    guard = LiveGuard()
    verdict = guard.validate_live_start(context={...})
    if not verdict["allowed_to_open_new_positions"]:
        engine._reduce_only = True
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.-]{0,9}$")


class LiveGuard:
    """Aggregate pre-flight checks for live trading sessions."""

    def __init__(self) -> None:
        self._errors: list[str] = []
        self._warnings: list[str] = []
        self._context: dict[str, Any] = {}

    # ---- Public API --------------------------------------------------------

    def validate_live_start(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run all validations and return a verdict dict.

        *context* may contain pre-loaded values:
            - ``mode``: "paper" | "live" | "backtest"
            - ``broker``: broker instance (for account/position queries)
            - ``ticker``: override ticker (default reads from TOP configs)
            - ``ignore_checks``: list of check names to skip (advanced)
        """
        self._errors.clear()
        self._warnings.clear()
        self._context = dict(context or {})

        # ---- Ordered check list ----
        self._check_mode()
        self._check_trading_day()
        self._check_trading_hours()
        self._check_top_configs_exist()
        self._check_ai_selection_report()
        self._check_selection_state()
        self._check_broker_account()
        self._check_broker_positions()
        self._check_positions_covered()
        self._check_risk_limits()

        new_positions = len(self._errors) == 0
        return {
            "allowed_to_open_new_positions": new_positions,
            "allowed_reduce_only": True,  # always allowed
            "errors": list(self._errors),
            "warnings": list(self._warnings),
        }

    # ---- Individual checks -------------------------------------------------

    def _error(self, msg: str) -> None:
        self._errors.append(msg)
        logger.error("LiveGuard: %s", msg)

    def _warn(self, msg: str) -> None:
        self._warnings.append(msg)
        logger.warning("LiveGuard: %s", msg)

    # -- 1. Mode check --

    def _check_mode(self) -> None:
        mode = str(self._context.get("mode") or "").strip().lower()
        if mode not in ("paper", "live", "backtest"):
            self._error(f"Invalid or missing mode: '{mode}' (must be paper/live/backtest)")
            return
        if mode != "live":
            self._warn(f"Mode is '{mode}' — live checks are advisory only")

    # -- 2. Trading day --

    def _check_trading_day(self) -> None:
        today = _et_today()
        if today.weekday() >= 5:
            self._warn(f"Weekend ({today.strftime('%A')}) — market closed")
        us_holidays = _us_market_holidays(today.year)
        if today in us_holidays:
            self._warn(f"Holiday ({today.isoformat()}) — market closed")

    # -- 3. Trading hours (approximate) --

    def _check_trading_hours(self) -> None:
        try:
            import pytz
            ny = pytz.timezone("America/New_York")
            now_ny = datetime.now(ny)
            minute_of_day = now_ny.hour * 60 + now_ny.minute
            # Regular session: 09:30 – 16:00 ET
            if minute_of_day < 570 or minute_of_day > 960:
                self._warn(f"Outside regular trading hours ({now_ny.strftime('%H:%M')} ET)")
        except Exception:
            pass  # timezone unavailable, skip check

    # -- 4. TOP configs exist and have today's date --

    def _check_top_configs_exist(self) -> None:
        top_dir = PROJECT_DIR / "configs"
        found_any = False
        today_str = _et_today().isoformat()
        for idx in range(1, 6):
            path = top_dir / f"TOP{idx}.yaml"
            if not path.exists():
                continue
            found_any = True
            try:
                data = _safe_yaml_load(path)
            except Exception:
                self._error(f"TOP{idx}.yaml is unreadable")
                continue
            ticker = str(data.get("ticker") or "").strip().upper()
            if not ticker or not EQUITY_SYMBOL_RE.fullmatch(ticker):
                self._error(f"TOP{idx}.yaml has no valid ticker")
            sel_date = str(data.get("selection", {}).get("selection_date") or "").strip()
            if sel_date and sel_date != today_str:
                self._error(f"TOP{idx}.yaml ({ticker}) selection_date={sel_date}, expected {today_str}")
        if not found_any:
            self._error("No TOP config files found in configs/")

    # -- 5. AI selection report --

    def _check_ai_selection_report(self) -> None:
        path = PROJECT_DIR / "reports" / "ai_selection_latest.json"
        if not path.exists():
            self._error("ai_selection_latest.json does not exist")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._error("ai_selection_latest.json is corrupt or unreadable")
            return
        today_str = _et_today().isoformat()
        sel_date = str(data.get("selection_date") or "").strip()
        if sel_date != today_str:
            self._error(f"AI selection_date={sel_date}, expected {today_str}")

    # -- 6. Selection state consistency --

    def _check_selection_state(self) -> None:
        state_path = PROJECT_DIR / "state" / "ai_selection_state.json"
        if not state_path.exists():
            self._error("ai_selection_state.json does not exist")
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            self._error("ai_selection_state.json is corrupt")
            return
        today_str = _et_today().isoformat()
        state_date = str(state.get("et_date") or "").strip()
        if state_date != today_str:
            self._error(f"selection_state et_date={state_date}, expected {today_str}")
        # Verify TOP config symbols match state
        state_symbols = {
            str(s or "").strip().upper()
            for s in (state.get("selected_symbols") or [])
            if s
        }
        top_symbols = set()
        top_dir = PROJECT_DIR / "configs"
        for idx in range(1, 6):
            path = top_dir / f"TOP{idx}.yaml"
            if not path.exists():
                continue
            data = _safe_yaml_load(path)
            t = str(data.get("ticker") or "").strip().upper()
            if t:
                top_symbols.add(t)
        if state_symbols and top_symbols and state_symbols != top_symbols:
            self._warn(f"State symbols {state_symbols} ≠ TOP configs {top_symbols}")

    # -- 7. Broker account --

    def _check_broker_account(self) -> None:
        broker = self._context.get("broker")
        if broker is None:
            self._error("No broker available for account check")
            return
        try:
            acct = broker.get_account()
        except Exception as exc:
            self._error(f"Broker account unavailable: {exc}")
            return
        if acct is None:
            self._error("Broker returned no account")
            return
        eq = getattr(acct, "equity", None)
        if eq is None or float(eq) <= 0:
            self._error(f"Broker equity is {eq} — cannot validate")

    # -- 8. Broker positions --

    def _check_broker_positions(self) -> None:
        broker = self._context.get("broker")
        if broker is None:
            return  # already reported above
        try:
            pos_list = broker.get_positions()
        except Exception as exc:
            self._error(f"Broker positions unavailable: {exc}")
            return
        if pos_list is None:
            self._error("Broker returned None for positions")
            return
        # Store for coverage check
        self._context["_live_positions"] = [
            {
                "ticker": str(getattr(p, "ticker", "") or "").strip().upper(),
                "quantity": int(getattr(p, "quantity", 0) or 0),
            }
            for p in pos_list
            if int(getattr(p, "quantity", 0) or 0) > 0
        ]

    # -- 9. Positions covered by TOP configs or orphan monitor --

    def _check_positions_covered(self) -> None:
        live_pos = self._context.get("_live_positions", [])
        if not live_pos:
            return  # no open positions to check
        top_symbols = set()
        top_dir = PROJECT_DIR / "configs"
        for idx in range(1, 6):
            path = top_dir / f"TOP{idx}.yaml"
            if not path.exists():
                continue
            data = _safe_yaml_load(path)
            t = str(data.get("ticker") or "").strip().upper()
            if t:
                top_symbols.add(t)
        for pos in live_pos:
            ticker = pos.get("ticker", "")
            qty = pos.get("quantity", 0)
            if ticker and ticker not in top_symbols:
                self._warn(f"Position {ticker} ({qty} shares) not in any TOP config — orphan monitor should cover it")

    # -- 10. Risk limit validity --

    def _check_risk_limits(self) -> None:
        # Read limits from the TOP configs
        top_dir = PROJECT_DIR / "configs"
        for idx in range(1, 4):
            path = top_dir / f"TOP{idx}.yaml"
            if not path.exists():
                continue
            try:
                data = _safe_yaml_load(path)
            except Exception:
                continue
            rk = data.get("risk") or {}
            daily_loss = _num(rk, "daily_loss_limit")
            if daily_loss is not None and daily_loss <= 0:
                self._error(f"TOP{idx}.yaml daily_loss_limit={daily_loss} must be > 0")


# ---- Helpers ---------------------------------------------------------------


def _safe_yaml_load(path: Path) -> dict[str, Any]:
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _num(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _us_market_holidays(year: int) -> set[date]:
    """Return US market holidays for a given *year* (naive set)."""
    import calendar

    def nth_weekday(m: int, wd: int, n: int) -> date:
        first = date(year, m, 1)
        day = 1 + ((wd - first.weekday()) % 7) + (n - 1) * 7
        return date(year, m, day)

    def last_weekday(m: int, wd: int) -> date:
        last_day = calendar.monthrange(year, m)[1]
        d = date(year, m, last_day)
        return d - __import__("datetime").timedelta(days=(d.weekday() - wd) % 7)

    def observed(d: date) -> date:
        if d.weekday() == 5:
            return d - __import__("datetime").timedelta(days=1)
        if d.weekday() == 6:
            return d + __import__("datetime").timedelta(days=1)
        return d

    easter = _easter(year)
    holidays: set[date] = {
        observed(date(year, 1, 1)),                           # New Year
        nth_weekday(1, 0, 3),                                  # MLK
        nth_weekday(2, 0, 3),                                  # Presidents'
        easter - __import__("datetime").timedelta(days=2),     # Good Friday
        last_weekday(5, 0),                                    # Memorial Day
        observed(date(year, 6, 19)),                           # Juneteenth
        observed(date(year, 7, 4)),                            # Independence Day
        nth_weekday(9, 0, 1),                                  # Labor Day
        nth_weekday(11, 3, 4),                                 # Thanksgiving
        observed(date(year, 12, 25)),                          # Christmas
    }
    # Monday-observed New Year could belong to the current or next calendar year.
    nny = observed(date(year + 1, 1, 1))
    if nny.year == year:
        holidays.add(nny)
    return holidays


def _et_today() -> date:
    """Return today's date in US Eastern Time, falling back to local."""
    try:
        import pytz
        return datetime.now(pytz.timezone("America/New_York")).date()
    except Exception:
        return date.today()


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    month_seed = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * month_seed) // 451
    month = (h + month_seed - 7 * m + 114) // 31
    day = (h + month_seed - 7 * m + 114) % 31 + 1
    return date(year, month, day)
