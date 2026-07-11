from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _coerce_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().split(".")[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StrategyStateStore:
    base_dir: Path | str
    schema_version: int = 1

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.base_dir / f"{_coerce_symbol(symbol)}.json"

    def list_symbols(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return sorted(
            path.stem.upper()
            for path in self.base_dir.glob("*.json")
            if path.is_file()
        )

    def save(self, symbol: str, state: dict[str, Any]) -> Path:
        path = self._path(symbol)
        payload = dict(state or {})
        payload.setdefault("symbol", _coerce_symbol(symbol))
        payload.setdefault("schema_version", self.schema_version)
        payload.setdefault("state_version", int(payload.get("state_version") or self.schema_version))
        payload.setdefault("updated_at", _now_iso())
        payload.setdefault("saved_at", _now_iso())

        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(self.base_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_name, path)
        except Exception:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except Exception:
                pass
            raise
        return path

    def load(self, symbol: str) -> dict[str, Any] | None:
        path = self._path(symbol)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def delete(self, symbol: str) -> bool:
        path = self._path(symbol)
        try:
            if path.exists():
                path.unlink()
                return True
        except Exception:
            return False
        return False

    def reconcile(self, symbol: str, broker_position_snapshot: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(state or {})
        payload["symbol"] = _coerce_symbol(symbol)
        payload["broker_position_snapshot"] = dict(broker_position_snapshot or {})
        payload["last_reconciliation_time"] = _now_iso()
        payload["schema_version"] = self.schema_version
        payload["state_version"] = int(payload.get("state_version") or self.schema_version)
        return payload
