"""Managed Universe — loads profiles, runs filters, persists snapshots.

The Universe Manager controls which candidates are available for the
AI Selector.  It never modifies trading state, weights, orders, or
live safety gates.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.universe.models import UniverseSymbol, UniverseSnapshot, UNIVERSE_ARTIFACT_DIR
from src.universe.profiles import default_universe
from src.universe.filters import run_all_filters


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UniverseManager:
    """Manages the symbol universe lifecycle: load, filter, snapshot."""

    def __init__(
        self,
        *,
        snapshot_path: str | Path | None = None,
        universe_path: str | Path | None = None,
    ) -> None:
        self.snapshot_path = (
            Path(snapshot_path) if snapshot_path
            else UNIVERSE_ARTIFACT_DIR / "universe_snapshot.json"
        )
        self.universe_path = (
            Path(universe_path) if universe_path
            else UNIVERSE_ARTIFACT_DIR / "universe.json"
        )
        # Filter defaults
        self.min_avg_volume: int = 500_000
        self.min_price: float = 4.0
        self.max_price: float = 300.0
        self.max_risk_score: float = 70.0
        self.max_volatility_score: float = 80.0
        self.max_leveraged_inverse: int = 1

    # ── Load / save ────────────────────────────────────────────────────

    def load_symbols(self) -> list[UniverseSymbol]:
        """Load symbols from persisted universe.json, or fall back to defaults."""
        if self.universe_path.exists():
            try:
                data = json.loads(self.universe_path.read_text(encoding="utf-8"))
                items = data.get("symbols") if isinstance(data, dict) else data
                if isinstance(items, list):
                    return [UniverseSymbol.from_dict(d) for d in items if isinstance(d, dict)]
            except Exception:
                pass
        # Fallback: default profiles
        return default_universe()

    def save_symbols(self, symbols: list[UniverseSymbol]) -> Path:
        self.universe_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"symbols": [s.to_dict() for s in symbols], "updated_at": _utc_now_iso()}
        fd, tmp = tempfile.mkstemp(prefix=".universe.", suffix=".tmp", dir=str(self.universe_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            os.replace(tmp, self.universe_path)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return self.universe_path

    # ── Snapshot ────────────────────────────────────────────────────────

    def build_snapshot(
        self,
        *,
        dry_run: bool = False,
    ) -> UniverseSnapshot:
        """Load symbols, run filter pipeline, return snapshot."""
        symbols = self.load_symbols()
        snapshot = run_all_filters(
            symbols,
            min_avg_volume=self.min_avg_volume,
            min_price=self.min_price,
            max_price=self.max_price,
            max_risk_score=self.max_risk_score,
            max_volatility_score=self.max_volatility_score,
            max_leveraged_inverse=self.max_leveraged_inverse,
        )
        if not dry_run:
            self._write_snapshot(snapshot)
        return snapshot

    def _write_snapshot(self, snapshot: UniverseSnapshot) -> Path:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".snapshot.", suffix=".tmp", dir=str(self.snapshot_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            os.replace(tmp, self.snapshot_path)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return self.snapshot_path

    def load_snapshot(self) -> UniverseSnapshot | None:
        if not self.snapshot_path.exists():
            return None
        try:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            symbols = [UniverseSymbol.from_dict(d) for d in (data.get("symbols") or []) if isinstance(d, dict)]
            snap = UniverseSnapshot(
                version=str(data.get("version") or "v1"),
                generated_at=str(data.get("generated_at") or ""),
                total_symbols=int(data.get("total_symbols", 0) or 0),
                enabled_symbols=int(data.get("enabled_symbols", 0) or 0),
                symbols=symbols,
                filter_results=dict(data.get("filter_results") or {}),
                composition=dict(data.get("composition") or {}),
                max_leveraged_inverse=int(data.get("max_leveraged_inverse", 1) or 1),
            )
            return snap
        except Exception:
            return None

    def get_enabled_symbols(self) -> list[str]:
        """Return list of enabled ticker strings for the AI Selector."""
        snap = self.build_snapshot() or self.load_snapshot()
        if snap is None:
            return [s.symbol for s in default_universe() if s.enabled]
        return sorted(s.symbol for s in snap.symbols if s.enabled)
