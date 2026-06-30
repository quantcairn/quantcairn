#!/bin/bash
# AI Top3 区间套利 — 自动启停脚本
# 由 cron 或 launchd 调用，北京时间 21:25 启动，04:05 停止

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
LOG_DIR="${SOXS_LOG_DIR:-${TMPDIR:-/private/tmp}/soxs-range-arbitrage/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/trading.log"
MULTI_LAUNCH="$PROJECT_DIR/multi_launch.sh"

cd "$PROJECT_DIR" || exit 1

case "$1" in
    start)
        # 清缓存
        find "$PROJECT_DIR" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null

        # 归档上一条日志，新一天从零开始
        if [ -f "$LOG_FILE" ]; then
            cp "$LOG_FILE" "${LOG_FILE%.log}-$(date +%Y%m%d).log" 2>/dev/null
        fi
        > "$LOG_FILE"
        > "$PROJECT_DIR/snapshots.log"

        "$MULTI_LAUNCH" start-foreground >> "$LOG_FILE" 2>&1
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
