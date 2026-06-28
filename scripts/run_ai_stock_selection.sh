#!/bin/zsh
set -euo pipefail

ROOT="/Users/chenwei/Documents/Codex/2026-06-21/all-10-checks-passed-no-errors/work/longbridge_patch"

for env_file in \
  "$HOME/.config/soxs-range-arbitrage/longbridge.env" \
  "$ROOT/.env" \
  "$ROOT/.env.local"
do
  if [ -f "$env_file" ]; then
    source "$env_file"
  fi
done

cd "$ROOT"
exec .venv/bin/python scripts/run_ai_stock_selection.py "$@"
