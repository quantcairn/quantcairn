"""Fail-closed authorization for broker mutations.

This module has no broker or network dependencies. Mutation is permitted only
for the exact LIVE_EXECUTION mode with an exact-value arm and an independently
readable kill switch set to OPEN. Missing, malformed, or unreadable state
denies by default.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIVE_ARM_VALUE = "YES"
KILL_SWITCH_VALUE = "OPEN"
LIVE_EXECUTION = "LIVE_EXECUTION"


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    reason_code: str
    execution_mode: str
    armed: bool
    kill_switch_state: str


def _normalized_mode(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw == "LIVE_EXECUTION":
        return raw
    if raw == "LIVE":
        return "LIVE"
    if raw in {"LIVE_OBSERVE_ONLY", "PAPER", "RESEARCH", "OFF"}:
        return raw
    return "UNKNOWN"


def _kill_switch_path() -> Path:
    override = os.environ.get("QUANTCAIRN_LIVE_KILL_SWITCH_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    state_dir = os.environ.get("SOXS_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser() / "live_execution_kill_switch.json"
    return Path(__file__).resolve().parents[2] / "state" / "live_execution_kill_switch.json"


def read_kill_switch_state(path: Path | None = None) -> str:
    try:
        payload = json.loads((path or _kill_switch_path()).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return "CLOSED"
        state = payload.get("state", payload.get("live_execution_kill_switch", ""))
        return KILL_SWITCH_VALUE if str(state).strip().upper() == KILL_SWITCH_VALUE else "CLOSED"
    except Exception:
        return "CLOSED"


def authorize_mutation(*, execution_mode: str | None = None, armed: str | None = None,
                       kill_switch_state: str | None = None,
                       kill_switch_path: Path | None = None) -> AuthorizationResult:
    try:
        mode = _normalized_mode(
            execution_mode
            if execution_mode is not None
            else os.environ.get("QUANTCAIRN_EXECUTION_MODE")
        )
        exact_armed = (
            armed if armed is not None else os.environ.get("QUANTCAIRN_LIVE_ARMED", "")
        ) == LIVE_ARM_VALUE
        if kill_switch_state is None:
            kill_state = read_kill_switch_state(kill_switch_path)
        else:
            kill_state = (
                KILL_SWITCH_VALUE if kill_switch_state == KILL_SWITCH_VALUE else "CLOSED"
            )
        if mode != LIVE_EXECUTION:
            return AuthorizationResult(False, "NOT_LIVE_EXECUTION", mode, exact_armed, kill_state)
        if not exact_armed:
            return AuthorizationResult(False, "LIVE_NOT_ARMED", mode, False, kill_state)
        if kill_state != KILL_SWITCH_VALUE:
            return AuthorizationResult(False, "KILL_SWITCH_CLOSED", mode, True, kill_state)
        return AuthorizationResult(True, "AUTHORIZED", mode, True, kill_state)
    except Exception:
        # Authorization failures must never become provider mutations.
        return AuthorizationResult(False, "AUTHORIZATION_ERROR", "UNKNOWN", False, "CLOSED")


def deny_result(*, execution_mode: str | None = None,
                reason_code: str = "AUTHORIZATION_ERROR") -> AuthorizationResult:
    """Construct an explicit deny result for fail-closed error paths."""
    mode = _normalized_mode(execution_mode or os.environ.get("QUANTCAIRN_EXECUTION_MODE"))
    return AuthorizationResult(False, reason_code, mode, False, "CLOSED")
