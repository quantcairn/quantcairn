#!/bin/bash
# QuantCairn TOP supervisor.
#
# The launchd-owned foreground process is the only TOP lifecycle owner.
# `restart` is a control client: it never starts or kills an engine itself.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LAUNCHER="$PROJECT_DIR/scripts/run_top_engine.sh"
STATE_DIR="${SOXS_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  echo "TOP_RUNTIME_ROOT_NOT_CONFIGURED: SOXS_STATE_DIR is required" >&2
  exit 12
fi
RUNTIME_LOG_DIR="${SOXS_LOG_DIR:-${SOXS_LOGS_DIR:-$STATE_DIR/logs}}"

runtime_event() {
  local event="$1" detail="${2:-}"
  mkdir -p "$RUNTIME_LOG_DIR" 2>/dev/null || true
  printf 'event=%s\npid=%s\npython=%s\npython_version=%s\nrelease_root=%s\nconfig_root=%s\nexecution_mode=%s\nrun_id=%s\ndetail=%s\n' \
    "$event" "$$" "${PYTHON_BIN:-}" "${PYTHON_VERSION:-}" "$PROJECT_DIR" \
    "${CONFIG_DIR:-}" "${QUANTCAIRN_EXECUTION_MODE:-}" "${BUNDLE_RUN_ID:-}" "$detail" \
    >> "$RUNTIME_LOG_DIR/top-supervisor-runtime.log" 2>/dev/null || true
}

runtime_failure() {
  local state="$1" detail="$2" code="${3:-12}"
  mkdir -p "$STATE_DIR/top_supervisor" 2>/dev/null || true
  printf 'state=%s\ndetail=%s\nsupervisor_pid=%s\nproject_dir=%s\npython_bin=%s\npython_version=%s\n' \
    "$state" "$detail" "$$" "$PROJECT_DIR" "${PYTHON_BIN:-}" "${PYTHON_VERSION:-}" \
    > "$STATE_DIR/top_supervisor/status" 2>/dev/null || true
  runtime_event "$state" "$detail"
  echo "[$state] $detail" >&2
  exit "$code"
}

PYTHON_BIN="${SOXS_PYTHON_BIN:-}"
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
if ! DEPENDENCY_OUTPUT="$($PYTHON_BIN - "$PROJECT_DIR" <<'PY'
import importlib
import os
import sys

required = [
    "flask",
    "yfinance",
    "longbridge",
    "yaml",
    "src.openalpha.selection_bundle",
    "src.config.runtime_paths",
]
missing = []
for name in required + [item for item in os.environ.get("SOXS_TOP_EXTRA_REQUIRED_MODULES", "").split(",") if item.strip()]:
    try:
        importlib.import_module(name.strip())
    except Exception as exc:
        missing.append(f"{name.strip()}:{type(exc).__name__}:{exc}")
if missing:
    print(";".join(missing))
    raise SystemExit(1)
print("dependencies_ok")
PY
)"; then
  runtime_failure "dependency_preflight_failed" "${DEPENDENCY_OUTPUT:-required imports failed}" 12
fi

resolve_config_identity() {
  local manifest="$STATE_DIR/selection_bundle_manifest.json"
  if [ -f "$manifest" ]; then
    local identity
    identity="$($PYTHON_BIN - "$manifest" "$STATE_DIR" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
state_root = Path(sys.argv[2]).resolve()
payload = json.loads(manifest_path.read_text(encoding="utf-8"))
raw_root = str(payload.get("bundle_root") or payload.get("selection_bundle_root_path") or "").strip()
if not raw_root:
    raise SystemExit("bundle_root_missing")
root = Path(raw_root).expanduser()
if not root.is_absolute():
    root = state_root.parent / raw_root if root.parts and root.parts[0] == state_root.name else state_root / raw_root
root = root.resolve()
if not root.is_dir():
    raise SystemExit("bundle_root_missing")
for slot in ("TOP1.yaml", "TOP2.yaml", "TOP3.yaml"):
    if not (root / slot).is_file():
        raise SystemExit(f"{slot}_missing")
print(f"{root}|{payload.get('selection_run_id', '')}|{payload.get('selection_bundle_hash', '')}")
PY
    )" || return 1
    printf '%s\n' "$identity"
    return 0
  fi
  local configured="${SOXS_TOP_CONFIG_DIR:-${SOXS_CONFIG_DIR:-$STATE_DIR/top_configs}}"
  configured="$(cd "$configured" 2>/dev/null && pwd)" || return 1
  printf '%s||\n' "$configured"
}

