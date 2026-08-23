#!/usr/bin/env python3
"""Wrapper to run AI selector only on US market trading days before the open.

Behavior:
- Checks current time in America/New_York.
- If it's a weekday (Mon-Fri) and time matches the season-specific pre-open
  ET window, runs the selector once per ET day.
- After a successful selection, restarts the TOP engines so they use the new configs.
- Respects env var `FORCE_AI_RUN=1` to force execution regardless of time.
"""
import os
import sys
import subprocess
import fcntl
from datetime import datetime
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


def _resolve_project_dir() -> str:
    for key in ("QUANTCAIRN_HOME", "SOXS_PROJECT_DIR"):
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


PROJECT_DIR = _resolve_project_dir()
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

from src.config.runtime_paths import resolve_logs_dir, resolve_state_dir
from src.config.local_env import load_local_ai_env
from src.utils.market_calendar import is_us_market_trading_day
from src.openalpha.top_restart import load_restart_status, record_restart_status, request_supervisor_restart

SELECTOR = os.path.join(PROJECT_DIR, 'scripts', 'run_ai_selector.py')
# Test-only dependency-injection hooks. Production paths are resolved by the
# operation-time helpers below, never frozen at module import.
OUT_LOG = None
ERR_LOG = None
STATE_DIR = None
LOCK_FILE = None


def _runtime_state_dir():
    return Path(STATE_DIR) if STATE_DIR is not None else resolve_state_dir(Path(PROJECT_DIR))


def _runtime_log_paths():
    logs_dir = resolve_logs_dir(Path(PROJECT_DIR))
    return (
        Path(OUT_LOG) if OUT_LOG is not None else logs_dir / "ai_selector.out.log",
        Path(ERR_LOG) if ERR_LOG is not None else logs_dir / "ai_selector.err.log",
    )


def _runtime_lock_file():
    return Path(LOCK_FILE) if LOCK_FILE is not None else _runtime_state_dir() / "ai_selector.lock"


