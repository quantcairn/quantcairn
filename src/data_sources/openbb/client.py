from __future__ import annotations

from collections.abc import Iterable
import json
import os
import subprocess
import sys
from typing import Any

try:
    from openbb import obb
except ImportError:
    obb = None


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "to_dict"):
        try:
            data = payload.to_dict()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        try:
            items = list(payload)
        except Exception:
            return {}
        if not items:
            return {}
        first = items[0]
        if isinstance(first, dict):
            return dict(first)
        if hasattr(first, "to_dict"):
            try:
                data = first.to_dict()
                if isinstance(data, dict):
                    return dict(data)
            except Exception:
                return {}
    return {}


def _is_enabled(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class OpenBBClient:
    def get_fundamentals(self, ticker: str) -> dict:
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "metrics"),
                ("equity", "fundamental", "overview"),
                ("equity", "fundamental", "profile"),
            ),
        )

    def get_income_statement(self, ticker: str) -> dict:
        if not _is_enabled("SOXS_OPENBB_ENABLE_STATEMENTS", "0"):
            return {}
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "income"),
                ("equity", "fundamental", "income_statement"),
            ),
        )

    def get_balance_sheet(self, ticker: str) -> dict:
        if not _is_enabled("SOXS_OPENBB_ENABLE_STATEMENTS", "0"):
            return {}
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "balance"),
                ("equity", "fundamental", "balance_sheet"),
            ),
        )

    def get_cash_flow(self, ticker: str) -> dict:
        if not _is_enabled("SOXS_OPENBB_ENABLE_STATEMENTS", "0"):
            return {}
        return self._safe_call(
            ticker,
            (
                ("equity", "fundamental", "cash"),
                ("equity", "fundamental", "cash_flow"),
            ),
        )

    def get_analyst_estimates(self, ticker: str) -> dict:
        if not _is_enabled("SOXS_OPENBB_ENABLE_ESTIMATES", "0"):
            return {}
        return self._safe_call(
            ticker,
            (
                ("equity", "estimates", "consensus"),
                ("equity", "estimates", "analyst"),
                ("equity", "fundamental", "estimates"),
            ),
        )

    def _safe_call(self, ticker: str, paths: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
        if obb is None:
            return {}
        symbol = str(ticker or "").strip().upper()
        if not symbol:
            return {}
        timeout = self._timeout_seconds()
        for path in paths:
            try:
                payload = self._call_with_timeout(symbol, path, timeout)
                normalized = _normalize_payload(payload)
                if normalized:
                    return normalized
            except Exception:
                continue
        return {}

    def _timeout_seconds(self) -> int:
        raw_value = str(os.environ.get("SOXS_OPENBB_TIMEOUT_SECONDS", "5") or "5").strip()
        try:
            timeout = int(raw_value)
        except (TypeError, ValueError):
            timeout = 5
        return max(3, min(timeout, 60))

    def _call_with_timeout(self, symbol: str, path: tuple[str, ...], timeout: int) -> dict[str, Any]:
        helper = """
import json
from collections.abc import Iterable
from openbb import obb

def normalize(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "to_dict"):
        try:
            data = payload.to_dict()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            return {}
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        try:
            items = list(payload)
        except Exception:
            return {}
        if not items:
            return {}
        first = items[0]
        if isinstance(first, dict):
            return dict(first)
        if hasattr(first, "to_dict"):
            try:
                data = first.to_dict()
                if isinstance(data, dict):
                    return dict(data)
            except Exception:
                return {}
    return {}

target = obb
for part in {path!r}:
    target = getattr(target, part)
payload = target(symbol={symbol!r})
print(json.dumps(normalize(payload), ensure_ascii=False, default=str))
""".format(path=tuple(path), symbol=symbol)
        proc = subprocess.run(
            [sys.executable, "-c", helper],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "openbb_call_failed")
        output = (proc.stdout or "").strip().splitlines()
        if not output:
            return {}
        data = json.loads(output[-1])
        return data if isinstance(data, dict) else {}
