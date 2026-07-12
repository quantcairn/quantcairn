from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bar_key(bar: dict[str, Any]) -> str:
    symbol = str(bar.get("symbol") or "").strip().upper()
    timestamp = str(bar.get("timestamp_utc") or bar.get("timestamp") or "").strip()
    version = str(bar.get("strategy_version") or "").strip()
    payload = f"{symbol}|{version}|{timestamp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ShadowRuntimeStateStore:
    path: Path
    schema_version: int = 1
    _state: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self.load()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": self.schema_version,
                "state_version": self.schema_version,
                "processed_bars": [],
                "last_processed_timestamp_utc": None,
                "last_processed_key": None,
                "processed_bar_count": 0,
                "last_run_at": None,
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {
            "schema_version": self.schema_version,
            "state_version": self.schema_version,
            "processed_bars": [],
            "last_processed_timestamp_utc": None,
            "last_processed_key": None,
            "processed_bar_count": 0,
            "last_run_at": None,
        }

    def save(self, state: dict[str, Any] | None = None) -> Path:
        payload = dict(state or self._state or {})
        payload.setdefault("schema_version", self.schema_version)
        payload.setdefault("state_version", self.schema_version)
        payload.setdefault("processed_bars", [])
        payload.setdefault("processed_bar_count", len(payload.get("processed_bars") or []))
        payload["last_run_at"] = _now_iso()
        fd, tmp_name = tempfile.mkstemp(prefix=f"{self.path.stem}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except Exception:
                pass
            raise
        self._state = payload
        return self.path

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def already_processed(self, bar: dict[str, Any]) -> bool:
        key = _bar_key(bar)
        return key in set(self._state.get("processed_bars") or [])

    def mark_processed(self, bars: Iterable[dict[str, Any]]) -> dict[str, Any]:
        processed = list(self._state.get("processed_bars") or [])
        seen = set(processed)
        latest_ts = self._state.get("last_processed_timestamp_utc")
        latest_key = self._state.get("last_processed_key")
        count = int(self._state.get("processed_bar_count") or 0)
        for bar in bars:
            key = _bar_key(bar)
            if key in seen:
                continue
            processed.append(key)
            seen.add(key)
            count += 1
            ts = str(bar.get("timestamp_utc") or bar.get("timestamp") or "").strip()
            if ts and (latest_ts is None or ts > str(latest_ts)):
                latest_ts = ts
                latest_key = key
        self._state.update(
            {
                "schema_version": self.schema_version,
                "state_version": self.schema_version,
                "processed_bars": processed,
                "last_processed_timestamp_utc": latest_ts,
                "last_processed_key": latest_key,
                "processed_bar_count": count,
                "last_run_at": _now_iso(),
            }
        )
        return dict(self._state)

    def update(self, **kwargs: Any) -> dict[str, Any]:
        self._state.update(kwargs)
        return dict(self._state)

