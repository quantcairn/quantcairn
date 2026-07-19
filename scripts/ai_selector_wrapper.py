#!/usr/bin/env python3
"""Wrapper to run AI selector only on US market trading days before the open.

Behavior:
- Checks current time in America/New_York.
- If it's a weekday (Mon-Fri) and time is near 09:00 ET, runs the selector once per ET day.
- After a successful selection, restarts the TOP engines so they use the new configs.
- Respects env var `FORCE_AI_RUN=1` to force execution regardless of time.
"""
import os
import sys
import subprocess
import fcntl
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
VENV_PY = os.path.join(PROJECT_DIR, '.venv', 'bin', 'python')
VENV_PREFIX = os.path.join(PROJECT_DIR, '.venv')
if (
    os.path.exists(VENV_PY)
    and os.path.realpath(sys.prefix) != os.path.realpath(VENV_PREFIX)
    and os.environ.get("SOXS_SKIP_VENV_REEXEC") != "1"
):
    env = os.environ.copy()
    env["SOXS_SKIP_VENV_REEXEC"] = "1"
    os.execve(VENV_PY, [VENV_PY, __file__, *sys.argv[1:]], env)

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from src.config.local_env import load_local_ai_env
from src.utils.market_calendar import is_us_market_trading_day

SELECTOR = os.path.join(PROJECT_DIR, 'scripts', 'run_ai_selector.py')
OUT_LOG = os.path.join(PROJECT_DIR, 'logs', 'ai_selector.out.log')
ERR_LOG = os.path.join(PROJECT_DIR, 'logs', 'ai_selector.err.log')
STATE_DIR = os.environ.get("SOXS_STATE_DIR") or os.path.join(PROJECT_DIR, 'state')
LOCK_FILE = os.path.join(STATE_DIR, 'ai_selector.lock')


def _verbose(message: str) -> None:
    if str(os.environ.get("AI_SELECTOR_WRAPPER_VERBOSE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        print(message)


def is_trading_day(now_et: datetime) -> bool:
    return is_us_market_trading_day(now_et.date())


def is_market_time(now_et: datetime) -> bool:
    if not is_trading_day(now_et):
        return False
    # Run 30 minutes before the regular US session open.
    target = now_et.replace(hour=9, minute=0, second=0, microsecond=0)
    delta = abs((now_et - target).total_seconds())
    return delta <= 90


def already_ran_today(now_et: datetime) -> bool:
    marker = os.path.join(STATE_DIR, f"ai_selector_{now_et.date().isoformat()}.done")
    return os.path.exists(marker)


def mark_ran_today(now_et: datetime) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    marker = os.path.join(STATE_DIR, f"ai_selector_{now_et.date().isoformat()}.done")
    with open(marker, "w") as f:
        f.write(datetime.now().isoformat() + "\n")


def _run_selection_if_due():
    force = os.environ.get('FORCE_AI_RUN') == '1'

    if ZoneInfo is None:
        _verbose('zoneinfo not available; running selector only if FORCE_AI_RUN=1')
        if not force:
            return
        now_et = datetime.utcnow()
    else:
        now_et = datetime.now(ZoneInfo('America/New_York'))

    if not is_trading_day(now_et):
        _verbose(f'Not a US trading day (ET now={now_et}). Exiting.')
        if force and os.environ.get("FORCE_AI_RUN_ON_NON_TRADING_DAY") != "1":
            sys.exit(2)
        if not force:
            return
    if not force and not is_market_time(now_et):
        _verbose(f'Not market time (ET now={now_et}). Exiting.')
        return
    if not force and already_ran_today(now_et):
        _verbose(f'AI selector already ran for ET date {now_et.date().isoformat()}. Exiting.')
        return

    # execute selector
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    cmd = [py, SELECTOR]
    env = os.environ.copy()
    env.setdefault("AI_SELECTOR_LIVE_DATA", "1")
    env.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    env.setdefault("AI_SELECTOR_MAX_SYMBOLS", "50")
    os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
    with open(OUT_LOG, 'a') as out, open(ERR_LOG, 'a') as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, cwd=PROJECT_DIR, env=env)
        proc.wait()
    if proc.returncode != 0:
        print(f'AI selector failed with exit code {proc.returncode}.')
        sys.exit(proc.returncode)

    mark_ran_today(now_et)
    print(f'AI selector completed and TOP engines refreshed for ET date {now_et.date().isoformat()}.')


def main():
    load_local_ai_env()
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        # The launch job and minute-based wrapper can fire together. Serialize
        # them so trading never starts while configs are half-written.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _run_selection_if_due()


if __name__ == '__main__':
    main()
