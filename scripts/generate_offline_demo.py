#!/usr/bin/env python3
"""Generate offline demo Top10/Top3 using synthetic data for validation."""
import os
import random
import yaml
from datetime import datetime
import subprocess

SAMPLES = [
    'AAPL','MSFT','AMZN','TSLA','NVDA','AMD','NFLX','GOOGL','META','ORCL',
    'INTC','IBM','ADBE','CSCO','CRM','UBER','LYFT','QCOM','TXN','AVGO'
]


def synth_score(ticker):
    tech = random.uniform(30,90)
    news = random.uniform(20,90)
    vol = random.uniform(10,100)
    score = 0.4*tech + 0.3*news + 0.2*vol + 0.1*(100-random.uniform(0,100))
    return {
        'ticker': ticker,
        'score': round(score,2),
        'tech_score': round(tech,2),
        'news_score': round(news,2),
        'vol_score': round(vol,2),
        'range_low': round(random.uniform(5, 50),2),
        'range_high': round(random.uniform(51, 150),2),
        'risk': {'stop_loss_pct': 1.5},
        'size': random.randint(10,500)
    }


def main():
    scored = [synth_score(t) for t in SAMPLES]
    scored = sorted(scored, key=lambda x: x['score'], reverse=True)
    top10 = scored[:10]
    top3 = top10[:3]

    base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    cfg_dir = os.path.join(base, 'configs')
    os.makedirs(cfg_dir, exist_ok=True)

    # write top configs
    for i, item in enumerate(top3, start=1):
        cfg = {
            'ticker': item['ticker'],
            'mode': 'paper',
            'range': {
                'mode': 'manual',
                'support_price': float(item['range_low']),
                'resistance_price': float(item['range_high']),
            },
            'position': {'size_per_trade': int(item.get('size', 100)), 'initial_capital': 333.0},
            'risk': item.get('risk', {}),
        }
        path = os.path.join(cfg_dir, f'TOP{i}.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(cfg, f)

    # write report
    reports_dir = os.path.join(base, 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    now = datetime.now().strftime('%Y%m%d')
    rpt_path = os.path.join(reports_dir, f'ai_selection_{now}.md')
    with open(rpt_path, 'w') as f:
        f.write(f'# AI Selection {now}\n\n')
        f.write('## Top10\n\n')
        for i, t in enumerate(top10, start=1):
            f.write(f"{i}. {t['ticker']} — score: {t['score']} (tech {t['tech_score']}, news {t['news_score']})\n")
        f.write('\n## Top3 configs written to configs/TOP1.yaml, TOP2.yaml, TOP3.yaml\n')

    print('Offline demo generated:')
    print('  report:', rpt_path)
    print('  TOP configs in', cfg_dir)
    # local macOS notification
    try:
        top3 = ', '.join([t['ticker'] for t in top3])
        subprocess.run(['osascript', '-e', f'display notification "Top3: {top3}" with title "AI Selector (demo)"'], check=False)
    except Exception:
        pass


if __name__ == '__main__':
    main()
