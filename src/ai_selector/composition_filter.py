from __future__ import annotations

from typing import Any


LEVERAGED_INVERSE_ETFS = {
    "SOXS",
    "LABD",
    "DRIP",
    "YINN",
    "TQQQ",
    "SQQQ",
    "SOXL",
    "LABU",
    "BOIL",
    "KOLD",
    "UVXY",
    "SPXS",
    "SPXL",
    "FAS",
    "FAZ",
}

INVERSE_ETFS = {
    "SOXS",
    "LABD",
    "DRIP",
    "SQQQ",
    "KOLD",
    "SPXS",
    "FAZ",
}


def is_leveraged_or_inverse_etf(ticker: str) -> bool:
    return str(ticker or "").strip().upper() in LEVERAGED_INVERSE_ETFS


def is_inverse_etf(ticker: str) -> bool:
    return str(ticker or "").strip().upper() in INVERSE_ETFS


class CompositionFilter:
    def filter_top_n(self, candidates: list, top_n: int = 3) -> dict:
        top_n = max(1, int(top_n or 3))
        ranked = [dict(item) for item in candidates or []]
        ranked.sort(
            key=lambda item: (
                -float(item.get("final_score") or item.get("score") or 0.0),
                str(item.get("ticker") or ""),
            )
        )

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        warnings: list[str] = []
        leveraged_count = 0

        for item in ranked:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue

            leveraged = is_leveraged_or_inverse_etf(ticker)
            inverse = is_inverse_etf(ticker)
            annotated = dict(item)
            annotated["ticker"] = ticker
            annotated["leveraged_etf"] = leveraged
            annotated["inverse_etf"] = inverse

            if leveraged:
                if leveraged_count >= 1:
                    rejected.append(
                        {
                            "ticker": ticker,
                            "reason": "leveraged_etf_limit_exceeded",
                            "leveraged_etf": True,
                            "inverse_etf": inverse,
                        }
                    )
                    continue
                leveraged_count += 1

            if len(accepted) < top_n:
                annotated["composition_filter_passed"] = True
                annotated["composition_reject_reason"] = ""
                annotated["final_rank"] = len(accepted) + 1
                accepted.append(annotated)
            else:
                reason = "leveraged_etf_limit_exceeded" if leveraged else "top_n_limit_exceeded"
                rejected.append(
                    {
                        "ticker": ticker,
                        "reason": reason,
                        "leveraged_etf": leveraged,
                        "inverse_etf": inverse,
                    }
                )

        if len(accepted) < top_n:
            warnings.append(
                f"top_n_not_filled:{len(accepted)}/{top_n}"
            )
        if any(item.get("reason") == "leveraged_etf_limit_exceeded" for item in rejected):
            warnings.append("leveraged_etf_limit_reached")

        return {
            "accepted": accepted,
            "rejected": rejected,
            "warnings": warnings,
        }
