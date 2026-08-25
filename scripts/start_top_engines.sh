#!/bin/bash
# QuantCairn TOP supervisor.
#
# The launchd-owned foreground process is the only TOP lifecycle owner.
# `restart` is a control client: it never starts or kills an engine itself.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LAUNCHER="$PROJECT_DIR/scripts/run_top_engine.sh"
STATE_DIR="${SOXS_STATE_DIR:-$PROJECT_DIR/state}"
CONTROL_DIR="${SOXS_TOP_CONTROL_DIR:-$STATE_DIR/top_supervisor}"
SUPERVISOR_PID_FILE="$CONTROL_DIR/supervisor.pid"
SUPERVISOR_LOCK_DIR="$CONTROL_DIR/supervisor.lock"
RESTART_LOCK_DIR="$CONTROL_DIR/restart.lock"
REQUEST_FILE="$CONTROL_DIR/restart.request"
STATUS_FILE="$CONTROL_DIR/status"
OWNER_FILE="$CONTROL_DIR/owner"
CHILD_EVIDENCE_DIR="$CONTROL_DIR/children"
REDIRECT="${SOXS_TOP_ENGINE_REDIRECT_STDIO:-1}"
MODE="${1:-start}"
PORT_OFFSET="${SOXS_TOP_PORT_OFFSET:-0}"

# TOP configuration is runtime input.  An immutable release must not depend
# on source-controlled or release-local configuration files.
CONFIG_DIR=""
if [ -n "${SOXS_TOP_CONFIG_DIR:-}" ]; then
  CONFIG_DIR="${SOXS_TOP_CONFIG_DIR}"
elif [ -n "${SOXS_CONFIG_DIR:-}" ]; then
  CONFIG_DIR="${SOXS_CONFIG_DIR%/}/top_configs"
fi
if [ -n "$CONFIG_DIR" ]; then
  CONFIG_DIR="$(cd "$CONFIG_DIR" 2>/dev/null && pwd)" || {
    echo "[top-supervisor] configured TOP config root is unavailable: $CONFIG_DIR" >&2
    exit 12
  }
fi

# Engine definitions: config port log-name. Ports are part of the production
# contract and must stay aligned with the Dashboard and launchd template.
ENGINES=(
  "$CONFIG_DIR/TOP1.yaml 8080 top1"
  "$CONFIG_DIR/TOP2.yaml 8081 top2"
  "$CONFIG_DIR/TOP3.yaml 8082 top3"
)

mkdir -p "$CONTROL_DIR"
mkdir -p "$CHILD_EVIDENCE_DIR"

write_child_start_evidence() {
  local slot="$1" pid="$2" cfg="$3" port="$4"
  local path="$CHILD_EVIDENCE_DIR/${slot}.status"
  {
    printf 'slot=%s\n' "$slot"
    printf 'pid=%s\n' "$pid"
    printf 'config=%s\n' "$cfg"
    printf 'port=%s\n' "$port"
    printf 'state=running\n'
    printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$path.tmp-$$"
  mv -f "$path.tmp-$$" "$path"
}

write_child_exit_evidence() {
  local slot="$1" pid="$2" exit_code="$3" exit_signal="$4" cfg="$5" port="$6"
  local path="$CHILD_EVIDENCE_DIR/${slot}.status"
  {
    printf 'slot=%s\n' "$slot"
    printf 'pid=%s\n' "$pid"
    printf 'config=%s\n' "$cfg"
    printf 'port=%s\n' "$port"
    printf 'state=exited\n'
    printf 'exit_code=%s\n' "$exit_code"
    printf 'exit_signal=%s\n' "$exit_signal"
    printf 'exit_timestamp=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$path.tmp-$$"
  mv -f "$path.tmp-$$" "$path"
}

write_status() {
  local state="$1" request_id="$2" detail="$3" generation="$4"
  local tmp="$STATUS_FILE.tmp-$$"
  {
    printf 'state=%s\n' "$state"
    printf 'request_id=%s\n' "$request_id"
    printf 'detail=%s\n' "$detail"
    printf 'generation=%s\n' "$generation"
    printf 'supervisor_pid=%s\n' "$$"
    printf 'project_dir=%s\n' "$PROJECT_DIR"
    printf 'config_dir=%s\n' "$CONFIG_DIR"
    printf 'active_engine_count=%s\n' "${#PIDS[@]}"
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

SUPERVISOR_LOCK_OWNED=0
PIDS=()
PIDS_CFG=()
PIDS_PORT=()
PIDS_NAME=()
RESTART_REQUESTED=0
GENERATION=0

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
        echo "[top-supervisor] refusing to stop $name PID $pid: ownership unproven" >&2
        return 1
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
  local engine cfg port name pids started=0 failed=0
  PIDS=()
  PIDS_CFG=()
  PIDS_PORT=()
  PIDS_NAME=()
  for engine in "${ENGINES[@]}"; do
    read -r cfg configured_port name <<< "$engine"
    port=$((configured_port + PORT_OFFSET))
    if [ -z "$CONFIG_DIR" ] || [ ! -f "$cfg" ]; then
      echo "[top-supervisor] SKIP $name: external TOP config is not selected" >&2
      continue
    fi
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
    write_child_start_evidence "$name" "$pid" "$cfg" "$port"
    started=$((started + 1))
  done
  echo "[top-supervisor] Launched $started TOP engines (supervisor PID $$ waiting)"
  if [ "$failed" -gt 0 ]; then
    return 1
  fi
  return 0
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
    write_status "restart_confirmed" "$request_id" "$detail" "$GENERATION"
  else
    write_status "restart_failed" "$request_id" "$detail" "$GENERATION"
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
if [ "${#PIDS[@]}" -eq 0 ]; then
  write_status "idle_no_selection" "" "no_active_top_configs" "$GENERATION"
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
      for index in "${!PIDS[@]}"; do
        pid="${PIDS[$index]}"
        exit_code=0
        wait "$pid" 2>/dev/null || exit_code=$?
        exit_signal=0
        if [ "$exit_code" -ge 128 ]; then
          exit_signal=$((exit_code - 128))
        fi
        write_child_exit_evidence "${PIDS_NAME[$index]}" "$pid" "$exit_code" "$exit_signal" "${PIDS_CFG[$index]}" "${PIDS_PORT[$index]}"
      done
      write_status "supervisor_degraded" "" "all_owned_engines_exited" "$GENERATION"
      exit 11
    fi
    sleep 1
  else
    sleep 1
  fi
done
