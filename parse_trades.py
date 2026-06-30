#!/usr/bin/env python3
"""
Parse [PAPER] trades from logs and generate CSV report
"""
import re
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

def parse_logs():
    """Parse [PAPER] BUY/SELL entries from all log files in logs/"""
    trades = []
    logs_dir = Path("logs")
    
    if not logs_dir.exists():
        print("❌ logs/ directory not found")
        return trades
    
    # Pattern: HH:MM:SS [INFO   ] [PAPER] BUY|SELL qty TICKER @ $price (commission: $comm, cash: $cash)
    paper_pattern = re.compile(
        r'(\d{2}:\d{2}:\d{2})\s+\[INFO\s+\]\s+\[PAPER\]\s+(BUY|SELL)\s+(\d+)\s+(\w+)\s+@\s+\$(\d+\.\d+)\s+\(commission:\s+\$(\d+\.\d+),\s+cash:\s+\$(\d+\.\d+)\)'
    )
    
    # Collect trades from each log file
    for log_file in sorted(logs_dir.glob("*.log")):
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            matches = paper_pattern.findall(content)
            for match in matches:
                time_str, action, qty, ticker, price, commission, cash = match
                trades.append({
                    'time': time_str,
                    'action': action,
                    'qty': int(qty),
                    'ticker': ticker,
                    'price': float(price),
                    'commission': float(commission),
                    'cash': float(cash),
                })
    
    # Sort by time
    trades.sort(key=lambda x: x['time'])
    
    # Extract P&L from matching SELL entries in logs (simple heuristic)
    # For each SELL, look for subsequent Loss/P&L line in the same log
    pnl_map = extract_pnl_map()
    for trade in trades:
        if trade['action'] == 'SELL':
            key = (trade['time'], trade['ticker'])
            trade['pnl'] = pnl_map.get(key, None)
    
    return trades

def extract_pnl_map():
    """Extract P&L from logs for SELL operations"""
    pnl_map = {}
    logs_dir = Path("logs")
    
    # Pattern: [92m[HH:MM:SS] 💱 SELL qty TICKER
    # followed by line with P&L: $±amount
    sell_time_pattern = re.compile(r'\[92m\[(\d{2}:\d{2}:\d{2})\]\s+💱\s+SELL\s+\d+\s+(\w+)')
    pnl_pattern = re.compile(r'P&L:\s+\$([+-]?\d+\.\d+)')
    
    for log_file in sorted(logs_dir.glob("*.log")):
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            for i, line in enumerate(lines):
                match = sell_time_pattern.search(line)
                if match:
                    time_str, ticker = match.groups()
                    # Look ahead for P&L in next few lines
                    for j in range(i, min(i+5, len(lines))):
                        pnl_match = pnl_pattern.search(lines[j])
                        if pnl_match:
                            pnl_map[(time_str, ticker)] = float(pnl_match.group(1))
                            break
    
    return pnl_map

def generate_csv(trades, today_str):
    """Generate CSV report"""
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    
    csv_file = reports_dir / f"trades-{today_str}.csv"
    
    with open(csv_file, 'w') as f:
        f.write("Time,Ticker,Action,Qty,Price,Commission,Total,Cash,P&L\n")
        
        for trade in trades:
            total = trade['qty'] * trade['price'] + trade['commission']
            pnl_str = f"{trade['pnl']:.2f}" if trade.get('pnl') is not None else ""
            
            line = f"{trade['time']},{trade['ticker']},{trade['action']},{trade['qty']},{trade['price']:.2f},{trade['commission']:.2f},{total:.2f},{trade['cash']:.2f},{pnl_str}\n"
            f.write(line)
    
    return csv_file

def main():
    today = datetime.now().strftime("%Y%m%d")
    print(f"📊 Parsing trades for {today}...")
    
    trades = parse_logs()
    print(f"✅ Found {len(trades)} trades")
    
    if trades:
        csv_file = generate_csv(trades, today)
        print(f"✅ Generated {csv_file}")
        
        # Calculate summary
        by_ticker = defaultdict(lambda: {'trades': 0, 'pnl': 0})
        for trade in trades:
            by_ticker[trade['ticker']]['trades'] += 1
            if trade['action'] == 'SELL' and trade.get('pnl') is not None:
                by_ticker[trade['ticker']]['pnl'] += trade['pnl']
        
        print("\n📈 Summary by ticker:")
        total_pnl = 0
        for ticker in sorted(by_ticker.keys()):
            stats = by_ticker[ticker]
            print(f"  {ticker}: {stats['trades']} trades, P&L: ${stats['pnl']:.2f}")
            total_pnl += stats['pnl']
        
        print(f"\n💰 Total P&L: ${total_pnl:.2f}")
        print(f"\n🌐 Report: file://{csv_file.absolute()}")
        print(f"📍 Or browse: http://localhost:8000/reports/trades-{today}.csv")
    else:
        print("⚠️  No trades found")

if __name__ == "__main__":
    main()
