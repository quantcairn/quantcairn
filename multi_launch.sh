#!/bin/bash
# AI 选股后的 TOP3 并发交易系统
# TOP1 (8091) + TOP2 (8092) + TOP3 (8093)

PROJECT_DIR="${SOXS_PROJECT_DIR:-/Users/chenwei/soxs-range-arbitrage}"
LOCAL_AI_ENV="$PROJECT_DIR/.env.ai_selector.local"
PYTHON_BIN="${SOXS_PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3)"
    fi
fi
VENV_PYTHON="$PYTHON_BIN"
LOG_DIR="${SOXS_LOG_DIR:-$PROJECT_DIR/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
RUNTIME_DIR="${SOXS_RUNTIME_DIR:-$PROJECT_DIR/runtime}"
mkdir -p "$RUNTIME_DIR" 2>/dev/null || true
USE_LAUNCHD_TOPS="${SOXS_USE_LAUNCHD_TOPS:-1}"
UID_NUM="$(id -u)"
TOP_ENGINES=(TOP1 TOP2 TOP3)
ORPHAN_MONITOR_SCRIPT="$PROJECT_DIR/scripts/start_orphan_monitor.py"
COMBINED_JOB="com.soxs.combined"
COMBINED_PID_FILE="$RUNTIME_DIR/combined.pid"

cd "$PROJECT_DIR" || exit 1

if [ -f "$LOCAL_AI_ENV" ]; then
    set -a
    . "$LOCAL_AI_ENV"
    set +a
fi

echo "Using Python: $PYTHON_BIN"

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
from src.ai_selector.selection_state import verify_live_startup_selection
from src.utils.market_calendar import required_selection_date

from datetime import datetime
from zoneinfo import ZoneInfo

required_date = required_selection_date(datetime.now(ZoneInfo("America/New_York")))
ok, reason, state = verify_live_startup_selection(required_et_date=required_date)
if not ok:
    print(f"Live startup blocked: {reason}")
    raise SystemExit(1)
print(f"Selection freshness verified for live startup required selection date: {required_date} ({reason})")
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

launchd_top_jobs() {
    local top_name
    for top_name in "${TOP_ENGINES[@]}"; do
        launchd_job_for_top "$top_name"
    done
}

top_pid_file() {
    local top_name="$1"
    local lower
    lower="$(printf '%s' "$top_name" | tr '[:upper:]' '[:lower:]')"
    printf '%s/%s.pid' "$RUNTIME_DIR" "$lower"
}

command_for_pid() {
    local pid="$1"
    ps -p "$pid" -o command= 2>/dev/null | tr -d '\n'
}

pid_alive() {
    local pid="$1"
    if [ -z "$pid" ] || ! kill -0 "$pid" >/dev/null 2>&1; then
        return 1
    fi
    local state
    state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
    case "$state" in
        Z*)
            return 1
            ;;
        *)
            return 0
            ;;
    esac
}

is_expected_top_command() {
    local top_name="$1"
    local cmd="$2"
    local port
    port="$(port_for_top "$top_name")"
    case "$cmd" in
        *"run.py"*"--config"*"configs/${top_name}.yaml"*"--port"*"${port}"*|*"run.py"*"--config"*"${PROJECT_DIR}/configs/${top_name}.yaml"*"--port"*"${port}"*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

log_scheduled_stop_decision() {
    local slot="$1"
    local pid="$2"
    local result="$3"
    echo "action=scheduled_stop slot=${slot} pid=${pid:-none} result=${result}"
}

kill_listener_on_port() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        local pids
        pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')"
        if [ -n "$pids" ]; then
            kill $pids 2>/dev/null || true
            sleep 2
            # Force kill remaining stubborn processes
            pids="$(lsof -tiTCP:"$port" 2>/dev/null | tr '\n' ' ')"
            if [ -n "$pids" ]; then
                kill -9 $pids 2>/dev/null || true
                sleep 1
            fi
        fi
    fi
}

kill_dashboard_ports() {
    kill_listener_on_port 8090
    for top_name in "${TOP_ENGINES[@]}"; do
        kill_listener_on_port "$(port_for_top "$top_name")"
    done
}

