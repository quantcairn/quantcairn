"""Cancel stuck pending DRIP order and restart TOP1 engine"""
import os, sys, yaml, time
os.chdir('/Users/chenwei/soxs-range-arbitrage')
sys.path.insert(0, '.')

with open('config.local.yaml') as f: priv = yaml.safe_load(f)
lb = priv['broker']['longbridge']

os.environ['LONGBRIDGE_APP_KEY'] = lb['app_key']
os.environ['LONGBRIDGE_APP_SECRET'] = lb['app_secret']
os.environ['LONGBRIDGE_ACCESS_TOKEN'] = lb['access_token']

from src.broker.longbridge_broker import LongBridgeBroker
from src.broker.base import OrderSide, OrderType

b = LongBridgeBroker(
    app_key=lb['app_key'],
    app_secret=***'app_secret'],
    access_token=***'access_token'],
    region=lb.get('region', 'cn'),
)
if not b.connect():
    print('FAIL: broker connect')
    sys.exit(1)

# Get active orders and cancel them
orders = b.get_active_orders('DRIP')
if orders:
    for o in orders:
        if getattr(o, 'status', None) and str(o.status) in ('SUBMITTED', 'PENDING', 'NEW'):
            print(f'CANCELLING order {o.order_id} {o.ticker} {o.side} {o.quantity}')
            result = b.cancel_order(o.order_id)
            print(f'CANCEL result: {result}')
else:
    print('No active orders for DRIP')

# Also check all active orders on the account
for pos in b.get_positions():
    print(f'POS: {pos.ticker} {pos.quantity} @ {pos.avg_entry_price}')

b.disconnect()

# Now kill and restart TOP1 engine
print('Restarting TOP1 engine...')
os.system('kill $(lsof -tiTCP:8091 2>/dev/null) 2>/dev/null')
time.sleep(3)
os.system('cd /Users/chenwei/soxs-range-arbitrage && .venv/bin/python run.py --config configs/TOP1.yaml --live --dashboard --port 8091 > logs/top1.log 2>&1 &')
print('TOP1 restarted')
