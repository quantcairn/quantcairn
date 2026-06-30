#!/usr/bin/env python3
"""Wrapper to run AI selector only on US market trading days before the open.

Behavior:
- Checks current time in America/New_York.
- If it's a weekday (Mon-Fri) and time is near 09:29 ET, runs the selector once per ET day.
- After a successful selection, restarts TOP1/TOP2/TOP3 paper engines so they use the new configs.
- Respects env var `FORCE_AI_RUN=1` to force execution regardless of time.
"""
import os
import sys
import subprocess
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
VENV_PY = os.path.join(PROJECT_DIR, '.venv', 'bin', 'python')
SELECTOR = os.path.join(PROJECT_DIR, 'scripts', 'run_ai_selector.py')
OUT_LOG = os.path.join(PROJECT_DIR, 'logs', 'ai_selector.out.log')
ERR_LOG = os.path.join(PROJECT_DIR, 'logs', 'ai_selector.err.log')
STATE_DIR = os.path.join(PROJECT_DIR, 'state')
MULTI_LAUNCH = os.path.join(PROJECT_DIR, 'multi_launch.sh')


def is_market_time(now_et: datetime) -> bool:
    # Trading days: Mon-Fri. (This wrapper does not yet account for exchange holidays.)
    if now_et.weekday() >= 5:
        return False
    # Accept a tight window around 09:29 ET. launchd runs this wrapper every minute.
    target = now_et.replace(hour=9, minute=29, second=0, microsecond=0)
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


def restart_top_engines() -> int:
    if os.environ.get("AI_SELECTOR_RESTART_TOP", "1") == "0":
        print("AI_SELECTOR_RESTART_TOP=0; not restarting TOP engines.")
        return 0
    if not os.path.exists(MULTI_LAUNCH):
        print(f"Missing launcher: {MULTI_LAUNCH}")
        return 1
    return subprocess.run(["/bin/bash", MULTI_LAUNCH, "restart-top"], cwd=PROJECT_DIR).returncode


def main():
    force = os.environ.get('FORCE_AI_RUN') == '1'

    if ZoneInfo is None:
        print('zoneinfo not available; running selector only if FORCE_AI_RUN=1')
        if not force:
            return
        now_et = datetime.utcnow()
    else:
        now_et = datetime.now(ZoneInfo('America/New_York'))

    if not force and not is_market_time(now_et):
        print(f'Not market time (ET now={now_et}). Exiting.')
        return
    if not force and already_ran_today(now_et):
        print(f'AI selector already ran for ET date {now_et.date().isoformat()}. Exiting.')
        return

    # execute selector
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    cmd = [py, SELECTOR]
    env = os.environ.copy()
    env.setdefault("AI_SELECTOR_LIVE_DATA", "0")
    env.setdefault("AI_SELECTOR_FETCH_NEWS", "0")
    env.setdefault("AI_SELECTOR_MAX_SYMBOLS", "20")
    os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
    with open(OUT_LOG, 'a') as out, open(ERR_LOG, 'a') as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, cwd=PROJECT_DIR, env=env)
        proc.wait()
    if proc.returncode != 0:
        print(f'AI selector failed with exit code {proc.returncode}.')
        sys.exit(proc.returncode)

    restart_code = restart_top_engines()
    if restart_code != 0:
        print(f'TOP restart failed with exit code {restart_code}.')
        sys.exit(restart_code)

    mark_ran_today(now_et)
    print(f'AI selector completed and TOP engines refreshed for ET date {now_et.date().isoformat()}.')


if __name__ == '__main__':
    main()
