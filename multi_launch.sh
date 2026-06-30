#!/bin/bash
# AI 选股后的 TOP5 并发交易系统
# TOP1 (8091) + TOP2 (8092) + TOP3 (8093) + TOP4 (8094) + TOP5 (8095)

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="${SOXS_LOG_DIR:-$PROJECT_DIR/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
USE_LAUNCHD_TOPS="${SOXS_USE_LAUNCHD_TOPS:-0}"
UID_NUM="$(id -u)"
TOP_ENGINES=(TOP1 TOP2 TOP3 TOP4 TOP5)

cd "$PROJECT_DIR" || exit 1

port_for_top() {
    local top_name="$1"
    printf '%s' $((8090 + ${top_name:3}))
}

kill_listener_on_port() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        local pids
        pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')"
        if [ -n "$pids" ]; then
            kill $pids 2>/dev/null || true
            sleep 1
            kill -9 $pids 2>/dev/null || true
        fi
    fi
}

kill_dashboard_ports() {
    kill_listener_on_port 8090
    for top_name in "${TOP_ENGINES[@]}"; do
        kill_listener_on_port "$(port_for_top "$top_name")"
    done
}

stop_existing() {
    pkill -f "run.py --config configs/DRIP.yaml" 2>/dev/null
    pkill -f "run.py --config configs/AMC.yaml" 2>/dev/null
    pkill -f "run.py --config configs/SMR.yaml" 2>/dev/null
    stop_top
    pkill -f "from src.dashboard.combined import start_combined" 2>/dev/null
    pkill -f "start_combined(8090)" 2>/dev/null
    kill_dashboard_ports
}

stop_top() {
    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        for job in com.soxs.top1 com.soxs.top2 com.soxs.top3 com.soxs.top4 com.soxs.top5; do
            launchctl bootout gui/"$UID_NUM"/"$job" 2>/dev/null || true
        done
    else
        for top_name in "${TOP_ENGINES[@]}"; do
            pkill -f "run.py --config .*configs/${top_name}.yaml" 2>/dev/null
        done
        for top_name in "${TOP_ENGINES[@]}"; do
            kill_listener_on_port "$(port_for_top "$top_name")"
        done
    fi
}

start_top() {
    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        for job in com.soxs.top1 com.soxs.top2 com.soxs.top3 com.soxs.top4 com.soxs.top5; do
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
    for TOP in "${TOP_ENGINES[@]}"; do
        cfg="$PROJECT_DIR/configs/${TOP}.yaml"
        if [ -f "$cfg" ]; then
            port="$(port_for_top "$TOP")"
            log_name=$(printf '%s' "$TOP" | tr '[:upper:]' '[:lower:]')
            read ENGINE_MODE SYNTH_START SYNTH_AMP <<EOF
$( "$VENV_PYTHON" - "$cfg" <<'PY'
import sys, yaml
cfg_path = sys.argv[1]
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
mode = str(cfg.get("mode", "paper")).strip().lower()
range_cfg = cfg.get("range") or {}
support = range_cfg.get("support_price")
resistance = range_cfg.get("resistance_price")
mid = 100.0
amp = 3.0
try:
    support = float(support) if support is not None else None
    resistance = float(resistance) if resistance is not None else None
except (TypeError, ValueError):
    support = None
    resistance = None
if support is not None and resistance is not None and support > 0 and resistance > support:
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
    for top_name in "${TOP_ENGINES[@]}"; do
        echo "   ${top_name}:     http://localhost:$(port_for_top "$top_name")"
    done
    echo "   COMBINED: http://localhost:8090  ← AI Top5 总览"

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
        echo "  📊 AI Top5 Trading Status"
        echo "═══════════════════════════════════"
        for ticker in "${TOP_ENGINES[@]}"; do
            port="$(port_for_top "$ticker")"

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
        echo "║  📊 AI Top5 总盈亏汇总                                  ║"
        echo "╠══════════════════════════════════════════════════════════╣"
        total_pnl=0
        for ticker in "${TOP_ENGINES[@]}"; do
            port="$(port_for_top "$ticker")"
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
