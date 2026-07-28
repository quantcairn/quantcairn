"""Pre-built universe symbol profiles — 48 symbols across 6 categories.

Each symbol has: asset_type, sector, benchmark, leverage_type, and
initial risk/liquidity/volatility scores.  These profiles are the
starting configuration for the Universe Manager filter pipeline.
"""

import os
from typing import Any, Iterable

from src.universe.models import UniverseSymbol

try:  # optional research dependency
    from yfinance.screener import screen as _yf_screen
except Exception:  # pragma: no cover - yfinance may be absent in core-only mode
    _yf_screen = None


INDEX_ETFS = [
    UniverseSymbol(symbol="SPY",  name="SPDR S&P 500 ETF",            asset_type="index_etf", benchmark="SPY",    risk_score=15.0, volatility_score=18.0, liquidity_score=98.0, tags=["major_index"]),
    UniverseSymbol(symbol="QQQ",  name="Invesco QQQ Trust",           asset_type="index_etf", benchmark="QQQ",    risk_score=20.0, volatility_score=25.0, liquidity_score=96.0, tags=["major_index"]),
    UniverseSymbol(symbol="IWM",  name="iShares Russell 2000 ETF",    asset_type="index_etf", benchmark="IWM",    risk_score=30.0, volatility_score=30.0, liquidity_score=90.0, tags=["major_index"]),
    UniverseSymbol(symbol="DIA",  name="SPDR Dow Jones Industrial Avg", asset_type="index_etf", benchmark="DIA", risk_score=12.0, volatility_score=15.0, liquidity_score=92.0, tags=["major_index"]),
]

MEGA_CAPS = [
    UniverseSymbol(symbol="AAPL",  name="Apple Inc.",                   asset_type="mega_cap", sector="technology",     risk_score=20.0, volatility_score=28.0, liquidity_score=98.0, min_market_cap=2_500_000_000_000),
    UniverseSymbol(symbol="MSFT",  name="Microsoft Corp.",              asset_type="mega_cap", sector="technology",     risk_score=18.0, volatility_score=25.0, liquidity_score=97.0, min_market_cap=2_400_000_000_000),
    UniverseSymbol(symbol="NVDA",  name="NVIDIA Corp.",                 asset_type="mega_cap", sector="technology",     risk_score=35.0, volatility_score=45.0, liquidity_score=95.0, min_market_cap=2_000_000_000_000),
    UniverseSymbol(symbol="AMZN",  name="Amazon.com Inc.",             asset_type="mega_cap", sector="consumer",        risk_score=25.0, volatility_score=30.0, liquidity_score=94.0, min_market_cap=1_500_000_000_000),
    UniverseSymbol(symbol="GOOGL", name="Alphabet Inc. Class A",       asset_type="mega_cap", sector="technology",     risk_score=22.0, volatility_score=28.0, liquidity_score=93.0, min_market_cap=1_500_000_000_000),
    UniverseSymbol(symbol="META",  name="Meta Platforms Inc.",         asset_type="mega_cap", sector="technology",     risk_score=30.0, volatility_score=38.0, liquidity_score=91.0, min_market_cap=900_000_000_000),
    UniverseSymbol(symbol="TSLA",  name="Tesla Inc.",                  asset_type="mega_cap", sector="consumer",        risk_score=40.0, volatility_score=50.0, liquidity_score=90.0, min_market_cap=600_000_000_000),
]

SEMICONDUCTOR = [
    UniverseSymbol(symbol="AMD",   name="Advanced Micro Devices",       asset_type="common_stock", sector="semiconductor", risk_score=35.0, volatility_score=42.0, liquidity_score=88.0),
    UniverseSymbol(symbol="INTC",  name="Intel Corp.",                  asset_type="common_stock", sector="semiconductor", risk_score=38.0, volatility_score=35.0, liquidity_score=85.0),
    UniverseSymbol(symbol="AVGO",  name="Broadcom Inc.",                asset_type="common_stock", sector="semiconductor", risk_score=28.0, volatility_score=32.0, liquidity_score=87.0),
]

