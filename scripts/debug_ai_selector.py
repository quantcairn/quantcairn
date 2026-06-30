#!/usr/bin/env python3
"""Debug runner for AI selector and optional broker dry-run/sandbox order path."""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.broker.longbridge import LongBridgeBroker
from src.news_agent.news_collector import NewsCollector
from src.scoring.scorer import Scorer
from src.universe.universe import Universe


def parse_args():
    parser = argparse.ArgumentParser(description="Trace AI selector scoring and broker integration")
    parser.add_argument("--symbol", help="Skip universe selection and score/order this symbol only")
    parser.add_argument("--execute-order", action="store_true", help="Submit selected symbol through broker")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--live", action="store_true", help="Use DRY_RUN=false for broker call")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.symbol:
        tickers = [args.symbol.upper()]
    else:
        u = Universe()
        print("Fetching universe...")
        tickers = u.build_universe()
        print(f"Universe size: {len(tickers)}")
        if not tickers:
            print("No tickers passed basic filters. Exiting debug.")
            return

    news = NewsCollector()
    scorer = Scorer()
    news_map = news.collect_for_symbols(tickers)
    selected = None

    for ticker in tickers:
        print("---")
        print(f"Ticker: {ticker}")
        snippets = news_map.get(ticker, [])
        print(f"  News snippets: {len(snippets)}")
        try:
            scored = scorer.score_universe([ticker], {ticker: snippets})
            if scored:
                item = scored[0]
                print(
                    f"  Score: {item['score']} "
                    f"(tech={item['tech_score']}, news={item['news_score']}, vol={item['vol_score']})"
                )
                if selected is None or item["score"] > selected["score"]:
                    selected = item
            else:
                print("  Scoring returned empty for this ticker.")
        except Exception as exc:
            print("  Scoring error:", exc)

    if not args.execute_order:
        return
    if not selected and args.symbol:
        selected = {
            "ticker": args.symbol.upper(),
            "score": None,
            "range_low": None,
            "range_high": None,
        }
        print("No score available; using explicit --symbol for broker dry-run.")
    if not selected:
        raise SystemExit("No selected ticker available for broker order")

    order = {
        "symbol": selected["ticker"],
        "side": args.side,
        "qty": args.qty,
        "order_type": args.order_type,
        "metadata": {
            "source": "debug_ai_selector",
            "score": selected.get("score"),
            "range_low": selected.get("range_low"),
            "range_high": selected.get("range_high"),
        },
    }
    if args.price is not None:
        order["price"] = args.price

    broker = LongBridgeBroker(dry_run=not args.live)
    print("---")
    print("Broker order request ->", order)
    placed = broker.place_order(order)
    print("Broker place_order ->", placed)
    order_id = placed.get("order_id") or placed.get("body", {}).get("order_id")
    if order_id:
        print("Broker get_order_status ->", broker.get_order_status(order_id))


if __name__ == "__main__":
    main()
