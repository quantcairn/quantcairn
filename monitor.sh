#!/bin/bash
# 交易时段状态快照，每15分钟记录一次
PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
SNAPSHOT_FILE="$PROJECT_DIR/snapshots.log"

cd "$PROJECT_DIR" || exit 1

# 取 API JSON 状态
STATUS=$(curl -s http://localhost:8080/api/status 2>/dev/null)

if [ -z "$STATUS" ]; then
    echo "[$(date '+%m-%d %H:%M')] ⚠️  Server not responding" >> "$SNAPSHOT_FILE"
    exit 1
fi

printf '%s' "$STATUS" | python3 - <<'PY' >> "$SNAPSHOT_FILE"
import json
from datetime import datetime
import sys

try:
    d = json.load(sys.stdin)
    price = d.get('price', 0.0)
    signal = d.get('last_signal', 'N/A')
    pnl = d.get('daily_pnl', 0.0)
    pos = d.get('position_shares', 0)
    support = d.get('support', 0.0)
    resistance = d.get('resistance', 0.0)
    trades = d.get('trades_today', 0)
    halted = '🛑HALTED' if d.get('halted') else '✅OK'
except Exception:
    price = 0.0
    signal = 'N/A'
    pnl = 0.0
    pos = 0
    support = 0.0
    resistance = 0.0
    trades = 0
    halted = 'UNKNOWN'

print(f"[{datetime.now():%m-%d %H:%M}] ${price:.2f} | {signal[:4]} | PnL=${pnl:.2f} | Pos={pos}sh | {trades} trades | {halted} | S=${support:.2f} R=${resistance:.2f}")
PY