SECTOR_ETFS = [
    UniverseSymbol(symbol="XLK",   name="Technology Select Sector SPDR",  asset_type="sector_etf", sector="technology",   risk_score=25.0, volatility_score=28.0, liquidity_score=92.0, tags=["sector"]),
    UniverseSymbol(symbol="XLF",   name="Financial Select Sector SPDR",   asset_type="sector_etf", sector="financial",     risk_score=22.0, volatility_score=22.0, liquidity_score=90.0, tags=["sector"]),
    UniverseSymbol(symbol="XLE",   name="Energy Select Sector SPDR",      asset_type="sector_etf", sector="energy",        risk_score=30.0, volatility_score=30.0, liquidity_score=88.0, tags=["sector"]),
    UniverseSymbol(symbol="XLV",   name="Health Care Select Sector SPDR", asset_type="sector_etf", sector="healthcare",    risk_score=18.0, volatility_score=20.0, liquidity_score=89.0, tags=["sector"]),
    UniverseSymbol(symbol="XLY",   name="Consumer Discretionary SPDR",    asset_type="sector_etf", sector="consumer",      risk_score=28.0, volatility_score=28.0, liquidity_score=87.0, tags=["sector"]),
    UniverseSymbol(symbol="XLI",   name="Industrial Select Sector SPDR",  asset_type="sector_etf", sector="industrial",    risk_score=20.0, volatility_score=22.0, liquidity_score=86.0, tags=["sector"]),
    UniverseSymbol(symbol="XLB",   name="Materials Select Sector SPDR",   asset_type="sector_etf", sector="industrial",    risk_score=24.0, volatility_score=25.0, liquidity_score=82.0, tags=["sector"]),
    UniverseSymbol(symbol="XLU",   name="Utilities Select Sector SPDR",   asset_type="sector_etf", sector="industrial",    risk_score=15.0, volatility_score=18.0, liquidity_score=84.0, tags=["sector"]),
]

LEVERAGED_AND_INVERSE = [
    UniverseSymbol(symbol="SOXL",  name="Direxion Daily Semiconductor Bull 3x",  asset_type="leveraged_etf", sector="semiconductor", benchmark="SOXX",  leverage_type="3x",  risk_score=55.0, volatility_score=65.0, liquidity_score=75.0, tags=["leveraged", "high_risk"]),
    UniverseSymbol(symbol="SOXS",  name="Direxion Daily Semiconductor Bear -3x", asset_type="inverse_etf",   sector="semiconductor", benchmark="SOXX",  leverage_type="-3x", risk_score=55.0, volatility_score=65.0, liquidity_score=72.0, tags=["inverse", "high_risk"]),
    UniverseSymbol(symbol="TQQQ",  name="ProShares UltraPro QQQ 3x",             asset_type="leveraged_etf", sector="technology",    benchmark="QQQ",    leverage_type="3x",  risk_score=60.0, volatility_score=70.0, liquidity_score=78.0, tags=["leveraged", "high_risk"]),
    UniverseSymbol(symbol="SQQQ",  name="ProShares UltraPro Short QQQ -3x",     asset_type="inverse_etf",   sector="technology",    benchmark="QQQ",    leverage_type="-3x", risk_score=60.0, volatility_score=70.0, liquidity_score=76.0, tags=["inverse", "high_risk"]),
    UniverseSymbol(symbol="LABU",  name="Direxion Daily S&P Biotech Bull 3x",    asset_type="leveraged_etf", sector="healthcare",    benchmark="XBI",    leverage_type="3x",  risk_score=58.0, volatility_score=68.0, liquidity_score=70.0, tags=["leveraged", "high_risk"]),
    UniverseSymbol(symbol="LABD",  name="Direxion Daily S&P Biotech Bear -3x",  asset_type="inverse_etf",   sector="healthcare",    benchmark="XBI",    leverage_type="-3x", risk_score=58.0, volatility_score=68.0, liquidity_score=65.0, tags=["inverse", "high_risk"]),
    UniverseSymbol(symbol="TNA",   name="Direxion Daily Small Cap Bull 3x",      asset_type="leveraged_etf", sector="financial",     benchmark="IWM",    leverage_type="3x",  risk_score=55.0, volatility_score=60.0, liquidity_score=72.0, tags=["leveraged", "high_risk"]),
    UniverseSymbol(symbol="TZA",   name="Direxion Daily Small Cap Bear -3x",    asset_type="inverse_etf",   sector="financial",     benchmark="IWM",    leverage_type="-3x", risk_score=55.0, volatility_score=60.0, liquidity_score=68.0, tags=["inverse", "high_risk"]),

    # 2x and 1x leveraged — lower risk than 3x
    UniverseSymbol(symbol="SSO",   name="ProShares Ultra S&P500 2x",             asset_type="leveraged_etf", sector="technology",   benchmark="SPY",  leverage_type="2x",  risk_score=40.0, volatility_score=45.0, liquidity_score=85.0, tags=["leveraged"]),
    UniverseSymbol(symbol="SDS",   name="ProShares UltraShort S&P500 -2x",      asset_type="inverse_etf",   sector="technology",   benchmark="SPY",  leverage_type="-2x", risk_score=40.0, volatility_score=45.0, liquidity_score=80.0, tags=["inverse"]),
    UniverseSymbol(symbol="QLD",   name="ProShares Ultra QQQ 2x",                asset_type="leveraged_etf", sector="technology",   benchmark="QQQ",  leverage_type="2x",  risk_score=45.0, volatility_score=50.0, liquidity_score=82.0, tags=["leveraged"]),
    UniverseSymbol(symbol="QID",   name="ProShares UltraShort QQQ -2x",         asset_type="inverse_etf",   sector="technology",   benchmark="QQQ",  leverage_type="-2x", risk_score=45.0, volatility_score=50.0, liquidity_score=78.0, tags=["inverse"]),
]