CONFIG_IDENTITY="$(resolve_config_identity)" || \
  runtime_failure "top_config_pointer_invalid" "current committed bundle/config identity is unavailable" 12
IFS='|' read -r CONFIG_DIR BUNDLE_RUN_ID BUNDLE_HASH <<EOF
$CONFIG_IDENTITY
EOF
CONFIG_DIR="$(cd "$CONFIG_DIR" 2>/dev/null && pwd)" || \
  runtime_failure "top_config_pointer_invalid" "resolved config root is unavailable" 12
runtime_event "startup_preflight_passed" "$DEPENDENCY_OUTPUT"
BUNDLE_SYNC_STATUS="OK"
export SOXS_TOP_SELECTION_RUN_ID="$BUNDLE_RUN_ID"
CONTROL_DIR="${SOXS_TOP_CONTROL_DIR:-$STATE_DIR/top_supervisor}"
SUPERVISOR_PID_FILE="$CONTROL_DIR/supervisor.pid"
SUPERVISOR_LOCK_DIR="$CONTROL_DIR/supervisor.lock"
RESTART_LOCK_DIR="$CONTROL_DIR/restart.lock"
REQUEST_FILE="$CONTROL_DIR/restart.request"
STATUS_FILE="$CONTROL_DIR/status"
OWNER_FILE="$CONTROL_DIR/owner"
REDIRECT="${SOXS_TOP_ENGINE_REDIRECT_STDIO:-1}"
MODE="${1:-start}"
PORT_OFFSET="${SOXS_TOP_PORT_OFFSET:-0}"
REQUIRE_READINESS="${SOXS_TOP_REQUIRE_READINESS:-1}"
READINESS_TIMEOUT="${SOXS_TOP_READINESS_TIMEOUT_SECONDS:-20}"
HEALTH_PATH="${SOXS_TOP_HEALTH_PATH:-/api/status}"

# Engine definitions: config port log-name. Ports are part of the production
# contract and must stay aligned with the Dashboard and launchd template.
ENGINES=(
  "$CONFIG_DIR/TOP1.yaml 8080 top1"
  "$CONFIG_DIR/TOP2.yaml 8081 top2"
  "$CONFIG_DIR/TOP3.yaml 8082 top3"
)

mkdir -p "$CONTROL_DIR"

write_status() {
  local state="$1" request_id="$2" detail="$3" generation="$4"
  local runtime_status="PENDING"
  local restart_status="PENDING"
  case "$state" in
    running|idle_no_selection|restart_confirmed) runtime_status="OK"; restart_status="OK" ;;
    restart_failed|start_failed|supervisor_degraded) runtime_status="FAILED"; restart_status="FAILED" ;;
  esac
  local tmp="$STATUS_FILE.tmp-$$"
  {
    printf 'state=%s\n' "$state"
    printf 'request_id=%s\n' "$request_id"
    printf 'detail=%s\n' "$detail"
    printf 'generation=%s\n' "$generation"
    printf 'supervisor_pid=%s\n' "$$"
    printf 'project_dir=%s\n' "$PROJECT_DIR"
    printf 'python_bin=%s\n' "$PYTHON_BIN"
    printf 'python_version=%s\n' "$PYTHON_VERSION"
    printf 'config_dir=%s\n' "$CONFIG_DIR"
    printf 'selection_run_id=%s\n' "$BUNDLE_RUN_ID"
    printf 'selection_bundle_hash=%s\n' "$BUNDLE_HASH"
    printf 'bundle_sync_status=%s\n' "${BUNDLE_SYNC_STATUS:-UNKNOWN}"
    printf 'runtime_sync_status=%s\n' "$runtime_status"
    printf 'top_restart_status=%s\n' "$restart_status"
    printf 'active_engine_count=%s\n' "${#PIDS[@]}"
    printf 'expected_active_engine_count=%s\n' "${ACTIVE_COUNT:-0}"
  } > "$tmp"
  mv -f "$tmp" "$STATUS_FILE"
}

