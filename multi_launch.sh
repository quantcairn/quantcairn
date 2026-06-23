#!/bin/bash
# 三标的并发交易系统
# DRIP (8080) + AMC (8081) + SMR (8082)

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR" || exit 1

stop_existing() {
    pkill -f "run.py --config configs/DRIP.yaml" 2>/dev/null
    pkill -f "run.py --config configs/AMC.yaml" 2>/dev/null
    pkill -f "run.py --config configs/SMR.yaml" 2>/dev/null
    pkill -f "from src.dashboard.combined import start_combined" 2>/dev/null
    pkill -f "start_combined(8090)" 2>/dev/null
}

case "$1" in
    start)
        stop_existing
        sleep 1

        find "$PROJECT_DIR" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null

        # Launch all 3
        nohup "$VENV_PYTHON" run.py --config configs/DRIP.yaml --paper --dashboard --port 8080 >> "$LOG_DIR/drip.log" 2>&1 &
        echo "🚀 DRIP on :8080 (PID $!)"

        nohup "$VENV_PYTHON" run.py --config configs/AMC.yaml --paper --dashboard --port 8081 >> "$LOG_DIR/amc.log" 2>&1 &
        echo "🚀 AMC  on :8081 (PID $!)"

        nohup "$VENV_PYTHON" run.py --config configs/SMR.yaml --paper --dashboard --port 8082 >> "$LOG_DIR/smr.log" 2>&1 &
        echo "🚀 SMR  on :8082 (PID $!)"

        # Start combined dashboard
        nohup "$VENV_PYTHON" -c "
import sys; sys.path.insert(0,'$PROJECT_DIR')
from src.dashboard.combined import start_combined
start_combined(8090)
import time
while True: time.sleep(60)
" >> "$LOG_DIR/combined.log" 2>&1 &
        echo "📊 COMBINED on :8090 (PID $!)"

        echo ""
        echo "📊 Dashboards:"
        echo "   DRIP:     http://localhost:8080"
        echo "   AMC:      http://localhost:8081"
        echo "   SMR:      http://localhost:8082"
        echo "   COMBINED: http://localhost:8090  ← 三标的总览"
        ;;

    stop)
        stop_existing
        echo "🛑 All engines stopped"
        ;;

    status)
        echo "═══════════════════════════════════"
        echo "  📊 Multi-Stock Trading Status"
        echo "═══════════════════════════════════"
        for ticker in DRIP AMC SMR; do
            port=8080
            [ "$ticker" = "AMC" ] && port=8081
            [ "$ticker" = "SMR" ] && port=8082

            status=$(curl -s "http://localhost:$port/api/status" 2>/dev/null)
            if [ -n "$status" ]; then
                price=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"\${d['price']:.2f}\")" 2>/dev/null)
                signal=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['last_signal'])" 2>/dev/null)
                pnl=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"\${d['daily_pnl']:+.2f}\")" 2>/dev/null)
                trades=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['trades_today'])" 2>/dev/null)
                halted=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print('⛔' if d['halted'] else '✅')" 2>/dev/null)
                echo "  $ticker: \$$price | $signal | PnL=\$$pnl | $trades trades | $halted"
            else
                echo "  $ticker: ❌ not running"
            fi
        done
        echo ""
        ;;

    summary)
        echo ""
        echo "╔══════════════════════════════════════════════════════════╗"
        echo "║  📊 三标的总盈亏汇总                                    ║"
        echo "╠══════════════════════════════════════════════════════════╣"
        total_pnl=0
        for ticker in DRIP AMC SMR; do
            port=8080
            [ "$ticker" = "AMC" ] && port=8081
            [ "$ticker" = "SMR" ] && port=8082
            status=$(curl -s "http://localhost:$port/api/status" 2>/dev/null)
            if [ -n "$status" ]; then
                pnl=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['daily_pnl'])" 2>/dev/null)
                trades=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['trades_today'])" 2>/dev/null)
                equity=$(echo "$status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"\${d['equity']:.2f}\")" 2>/dev/null)
                echo "║  $ticker  PnL=\$$(printf '%+8.2f' $pnl)  Trades=$trades  Equity=\$$equity"
                total_pnl=$(echo "$total_pnl + $pnl" | bc -l 2>/dev/null || echo "0")
            fi
        done
        echo "╠══════════════════════════════════════════════════════════╣"
        echo "║  💰 TOTAL P&L: \$$(printf '%+8.2f' $total_pnl)"
        echo "╚══════════════════════════════════════════════════════════╝"
        echo ""
        ;;

    *)
        echo "Usage: $0 {start|stop|status|summary}"
        ;;
esac
