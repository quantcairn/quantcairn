#!/bin/bash
# SOXS 区间套利 — 自动启停脚本
# 由 cron 调用，北京时间 21:25 启动，04:05 停止

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_FILE="$PROJECT_DIR/trading.log"

cd "$PROJECT_DIR" || exit 1

case "$1" in
    start)
        # 杀掉可能残留的旧进程
        pkill -f "run.py" 2>/dev/null
        sleep 1

        # 清缓存
        find "$PROJECT_DIR" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null

        # 归档上一条日志，新一天从零开始
        if [ -f "$LOG_FILE" ]; then
            cp "$LOG_FILE" "${LOG_FILE%.log}-$(date +%Y%m%d).log" 2>/dev/null
        fi
        > "$LOG_FILE"
        > "$PROJECT_DIR/snapshots.log"

        # 启动（不跳过交易时间检查，仅盘中运行）
        nohup "$VENV_PYTHON" run.py --paper --dashboard >> "$LOG_FILE" 2>&1 &

        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 SOXS Range Arbitrage started (PID: $!)" | tee -a "$LOG_FILE"
        ;;

    stop)
        pkill -f "run.py" 2>/dev/null
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛑 SOXS Range Arbitrage stopped" | tee -a "$LOG_FILE"
        ;;

    status)
        if pgrep -f "run.py" > /dev/null; then
            echo "✅ Running (PID: $(pgrep -f 'run.py'))"
            echo "Dashboard: http://localhost:8080"
        else
            echo "❌ Not running"
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