read_status_value() {
  local key="$1"
  [ -f "$STATUS_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$STATUS_FILE" | head -n 1
}

pid_is_alive() {
  kill -0 "$1" 2>/dev/null
}

process_command() {
  ps -p "$1" -o command= 2>/dev/null | sed 's/^ *//'
}

supervisor_is_owned() {
  [ -f "$SUPERVISOR_PID_FILE" ] || return 1
  local pid command
  pid="$(sed -n 's/^pid=//p' "$SUPERVISOR_PID_FILE" | head -n 1)"
  [ -n "$pid" ] && pid_is_alive "$pid" || return 1
  command="$(process_command "$pid")"
  case "$command" in
    *"$PROJECT_DIR/scripts/start_top_engines.sh"*) return 0 ;;
    *) return 1 ;;
  esac
}

remove_stale_lock() {
  local lock_dir="$1"
  [ -d "$lock_dir" ] || return 0
  local pid="$(sed -n 's/^pid=//p' "$lock_dir/owner" 2>/dev/null | head -n 1)"
  if [ -n "$pid" ] && pid_is_alive "$pid"; then
    return 1
  fi
  rm -f "$lock_dir/owner"
  rmdir "$lock_dir" 2>/dev/null || true
  [ ! -d "$lock_dir" ]
}

acquire_lock() {
  local lock_dir="$1"
  if mkdir "$lock_dir" 2>/dev/null; then
    printf 'pid=%s\nproject_dir=%s\n' "$$" "$PROJECT_DIR" > "$lock_dir/owner"
    return 0
  fi
  remove_stale_lock "$lock_dir" || true
  if mkdir "$lock_dir" 2>/dev/null; then
    printf 'pid=%s\nproject_dir=%s\n' "$$" "$PROJECT_DIR" > "$lock_dir/owner"
    return 0
  fi
  return 1
}

release_lock() {
  local lock_dir="$1"
  local pid="$(sed -n 's/^pid=//p' "$lock_dir/owner" 2>/dev/null | head -n 1)"
  if [ "$pid" = "$$" ]; then
    rm -f "$lock_dir/owner"
    rmdir "$lock_dir" 2>/dev/null || true
  fi
}

port_pids() {
  if ! command -v lsof >/dev/null 2>&1; then
    echo "cannot verify port ownership: lsof is unavailable" >&2
    return 1
  fi
  # lsof exits 1 for a valid query with no matching listener; pipe that
  # expected result through while preserving fail-closed behavior for a
  # missing executable above.
  { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ' '
}

port_is_free_or_owned() {
  local port="$1" allowed="${2:-}"
  local pids pid
  if ! pids="$(port_pids "$port")"; then
    return 1
  fi
  [ -z "$pids" ] && return 0
  for pid in $pids; do
    case " $allowed " in
      *" $pid "*) continue ;;
      *) echo "unknown process $pid owns port $port" >&2; return 1 ;;
    esac
  done
  return 0
}

port_owned_by_pid() {
  local port="$1" expected_pid="$2" pid
  for pid in $(port_pids "$port"); do
    [ "$pid" = "$expected_pid" ] && return 0
  done
  return 1
}

SUPERVISOR_LOCK_OWNED=0
PIDS=()
PIDS_CFG=()
PIDS_PORT=()
PIDS_NAME=()
SLOT_ACTIVE=()
SLOT_CONFIG=()
ACTIVE_COUNT=0
RESTART_REQUESTED=0
GENERATION=0

