#!/bin/bash
# Quick operational health check for the Top3 trading system.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${SOXS_PROJECT_DIR:-$SCRIPT_DIR}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="${SOXS_PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3)"
    fi
fi
LOG_DIR="$PROJECT_DIR/logs"
cd "$PROJECT_DIR" || exit 1

echo "Using Python: $PYTHON_BIN"

market_is_open() {
    "$PYTHON_BIN" - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
from src.config.loader import AppConfig
from src.engine.trading_engine import TradingEngine

engine = TradingEngine(AppConfig(), ignore_trading_hours=False)
raise SystemExit(0 if engine._is_trading_hours() else 1)
PY
}

check_launchd() {
    local label="$1"
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        echo "OK   launchd loaded: $label"
    else
        echo "FAIL launchd missing: $label"
    fi
}

check_selection_sync() {
    "$PYTHON_BIN" - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
from src.ai_selector.selection_state import verify_selection_state

required = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
ok, reason, state = verify_selection_state(required_et_date=required)
state = state or {}
selection_state_symbols = state.get("selection_state_symbols") or state.get("selected_symbols") or []
current_top_config_symbols = state.get("current_top_config_symbols") or state.get("top_config_symbols") or []
print("== selection sync ==")
print("selection_state tickers: %s" % selection_state_symbols)
print("current TOP config tickers: %s" % current_top_config_symbols)
if ok:
    print("OK   selection sync: aligned (%s)" % required)
else:
    mismatch_reason = reason
    if reason == "top_config_symbols_mismatch":
        mismatch_reason = "top_config_symbols_do_not_match_selection_state"
    print("WARN selection sync mismatch: %s" % mismatch_reason)
    if reason.startswith("selection_state_date_mismatch"):
        print("WARN mismatch detail: selection_state date does not match required date %s" % required)
    print("WARN suggestion: .venv/bin/python scripts/run_ai_selector.py && ./multi_launch.sh restart-top && bash health_check.sh")
PY
}

check_port() {
    local port="$1"
    local name="$2"
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "OK   port $port listening: $name"
    else
        echo "WARN port $port not listening: $name"
    fi
}

check_top_slot() {
    local top_name="$1"
    local port="$2"
    local cfg="$PROJECT_DIR/configs/${top_name}.yaml"
    if [ ! -f "$cfg" ]; then
        local pid cmd
        pid=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fp 2>/dev/null | tr -d 'p' | head -n 1)
        if [[ -n "$pid" ]]; then
            cmd=$(ps -o command= -p "$pid" 2>/dev/null)
        else
            cmd=""
        fi
        if [[ -n "$pid" && "$cmd" == *"$PROJECT_DIR"* ]]; then
            echo "WARN $top_name: config missing but port $port is still listening"
        else
            echo "OK   $top_name: disabled / config missing"
        fi
        return
    fi
    check_port "$port" "$top_name"
    check_fd "$port" "$top_name"
}

check_fd() {
    local port="$1"
    local name="$2"
    local pid
    pid=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fp 2>/dev/null | tr -d 'p' | head -n 1)
    if [[ -n "$pid" ]]; then
        local fd_count
        fd_count=$(lsof -p "$pid" 2>/dev/null | wc -l)
        if [[ "$fd_count" -gt 250 ]]; then
            echo "WARN high FD usage on $name ($pid): $fd_count open files)"
        else
            echo "OK   $name FD count: $fd_count"
        fi
    else
        echo "WARN cannot determine pid for $name on port $port"
    fi
}

check_api() {
    local port="$1"
    local name="$2"
    local body
    body=$(curl -fsS --max-time 3 "http://127.0.0.1:$port/api/status" 2>/dev/null)
    if [ -n "$body" ]; then
        "$PYTHON_BIN" -c "import json,sys; d=json.load(sys.stdin); print('OK   API %s: price=$%.2f signal=%s halted=%s' % ('$name', d.get('price') or 0, d.get('last_signal'), d.get('halted')))" <<< "$body" 2>/dev/null \
            || echo "WARN API $name returned invalid JSON"
    else
        echo "WARN API not responding: $name"
    fi
}

