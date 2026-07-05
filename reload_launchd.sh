#!/bin/bash
set -euo pipefail

PROJECT_DIR="/Users/chenwei/soxs-range-arbitrage"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLISTS=(
  "com.soxs.ai_selector.plist"
  "com.soxs.arbitrage.plist"
  "com.soxs.arbitrage.stop.plist"
)

mkdir -p "$LAUNCH_AGENTS_DIR"
cd "$PROJECT_DIR" || exit 1

for plist in "${PLISTS[@]}"; do
    src="$PROJECT_DIR/launchd/$plist"
    dst="$LAUNCH_AGENTS_DIR/$plist"
    if [ ! -f "$src" ]; then
        echo "缺少文件: $src"
        exit 1
    fi
    plutil -lint "$src" >/dev/null
    launchctl unload "$dst" 2>/dev/null || true
    cp "$src" "$dst"
    launchctl load "$dst"
    echo "已重载: $plist"
done

echo ""
echo "当前定时任务:"
launchctl list | rg 'com\.soxs\.(ai_selector|arbitrage|arbitrage\.stop)' || true
