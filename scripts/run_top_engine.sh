#!/bin/bash
set -euo pipefail

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

cfg="${1:?config path required}"
port="${2:?port required}"
log_name="${3:?log name required}"

cd "$PROJECT_DIR"

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

mkdir -p "${SOXS_LOG_DIR:-${TMPDIR:-/private/tmp}/soxs-range-arbitrage/logs}" 2>/dev/null || true

if [ "$ENGINE_MODE" = "live" ]; then
    exec "$VENV_PYTHON" run.py --config "$cfg" --live --dashboard --anytime --port "$port" \
        >> "${SOXS_LOG_DIR:-${TMPDIR:-/private/tmp}/soxs-range-arbitrage/logs}/${log_name}.log" 2>&1
fi

exec env \
    SOXS_SYNTHETIC_MARKET=1 \
    SOXS_SYNTHETIC_START_PRICE="$SYNTH_START" \
    SOXS_SYNTHETIC_AMPLITUDE_PCT="$SYNTH_AMP" \
    SOXS_SYNTHETIC_PERIOD_SECONDS=120 \
    "$VENV_PYTHON" run.py --config "$cfg" --paper --dashboard --anytime --port "$port" \
    >> "${SOXS_LOG_DIR:-${TMPDIR:-/private/tmp}/soxs-range-arbitrage/logs}/${log_name}.log" 2>&1
