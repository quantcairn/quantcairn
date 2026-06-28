#!/usr/bin/env python3
"""Debug LongBridge broker adapter in dry-run or live OAuth mode."""
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
    parser.add_argument("--live", action="store_true", help="Set DRY_RUN=false and use live OAuth mode")
    parser.add_argument("--skip-cancel", action="store_true")
    parser.add_argument("--balance", action="store_true", help="Print account balance and exit")
    parser.add_argument("--positions-only", action="store_true", help="Print positions and exit")
    return parser.parse_args()


def main():
    args = parse_args()
    broker = LongBridgeBroker(dry_run=not args.live)
    print(f"broker_mode -> {broker.runtime_mode}")

    if args.balance:
        balance = broker.get_account_balance()
        print("account_balance ->", balance)
        payload = balance.get("account_balance") if isinstance(balance, dict) else None
        if isinstance(payload, dict):
            available = payload.get("available_buying_power") or payload.get("buying_power") or payload.get("cash_balance")
            if available is not None:
                print(f"available_buying_power -> {available}")
            if payload.get("cash_balance") is not None:
                print(f"cash_balance -> {payload.get('cash_balance')}")
            if payload.get("equity") is not None:
                print(f"equity -> {payload.get('equity')}")
        return

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

    positions = broker.get_positions()
    print("positions ->", positions)
    account_channel = positions.get("account_channel") if isinstance(positions, dict) else None
    account_mode = positions.get("account_mode") if isinstance(positions, dict) else None
    if account_channel:
        print(f"account_channel -> {account_channel}")
    if account_mode:
        print(f"account_mode -> {account_mode}")
        if str(account_mode).lower() == "paper":
            print("mode_note -> paper trading account")
        elif str(account_mode).lower() == "live":
            print("mode_note -> live trading account")

    if args.positions_only:
        return

    print("get_order_status ->", broker.get_order_status(order_id))
    if not args.skip_cancel:
        print("cancel_order ->", broker.cancel_order(order_id))


if __name__ == "__main__":
    main()
