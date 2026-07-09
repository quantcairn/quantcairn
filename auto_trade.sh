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

cd "$PROJECT_DIR" || exit 1

if [ -f "$LOCAL_AI_ENV" ]; then
    set -a
    . "$LOCAL_AI_ENV"
    set +a
fi

echo "Using Python: $PYTHON_BIN"

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

case "$1" in
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
from datetime import datetime
from zoneinfo import ZoneInfo

from src.ai_selector.selection_state import verify_selection_state

required_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
ok, reason, state = verify_selection_state(required_et_date=required_date)
if not ok:
    print(f"Selection state verification failed: {reason}; state={state}")
    raise SystemExit(1)
print(f"Selection state verified for ET date {required_date}.")
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
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
