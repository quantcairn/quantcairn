#!/bin/bash
# AI 选股后的 TOP5 并发交易系统
# TOP1 (8091) + TOP2 (8092) + TOP3 (8093) + TOP4 (8094) + TOP5 (8095)

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
LOCAL_AI_ENV="$PROJECT_DIR/.env.ai_selector.local"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="${SOXS_LOG_DIR:-$PROJECT_DIR/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
USE_LAUNCHD_TOPS="${SOXS_USE_LAUNCHD_TOPS:-0}"
UID_NUM="$(id -u)"
TOP_ENGINES=(TOP1 TOP2 TOP3 TOP4 TOP5)
ORPHAN_MONITOR_SCRIPT="$PROJECT_DIR/scripts/start_orphan_monitor.py"
COMBINED_JOB="com.soxs.combined"

cd "$PROJECT_DIR" || exit 1

if [ -f "$LOCAL_AI_ENV" ]; then
    set -a
    . "$LOCAL_AI_ENV"
    set +a
fi

is_trading_day_now() {
    "$VENV_PYTHON" - <<'PY'
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

verify_same_day_selection_if_live() {
    "$VENV_PYTHON" - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo

from src.ai_selector.selection_state import verify_live_startup_selection

required_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
ok, reason, state = verify_live_startup_selection(required_et_date=required_date)
if not ok:
    print(f"Live startup blocked: {reason}")
    raise SystemExit(1)
print(f"Selection freshness verified for live startup: {required_date} ({reason})")
PY
}

port_for_top() {
    local top_name="$1"
    printf '%s' $((8090 + ${top_name:3}))
}

launchd_job_for_top() {
    local top_name="$1"
    printf 'com.soxs.top%s' "${top_name:3}"
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

wait_for_port() {
    local port="$1"
    local timeout="${2:-15}"
    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

start_combined_dashboard() {
    : > "$LOG_DIR/combined.log"
    COMBINED_PID=""
    if command -v launchctl >/dev/null 2>&1; then
        local plist="$PROJECT_DIR/launchd/${COMBINED_JOB}.plist"
        if [ -f "$plist" ]; then
            launchctl enable gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
            launchctl bootstrap gui/"$UID_NUM" "$plist" 2>/dev/null || true
            launchctl kickstart -k gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
            COMBINED_PID="launchd"
        fi
    fi
    if [ -z "${COMBINED_PID:-}" ]; then
        if command -v setsid >/dev/null 2>&1; then
            setsid "$VENV_PYTHON" scripts/start_combined.py >> "$LOG_DIR/combined.log" 2>&1 < /dev/null &
        else
            nohup "$VENV_PYTHON" scripts/start_combined.py >> "$LOG_DIR/combined.log" 2>&1 < /dev/null &
        fi
        COMBINED_PID=$!
    fi
    if wait_for_port 8090 15; then
        return 0
    fi
    if [ "${COMBINED_PID:-}" != "launchd" ] && [ -n "${COMBINED_PID:-}" ] && ! kill -0 "$COMBINED_PID" >/dev/null 2>&1; then
        echo "❌ Combined dashboard failed to start (PID $COMBINED_PID exited)"
    else
        echo "❌ Combined dashboard did not bind to :8090 within timeout"
    fi
    tail -n 40 "$LOG_DIR/combined.log" 2>/dev/null || true
    return 1
}

stop_existing() {
    pkill -f "run.py --config configs/DRIP.yaml" 2>/dev/null
    pkill -f "run.py --config configs/AMC.yaml" 2>/dev/null
    pkill -f "run.py --config configs/SMR.yaml" 2>/dev/null
    stop_top
    pkill -f "scripts/start_combined.py" 2>/dev/null
    pkill -f "scripts/start_orphan_monitor.py" 2>/dev/null
    pkill -f "from src.dashboard.combined import start_combined" 2>/dev/null
    pkill -f "start_combined(8090)" 2>/dev/null
    launchctl bootout gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
    launchctl disable gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
    kill_dashboard_ports
}

stop_top() {
    for top_name in "${TOP_ENGINES[@]}"; do
        pkill -f "run.py --config .*configs/${top_name}.yaml" 2>/dev/null || true
        pkill -f "run.py --config ${PROJECT_DIR}/configs/${top_name}.yaml" 2>/dev/null || true
    done
    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        for job in com.soxs.top1 com.soxs.top2 com.soxs.top3 com.soxs.top4 com.soxs.top5; do
            launchctl disable gui/"$UID_NUM"/"$job" 2>/dev/null || true
            launchctl bootout gui/"$UID_NUM"/"$job" 2>/dev/null || true
        done
    fi
    for top_name in "${TOP_ENGINES[@]}"; do
        kill_listener_on_port "$(port_for_top "$top_name")"
    done
}

start_top() {
    startup_delay="${SOXS_ENGINE_STARTUP_DELAY_SECONDS:-6}"
    launchd_startup_timeout="${SOXS_LAUNCHD_ENGINE_STARTUP_TIMEOUT_SECONDS:-20}"
    start_top_manual() {
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
                    nohup "$VENV_PYTHON" run.py --config "$cfg" "$cli_mode" --dashboard --port $port >> "$LOG_DIR/${log_name}.log" 2>&1 &
                    disown $! 2>/dev/null || true
                else
                    : > "$LOG_DIR/${log_name}.log"
                    SOXS_SYNTHETIC_MARKET=1 \
                    SOXS_SYNTHETIC_START_PRICE="$SYNTH_START" \
                    SOXS_SYNTHETIC_AMPLITUDE_PCT="$SYNTH_AMP" \
                    SOXS_SYNTHETIC_PERIOD_SECONDS=120 \
                    nohup "$VENV_PYTHON" run.py --config "$cfg" "$cli_mode" --dashboard --anytime --port $port >> "$LOG_DIR/${log_name}.log" 2>&1 &
                    disown $! 2>/dev/null || true
                fi
                TOP_PIDS="$TOP_PIDS $!"
                echo "🚀 $TOP on :$port (PID $!, mode=$ENGINE_MODE)"
                sleep "$startup_delay"
                wait_for_port "$port" "$startup_delay" || {
                    echo "❌ $TOP failed to bind to :$port in manual fallback mode"
                    return 1
                }
            fi
        done
        return 0
    }

    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        launchd_failed=0
        for job in com.soxs.top1 com.soxs.top2 com.soxs.top3 com.soxs.top4 com.soxs.top5; do
            plist="$PROJECT_DIR/launchd/${job}.plist"
            if [ -f "$plist" ]; then
                launchctl enable gui/"$UID_NUM"/"$job" 2>/dev/null || true
                launchctl bootstrap gui/"$UID_NUM" "$plist" 2>/dev/null || true
                launchctl kickstart -k gui/"$UID_NUM"/"$job" 2>/dev/null || true
                port="$(port_for_top "TOP${job##*.top}")"
                wait_for_port "$port" "$launchd_startup_timeout" || {
                    echo "⚠️ $job failed to bind to :$port via launchd"
                    launchd_failed=1
                    break
                }
            fi
        done
        if [ "$launchd_failed" = "0" ]; then
            echo "🚀 TOP engines managed by launchd"
            return 0
        fi
        echo "↩️ Launchd startup incomplete; keeping any already-started TOP jobs running"
        echo "   Set SOXS_ALLOW_MANUAL_TOP_FALLBACK=1 to force manual fallback."
        if [ "${SOXS_ALLOW_MANUAL_TOP_FALLBACK:-0}" = "1" ]; then
            echo "↩️ Falling back to manual TOP engine startup"
            stop_top
            sleep 1
            start_top_manual
            return $?
        fi
        return 1
    fi

    start_top_manual
}

start_all() {
    wait_for_children="$1"

    if [ "${SOXS_ALLOW_NON_TRADING_DAY_START:-0}" != "1" ]; then
        if ! is_trading_day_now >> "$LOG_DIR/combined.log" 2>&1; then
            echo "❌ Non-trading day; startup aborted"
            return 1
        fi
    fi

    stop_existing
    sleep 1

    find "$PROJECT_DIR" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null

    if ! "$VENV_PYTHON" "$ORPHAN_MONITOR_SCRIPT" --verify-only >> "$LOG_DIR/combined.log" 2>&1; then
        echo "❌ Broker position verification failed; aborting live startup"
        return 1
    fi

    if ! verify_same_day_selection_if_live >> "$LOG_DIR/combined.log" 2>&1; then
        echo "❌ Same-day AI selection missing or stale; live startup aborted"
        return 1
    fi

    # Start combined dashboard with the dedicated launcher.
    start_combined_dashboard || return 1
    pids="$pids $COMBINED_PID"
    echo "📊 COMBINED on :8090 (PID $COMBINED_PID)"

    : > "$LOG_DIR/orphan-monitor.log"
    nohup "$VENV_PYTHON" "$ORPHAN_MONITOR_SCRIPT" >> "$LOG_DIR/orphan-monitor.log" 2>&1 &
    pids="$pids $!"
    echo "🛡️ ORPHAN MONITOR started (PID $!)"

    start_top || return 1
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
        start_top >/dev/null || exit 1
        echo "🔄 TOP engines restarted"
        ;;

    restart-combined)
        pkill -f "scripts/start_combined.py" 2>/dev/null
        pkill -f "from src.dashboard.combined import start_combined" 2>/dev/null
        pkill -f "start_combined(8090)" 2>/dev/null
        launchctl bootout gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
        launchctl disable gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
        kill_listener_on_port 8090
        sleep 1
        if start_combined_dashboard; then
            if [ -n "${COMBINED_PID:-}" ]; then
                echo "🔄 Combined dashboard restarted (PID $COMBINED_PID)"
            else
                echo "🔄 Combined dashboard restarted (launchd)"
            fi
        else
            exit 1
        fi
        ;;

    restart-all)
        stop_existing
        sleep 1
        start_all
        echo "🔄 All services restarted"
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
            elif lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
                echo "  $ticker: 端口 $port 已监听 | 状态接口暂未返回 | ✅"
            elif command -v launchctl >/dev/null 2>&1 && launchctl print gui/"$UID_NUM"/"$(launchd_job_for_top "$ticker")" >/dev/null 2>&1; then
                echo "  $ticker: launchd 已运行 | 等待端口 $port | ⏳"
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
        echo "Usage: $0 {start|start-foreground|stop|restart-top|restart-combined|restart-all|status|summary}"
        ;;
esac