read_slot_configs() {
  local output line slot active cfg
  output="$($PYTHON_BIN - "$CONFIG_DIR" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
symbol_re = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

def parse_scalar(value):
    value = value.strip()
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value in {"null", "Null", "NULL", "~", ""}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value

def load_flat_mapping(path):
    payload = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # TOP configs contain additional nested engine settings.  The
        # supervisor contract only needs the top-level slot fields; nested
        # content is validated by the engine itself.
        if raw[:1].isspace():
            continue
        if ":" not in line:
            raise ValueError(f"invalid_mapping_line_{number}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key in payload:
            raise ValueError(f"invalid_mapping_key_{number}")
        payload[key] = parse_scalar(value)
    return payload

for slot in range(1, 4):
    path = root / f"TOP{slot}.yaml"
    if not path.is_file():
        print(f"ERROR|CONFIG_MISSING|{slot}|{path}")
        raise SystemExit(2)
    try:
        payload = load_flat_mapping(path)
    except Exception as exc:
        print(f"ERROR|CONFIG_INVALID|{slot}|{exc}")
        raise SystemExit(2)
    if not isinstance(payload, dict):
        print(f"ERROR|CONFIG_INVALID|{slot}|mapping_required")
        raise SystemExit(2)
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        print(f"ERROR|CONFIG_INVALID|{slot}|enabled_must_be_boolean")
        raise SystemExit(2)
    ticker = str(payload.get("ticker") or "").strip().upper()
    if enabled and (not ticker or not symbol_re.fullmatch(ticker)):
        print(f"ERROR|CONFIG_INVALID|{slot}|enabled_requires_valid_ticker")
        raise SystemExit(2)
    if not enabled and ticker:
        print(f"ERROR|CONFIG_INVALID|{slot}|disabled_slot_must_not_have_ticker")
        raise SystemExit(2)
    mode = str(payload.get("mode") or "paper").strip().lower()
    if enabled and mode != "paper":
        print(f"ERROR|CONFIG_INVALID|{slot}|active_top_must_be_paper")
        raise SystemExit(2)
    print(f"SLOT|{slot}|{1 if enabled else 0}|{path}")
PY
  )" || {
    echo "[top-supervisor] ${output:-CONFIG_INVALID}" >&2
    return 1
  }
  SLOT_ACTIVE=()
  SLOT_CONFIG=()
  ACTIVE_COUNT=0
  while IFS='|' read -r line slot active cfg; do
    [ "$line" = "SLOT" ] || continue
    SLOT_ACTIVE[$slot]="$active"
    SLOT_CONFIG[$slot]="$cfg"
    [ "$active" = "1" ] && ACTIVE_COUNT=$((ACTIVE_COUNT + 1))
  done <<EOF
$output
EOF
  [ "${#SLOT_ACTIVE[@]}" -eq 3 ] || {
    echo "[top-supervisor] CONFIG_INVALID: incomplete slot set" >&2
    return 1
  }
}

cleanup_children() {
  local index pid cfg port name command
  # Validate every live child before killing any child. A mixed valid/unknown
  # set must fail without partially restarting the supervisor-owned set.
  for index in "${!PIDS[@]}"; do
    pid="${PIDS[$index]}"
    cfg="${PIDS_CFG[$index]}"
    port="${PIDS_PORT[$index]}"
    name="${PIDS_NAME[$index]}"
    if ! pid_is_alive "$pid"; then
      continue
    fi
    command="$(process_command "$pid")"
    # Only terminate direct children whose command still carries the expected
    # supervisor-launched config. Unknown ownership is left untouched.
    case "$command" in
      *"$cfg"*|*"run_top_engine.sh"*)
        kill "$pid" 2>/dev/null || true
        ;;
      *)
        if port_owned_by_pid "$port" "$pid"; then
          kill "$pid" 2>/dev/null || true
        else
          echo "[top-supervisor] refusing to stop $name PID $pid: ownership unproven" >&2
          return 1
        fi
        ;;
    esac
  done
  for index in "${!PIDS[@]}"; do
    pid="${PIDS[$index]}"
    if pid_is_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for index in "${!PIDS[@]}"; do
    pid="${PIDS[$index]}"
    for _ in $(seq 1 30); do
      pid_is_alive "$pid" || break
      sleep 0.2
    done
    pid_is_alive "$pid" && return 1 || true
  done
  return 0
}

cleanup() {
  cleanup_children || true
  if [ -f "$SUPERVISOR_PID_FILE" ] && grep -q "^pid=$$\$" "$SUPERVISOR_PID_FILE"; then
    rm -f "$SUPERVISOR_PID_FILE" "$OWNER_FILE"
  fi
  if [ "$SUPERVISOR_LOCK_OWNED" -eq 1 ]; then
    release_lock "$SUPERVISOR_LOCK_DIR"
  fi
}