# ── Additional high-quality common stocks ─────────────────────────────
ADDITIONAL_STOCKS = [
    UniverseSymbol(symbol="JPM",   name="JPMorgan Chase & Co.",           asset_type="common_stock", sector="financial",     risk_score=22.0, volatility_score=25.0, liquidity_score=88.0),
    UniverseSymbol(symbol="JNJ",   name="Johnson & Johnson",              asset_type="common_stock", sector="healthcare",    risk_score=15.0, volatility_score=18.0, liquidity_score=85.0),
    UniverseSymbol(symbol="V",     name="Visa Inc.",                      asset_type="common_stock", sector="financial",     risk_score=20.0, volatility_score=22.0, liquidity_score=86.0),
    UniverseSymbol(symbol="UNH",   name="UnitedHealth Group Inc.",        asset_type="common_stock", sector="healthcare",    risk_score=18.0, volatility_score=22.0, liquidity_score=84.0),
    UniverseSymbol(symbol="WMT",   name="Walmart Inc.",                   asset_type="common_stock", sector="consumer",      risk_score=12.0, volatility_score=16.0, liquidity_score=82.0),
    UniverseSymbol(symbol="PG",    name="Procter & Gamble Co.",           asset_type="common_stock", sector="consumer",      risk_score=10.0, volatility_score=14.0, liquidity_score=80.0),
    UniverseSymbol(symbol="XOM",   name="Exxon Mobil Corp.",              asset_type="common_stock", sector="energy",        risk_score=25.0, volatility_score=28.0, liquidity_score=82.0),
    UniverseSymbol(symbol="BAC",   name="Bank of America Corp.",          asset_type="common_stock", sector="financial",     risk_score=28.0, volatility_score=30.0, liquidity_score=85.0),
    UniverseSymbol(symbol="NFLX",  name="Netflix Inc.",                   asset_type="common_stock", sector="consumer",      risk_score=32.0, volatility_score=38.0, liquidity_score=83.0),
    UniverseSymbol(symbol="ADBE",  name="Adobe Inc.",                     asset_type="common_stock", sector="technology",    risk_score=28.0, volatility_score=32.0, liquidity_score=81.0),
    UniverseSymbol(symbol="CRM",   name="Salesforce Inc.",                asset_type="common_stock", sector="technology",    risk_score=30.0, volatility_score=35.0, liquidity_score=82.0),
    UniverseSymbol(symbol="DIS",   name="Walt Disney Co.",                asset_type="common_stock", sector="consumer",      risk_score=25.0, volatility_score=30.0, liquidity_score=80.0),
]


DEFAULT_DYNAMIC_SCREENERS = (
    "most_actives",
    "day_gainers",
    "undervalued_large_caps",
    "growth_technology_stocks",
    "small_cap_gainers",
    "undervalued_growth_stocks",
)
DEFAULT_DYNAMIC_EXCHANGES = ("NMS", "NYQ", "NGM", "NCM")
DEFAULT_DYNAMIC_TARGET_SIZE = 300
DEFAULT_DYNAMIC_MIN_PRICE = 5.0
DEFAULT_DYNAMIC_MIN_MARKET_CAP = 2_000_000_000.0
DEFAULT_DYNAMIC_MIN_AVG_VOLUME = 1_000_000.0


