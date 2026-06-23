#!/bin/bash
# 交易时段状态快照，每15分钟记录一次
PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
SNAPSHOT_FILE="$PROJECT_DIR/snapshots.log"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

cd "$PROJECT_DIR" || exit 1

# 取 API JSON 状态
STATUS=$(curl -s http://localhost:8080/api/status 2>/dev/null)

if [ -z "$STATUS" ]; then
    echo "[$(date '+%m-%d %H:%M')] ⚠️  Server not responding" >> "$SNAPSHOT_FILE"
    exit 1
fi

# 提取关键字段
PRICE=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"\${d['price']:.2f}\")" 2>/dev/null)
SIGNAL=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['last_signal'])" 2>/dev/null)
PNL=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"\${d['daily_pnl']:+.2f}\")" 2>/dev/null)
POS=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['position_shares'])" 2>/dev/null)
SUPP=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"\${d['support']:.2f}\")" 2>/dev/null)
RES=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"\${d['resistance']:.2f}\")" 2>/dev/null)
TRADES=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['trades_today'])" 2>/dev/null)
HALTED=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print('🛑HALTED' if d['halted'] else '✅OK')" 2>/dev/null)

echo "[$(date '+%m-%d %H:%M')] $PRICE | ${SIGNAL:0:4} | PnL=$PNL | Pos=${POS}sh | $TRADES trades | $HALTED | S=$SUPP R=$RES" >> "$SNAPSHOT_FILE"