start_engines() {
  local engine cfg port name pids started=0 failed=0 expected=0 slot
  PIDS=()
  PIDS_CFG=()
  PIDS_PORT=()
  PIDS_NAME=()
  read_slot_configs || return 1
  for slot in 1 2 3; do
    read -r cfg port name <<< "${ENGINES[$((slot - 1))]}"
    if [ "${SLOT_ACTIVE[$slot]}" != "1" ]; then
      if ! port_is_free_or_owned "$port" "${PIDS[*]-}"; then
        echo "[top-supervisor] STALE_CHILD: idle slot $slot port $port is occupied" >&2
        failed=$((failed + 1))
      fi
      continue
    fi
    expected=$((expected + 1))
    engine="${ENGINES[$((slot - 1))]}"
    read -r cfg configured_port name <<< "$engine"
    port=$((configured_port + PORT_OFFSET))
    if ! port_is_free_or_owned "$port" "${PIDS[*]-}"; then
      failed=$((failed + 1))
      continue
    fi
    echo "[top-supervisor] START $name: $cfg port=$port"
    SOXS_TOP_ENGINE_REDIRECT_STDIO="$REDIRECT" \
      bash "$LAUNCHER" "$cfg" "$port" "$name" &>/dev/null &
    pid=$!
    PIDS+=("$pid")
    PIDS_CFG+=("$cfg")
    PIDS_PORT+=("$port")
    PIDS_NAME+=("$name")
    started=$((started + 1))
  done
  echo "[top-supervisor] Launched $started TOP engines (supervisor PID $$ waiting)"
  if [ "$failed" -gt 0 ] || [ "$started" -ne "$expected" ]; then
    echo "[top-supervisor] refusing readiness: expected $expected active engines, launched $started" >&2
    return 1
  fi
  [ "$expected" -eq 0 ] && return 0
  if [ "$REQUIRE_READINESS" = "1" ] && ! wait_for_readiness; then
    echo "[top-supervisor] refusing readiness: one or more TOP engines are not healthy" >&2
    cleanup_children || true
    return 1
  fi
  return 0
}

wait_for_readiness() {
  local index pid port name pids url response
  for _ in $(seq 1 "$READINESS_TIMEOUT"); do
    local all_ready=1
    for index in "${!PIDS[@]}"; do
      pid="${PIDS[$index]}"
      port="${PIDS_PORT[$index]}"
      name="${PIDS_NAME[$index]}"
      if ! pid_is_alive "$pid"; then
        echo "[top-supervisor] $name exited before readiness" >&2
        return 1
      fi
      if ! port_pids "$port" | tr ' ' '\n' | grep -qx "$pid" 2>/dev/null; then
        all_ready=0
        continue
      fi
      url="http://127.0.0.1:${port}${HEALTH_PATH}"
      if ! response="$(curl --noproxy '*' --silent --show-error --fail --max-time 1 "$url" 2>/dev/null)"; then
        all_ready=0
        continue
      fi
      case "$response" in
        *'"mode":"paper"'*|*'"mode": "paper"'*|*'"execution_mode":"paper"'*|*'"execution_mode": "paper"'*) ;;
        *)
          echo "[top-supervisor] $name health response is not PAPER" >&2
          return 1
          ;;
      esac
    done
    [ "$all_ready" -eq 1 ] && return 0
    sleep 1
  done
  return 1
}

process_restart() {
  local request_id="$(sed -n 's/^request_id=//p' "$REQUEST_FILE" 2>/dev/null | head -n 1)"
  local detail="restart_confirmed"
  local result=0
  [ -n "$request_id" ] || request_id="signal-$(date +%s)-$$"
  rm -f "$REQUEST_FILE"
  write_status "restart_pending" "$request_id" "stopping_owned_children" "$GENERATION"
  if ! cleanup_children; then
    GENERATION=$((GENERATION + 1))
    write_status "restart_failed" "$request_id" "child_ownership_unproven" "$GENERATION"
    return
  fi
  PIDS=()
  PIDS_CFG=()
  PIDS_PORT=()
  PIDS_NAME=()
  if ! start_engines; then
    detail="restart_failed"
    result=1
  fi
  GENERATION=$((GENERATION + 1))
  if [ "$result" -eq 0 ]; then
    if [ "$ACTIVE_COUNT" -eq 0 ]; then
      detail="idle_no_selection"
    fi
    write_status "restart_confirmed" "$request_id" "$detail" "$GENERATION"
  else
    write_status "restart_failed" "$request_id" "$detail" "$GENERATION"
    # A failed restart is a contained degraded state. Keep the canonical
    # supervisor alive so a subsequent control request can recover it; never
    # let launchd race a second supervisor into the same ports.
    PIDS=()
    PIDS_CFG=()
    PIDS_PORT=()
    PIDS_NAME=()
  fi
}

