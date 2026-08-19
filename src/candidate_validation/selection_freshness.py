"""Fail-closed freshness evidence for Candidate Validation inputs."""

from __future__ import annotations

import os
import fcntl
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any


LOCAL_ZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_SELECTOR_WINDOWS = ("21:35", "21:45", "22:00", "22:15", "22:30")


def _enabled() -> bool:
    return str(os.environ.get("QUANTCAIRN_SELECTION_FRESHNESS_GATE", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _selector_windows() -> tuple[time, ...]:
    raw = os.environ.get("QUANTCAIRN_SELECTOR_WINDOW_STARTS", "")
    values = [item.strip() for item in raw.split(",") if item.strip()] if raw else list(DEFAULT_SELECTOR_WINDOWS)
    result: list[time] = []
    for value in values:
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
            result.append(time(hour=hour, minute=minute))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(result))) or tuple(time.fromisoformat(item) for item in DEFAULT_SELECTOR_WINDOWS)


def _lock_pid(path: Path) -> int | None:
    try:
        payload = path.read_text(encoding="utf-8")
        for line in payload.splitlines():
            if line.startswith("pid="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def selector_is_active(state_root: Path) -> bool:
    explicit = str(os.environ.get("QUANTCAIRN_SELECTOR_ACTIVE", "") or "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    for path in (state_root / "ai_selector.lock", state_root / "selector.lock"):
        if not path.exists():
            continue
        if path.name == "ai_selector.lock":
            try:
                with path.open("r+", encoding="utf-8") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        continue
            except OSError:
                # An unreadable lock cannot prove that the selector is idle.
                return True
        pid = _lock_pid(path)
        if pid is None:
            # An unreadable lock is not safe evidence of idleness.
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            if getattr(exc, "errno", None) == 1:  # permission denied: assume active
                return True
            if getattr(exc, "errno", None) != 3:  # ESRCH means no such process
                return True
    return False


def _bundle_field(bundle: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(bundle, dict):
        return None
    for section in (bundle.get("manifest"), bundle.get("report"), bundle.get("state"), bundle.get("metadata"), bundle):
        if isinstance(section, dict) and section.get(key) not in (None, ""):
            return section.get(key)
    return None


def _generated_at(bundle: dict[str, Any] | None) -> datetime | None:
    raw = _bundle_field(bundle, "generated_at") or _bundle_field(bundle, "published_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(LOCAL_ZONE)


def evaluate_selection_freshness(
    bundle: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Return an auditable decision without mutating bundle or lifecycle state."""

    root = Path(state_root or os.environ.get("SOXS_STATE_DIR", "state")).expanduser().resolve()
    current = (now or datetime.now(LOCAL_ZONE)).astimezone(LOCAL_ZONE)
    run_id = _bundle_field(bundle, "selection_run_id")
    windows = _selector_windows()
    result: dict[str, Any] = {
        "enabled": _enabled(),
        "status": "READY",
        "reason": "gate_disabled",
        "expected_selector_window": None,
        "latest_committed_run_id": run_id,
        "selector_active": False,
    }
    if not result["enabled"]:
        return result
    active = selector_is_active(root)
    result["selector_active"] = active
    if active:
        result.update(status="DEFERRED", reason="selector_active")
        return result

    latest_window: datetime | None = None
    for window in windows:
        candidate = current.replace(hour=window.hour, minute=window.minute, second=0, microsecond=0)
        if candidate <= current:
            latest_window = candidate
    if latest_window is None:
        result["reason"] = "before_selector_window"
        return result
    result["expected_selector_window"] = latest_window.isoformat()
    generated = _generated_at(bundle)
    if generated is None:
        result.update(status="STALE", reason="bundle_timestamp_missing")
        return result
    if generated < latest_window:
        result.update(status="STALE", reason="bundle_older_than_expected_window")
        return result
    result["reason"] = "latest_completed_bundle_after_expected_window"
    return result