wait_until_port_free() {
    local port="$1"
    local timeout="${2:-15}"
    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

spawn_top_process() {
    local cfg="$1"
    local port="$2"
    local log_path="$3"
    local cli_mode="$4"
    local synth_start="${5:-}"
    local synth_amp="${6:-}"

    if [ "$cli_mode" = "--live" ]; then
        "$VENV_PYTHON" - "$cfg" "$port" "$log_path" <<'PY'
import os
import pathlib
import subprocess
import sys

cfg_path, port, log_path = sys.argv[1:4]
project = pathlib.Path("/Users/chenwei/soxs-range-arbitrage")
cmd = [
    str(project / ".venv/bin/python"),
    "run.py",
    "--config",
    cfg_path,
    "--live",
    "--dashboard",
    "--port",
    port,
]
with open(log_path, "ab", buffering=0) as log_handle:
    proc = subprocess.Popen(
        cmd,
        cwd=str(project),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=os.environ.copy(),
        start_new_session=True,
        close_fds=True,
    )
print(proc.pid)
PY
    else
        env \
        SOXS_SYNTHETIC_MARKET=1 \
        SOXS_SYNTHETIC_START_PRICE="$synth_start" \
        SOXS_SYNTHETIC_AMPLITUDE_PCT="$synth_amp" \
        SOXS_SYNTHETIC_PERIOD_SECONDS=120 \
        "$VENV_PYTHON" - "$cfg" "$port" "$log_path" <<'PY'
import os
import pathlib
import subprocess
import sys

cfg_path, port, log_path = sys.argv[1:4]
project = pathlib.Path("/Users/chenwei/soxs-range-arbitrage")
cmd = [
    str(project / ".venv/bin/python"),
    "run.py",
    "--config",
    cfg_path,
    "--paper",
    "--dashboard",
    "--anytime",
    "--port",
    port,
]
with open(log_path, "ab", buffering=0) as log_handle:
    proc = subprocess.Popen(
        cmd,
        cwd=str(project),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=os.environ.copy(),
        start_new_session=True,
        close_fds=True,
    )
print(proc.pid)
PY
    fi
}

combined_command_for_pid() {
    local pid="$1"
    ps -p "$pid" -o command= 2>/dev/null | tr -d '\n'
}

is_project_combined_command() {
    local cmd="$1"
    case "$cmd" in
        *"scripts/start_combined.py"*|*"src.dashboard.combined"*|*"start_combined(8090)"*|*"start_combined.py"*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

combined_pid_file_pid() {
    if [ -f "$COMBINED_PID_FILE" ]; then
        cat "$COMBINED_PID_FILE" 2>/dev/null | tr -d '[:space:]'
    fi
}

combined_pid_alive() {
    local pid="$1"
    [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

remove_combined_pid_file() {
    rm -f "$COMBINED_PID_FILE" 2>/dev/null || true
}

stop_combined_process() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return 0
    fi
    if ! combined_pid_alive "$pid"; then
        return 0
    fi
    kill "$pid" 2>/dev/null || true
    sleep 2
    if combined_pid_alive "$pid"; then
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
    return 0
}

stop_combined() {
    local cleaned=0
    local pid_from_file
    pid_from_file="$(combined_pid_file_pid)"
    if [ -n "$pid_from_file" ]; then
        local cmd
        cmd="$(combined_command_for_pid "$pid_from_file")"
        if is_project_combined_command "$cmd"; then
            stop_combined_process "$pid_from_file"
            cleaned=1
        fi
    fi

    if command -v launchctl >/dev/null 2>&1; then
        launchctl bootout gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
        launchctl disable gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
    fi

    local listeners
    listeners="$(lsof -tiTCP:8090 -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
    if [ -n "$listeners" ]; then
        local pid
        for pid in $listeners; do
            local cmd
            cmd="$(combined_command_for_pid "$pid")"
            if is_project_combined_command "$cmd"; then
                stop_combined_process "$pid"
                cleaned=1
            fi
        done
    fi

    remove_combined_pid_file
    if [ "$cleaned" = "1" ]; then
        echo "🧹 Combined dashboard stopped"
    fi
}

combined_port_is_project_owned() {
    local listeners
    listeners="$(lsof -tiTCP:8090 -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
    if [ -z "$listeners" ]; then
        return 1
    fi
    local pid
    for pid in $listeners; do
        local cmd
        cmd="$(combined_command_for_pid "$pid")"
        if ! is_project_combined_command "$cmd"; then
            return 2
        fi
    done
    return 0
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
    local pid_from_file
    pid_from_file="$(combined_pid_file_pid)"
    if [ -n "$pid_from_file" ]; then
        local cmd
        cmd="$(combined_command_for_pid "$pid_from_file")"
        if combined_pid_alive "$pid_from_file" && is_project_combined_command "$cmd"; then
            echo "✅ Combined dashboard already running (PID $pid_from_file, pid_file=$COMBINED_PID_FILE)"
            COMBINED_PID="$pid_from_file"
            return 0
        fi
        remove_combined_pid_file
    fi
    if command -v lsof >/dev/null 2>&1; then
        local port_state
        combined_port_is_project_owned
        port_state=$?
        if [ "$port_state" = "2" ]; then
            echo "❌ Port 8090 occupied by non-project process"
            lsof -nP -iTCP:8090 -sTCP:LISTEN 2>/dev/null || true
            tail -n 40 "$LOG_DIR/combined.log" 2>/dev/null || true
            return 1
        elif [ "$port_state" = "0" ]; then
            stop_combined
            wait_until_port_free 8090 5 || {
                echo "❌ Port 8090 still busy after stopping existing combined"
                tail -n 40 "$LOG_DIR/combined.log" 2>/dev/null || true
                return 1
            }
        fi
    fi
    if command -v launchctl >/dev/null 2>&1; then
        local plist="$PROJECT_DIR/launchd/${COMBINED_JOB}.plist"
        if [ -f "$plist" ]; then
            launchctl enable gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
            if launchctl print gui/"$UID_NUM"/"$COMBINED_JOB" >/dev/null 2>&1; then
                launchctl kickstart gui/"$UID_NUM"/"$COMBINED_JOB" 2>/dev/null || true
            else
                launchctl bootstrap gui/"$UID_NUM" "$plist" 2>/dev/null || true
            fi
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
    stop_combined
    kill_dashboard_ports
}

stop_top() {
    for top_name in "${TOP_ENGINES[@]}"; do
        pkill -f "run.py --config .*configs/${top_name}.yaml" 2>/dev/null || true
        pkill -f "run.py --config ${PROJECT_DIR}/configs/${top_name}.yaml" 2>/dev/null || true
    done
    # Also kill any remaining run.py processes by port pattern
    for port in 8091 8092 8093; do
        pids="$(lsof -tiTCP:"$port" 2>/dev/null | tr '\n' ' ')"
        if [ -n "$pids" ]; then
            kill $pids 2>/dev/null || true
        fi
    done
    for job in $(launchd_top_jobs); do
        launchctl disable gui/"$UID_NUM"/"$job" 2>/dev/null || true
        launchctl bootout gui/"$UID_NUM"/"$job" 2>/dev/null || true
    done
    for top_name in "${TOP_ENGINES[@]}"; do
        kill_listener_on_port "$(port_for_top "$top_name")"
    done
}

stop_verified_top_pid() {
    local top_name="$1"
    local pid_file="$2"
    local pid="$3"
    local timeout="${SOXS_TOP_STOP_TIMEOUT_SECONDS:-10}"
    local elapsed=0
    if [ -z "$pid" ]; then
        log_scheduled_stop_decision "$top_name" "" "already_stopped"
        return 0
    fi
    if ! pid_alive "$pid"; then
        rm -f "$pid_file" 2>/dev/null || true
        log_scheduled_stop_decision "$top_name" "$pid" "stale_pid"
        return 0
    fi

    local cmd
    cmd="$(command_for_pid "$pid")"
    if ! is_expected_top_command "$top_name" "$cmd"; then
        log_scheduled_stop_decision "$top_name" "$pid" "identity_mismatch"
        return 1
    fi

    kill "$pid" 2>/dev/null || true
    while [ "$elapsed" -lt "$timeout" ]; do
        if ! pid_alive "$pid"; then
            rm -f "$pid_file" 2>/dev/null || true
            log_scheduled_stop_decision "$top_name" "$pid" "stopped"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    if ! pid_alive "$pid"; then
        rm -f "$pid_file" 2>/dev/null || true
        log_scheduled_stop_decision "$top_name" "$pid" "stopped"
        return 0
    fi

    cmd="$(command_for_pid "$pid")"
    if is_expected_top_command "$top_name" "$cmd"; then
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
        if ! pid_alive "$pid"; then
            rm -f "$pid_file" 2>/dev/null || true
            log_scheduled_stop_decision "$top_name" "$pid" "stopped"
            return 0
        fi
    fi
    log_scheduled_stop_decision "$top_name" "$pid" "failed"
    return 1
}

stop_top_scheduled_only() {
    local status=0
    local top_name
    for top_name in "${TOP_ENGINES[@]}"; do
        local pid_file
        local pid=""
        pid_file="$(top_pid_file "$top_name")"
        if [ -f "$pid_file" ]; then
            pid="$(cat "$pid_file" 2>/dev/null | tr -d '[:space:]')"
        fi
        if ! stop_verified_top_pid "$top_name" "$pid_file" "$pid"; then
            status=1
        fi
    done
    return "$status"
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
                fi
                : > "$LOG_DIR/${log_name}.log"
                pid="$(spawn_top_process "$cfg" "$port" "$LOG_DIR/${log_name}.log" "$cli_mode" "$SYNTH_START" "$SYNTH_AMP")"
                if [ -z "$pid" ]; then
                    echo "❌ $TOP failed to spawn detached process"
                    return 1
                fi
                printf '%s\n' "$pid" > "$(top_pid_file "$TOP")"
                TOP_PIDS="$TOP_PIDS $pid"
                echo "🚀 $TOP on :$port (PID $pid, mode=$ENGINE_MODE)"
                sleep "$startup_delay"
                wait_for_port "$port" "$startup_delay" || {
                    echo "❌ $TOP failed to bind to :$port in manual fallback mode"
                    return 1
                }
            else
                echo "Skipping $TOP: config not found"
            fi
        done
        return 0
    }

    if [ "$USE_LAUNCHD_TOPS" = "1" ]; then
        launchd_failed=0
        for job in $(launchd_top_jobs); do
            plist="$PROJECT_DIR/launchd/${job}.plist"
            top_name="TOP${job##*.top}"
            cfg="$PROJECT_DIR/configs/${top_name}.yaml"
            if [ ! -f "$cfg" ]; then
                echo "Skipping $top_name: config not found"
                continue
            fi
            if [ -f "$plist" ]; then
                launchctl enable gui/"$UID_NUM"/"$job" 2>/dev/null || true
                if launchctl print gui/"$UID_NUM"/"$job" >/dev/null 2>&1; then
                    launchctl kickstart gui/"$UID_NUM"/"$job" 2>/dev/null || true
                else
                    launchctl bootstrap gui/"$UID_NUM" "$plist" 2>/dev/null || true
                fi
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
        echo "↩️ Launchd startup incomplete; falling back to manual TOP engine startup"
        stop_top
        sleep 1
        start_top_manual
        return $?
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

    stop-top)
        stop_top_scheduled_only
        status=$?
        echo "🛑 Scheduled TOP engines stopped"
        exit "$status"
        ;;

    restart-top)
        stop_combined
        stop_top
        # Give launchd/child processes time to fully release ports before restart.
        sleep 3
        wait_until_port_free 8090 10 || true
        wait_until_port_free 8091 10 || true
        wait_until_port_free 8092 10 || true
        wait_until_port_free 8093 10 || true
        USE_LAUNCHD_TOPS=0 start_top || exit 1
        start_combined_dashboard || exit 1
        bash "$PROJECT_DIR/health_check.sh" || true
        echo "🔄 TOP engines restarted with new configs"
        ;;

    restart-combined)
        stop_combined
        wait_until_port_free 8090 10 || true
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
        echo "  📊 AI Top3 Trading Status"
        echo "═══════════════════════════════════"
        for ticker in "${TOP_ENGINES[@]}"; do
            port="$(port_for_top "$ticker")"
            cfg="$PROJECT_DIR/configs/${ticker}.yaml"
            if [ ! -f "$cfg" ]; then
                echo "  $ticker: disabled / config missing"
                continue
            fi

            status=$(curl -s "http://localhost:$port/api/status" 2>/dev/null)
            if [ -n "$status" ]; then
                price=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(f\"\${d['price']:.2f}\")" 2>/dev/null)
                signal=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(d['last_signal'])" 2>/dev/null)
                pnl=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(f\"\${d['daily_pnl']:+.2f}\")" 2>/dev/null)
                trades=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(d['trades_today'])" 2>/dev/null)
                halted=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print('⛔' if d['halted'] else '✅')" 2>/dev/null)
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
        echo "║  📊 AI Top3 总盈亏汇总                                  ║"
        echo "╠══════════════════════════════════════════════════════════╣"
        total_pnl=0
        for ticker in "${TOP_ENGINES[@]}"; do
            port="$(port_for_top "$ticker")"
            status=$(curl -s "http://localhost:$port/api/status" 2>/dev/null)
            if [ -n "$status" ]; then
                pnl=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(d['daily_pnl'])" 2>/dev/null)
                trades=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(d['trades_today'])" 2>/dev/null)
                equity=$(echo "$status" | "$PYTHON_BIN" -c "import sys,json;d=json.load(sys.stdin);print(f\"\${d['equity']:.2f}\")" 2>/dev/null)
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
        echo "Usage: $0 {start|start-foreground|stop|stop-top|restart-top|restart-combined|restart-all|status|summary}"
        ;;
esac
