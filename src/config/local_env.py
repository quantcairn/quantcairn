from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
LOCAL_AI_ENV_PATH = PROJECT_DIR / ".env.ai_selector.local"

# These names represent execution capability, not ordinary application
# configuration.  PAPER/RESEARCH processes must not inherit or load them.
LIVE_EXECUTION_CREDENTIAL_ENV_KEYS = (
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_API_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_API_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
)


def sanitize_paper_environment() -> int:
    """Remove execution credentials and deny private-config fallback.

    This is intentionally process-local.  It never edits a secrets file and
    is safe to call repeatedly at PAPER/RESEARCH entry points.
    """
    removed = 0
    for key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS:
        if os.environ.pop(key, None) is not None:
            removed += 1
    os.environ["SOXS_DISABLE_LIVE_CREDENTIALS"] = "1"
    return removed


def load_live_execution_credentials(
    execution_mode: str | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Single explicit boundary for a future LIVE execution worker.

    PAPER, RESEARCH, and OBSERVE_ONLY callers are denied before any value is
    read.  LIVE workers receive only explicitly injected environment values;
    this function does not read files or private config.
    """
    mode = str(execution_mode or os.environ.get("QUANTCAIRN_EXECUTION_MODE", "")).strip().upper()
    if mode != "LIVE_EXECUTION":
        raise PermissionError("live execution credentials denied outside LIVE_EXECUTION")
    source = environ if environ is not None else os.environ
    values = {key: str(source.get(key, "")).strip() for key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS}
    required = (
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
    )
    if any(not values[key] for key in required):
        raise RuntimeError("live execution credentials unavailable")
    return values


def load_local_ai_env(path: Path | None = None) -> bool:
    target = path or LOCAL_AI_ENV_PATH
    if not target.exists():
        return False
    loaded = False
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key or key in os.environ:
            continue
        if key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS:
            continue
        os.environ[key] = value
        loaded = True
    return loaded
