#!/bin/bash
# QuantCairn — TOP Trading Engines Launcher
#
# Starts all 3 TOP trading engines on their designated ports using the
# existing run_top_engine.sh script.
#
# This script is designed to be invoked directly (for manual testing) or
# via launchd (com.quantcairn.top-engines.plist) for automatic startup.
#
# Usage:
#   bash scripts/start_top_engines.sh
#
# Environment:
#   SOXS_PYTHON_BIN       Python interpreter path (default: .venv/bin/python)
#   SOXS_TOP_ENGINE_REDIRECT_STDIO  Redirect engine output to log files (default: 1)
#   SOXS_SYNTHETIC_MARKET          Synthetic market mode (default: 1)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LAUNCHER="$PROJECT_DIR/scripts/run_top_engine.sh"

REDIRECT="${SOXS_TOP_ENGINE_REDIRECT_STDIO:-1}"

# Engine definitions: config port log-name
# These must stay in sync with deploy/launchd/com.quantcairn.top-engines.plist.template
# and src/dashboard/combined.py TICKERS.
ENGINES=(
  "configs/TOP1.yaml 8080 top1"
  "configs/TOP2.yaml 8081 top2"
  "configs/TOP3.yaml 8082 top3"
)

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
  started=$((started + 1))
done

echo "[top-engines] Launched $started TOP engines"