request_restart() {
  if ! supervisor_is_owned; then
    echo "[top-supervisor] restart refused: canonical supervisor is not running or ownership is unknown" >&2
    return 3
  fi
  if ! acquire_lock "$RESTART_LOCK_DIR"; then
    echo "[top-supervisor] restart refused: another restart is in progress" >&2
    return 4
  fi
  trap 'release_lock "$RESTART_LOCK_DIR"' EXIT
  local request_id="restart-$(date +%s)-$$"
  local supervisor_pid="$(sed -n 's/^pid=//p' "$SUPERVISOR_PID_FILE" | head -n 1)"
  local before_generation="$(read_status_value generation)"
  printf 'request_id=%s\n' "$request_id" > "$REQUEST_FILE.tmp-$$"
  mv -f "$REQUEST_FILE.tmp-$$" "$REQUEST_FILE"
  kill -USR1 "$supervisor_pid" 2>/dev/null || {
    echo "[top-supervisor] restart refused: supervisor signal failed" >&2
    return 5
  }
  local state generation detail
  for _ in $(seq 1 "${SOXS_TOP_RESTART_TIMEOUT_SECONDS:-20}"); do
    state="$(read_status_value state)"
    generation="$(read_status_value generation)"
    detail="$(read_status_value detail)"
    if [ "$generation" != "$before_generation" ] && [ "$(read_status_value request_id)" = "$request_id" ]; then
      if [ "$state" = "restart_confirmed" ]; then
        echo "[top-supervisor] restart confirmed generation=$generation"
        return 0
      fi
      echo "[top-supervisor] restart failed: $detail" >&2
      return 6
    fi
    sleep 1
  done
  echo "[top-supervisor] restart pending: supervisor did not confirm within timeout" >&2
  return 7
}

if [ "$MODE" = "restart" ]; then
  request_restart
  exit $?
fi

if [ "$MODE" != "start" ]; then
  echo "usage: $0 [start|restart]" >&2
  exit 2
fi

if ! acquire_lock "$SUPERVISOR_LOCK_DIR"; then
  echo "[top-supervisor] start refused: another supervisor owns the control boundary" >&2
  exit 8
fi
SUPERVISOR_LOCK_OWNED=1
if supervisor_is_owned; then
  echo "[top-supervisor] start refused: supervisor already running" >&2
  exit 9
fi
trap cleanup EXIT
trap 'exit 0' INT TERM
printf 'pid=%s\nproject_dir=%s\nscript=%s\n' "$$" "$PROJECT_DIR" "$PROJECT_DIR/scripts/start_top_engines.sh" > "$SUPERVISOR_PID_FILE.tmp-$$"
mv -f "$SUPERVISOR_PID_FILE.tmp-$$" "$SUPERVISOR_PID_FILE"
printf 'pid=%s\nproject_dir=%s\nscript=%s\n' "$$" "$PROJECT_DIR" "$PROJECT_DIR/scripts/start_top_engines.sh" > "$OWNER_FILE.tmp-$$"
mv -f "$OWNER_FILE.tmp-$$" "$OWNER_FILE"
write_status "starting" "" "initial_start" "$GENERATION"
trap 'RESTART_REQUESTED=1' USR1

if ! start_engines; then
  write_status "start_failed" "" "unknown_port_or_engine_start_failure" "$GENERATION"
  exit 10
fi
if [ "$ACTIVE_COUNT" -eq 0 ]; then
  write_status "idle_no_selection" "" "supervisor_ready" "$GENERATION"
else
  write_status "running" "" "supervisor_ready" "$GENERATION"
fi

while :; do
  if [ "$RESTART_REQUESTED" -eq 1 ]; then
    RESTART_REQUESTED=0
    process_restart
    continue
  fi
  # Bash 3/macOS has no wait -n; waiting on the known children preserves the
  # supervisor boundary and is interruptible by USR1.
  if [ "${#PIDS[@]}" -gt 0 ]; then
    wait "${PIDS[@]}" 2>/dev/null || true
    live_children=0
    for pid in "${PIDS[@]}"; do
      if pid_is_alive "$pid"; then
        live_children=1
        break
      fi
    done
    if [ "$live_children" -eq 0 ]; then
      write_status "supervisor_degraded" "" "all_owned_engines_exited" "$GENERATION"
      exit 11
    fi
    sleep 1
  else
    sleep 1
  fi
done
