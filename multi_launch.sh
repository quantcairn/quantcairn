#!/bin/bash
# AI 选股后的 TOP3 并发交易系统
# TOP1 (8091) + TOP2 (8092) + TOP3 (8093)

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="${SOXS_LOG_DIR:-$PROJECT_DIR/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
USE_LAUNCHD_TOPS="${SOXS_USE_LAUNCHD_TOPS:-0}"
UID_NUM="$(id -u)"

cd "$PROJECT_DIR" || exit 1

stop_existing() {
    pkill -f "run.py --config configs/DRIP.yaml" 2>/dev/null
    pkill -f "run.py --config configs/AMC.yaml" 2>/dev/null
    pkill -f "run.py --config configs/SMR.yaml" 2>/dev/null
    stop_top
    pkill -f "from src.dashboard.combined import start_combined" 2>/dev/null
    pkill -f "start_combined(8090)" 2>/dev/null
}

stop_top() {
    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        launchctl bootout gui/"$UID_NUM"/com.soxs.top1 2>/dev/null || true
        launchctl bootout gui/"$UID_NUM"/com.soxs.top2 2>/dev/null || true
        launchctl bootout gui/"$UID_NUM"/com.soxs.top3 2>/dev/null || true
    else
        pkill -f "run.py --config .*configs/TOP1.yaml" 2>/dev/null
        pkill -f "run.py --config .*configs/TOP2.yaml" 2>/dev/null
        pkill -f "run.py --config .*configs/TOP3.yaml" 2>/dev/null
    fi
}

start_top() {
    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        for job in com.soxs.top1 com.soxs.top2 com.soxs.top3; do
            plist="$PROJECT_DIR/launchd/${job}.plist"
            if [ -f "$plist" ]; then
                launchctl bootstrap gui/"$UID_NUM" "$plist" 2>/dev/null || true
                launchctl kickstart -k gui/"$UID_NUM"/"$job" 2>/dev/null || true
            fi
        done
        echo "🚀 TOP engines managed by launchd"
        return 0
    fi

    TOP_PIDS=""
    for TOP in TOP1 TOP2 TOP3; do
        cfg="$PROJECT_DIR/configs/${TOP}.yaml"
        if [ -f "$cfg" ]; then
            port=$((8090 + ${TOP:3} ))
            log_name=$(printf '%s' "$TOP" | tr '[:upper:]' '[:lower:]')
            read ENGINE_MODE SYNTH_START SYNTH_AMP <<EOF
$( "$VENV_PYTHON" - "$cfg" <<'PY'
import sys, yaml
cfg_path = sys.argv[1]
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
mode = str(cfg.get("mode", "paper")).strip().lower()
support = float(cfg["range"]["support_price"])
resistance = float(cfg["range"]["resistance_price"])
mid = (support + resistance) / 2.0
amp = (((resistance - support) / 2.0) / mid * 100.0) + 2.0
print(f"{mode} {mid:.4f} {amp:.4f}")
PY
)
EOF
            cli_mode="--paper"
            if [ "$ENGINE_MODE" = "live" ]; then
                cli_mode="--live"
                : > "$LOG_DIR/${log_name}.log"
                nohup "$VENV_PYTHON" run.py --config "$cfg" "$cli_mode" --dashboard --anytime --port $port >> "$LOG_DIR/${log_name}.log" 2>&1 &
            else
                : > "$LOG_DIR/${log_name}.log"
                SOXS_SYNTHETIC_MARKET=1 \
                SOXS_SYNTHETIC_START_PRICE="$SYNTH_START" \
                SOXS_SYNTHETIC_AMPLITUDE_PCT="$SYNTH_AMP" \
                SOXS_SYNTHETIC_PERIOD_SECONDS=120 \
                nohup "$VENV_PYTHON" run.py --config "$cfg" "$cli_mode" --dashboard --anytime --port $port >> "$LOG_DIR/${log_name}.log" 2>&1 &
            fi
            TOP_PIDS="$TOP_PIDS $!"
            echo "🚀 $TOP on :$port (PID $!, mode=$ENGINE_MODE)"
        fi
    done
}

start_all() {
    wait_for_children="$1"

    stop_existing
    sleep 1

    find "$PROJECT_DIR" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null

    # Start combined dashboard
    : > "$LOG_DIR/combined.log"
    nohup "$VENV_PYTHON" -c "
import sys; sys.path.insert(0,'$PROJECT_DIR')
from src.dashboard.combined import start_combined
start_combined(8090)
import time
while True: time.sleep(60)
" >> "$LOG_DIR/combined.log" 2>&1 &
    pids="$pids $!"
    echo "📊 COMBINED on :8090 (PID $!)"

    start_top
    pids="$pids $TOP_PIDS"

    echo ""
    echo "📊 Dashboards:"
    echo "   TOP1:     http://localhost:8091"
    echo "   TOP2:     http://localhost:8092"
    echo "   TOP3:     http://localhost:8093"
    echo "   COMBINED: http://localhost:8090  ← AI Top3 总览"

    if [ "$wait_for_children" = "wait" ]; then
        trap 'stop_existing; exit 0' INT TERM
        wait $pids
    fi
}

case "$1" in
    start)
        start_all
        ;;

    start-foreground)
        start_all wait
        ;;

    stop)
        stop_existing
        echo "🛑 All engines stopped"
        ;;

    restart-top)
        stop_top
        sleep 1
        start_top >/dev/null
        echo "🔄 TOP engines restarted"
        ;;

    restart-combined)
        pkill -f "from src.dashboard.combined import start_combined" 2>/dev/null
        pkill -f "start_combined(8090)" 2>/dev/null
        sleep 1
        : > "$LOG_DIR/combined.log"
        nohup "$VENV_PYTHON" scripts/start_combined.py >> "$LOG_DIR/combined.log" 2>&1 &
        echo "🔄 Combined dashboard restarted"
        ;;

    status)
        echo "═══════════════════════════════════"
        echo "  📊 AI Top3 Trading Status"
        echo "═══════════════════════════════════"
        for ticker in TOP1 TOP2 TOP3; do
            port=8091
            [ "$ticker" = "TOP2" ] && port=8092
            [ "$ticker" = "TOP3" ] && port=8093

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
        echo "║  📊 AI Top3 总盈亏汇总                                  ║"
        echo "╠══════════════════════════════════════════════════════════╣"
        total_pnl=0
        for ticker in TOP1 TOP2 TOP3; do
            port=8091
            [ "$ticker" = "TOP2" ] && port=8092
            [ "$ticker" = "TOP3" ] && port=8093
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
        echo "Usage: $0 {start|start-foreground|stop|restart-top|restart-combined|status|summary}"
        ;;
esac
