#!/bin/bash
# Operational: launch a TOP{N} trading engine instance (paper or live).
# This is a personal operational script from the QuantCairn maintainer's
# macOS environment. Not part of the core library. Adapt paths, ports,
# and environment variables before using on your own machine.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_AI_ENV="$PROJECT_DIR/.env.ai_selector.local"
PYTHON_BIN="${SOXS_PYTHON_BIN:-}"
VENV_PYTHON="$PYTHON_BIN"

RUNTIME_LOG_DIR="${SOXS_LOG_DIR:-${SOXS_LOGS_DIR:-${SOXS_STATE_DIR:-$PROJECT_DIR}/logs}}"
runtime_event() {
    local event="$1" detail="${2:-}"
    mkdir -p "$RUNTIME_LOG_DIR" 2>/dev/null || true
    printf 'event=%s\npid=%s\npython=%s\npython_version=%s\nrelease_root=%s\nconfig_root=%s\nexecution_mode=%s\nrun_id=%s\ndetail=%s\n' \
        "$event" "$$" "${PYTHON_BIN:-}" "${PYTHON_VERSION:-}" "$PROJECT_DIR" \
        "${cfg:-}" "${QUANTCAIRN_EXECUTION_MODE:-}" "${SOXS_TOP_SELECTION_RUN_ID:-${selection_run_id:-}}" "$detail" \
        >> "$RUNTIME_LOG_DIR/top-engine-runtime.log" 2>/dev/null || true
}
runtime_failure() {
    local state="$1" detail="$2" code="${3:-12}"
    runtime_event "$state" "$detail"
    echo "[$state] $detail" >&2
    exit "$code"
}
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    runtime_failure "python_runtime_invalid" "SOXS_PYTHON_BIN must name an executable stable runtime" 12
fi
case "$PYTHON_BIN" in
    /tmp/*|/private/tmp/*) runtime_failure "python_runtime_invalid" "temporary interpreter paths are not allowed" 12 ;;
esac
PYTHON_VERSION="$($PYTHON_BIN -c 'import platform; print(platform.python_version())' 2>/dev/null)" || \
    runtime_failure "python_runtime_invalid" "interpreter could not report its version" 12
if [ "$PYTHON_VERSION" != "${SOXS_EXPECTED_PYTHON_VERSION:-3.14.4}" ]; then
    runtime_failure "python_runtime_invalid" "unexpected Python version: $PYTHON_VERSION" 12
fi
DEPENDENCY_OUTPUT=""
if ! DEPENDENCY_OUTPUT="$($PYTHON_BIN - <<'PY'
import importlib
required = [
    "flask", "yfinance", "longbridge", "yaml",
    "src.config.runtime_paths", "src.engine.trading_engine",
]
missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}:{type(exc).__name__}:{exc}")
if missing:
    print(";".join(missing))
    raise SystemExit(1)
print("dependencies_ok")
PY
)"; then
    runtime_failure "dependency_preflight_failed" "${DEPENDENCY_OUTPUT:-required imports failed}" 12
fi

cfg="${1:?config path required}"
port="${2:?port required}"
log_name="${3:?log name required}"

if [[ "$cfg" != /* ]]; then
    if [ -n "${SOXS_TOP_CONFIG_DIR:-}" ]; then
        cfg="$SOXS_TOP_CONFIG_DIR/$cfg"
    elif [ -n "${SOXS_CONFIG_DIR:-}" ]; then
        cfg="$SOXS_CONFIG_DIR/$cfg"
    elif [ -n "${SOXS_STATE_DIR:-}" ]; then
        cfg="$SOXS_STATE_DIR/top_configs/$cfg"
    else
        echo "TOP_RUNTIME_ROOT_NOT_CONFIGURED" >&2
        exit 12
    fi
fi
cfg="$(cd "$(dirname "$cfg")" 2>/dev/null && pwd)/$(basename "$cfg")" || {
    echo "TOP_RUNTIME_ROOT_NOT_CONFIGURED" >&2
    exit 12
}
[ -f "$cfg" ] || { echo "CONFIG_MISSING: $cfg" >&2; exit 13; }
runtime_event "startup_preflight_passed" "$DEPENDENCY_OUTPUT"

cd "$PROJECT_DIR"

if [ -f "$LOCAL_AI_ENV" ]; then
    set -a
    # Load only non-secret application settings.  PAPER must never source
    # LongBridge execution credentials, even transiently from this file.
    . <(grep -Ev '^[[:space:]]*(LONGBRIDGE_|LONGPORT_)' "$LOCAL_AI_ENV")
    set +a
fi

echo "Using Python: $PYTHON_BIN"

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

if [ "$ENGINE_MODE" != "live" ]; then
    unset LONGBRIDGE_APP_KEY LONGBRIDGE_APP_SECRET LONGBRIDGE_API_KEY \
        LONGBRIDGE_API_SECRET LONGBRIDGE_ACCESS_TOKEN LONGBRIDGE_ACCOUNT_TYPE \
        LONGBRIDGE_ENV LONGBRIDGE_BASE_URL LONGBRIDGE_HTTP_URL \
        LONGBRIDGE_QUOTE_WS_URL LONGBRIDGE_TRADE_WS_URL
fi

LOG_DIR="${SOXS_LOG_DIR:-${SOXS_LOGS_DIR:-${TMPDIR:-/private/tmp}/soxs-range-arbitrage/logs}}"
REDIRECT_STDIO="${SOXS_TOP_ENGINE_REDIRECT_STDIO:-0}"
mkdir -p "$LOG_DIR" 2>/dev/null || true

wait_until_port_free() {
    local target_port="$1"
    local attempts="${2:-50}"
    local sleep_seconds="${3:-0.2}"
    if ! command -v lsof >/dev/null 2>&1; then
        echo "cannot verify port ownership: lsof is unavailable" >&2
        return 1
    fi
    local i
    for ((i=0; i<attempts; i++)); do
        if lsof -tiTCP:"$target_port" -sTCP:LISTEN >/dev/null 2>&1; then
            sleep "$sleep_seconds"
            continue
        fi
        local lsof_status=$?
        if [ "$lsof_status" -eq 1 ]; then
            return 0
        fi
        return 1
    done
    echo "port $target_port still busy after waiting" >&2
    return 1
}

run_engine() {
    if [ "$ENGINE_MODE" = "live" ]; then
        local base_delay="${SOXS_LIVE_ENGINE_STARTUP_STAGGER_SECONDS:-4}"
        local offset=$((port - 8091))
        if [ "$offset" -gt 0 ] && [ "$base_delay" -gt 0 ] 2>/dev/null; then
            sleep_seconds=$((offset * base_delay))
            sleep "$sleep_seconds"
        fi
    fi
    if [ "$ENGINE_MODE" = "live" ]; then
        exec "$VENV_PYTHON" run.py --config "$cfg" --live --dashboard --port "$port"
    fi

    exec env \
        SOXS_SYNTHETIC_MARKET=1 \
        SOXS_SYNTHETIC_START_PRICE="$SYNTH_START" \
        SOXS_SYNTHETIC_AMPLITUDE_PCT="$SYNTH_AMP" \
        SOXS_SYNTHETIC_PERIOD_SECONDS=120 \
        "$VENV_PYTHON" run.py --config "$cfg" --paper --dashboard --anytime --port "$port"
}

if [ "$REDIRECT_STDIO" = "1" ]; then
    exec >> "$LOG_DIR/${log_name}.log" 2>&1
fi

wait_until_port_free "$port"
run_engine
