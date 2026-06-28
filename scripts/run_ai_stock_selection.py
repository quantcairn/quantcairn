#!/usr/bin/env python3
"""Daily premarket AI stock selection runner."""
import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.ai_selector.pipeline import AIStockSelector, write_report, write_top_configs
from src.broker.longbridge import LongBridgeBroker


def parse_args():
    parser = argparse.ArgumentParser(description="Run AI stock selection and generate TOP1/TOP2/TOP3 configs")
    parser.add_argument("--symbols", help="Comma-separated candidate symbols")
    parser.add_argument("--candidate-limit", type=int, default=int(os.getenv("AI_SELECTOR_CANDIDATE_LIMIT", "200")))
    parser.add_argument("--market-proxy", default=os.getenv("LONGBRIDGE_MARKET_PROXY", "SPY.US"))
    parser.add_argument("--config-dir", default="configs")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--skip-configs", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    return parser.parse_args()


def _parse_symbols(value: str | None):
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main():
    args = parse_args()
    symbols = _parse_symbols(args.symbols or os.getenv("LONGBRIDGE_TOP_SYMBOLS") or os.getenv("LONGBRIDGE_SYMBOLS"))
    try:
        broker = LongBridgeBroker(dry_run=True)
    except Exception as exc:
        raise SystemExit(
            "LongBridge is not ready for the selector. Set the trading credentials "
            "(API key/secret and access token or OAuth mode) before running the daily scan."
        ) from exc
    selector = AIStockSelector(
        broker.quote_context,
        candidate_limit=args.candidate_limit,
        market_proxy=args.market_proxy,
    )
    report = selector.run(symbols=symbols)

    print(f"Market proxy: {report.market_proxy} trend={report.market.trend} score={report.market.score:.1f}")
    print(f"Universe size: {report.universe_count}, screened: {report.screened_count}")
    print("Top10:")
    for idx, row in enumerate(report.top10, start=1):
        print(
            f"{idx:>2}. {row.symbol:<10} total={row.total_score:>5.1f} tech={row.technical_score:>5.1f} "
            f"news={row.news_score:>5.1f} vol={row.volume_score:>5.1f} risk={row.risk_score:>5.1f} "
            f"action={row.action} price={row.price:.2f}"
        )

    if not args.skip_report:
        json_path, md_path = write_report(report, args.output_dir)
        print("Report written ->", json_path)
        print("Report written ->", md_path)
    if not args.skip_configs:
        written = write_top_configs(report, args.config_dir)
        for path in written:
            print("Config written ->", path)


if __name__ == "__main__":
    main()
