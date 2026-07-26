#!/bin/bash
cd "$(dirname "$0")"

echo "=== 停止旧进程 ==="
kill $(lsof -i :8081 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}') 2>/dev/null
kill $(lsof -i :8082 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}') 2>/dev/null
kill $(lsof -i :8093 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}') 2>/dev/null
sleep 2

echo "=== 启动合并监控面板(8090) ==="
.venv/bin/python scripts/start_combined.py > /dev/null 2>&1 &
sleep 2

echo "=== 启动 TOP1 SOXS(8081) ==="
nohup .venv/bin/python run.py --config configs/TOP1.yaml --live --port 8081 > logs/soxs.log 2>&1 &
sleep 3

echo "=== 启动 TOP2 LABD(8082) ==="
nohup .venv/bin/python run.py --config configs/TOP2.yaml --live --port 8082 > logs/labd.log 2>&1 &
sleep 3

echo "=== 启动 TOP3 YINN(8093) ==="
nohup .venv/bin/python run.py --config configs/TOP3.yaml --live --port 8093 > logs/yinn.log 2>&1 &
sleep 3

echo ""
echo "=== 确认状态 ==="
for p in 8090 8081 8082 8093; do
  pid=$(lsof -i :$p -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}')
  if [ -n "$pid" ]; then echo "  Port $p: ✅ PID=$pid"; else echo "  Port $p: ❌"; fi
done

echo ""
echo "=== 最新日志 ==="
echo "SOXS:" && tail -1 logs/soxs.log 2>/dev/null
echo "LABD:" && tail -1 logs/labd.log 2>/dev/null
echo "YINN:" && tail -1 logs/yinn.log 2>/dev/null
