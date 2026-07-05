#!/bin/bash
set -euo pipefail

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
MULTI_LAUNCH="$PROJECT_DIR/multi_launch.sh"
AUTO_TRADE="$PROJECT_DIR/auto_trade.sh"
LOG_DIR="${SOXS_LOG_DIR:-$PROJECT_DIR/logs}"

cd "$PROJECT_DIR" || exit 1

show_help() {
    cat <<'EOF'
用法: ./tradectl.sh {up|down|restart|restart-top|restart-combined|status|logs}

  up                启动整套交易服务
  down              停止整套交易服务
  restart           重启整套交易服务
  restart-top       仅重启 TOP1-5 引擎
  restart-combined  仅重启 8090 总览页
  status            查看当前运行状态
  logs              查看主日志尾部
EOF
}

case "${1:-}" in
    up)
        "$AUTO_TRADE" start
        ;;
    down)
        "$AUTO_TRADE" stop
        ;;
    restart)
        "$MULTI_LAUNCH" restart-all
        ;;
    restart-top)
        "$MULTI_LAUNCH" restart-top
        ;;
    restart-combined)
        "$MULTI_LAUNCH" restart-combined
        ;;
    status)
        "$AUTO_TRADE" status
        echo ""
        "$MULTI_LAUNCH" status || true
        ;;
    logs)
        tail -n 80 "$LOG_DIR/trading.log"
        ;;
    *)
        show_help
        exit 1
        ;;
esac
