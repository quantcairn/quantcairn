#!/usr/bin/env python3
"""QuantCairn basic demo — uses the public Python API only.

No API keys, broker connections, or external data required.
Run with: python examples/basic_demo.py
"""

from quantcairn import DemoDataProvider

# Create a demo data provider with deterministic synthetic OHLCV data.
# 5 well-known symbols, 252 trading days each, seeded random walk.
provider = DemoDataProvider()

print("QuantCairn Demo")
print("===============")
print()
print(f"Symbols: {', '.join(provider.symbols)}")
print(f"Data rows per symbol: {len(provider.get_ohlcv('AAPL'))}")
print()

# Print the most recent close and volume for each symbol
print("Recent data (last close):")
print("-" * 45)
print(f"{'Symbol':<8} {'Close':>10} {'Volume':>14}")
print("-" * 45)
for sym in provider.symbols:
    price = provider.price_at(sym)
    volume = provider.daily_volume_at(sym)
    print(f"{sym:<8} ${price:>9.2f} {int(volume):>14,d}")

print()
print("Demo complete. No API keys or broker connection required.")