def _csv_env(name: str, default: Iterable[str]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return tuple(default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return int(default)
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return int(default)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
        return value if value == value else float(default)
    except (TypeError, ValueError):
        return float(default)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        parsed = float(value)
        return parsed if parsed == parsed else float(default)
    except (TypeError, ValueError):
        return float(default)


def _quote_rank(row: dict[str, Any]) -> tuple[float, float, float, str]:
    return (
        _safe_float(row.get("averageDailyVolume3Month") or row.get("regularMarketVolume")),
        _safe_float(row.get("marketCap")),
        _safe_float(row.get("regularMarketPrice")),
        _normalize_symbol(row.get("symbol")),
    )


def _quote_is_eligible(
    row: dict[str, Any],
    *,
    allowed_exchanges: set[str],
    min_price: float,
    min_market_cap: float,
    min_avg_volume: float,
) -> bool:
    if str(row.get("quoteType") or "").upper() != "EQUITY":
        return False
    if str(row.get("market") or "").strip().lower() != "us_market":
        return False
    if str(row.get("exchange") or "").strip().upper() not in allowed_exchanges:
        return False
    if _safe_float(row.get("regularMarketPrice")) < min_price:
        return False
    if _safe_float(row.get("marketCap")) < min_market_cap:
        return False
    if _safe_float(row.get("averageDailyVolume3Month") or row.get("regularMarketVolume")) < min_avg_volume:
        return False
    return True


def _load_dynamic_common_stock_quotes() -> list[dict[str, Any]]:
    """Pull liquid US equity candidates from Yahoo screener data.

    Returns an empty list if the optional research dependency is missing
    or Yahoo is temporarily unavailable. The caller will then fall back
    to the curated seed universe.
    """

    if _yf_screen is None:
        return []

    screeners = _csv_env("QUANTCAIRN_UNIVERSE_SCREENERS", DEFAULT_DYNAMIC_SCREENERS)
    allowed_exchanges = {item.upper() for item in _csv_env("QUANTCAIRN_UNIVERSE_INCLUDED_EXCHANGES", DEFAULT_DYNAMIC_EXCHANGES)}
    target_size = _int_env("QUANTCAIRN_UNIVERSE_TARGET_SIZE", DEFAULT_DYNAMIC_TARGET_SIZE)
    min_price = _float_env("QUANTCAIRN_UNIVERSE_MIN_PRICE", DEFAULT_DYNAMIC_MIN_PRICE)
    min_market_cap = _float_env("QUANTCAIRN_UNIVERSE_MIN_MARKET_CAP", DEFAULT_DYNAMIC_MIN_MARKET_CAP)
    min_avg_volume = _float_env("QUANTCAIRN_UNIVERSE_MIN_AVG_VOLUME", DEFAULT_DYNAMIC_MIN_AVG_VOLUME)

    best_by_symbol: dict[str, dict[str, Any]] = {}
    for screener_name in screeners:
        try:
            payload = _yf_screen(screener_name, count=100)
        except Exception:
            continue
        quotes = payload.get("quotes") if isinstance(payload, dict) else []
        if not isinstance(quotes, list):
            continue
        for row in quotes:
            if not isinstance(row, dict):
                continue
            symbol = _normalize_symbol(row.get("symbol"))
            if not symbol:
                continue
            if not _quote_is_eligible(
                row,
                allowed_exchanges=allowed_exchanges,
                min_price=min_price,
                min_market_cap=min_market_cap,
                min_avg_volume=min_avg_volume,
            ):
                continue
            current = best_by_symbol.get(symbol)
            if current is None or _quote_rank(row) > _quote_rank(current):
                best_by_symbol[symbol] = dict(row)

    ordered = sorted(best_by_symbol.values(), key=_quote_rank, reverse=True)
    return ordered[:target_size]


def _clone_symbol(symbol: UniverseSymbol) -> UniverseSymbol:
    return UniverseSymbol.from_dict(symbol.to_dict())


def _dedupe_profiles(groups: Iterable[Iterable[UniverseSymbol]]) -> list[UniverseSymbol]:
    result: list[UniverseSymbol] = []
    seen: set[str] = set()
    for group in groups:
        for symbol in group:
            key = _normalize_symbol(symbol.symbol)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(_clone_symbol(symbol))
    return result


def default_universe() -> list[UniverseSymbol]:
    """Return the complete default universe.

    The curated seed symbols remain intact, while an additional liquid
    US equity pool is sourced from Yahoo screener results at runtime.
    """

    dynamic_common_stocks = [
        UniverseSymbol(
            symbol=_normalize_symbol(row.get("symbol")),
            name=str(row.get("longName") or row.get("shortName") or row.get("displayName") or _normalize_symbol(row.get("symbol"))),
            asset_type="common_stock",
            sector=str(row.get("sector") or ""),
            benchmark="SPY",
            leverage_type="1x",
            min_price=DEFAULT_DYNAMIC_MIN_PRICE,
            min_avg_volume=500_000,
            min_market_cap=DEFAULT_DYNAMIC_MIN_MARKET_CAP,
            liquidity_score=90.0,
            risk_score=35.0,
            volatility_score=35.0,
            tags=["dynamic", "yahoo_screener"],
        )
        for row in _load_dynamic_common_stock_quotes()
        if _normalize_symbol(row.get("symbol"))
    ]

    return _dedupe_profiles([
        INDEX_ETFS,
        MEGA_CAPS,
        SEMICONDUCTOR,
        SECTOR_ETFS,
        LEVERAGED_AND_INVERSE,
        ADDITIONAL_STOCKS,
        dynamic_common_stocks,
    ])
