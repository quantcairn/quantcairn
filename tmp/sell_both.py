import os, sys, json, yaml
os.chdir('/Users/chenwei/soxs-range-arbitrage')
sys.path.insert(0, '.')
with open('config.local.yaml') as f: priv = yaml.safe_load(f)
lb = priv['broker']['longbridge']
os.environ['LONGBRIDGE_APP_KEY'] = lb['app_key']
os.environ['LONGBRIDGE_APP_SECRET'] = lb['app_secret']
os.environ['LONGBRIDGE_ACCESS_TOKEN'] = lb['access_token']
from src.broker.longbridge_broker import LongBridgeBroker
from src.broker.base import OrderSide, OrderType
b = LongBridgeBroker(app_key=lb['app_key'], app_secret=lb['app_secret'], access_token=lb['access_token'], region=lb.get('region','cn'))
if b.connect():
    for p in b.get_positions():
        if p.ticker in ('NVDA','SOXS') and p.quantity > 0:
            print('SELL', p.ticker, p.quantity, 'at', p.current_price)
            o = b.place_order(ticker=p.ticker, side=OrderSide.SELL, quantity=p.quantity, order_type=OrderType.MARKET, current_bid=p.current_price, notes='manual_force_sell')
            print('ORDER', p.ticker, o.status, 'filled', o.filled_quantity)
    b.disconnect()
print('DONE')