check_combined() {
    local body
    body=$(curl -fsS --max-time 5 "http://127.0.0.1:8090/api/status" 2>/dev/null)
    if [ -n "$body" ]; then
        "$PYTHON_BIN" -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok') is True; print('OK   combined dashboard responding: mode=%s synced=%s' % (d.get('mode'), (d.get('selection') or {}).get('synced')))" <<< "$body" 2>/dev/null \
            || echo "WARN combined dashboard /api/status returned invalid status"
    else
        echo "WARN combined dashboard not responding correctly"
    fi
}

check_combined_process_count() {
    local count
    count=$(pgrep -af 'scripts/start_combined.py|src.dashboard.combined|start_combined\(8090\)' 2>/dev/null | grep -v 'health_check.sh' | wc -l | tr -d ' ')
    if [ "${count:-0}" -gt 1 ] 2>/dev/null; then
        echo "WARN combined process count > 1: $count"
    elif [ "${count:-0}" -eq 1 ] 2>/dev/null; then
        echo "OK   combined process count: $count"
    else
        echo "WARN combined process count: 0"
    fi
}

check_log_risks() {
    local file="$1"
    local name="$2"
    if [ ! -f "$file" ]; then
        echo "WARN missing log: $name"
        return
    fi

    local hits
    hits=$(tail -n 80 "$file" | grep -E "ERROR|WARNING|Address already in use|no price data|Traceback" | tail -n 3)
    if [ -n "$hits" ]; then
        echo "WARN recent log issues: $name"
        echo "$hits" | sed 's/^/     /'
    else
        echo "OK   recent log clean: $name"
    fi
}

echo "== launchd =="
check_launchd "com.soxs.arbitrage"
check_launchd "com.soxs.ai_selector"
check_launchd "com.soxs.arbitrage.stop"

echo
check_selection_sync

echo
echo "== ports =="
if market_is_open; then
    check_top_slot "TOP1" 8091
    check_top_slot "TOP2" 8092
    check_top_slot "TOP3" 8093
else
    echo "OK   US market closed; TOP1-TOP3 may remain online for snapshot-only sync"
    check_top_slot "TOP1" 8091
    check_top_slot "TOP2" 8092
    check_top_slot "TOP3" 8093
fi
check_port 8090 "combined"
check_fd 8090 "combined"
check_combined_process_count

echo
echo "== APIs =="
if market_is_open; then
    check_api 8091 "TOP1"
    check_api 8092 "TOP2"
    check_api 8093 "TOP3"
else
    for port in 8091 8092 8093; do
        body=$(curl -fsS --max-time 3 "http://127.0.0.1:$port/api/status" 2>/dev/null)
        if [ -n "$body" ]; then
            "$PYTHON_BIN" -c "import json,sys; d=json.load(sys.stdin); print('OK   After-hours API %s: reason=%s' % ('$port', d.get('last_signal_reason') or ''))" <<< "$body" 2>/dev/null \
                || echo "WARN after-hours API invalid JSON: $port"
        fi
    done
fi
check_combined
if ! lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "WARN 8090 not listening; recent combined errors:"
    if [ -f "$LOG_DIR/combined.err.log" ]; then
        tail -n 20 "$LOG_DIR/combined.err.log" | sed 's/^/     /'
    fi
    if [ -f "$LOG_DIR/combined.log" ]; then
        tail -n 20 "$LOG_DIR/combined.log" | sed 's/^/     /'
    fi
fi

echo
echo "== logs =="
check_log_risks "$LOG_DIR/top1.log" "TOP1"
check_log_risks "$LOG_DIR/top2.log" "TOP2"
check_log_risks "$LOG_DIR/top3.log" "TOP3"
check_log_risks "$LOG_DIR/combined.log" "combined"