def _verbose(message: str) -> None:
    # Always emit scheduler decisions to stderr so launchd logs capture them.
    print(message, file=sys.stderr, flush=True)
    if str(os.environ.get("OPENALPHA_WRAPPER_VERBOSE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        print(message)


def is_trading_day(now_et: datetime) -> bool:
    return is_us_market_trading_day(now_et.date())


def is_market_time(now_et: datetime) -> bool:
    if not is_trading_day(now_et):
        return False
    # Allow the selector to run in the early intraday window once the
    # market has settled past the open gap but before the midday stretch.
    # ET handles DST/standard time automatically, so the window is expressed
    # entirely in America/New_York time.
    window_start = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    window_end = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    return window_start <= now_et <= window_end


def already_ran_today(now_et: datetime) -> bool:
    marker = _runtime_state_dir() / f"ai_selector_{now_et.date().isoformat()}.done"
    return os.path.exists(marker)


def mark_ran_today(now_et: datetime) -> None:
    state_dir = _runtime_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / f"ai_selector_{now_et.date().isoformat()}.done"
    with open(marker, "w") as f:
        f.write(datetime.now().isoformat() + "\n")


def _compensate_top_restart() -> bool:
    """Retry only the supervisor restart; never rerun selector work."""

    status = load_restart_status(PROJECT_DIR) or {}
    if str(status.get("status") or "").upper() not in {"FAILED", "PENDING"}:
        return False
    selection_run_id = str(status.get("selection_run_id") or "").strip()
    if not selection_run_id:
        return False
    bundle_hash = str(status.get("selection_bundle_hash") or "")
    print(f"TOP restart compensation requested for selection {selection_run_id}.")
    code = request_supervisor_restart(PROJECT_DIR)
    if code != 0:
        record_restart_status(
            status="FAILED",
            selection_run_id=selection_run_id,
            selection_bundle_hash=bundle_hash,
            error=f"compensation_exit_{code}",
            project_dir=PROJECT_DIR,
        )
        return False
    record_restart_status(
        status="CONFIRMED",
        selection_run_id=selection_run_id,
        selection_bundle_hash=bundle_hash,
        project_dir=PROJECT_DIR,
    )
    return True


def _run_selection_if_due():
    force = os.environ.get('FORCE_AI_RUN') == '1'

    if ZoneInfo is None:
        _verbose('zoneinfo not available; running selector only if FORCE_AI_RUN=1')
        if not force:
            return
        now_et = datetime.utcnow()
    else:
        now_et = datetime.now(ZoneInfo('America/New_York'))

    trading_day = is_trading_day(now_et)

    if not trading_day:
        reason = "weekend" if now_et.weekday() >= 5 else "market_holiday"
        _verbose(
            f'[SCHEDULER] ai_selector decision=skipped'
            f' reason=non_trading_day({reason})'
            f' et_date={now_et.date().isoformat()}'
            f' et_time={now_et.strftime("%H:%M")}'
            f' force={force}'
            f' pid={os.getpid()}'
        )
        if force and os.environ.get("FORCE_AI_RUN_ON_NON_TRADING_DAY") != "1":
            sys.exit(2)
        if not force:
            return

    if not force and not is_market_time(now_et):
        _verbose(
            f'[SCHEDULER] ai_selector decision=skipped'
            f' reason=outside_market_time_window'
            f' et_date={now_et.date().isoformat()}'
            f' et_time={now_et.strftime("%H:%M")}'
            f' trading_day={trading_day}'
            f' pid={os.getpid()}'
        )
        return

    if not force and already_ran_today(now_et):
        _verbose(
            f'[SCHEDULER] ai_selector decision=skipped'
            f' reason=already_ran_today'
            f' et_date={now_et.date().isoformat()}'
            f' pid={os.getpid()}'
        )
        return

    _verbose(
        f'[SCHEDULER] ai_selector decision=run'
        f' et_date={now_et.date().isoformat()}'
        f' et_time={now_et.strftime("%H:%M")}'
        f' trading_day={trading_day}'
        f' force={force}'
        f' pid={os.getpid()}'
    )

    # execute selector
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    cmd = [py, SELECTOR]
    env = os.environ.copy()
    env.setdefault("OPENALPHA_LIVE_DATA", "1")
    env.setdefault("OPENALPHA_FETCH_NEWS", "0")
    env.setdefault("OPENALPHA_MAX_SYMBOLS", "35")
    env.setdefault("OPENALPHA_TOTAL_BUDGET_SECONDS", "180")
    env.setdefault("OPENALPHA_QUALITY_BUDGET_SECONDS", "60")
    # Disable curl_cffi to avoid TLS library conflicts with Surge proxy.
    # curl_cffi bundles its own libcurl/TLS which can't validate Surge's
    # MITM certificates, producing:
    #   curl: (35) TLS connect error: error:00000000:invalid library (0)
    #   :OPENSSL_internal:invalid library (0)
    # Python's native requests + Homebrew OpenSSL works correctly with Surge.
    # Set YF_DISABLE_CURL_CFFI=0 to override (e.g. when not behind Surge).
    env.setdefault("YF_DISABLE_CURL_CFFI", "1")
    out_log, err_log = _runtime_log_paths()
    out_log.parent.mkdir(parents=True, exist_ok=True)
    with open(out_log, 'a') as out, open(err_log, 'a') as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, cwd=PROJECT_DIR, env=env)
        proc.wait()
    if proc.returncode != 0:
        if _compensate_top_restart():
            print('TOP restart compensation completed without rerunning selector.')
            return
        print(f'AI selector failed with exit code {proc.returncode}.')
        sys.exit(proc.returncode)

    mark_ran_today(now_et)
    print(f'AI selector completed and TOP engines refreshed for ET date {now_et.date().isoformat()}.')

    # Fire-and-forget: generate daily research report after successful selection.
    # The research report is a non-critical artifact consumed by the dashboard.
    # A failure here is logged but never blocks the wrapper or affects trading.
    try:
        _verbose('[SCHEDULER] triggering daily research report generation')
        report_script = os.path.join(PROJECT_DIR, 'scripts', 'generate_daily_research_report.py')
        report_env = os.environ.copy()
        report_env.setdefault("YF_DISABLE_CURL_CFFI", "1")
        subprocess.run(
            [py, report_script, '--no-site'],
            cwd=PROJECT_DIR,
            env=report_env,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        pass  # fire-and-forget — research report generation is best-effort


def main():
    load_local_ai_env()
    lock_file = _runtime_lock_file()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as lock:
        # The launch job and minute-based wrapper can fire together. Serialize
        # them so trading never starts while configs are half-written.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _run_selection_if_due()


if __name__ == '__main__':
    main()
