#!/bin/bash
# AI Top5 区间套利 — 自动启停脚本
# 由 cron 或 launchd 调用。
# AI 选股由独立任务在美东 09:00 运行；本脚本负责后续启动/停止交易引擎。

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
LOCAL_AI_ENV="$PROJECT_DIR/.env.ai_selector.local"
PYTHON_BIN="${SOXS_PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3)"
    fi
fi
LOG_DIR="${SOXS_LOG_DIR:-${TMPDIR:-/private/tmp}/soxs-range-arbitrage/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/trading.log"
MULTI_LAUNCH="$PROJECT_DIR/multi_launch.sh"
STATE_DIR="${SOXS_STATE_DIR:-$PROJECT_DIR/state}"
SCHEDULE_DIR="$STATE_DIR/auto_trade_schedule"

cd "$PROJECT_DIR" || exit 1

if [ -f "$LOCAL_AI_ENV" ]; then
    set -a
    . "$LOCAL_AI_ENV"
    set +a
fi

case "${1:-}" in
    scheduled-start|scheduled-stop)
        ;;
    *)
        echo "Using Python: $PYTHON_BIN"
        ;;
esac

is_trading_day_now() {
    "$PYTHON_BIN" - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.ai_selector_wrapper import is_trading_day

now_et = datetime.now(ZoneInfo("America/New_York"))
if not is_trading_day(now_et):
    print(f"Non-trading day in ET: {now_et.date().isoformat()}")
    raise SystemExit(1)
print(f"Trading day verified in ET: {now_et.date().isoformat()}")
PY
}

schedule_marker_path() {
    local action="$1"
    "$PYTHON_BIN" - "$SCHEDULE_DIR" "$action" <<'PY'
import sys
from pathlib import Path
from src.utils.trading_schedule import parse_env_now

schedule_dir = Path(sys.argv[1])
action = sys.argv[2]
now_et = parse_env_now(__import__("os").environ.get("SOXS_SCHEDULE_NOW_ET"))
if now_et is None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
print(schedule_dir / f"{action}_{now_et.date().isoformat()}.done")
PY
}

should_run_scheduled_action() {
    local action="$1"
    local marker="$2"
    "$PYTHON_BIN" - "$action" "$marker" <<'PY'
import os
import sys
from pathlib import Path

from src.utils.trading_schedule import auto_trade_decision, parse_env_now

action = sys.argv[1]
marker = Path(sys.argv[2])
decision = auto_trade_decision(
    action,
    now_et=parse_env_now(os.environ.get("SOXS_SCHEDULE_NOW_ET")),
    already_ran=marker.exists(),
)
verbose = str(os.environ.get("SOXS_SCHEDULE_VERBOSE") or "").strip().lower() in {"1", "true", "yes", "on"}
if decision.should_run or verbose:
    print(
        "scheduled_decision "
        f"action={decision.action} "
        f"should_run={str(decision.should_run).lower()} "
        f"reason={decision.reason} "
        f"now_et={decision.now_et.isoformat()} "
        f"session_date={decision.session_date} "
        f"required_selection_date={decision.required_selection_date}"
    )
raise SystemExit(0 if decision.should_run else 2)
PY
}

mark_scheduled_action_done() {
    local action="$1"
    local marker="$2"
    mkdir -p "$(dirname "$marker")"
    "$PYTHON_BIN" - "$marker" <<'PY'
import sys
from pathlib import Path
from src.utils.trading_schedule import marker_timestamp, parse_env_now
import os

marker = Path(sys.argv[1])
marker.parent.mkdir(parents=True, exist_ok=True)
marker.write_text(marker_timestamp(parse_env_now(os.environ.get("SOXS_SCHEDULE_NOW_ET"))) + "\n", encoding="utf-8")
PY
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scheduled $action marked done: $marker" \
        | tee -a "$LOG_FILE"
}

case "$1" in
    scheduled-start)
        marker="$(schedule_marker_path scheduled-start)"
        if ! should_run_scheduled_action scheduled-start "$marker" >> "$LOG_FILE" 2>&1; then
            exit 0
        fi
        "$0" start
        status=$?
        if [ "$status" -eq 0 ]; then
            mark_scheduled_action_done scheduled-start "$marker"
        fi
        exit "$status"
        ;;

    scheduled-stop)
        marker="$(schedule_marker_path scheduled-stop)"
        if ! should_run_scheduled_action scheduled-stop "$marker" >> "$LOG_FILE" 2>&1; then
            exit 0
        fi
        "$0" stop
        status=$?
        if [ "$status" -eq 0 ]; then
            mark_scheduled_action_done scheduled-stop "$marker"
        fi
        exit "$status"
        ;;

    start)
        if ! is_trading_day_now >> "$LOG_FILE" 2>&1; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Non-trading day; trading start skipped" \
                | tee -a "$LOG_FILE"
            exit 0
        fi

        # 清缓存
        find "$PROJECT_DIR" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null

        # 归档上一条日志，新一天从零开始
        if [ -f "$LOG_FILE" ]; then
            cp "$LOG_FILE" "${LOG_FILE%.log}-$(date +%Y%m%d).log" 2>/dev/null
        fi
        > "$LOG_FILE"
        > "$PROJECT_DIR/snapshots.log"

        # Refresh configs only after live holdings are verified. A failed
        # selector must prevent old TOP configs from entering live trading.
        if ! FORCE_AI_RUN=1 AI_SELECTOR_RESTART_TOP=0 \
            "$PYTHON_BIN" "$PROJECT_DIR/scripts/ai_selector_wrapper.py" \
            >> "$LOG_FILE" 2>&1; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] AI selection failed; trading start aborted" \
                | tee -a "$LOG_FILE"
            exit 1
        fi

        if ! "$PYTHON_BIN" - <<'PY' >> "$LOG_FILE" 2>&1
from zoneinfo import ZoneInfo

from src.ai_selector.selection_state import verify_selection_state
from src.utils.market_calendar import required_selection_date

from datetime import datetime

now_et = datetime.now(ZoneInfo("America/New_York"))
required_date = required_selection_date(now_et)
ok, reason, state = verify_selection_state(required_et_date=required_date)
if not ok:
    print(f"Selection state verification failed: {reason}; state={state}")
    raise SystemExit(1)
print(f"Selection state verified for required selection date {required_date}.")
PY
        then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Selection freshness check failed; trading start aborted" \
                | tee -a "$LOG_FILE"
            exit 1
        fi

        if ! "$MULTI_LAUNCH" start >> "$LOG_FILE" 2>&1; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Multi-launch startup failed; trading start aborted" \
                | tee -a "$LOG_FILE"
            exit 1
        fi
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 AI Top3 trading started" | tee -a "$LOG_FILE"
        ;;

    stop)
        "$MULTI_LAUNCH" stop >> "$LOG_FILE" 2>&1
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛑 AI Top3 trading stopped" | tee -a "$LOG_FILE"
        ;;

    status)
        if pgrep -f "run.py --config .*configs/TOP" > /dev/null; then
            echo "✅ Running"
            echo "Dashboard: http://localhost:8090"
        else
            echo "❌ Not running"
        fi
        ;;

    *)
        echo "Usage: $0 {scheduled-start|scheduled-stop|start|stop|status}"
        exit 1
        ;;
esac
