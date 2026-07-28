#!/usr/bin/env python3
"""Read-only per-symbol MARKET_DATA audit."""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from src.openalpha.data_diagnostics import diagnose_market_data


def _format_cell(value: object) -> str:
    text = str(value or "").strip()
    return text if text else "-"


def _format_table(rows: list[dict[str, object]], headers: list[str]) -> str:
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(_format_cell(row.get(header))))

    def render_row(row: dict[str, object]) -> str:
        return "  ".join(_format_cell(row.get(header)).ljust(widths[header]) for header in headers)

    lines = [
        "  ".join(header.ljust(widths[header]) for header in headers),
        "  ".join("-" * widths[header] for header in headers),
    ]
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines)


def _build_rows(audits) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for audit in audits:
        data = audit.to_dict()
        rows.append(
            {
                "Symbol": data.get("symbol"),
                "Cache": data.get("cache_status"),
                "Provider": data.get("provider_used"),
                "Quote": data.get("quote_status"),
                "OHLCV": data.get("ohlcv_status"),
                "History": data.get("history_status"),
                "Benchmark": data.get("benchmark_status"),
                "First Failure": data.get("first_failure_node"),
                "Reason": data.get("normalized_failure_reason"),
                "Formal Ready": "YES" if data.get("formal_data_ready") else "NO",
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MARKET_DATA diagnostic")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to audit")
    parser.add_argument("--no-trade", action="store_true", help="No-op flag for safety")
    args = parser.parse_args(argv)

    symbols = [str(symbol).strip().upper() for symbol in args.symbols if str(symbol).strip()]
    if not symbols:
        print("No symbols supplied.")
        return 2

    audits = diagnose_market_data(symbols)
    rows = _build_rows(audits)
    print(_format_table(rows, ["Symbol", "Cache", "Provider", "Quote", "OHLCV", "History", "Benchmark", "First Failure", "Reason", "Formal Ready"]))

    ready_count = sum(1 for audit in audits if audit.formal_data_ready)
    if ready_count == len(audits):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
