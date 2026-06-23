#!/bin/bash
# 明早运行这个，一键看昨晚交易总结
PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"

echo ""
echo "══════════════════════════════════════════════"
echo "  📊 SOXS 区间套利 — 交易总结"
echo "  $(date '+%Y-%m-%d %H:%M')"
echo "══════════════════════════════════════════════"
echo ""

# 进程状态
if pgrep -f "run.py" > /dev/null; then
    echo "🟢 引擎状态: 运行中"
else
    echo "🔴 引擎状态: 已停止"
fi
echo ""

# 日志摘要
LOG="$PROJECT_DIR/trading.log"
if [ -f "$LOG" ]; then
    echo "📋 关键事件:"
    grep -E "BUY|SELL|STOP|HALTED|🚀|🛑|P&L|Summary|profit|loss" "$LOG" | tail -20
    echo ""
fi

# 快照文件
SNAP="$PROJECT_DIR/snapshots.log"
if [ -f "$SNAP" ]; then
    echo "📸 盘中快照:"
    cat "$SNAP"
    echo ""
fi

# 统计
echo "📈 统计:"
if [ -f "$LOG" ]; then
    TRADES=$(grep -c "\[PAPER\]" "$LOG" 2>/dev/null || echo "0")
    BUYS=$(grep -c "\[PAPER\] BUY" "$LOG" 2>/dev/null || echo "0")
    SELLS=$(grep -c "\[PAPER\] SELL" "$LOG" 2>/dev/null || echo "0")
    echo "  成交笔数: $TRADES (买$BUYS / 卖$SELLS)"
fi

echo ""
echo "══════════════════════════════════════════════"
