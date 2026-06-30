#!/usr/bin/env python3
"""Debug LongBridge broker adapter in dry-run or sandbox/prod mode."""
import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.broker.longbridge import LongBridgeBroker


def parse_args():
    parser = argparse.ArgumentParser(description="Validate LongBridge broker order flow")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--live", action="store_true", help="Set DRY_RUN=false for this run")
    parser.add_argument("--skip-cancel", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    broker = LongBridgeBroker(dry_run=not args.live)
    order = {
        "symbol": args.symbol,
        "side": args.side,
        "qty": args.qty,
        "order_type": args.order_type,
    }
    if args.price is not None:
        order["price"] = args.price

    placed = broker.place_order(order)
    print("place_order ->", placed)
    order_id = placed.get("order_id") or placed.get("body", {}).get("order_id")
    if not order_id:
        print("No order_id returned; skipping status/cancel checks")
        return

    print("get_order_status ->", broker.get_order_status(order_id))
    if not args.skip_cancel:
        print("cancel_order ->", broker.cancel_order(order_id))
    print("positions ->", broker.get_positions())


if __name__ == "__main__":
    main()
