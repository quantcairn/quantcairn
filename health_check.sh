#!/bin/bash
# Quick operational health check for the Top3 trading system.

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
LOG_DIR="$PROJECT_DIR/logs"
cd "$PROJECT_DIR" || exit 1

market_is_open() {
    "$PROJECT_DIR/.venv/bin/python" - <<'PY'
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

check_port() {
    local port="$1"
    local name="$2"
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "OK   port $port listening: $name"
    else
        echo "WARN port $port not listening: $name"
    fi
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
        python3 -c "import json,sys; d=json.load(sys.stdin); print('OK   API %s: price=$%.2f signal=%s halted=%s' % ('$name', d.get('price') or 0, d.get('last_signal'), d.get('halted')))" <<< "$body" 2>/dev/null \
            || echo "WARN API $name returned invalid JSON"
    else
        echo "WARN API not responding: $name"
    fi
}

check_combined() {
    local body
    body=$(curl -fsS --max-time 5 "http://127.0.0.1:8090/" 2>/dev/null)
    if [[ "$body" == *"AI 区间交易总览"* ]] || [[ "$body" == *"真实账户"* ]]; then
        echo "OK   combined dashboard responding"
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
echo "== ports =="
if market_is_open; then
    check_port 8091 "TOP1"
    check_fd 8091 "TOP1"
    check_port 8092 "TOP2"
    check_fd 8092 "TOP2"
    check_port 8093 "TOP3"
    check_fd 8093 "TOP3"
else
    echo "OK   US market closed; TOP1-TOP3 may remain online for snapshot-only sync"
    check_port 8091 "TOP1"
    check_port 8092 "TOP2"
    check_port 8093 "TOP3"
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
            python3 -c "import json,sys; d=json.load(sys.stdin); print('OK   After-hours API %s: reason=%s' % ('$port', d.get('last_signal_reason') or ''))" <<< "$body" 2>/dev/null \
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
