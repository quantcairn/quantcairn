#!/bin/bash
# QuantCairn — TOP Trading Engines Launcher
#
# Starts all 3 TOP trading engines on their designated ports using the
# existing run_top_engine.sh script.  Stays in the foreground (waits for
# child engines) so that launchd KeepAlive does not restart-loop.
#
# Usage:
#   bash scripts/start_top_engines.sh            # launchd / manual foreground
#   bash scripts/start_top_engines.sh restart    # restart engines (used by selector)
#
# Environment:
#   SOXS_PYTHON_BIN        Python interpreter path (default: .venv/bin/python)
#   SOXS_TOP_ENGINE_REDIRECT_STDIO  Redirect engine output to log files (default: 1)
#   SOXS_SYNTHETIC_MARKET           Synthetic market mode (default: 1)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LAUNCHER="$PROJECT_DIR/scripts/run_top_engine.sh"

REDIRECT="${SOXS_TOP_ENGINE_REDIRECT_STDIO:-1}"
MODE="${1:-start}"

# Engine definitions: config port log-name
# These must stay in sync with deploy/launchd/com.quantcairn.top-engines.plist.template
# and src/dashboard/combined.py TICKERS.
ENGINES=(
  "configs/TOP1.yaml 8080 top1"
  "configs/TOP2.yaml 8081 top2"
  "configs/TOP3.yaml 8082 top3"
)

# ── restart mode: kill existing engines on known ports, then restart ──
if [ "$MODE" = "restart" ]; then
  echo "[top-engines] RESTART: stopping existing engines..."
  for engine in "${ENGINES[@]}"; do
    read -r cfg port name <<< "$engine"
    # Kill any process listening on this engine's port
    if command -v lsof >/dev/null 2>&1; then
      pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
      if [ -n "$pids" ]; then
        echo "[top-engines]   stopping $name (port $port, PID $pids)"
        kill $pids 2>/dev/null || true
      fi
    fi
  done
  # Wait for ports to actually free up
  for engine in "${ENGINES[@]}"; do
    read -r cfg port name <<< "$engine"
    for i in $(seq 1 30); do
      if ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        break
      fi
      sleep 0.2
    done
    if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "[top-engines]   WARNING: port $port still occupied — force killing"
      lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
      sleep 1
    fi
  done
  echo "[top-engines] Old engines stopped. Starting fresh engines..."
  for engine in "${ENGINES[@]}"; do
    read -r cfg port name <<< "$engine"
    if [ ! -f "$cfg" ]; then
      echo "[top-engines]   SKIP $name: config $cfg not found" >&2
      continue
    fi
    echo "[top-engines]   START $name: $cfg port=$port"
    SOXS_TOP_ENGINE_REDIRECT_STDIO="$REDIRECT" \
      bash "$LAUNCHER" "$cfg" "$port" "$name" &>/dev/null &
  done
  echo "[top-engines] Restart complete — 3 engines launched"
  exit 0
fi

# ── foreground (launchd / normal) mode ──

PIDS=()

cleanup() {
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait "${PIDS[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

started=0

for engine in "${ENGINES[@]}"; do
  read -r cfg port name <<< "$engine"
  if [ ! -f "$cfg" ]; then
    echo "[top-engines] SKIP $name: config $cfg not found" >&2
    continue
  fi
  echo "[top-engines] START $name: $cfg port=$port"
  SOXS_TOP_ENGINE_REDIRECT_STDIO="$REDIRECT" \
    bash "$LAUNCHER" "$cfg" "$port" "$name" &>/dev/null &
  PIDS+=($!)
  started=$((started + 1))
done

echo "[top-engines] Launched $started TOP engines (supervisor PID $$ waiting)"

# Keep the supervisor alive so launchd KeepAlive does not restart-loop.
# If any child exits early, the remaining children continue running.
# The trap above ensures clean shutdown on SIGTERM.
wait "${PIDS[@]}" 2>/dev/null || true
