import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
# Module-level optional imports: these are try/except'd so the module
# loads even in core-only mode. They also capture the real module
# objects at import time, making them immune to sys.modules monkeypatching
# by other tests.
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    from ta.momentum import rsi  # noqa: F811
    from ta.trend import MACD
    from ta.volatility import AverageTrueRange
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False

from src.config.runtime_values import get_runtime_env, has_longbridge_runtime_credentials
from src.openalpha.settings import get_float_setting
from src.openalpha.universe_filter import evaluate_universe_candidate, infer_asset_type
from src.data.fetcher import PriceFetcher, _configure_yfinance_cache, _provider_ticker


class Scorer:
    """Score symbols for range-bound swing trading.

    The scoring model is intentionally biased toward names that:
    - trade actively,
    - move enough to create a band,
    - but do not trend too hard in one direction,
    - and have a repeatable tendency to rotate through the same price area.
    """

    MIN_PRICE = 4.0
    MAX_PRICE = 30.0
    MIN_AVG_VOLUME = 1_000_000
    MIN_MARKET_CAP = 1_000_000_000
    MIN_HISTORY_ROWS = 60
    MAX_RANGE_WIDTH_PCT = 45.0
    MIN_RANGE_WIDTH_PCT = 4.0
    MIN_ATR_PCT = 1.0
    MAX_ATR_PCT = 12.0
    GAP_LIMIT_PCT = 5.0
    EVENT_NEWS_SCORE = 80.0
    DEFAULT_MARKET_TIMEOUT = 2.0
    DEFAULT_SCORE_WORKERS = 8
    DEFAULT_MIN_SPREAD_PCT = 3.0

    FALLBACK_PROFILES = {
        "NVDA": {"score": 74.0, "range_low": 118.0, "range_high": 154.0, "volume": 220_000_000},
        "AMD": {"score": 70.0, "range_low": 110.0, "range_high": 168.0, "volume": 70_000_000},
        "TSLA": {"score": 68.0, "range_low": 170.0, "range_high": 260.0, "volume": 85_000_000},
        "META": {"score": 66.0, "range_low": 470.0, "range_high": 660.0, "volume": 18_000_000},
        "AVGO": {"score": 65.0, "range_low": 160.0, "range_high": 260.0, "volume": 30_000_000},
        "MSFT": {"score": 64.0, "range_low": 410.0, "range_high": 500.0, "volume": 25_000_000},
        "AMZN": {"score": 63.0, "range_low": 165.0, "range_high": 230.0, "volume": 45_000_000},
        "GOOGL": {"score": 62.0, "range_low": 145.0, "range_high": 190.0, "volume": 35_000_000},
        "AAPL": {"score": 61.0, "range_low": 170.0, "range_high": 230.0, "volume": 55_000_000},
        "NFLX": {"score": 60.0, "range_low": 600.0, "range_high": 1000.0, "volume": 5_000_000},
        "QCOM": {"score": 58.0, "range_low": 130.0, "range_high": 190.0, "volume": 9_000_000},
        "UBER": {"score": 57.0, "range_low": 60.0, "range_high": 95.0, "volume": 20_000_000},
        "LYFT": {"score": 61.0, "range_low": 12.5, "range_high": 16.8, "volume": 4_000_000},
        "PLTR": {"score": 62.0, "range_low": 104.0, "range_high": 138.0, "volume": 35_000_000},
        "QBTS": {"score": 67.0, "range_low": 20.0, "range_high": 26.5, "volume": 10_000_000},
        "WULF": {"score": 66.0, "range_low": 21.0, "range_high": 27.8, "volume": 10_000_000},
        "SOFI": {"score": 64.0, "range_low": 15.2, "range_high": 19.8, "volume": 22_000_000},
        "NIO": {"score": 59.0, "range_low": 4.5, "range_high": 5.6, "volume": 9_000_000},
        "SMR": {"score": 63.0, "range_low": 8.5, "range_high": 11.2, "volume": 10_000_000},
        "SOXL": {"score": 71.0, "range_low": 18.2, "range_high": 24.8, "volume": 42_000_000},
        "SOXS": {"score": 69.0, "range_low": 4.0, "range_high": 5.4, "volume": 36_000_000},
        "LABU": {"score": 67.0, "range_low": 13.8, "range_high": 18.9, "volume": 16_000_000},
        "LABD": {"score": 67.0, "range_low": 6.2, "range_high": 8.8, "volume": 11_000_000},
        "TQQQ": {"score": 70.0, "range_low": 21.4, "range_high": 28.7, "volume": 58_000_000},
        "SQQQ": {"score": 68.0, "range_low": 7.8, "range_high": 10.7, "volume": 95_000_000},
        "TNA": {"score": 66.0, "range_low": 18.0, "range_high": 24.6, "volume": 9_000_000},
        "TZA": {"score": 66.0, "range_low": 11.8, "range_high": 16.2, "volume": 14_000_000},
        "FAS": {"score": 65.0, "range_low": 21.2, "range_high": 28.1, "volume": 18_000_000},
        "FAZ": {"score": 65.0, "range_low": 10.4, "range_high": 14.7, "volume": 14_000_000},
        "GUSH": {"score": 67.0, "range_low": 18.7, "range_high": 25.3, "volume": 13_000_000},
        "DRIP": {"score": 67.0, "range_low": 4.5, "range_high": 6.5, "volume": 8_000_000},
        "YINN": {"score": 64.0, "range_low": 16.2, "range_high": 22.4, "volume": 15_000_000},
        "YANG": {"score": 64.0, "range_low": 9.8, "range_high": 13.9, "volume": 9_000_000},
        "NAIL": {"score": 65.0, "range_low": 18.4, "range_high": 25.1, "volume": 6_000_000},
        "DPST": {"score": 65.0, "range_low": 17.6, "range_high": 24.2, "volume": 5_000_000},
        # ── Universe expansion: 25 active managed-universe symbols (2026-07-24 pricing) ──
        "ADBE": {"score": 54.0, "range_low": 211.5, "range_high": 227.5, "volume": 3_900_000},
        "BAC":  {"score": 56.0, "range_low": 60.3,  "range_high": 63.3,  "volume": 29_300_000},
        "CRM":  {"score": 55.0, "range_low": 155.6, "range_high": 166.9, "volume": 10_600_000},
        "DIA":  {"score": 58.0, "range_low": 507.0, "range_high": 528.0, "volume": 2_800_000},
        "DIS":  {"score": 53.0, "range_low": 91.2,  "range_high": 97.9,  "volume": 8_800_000},
        "INTC": {"score": 50.0, "range_low": 92.3,  "range_high": 100.0, "volume": 91_400_000},
        "IWM":  {"score": 57.0, "range_low": 285.6, "range_high": 297.2, "volume": 17_900_000},
        "JNJ":  {"score": 52.0, "range_low": 253.8, "range_high": 269.5, "volume": 6_800_000},
        "JPM":  {"score": 55.0, "range_low": 340.0, "range_high": 361.0, "volume": 6_700_000},
        "PG":   {"score": 54.0, "range_low": 143.4, "range_high": 150.8, "volume": 5_100_000},
        "QQQ":  {"score": 60.0, "range_low": 672.4, "range_high": 700.0, "volume": 27_700_000},
        "SPY":  {"score": 62.0, "range_low": 725.0, "range_high": 752.0, "volume": 35_500_000},
        "SSO":  {"score": 51.0, "range_low": 63.4,  "range_high": 67.8,  "volume": 1_900_000},
        "UNH":  {"score": 53.0, "range_low": 408.4, "range_high": 438.0, "volume": 4_200_000},
        "V":    {"score": 56.0, "range_low": 342.6, "range_high": 363.8, "volume": 4_600_000},
        "WMT":  {"score": 55.0, "range_low": 105.6, "range_high": 112.1, "volume": 17_600_000},
        "XLB":  {"score": 50.0, "range_low": 49.2,  "range_high": 51.7,  "volume": 8_500_000},
        "XLE":  {"score": 52.0, "range_low": 58.2,  "range_high": 61.7,  "volume": 23_700_000},
        "XLF":  {"score": 53.0, "range_low": 54.8,  "range_high": 57.0,  "volume": 23_700_000},
        "XLI":  {"score": 51.0, "range_low": 177.6, "range_high": 187.7, "volume": 5_000_000},
        "XLK":  {"score": 57.0, "range_low": 171.9, "range_high": 180.7, "volume": 5_800_000},
        "XLU":  {"score": 52.0, "range_low": 45.0,  "range_high": 47.8,  "volume": 13_600_000},
        "XLV":  {"score": 50.0, "range_low": 158.4, "range_high": 166.6, "volume": 5_300_000},
        "XLY":  {"score": 51.0, "range_low": 105.8, "range_high": 112.3, "volume": 7_100_000},
        "XOM":  {"score": 54.0, "range_low": 150.9, "range_high": 163.5, "volume": 11_300_000},
        # ── Phase 1 coverage expansion: 50 high-liquidity symbols (2026-08 approx pricing) ──
        "AA":   {"score": 55.0, "range_low": 32.0,  "range_high": 36.5,  "volume": 6_500_000},
        "AAL":  {"score": 53.0, "range_low": 12.0,  "range_high": 14.2,  "volume": 23_000_000},
        "ADI":  {"score": 56.0, "range_low": 195.0, "range_high": 218.0, "volume": 3_200_000},
        "AEM":  {"score": 57.0, "range_low": 78.0,  "range_high": 88.0,  "volume": 2_800_000},
        "BMY":  {"score": 54.0, "range_low": 48.0,  "range_high": 52.5,  "volume": 12_000_000},
        "BSX":  {"score": 55.0, "range_low": 88.0,  "range_high": 96.0,  "volume": 6_500_000},
        "CCL":  {"score": 53.0, "range_low": 22.0,  "range_high": 26.5,  "volume": 28_000_000},
        "CDE":  {"score": 56.0, "range_low": 5.5,   "range_high": 7.2,   "volume": 9_000_000},
        "CIEN": {"score": 55.0, "range_low": 70.0,  "range_high": 78.0,  "volume": 2_100_000},
        "CMCSA":{"score": 54.0, "range_low": 38.0,  "range_high": 42.0,  "volume": 18_000_000},
        "CSCO": {"score": 55.0, "range_low": 56.0,  "range_high": 61.0,  "volume": 17_000_000},
        "EGO":  {"score": 56.0, "range_low": 14.5,  "range_high": 17.2,  "volume": 3_500_000},
        "ET":   {"score": 58.0, "range_low": 18.0,  "range_high": 20.8,  "volume": 12_500_000},
        "F":    {"score": 54.0, "range_low": 10.0,  "range_high": 11.2,  "volume": 45_000_000},
        "GLW":  {"score": 55.0, "range_low": 42.0,  "range_high": 47.0,  "volume": 5_000_000},
        "GOOG": {"score": 60.0, "range_low": 170.0, "range_high": 195.0, "volume": 22_000_000},
        "GS":   {"score": 56.0, "range_low": 550.0, "range_high": 610.0, "volume": 1_800_000},
        "HOOD": {"score": 58.0, "range_low": 42.0,  "range_high": 52.0,  "volume": 15_000_000},
        "HPE":  {"score": 54.0, "range_low": 18.0,  "range_high": 20.5,  "volume": 11_000_000},
        "IBM":  {"score": 56.0, "range_low": 240.0, "range_high": 265.0, "volume": 3_500_000},
        "KGC":  {"score": 57.0, "range_low": 9.5,   "range_high": 11.5,  "volume": 10_000_000},
        "KHC":  {"score": 53.0, "range_low": 30.0,  "range_high": 33.5,  "volume": 7_500_000},
        "KO":   {"score": 54.0, "range_low": 68.0,  "range_high": 73.0,  "volume": 12_000_000},
        "MET":  {"score": 55.0, "range_low": 78.0,  "range_high": 85.0,  "volume": 3_800_000},
        "MRVL": {"score": 59.0, "range_low": 70.0,  "range_high": 85.0,  "volume": 14_000_000},
        "MS":   {"score": 56.0, "range_low": 120.0, "range_high": 134.0, "volume": 6_000_000},
        "MU":   {"score": 58.0, "range_low": 90.0,  "range_high": 108.0, "volume": 18_000_000},
        "NCLH": {"score": 55.0, "range_low": 24.0,  "range_high": 28.5,  "volume": 10_000_000},
        "NEM":  {"score": 57.0, "range_low": 52.0,  "range_high": 59.0,  "volume": 7_000_000},
        "NKE":  {"score": 54.0, "range_low": 75.0,  "range_high": 82.0,  "volume": 8_500_000},
        "NOW":  {"score": 60.0, "range_low": 850.0, "range_high": 960.0, "volume": 1_200_000},
        "NTR":  {"score": 55.0, "range_low": 55.0,  "range_high": 61.0,  "volume": 2_500_000},
        "ORCL": {"score": 57.0, "range_low": 155.0, "range_high": 172.0, "volume": 8_000_000},
        "OVV":  {"score": 56.0, "range_low": 42.0,  "range_high": 48.0,  "volume": 4_000_000},
        "PCG":  {"score": 54.0, "range_low": 19.0,  "range_high": 21.2,  "volume": 14_000_000},
        "PFE":  {"score": 53.0, "range_low": 27.0,  "range_high": 30.0,  "volume": 30_000_000},
        "PYPL": {"score": 55.0, "range_low": 75.0,  "range_high": 84.0,  "volume": 10_000_000},
        "QLD":  {"score": 61.0, "range_low": 98.0,  "range_high": 112.0, "volume": 2_500_000},
        "RIG":  {"score": 56.0, "range_low": 4.2,   "range_high": 5.1,   "volume": 16_000_000},
        "SDS":  {"score": 54.0, "range_low": 20.0,  "range_high": 22.5,  "volume": 4_000_000},
        "SMCI": {"score": 60.0, "range_low": 400.0, "range_high": 480.0, "volume": 12_000_000},
        "STLA": {"score": 53.0, "range_low": 12.0,  "range_high": 13.8,  "volume": 8_000_000},
        "STM":  {"score": 55.0, "range_low": 28.0,  "range_high": 32.0,  "volume": 4_500_000},
        "SYF":  {"score": 56.0, "range_low": 55.0,  "range_high": 62.0,  "volume": 3_500_000},
        "T":    {"score": 53.0, "range_low": 22.0,  "range_high": 24.5,  "volume": 28_000_000},
        "THC":  {"score": 57.0, "range_low": 140.0, "range_high": 158.0, "volume": 1_500_000},
        "TSM":  {"score": 58.0, "range_low": 160.0, "range_high": 185.0, "volume": 12_000_000},
        "TTD":  {"score": 60.0, "range_low": 95.0,  "range_high": 115.0, "volume": 3_500_000},
        "UAL":  {"score": 55.0, "range_low": 80.0,  "range_high": 92.0,  "volume": 4_500_000},
        "VZ":   {"score": 53.0, "range_low": 42.0,  "range_high": 46.0,  "volume": 15_000_000},
        # ── Phase 2: 25 high-priority symbols from selector diagnostics (2026-08 pricing) ──
        "ACN":  {"score": 55.0, "range_low": 320.0, "range_high": 355.0, "volume": 2_200_000},
        "ADSK": {"score": 58.0, "range_low": 250.0, "range_high": 280.0, "volume": 1_500_000},
        "AGNC": {"score": 56.0, "range_low": 10.0,  "range_high": 11.5,  "volume": 14_000_000},
        "AIG":  {"score": 54.0, "range_low": 75.0,  "range_high": 82.0,  "volume": 3_500_000},
        "ALL":  {"score": 55.0, "range_low": 185.0, "range_high": 205.0, "volume": 1_200_000},
        "AMKR": {"score": 56.0, "range_low": 28.0,  "range_high": 33.0,  "volume": 1_800_000},
        "APH":  {"score": 57.0, "range_low": 65.0,  "range_high": 73.0,  "volume": 2_500_000},
        "ARCC": {"score": 58.0, "range_low": 21.0,  "range_high": 23.5,  "volume": 3_200_000},
        "AU":   {"score": 57.0, "range_low": 28.0,  "range_high": 33.0,  "volume": 4_000_000},
        "AXTA": {"score": 55.0, "range_low": 34.0,  "range_high": 38.0,  "volume": 2_800_000},
        "BB":   {"score": 56.0, "range_low": 3.8,   "range_high": 5.0,   "volume": 8_000_000},
        "BBY":  {"score": 54.0, "range_low": 85.0,  "range_high": 95.0,  "volume": 2_500_000},
        "BCE":  {"score": 53.0, "range_low": 32.0,  "range_high": 35.0,  "volume": 2_000_000},
        "BE":   {"score": 57.0, "range_low": 22.0,  "range_high": 27.0,  "volume": 5_000_000},
        "BKNG": {"score": 56.0, "range_low": 3800.0,"range_high": 4300.0,"volume": 250_000},
        "APLD": {"score": 58.0, "range_low": 8.5,   "range_high": 11.5,  "volume": 6_000_000},
        "APA":  {"score": 55.0, "range_low": 28.0,  "range_high": 32.0,  "volume": 5_500_000},
        "ACGL": {"score": 56.0, "range_low": 100.0, "range_high": 112.0, "volume": 1_800_000},
        "AR":   {"score": 55.0, "range_low": 30.0,  "range_high": 35.0,  "volume": 3_500_000},
        "AUR":  {"score": 57.0, "range_low": 5.5,   "range_high": 7.5,   "volume": 12_000_000},
        "ACAD": {"score": 55.0, "range_low": 16.0,  "range_high": 18.5,  "volume": 2_000_000},
        "ALLY": {"score": 54.0, "range_low": 40.0,  "range_high": 45.0,  "volume": 3_000_000},
        "AGI":  {"score": 56.0, "range_low": 18.0,  "range_high": 21.5,  "volume": 2_500_000},
        "AEO":  {"score": 55.0, "range_low": 20.0,  "range_high": 23.0,  "volume": 3_000_000},
        "BSY":  {"score": 56.0, "range_low": 48.0,  "range_high": 54.0,  "volume": 1_200_000},
    }

    FALLBACK_RANGE_PCT = {
        "NVDA": 0.035,
        "AMD": 0.04,
        "TSLA": 0.045,
        "META": 0.03,
        "AVGO": 0.03,
        "MSFT": 0.025,
        "AMZN": 0.03,
        "GOOGL": 0.03,
        "AAPL": 0.025,
        "NFLX": 0.035,
        "QCOM": 0.03,
        "UBER": 0.035,
        "LYFT": 0.06,
        "PLTR": 0.03,
        "QBTS": 0.08,
        "WULF": 0.08,
        "SOFI": 0.06,
        "NIO": 0.08,
        "SMR": 0.08,
        "SOXL": 0.09,
        "SOXS": 0.1,
        "LABU": 0.1,
        "LABD": 0.1,
        "TQQQ": 0.09,
        "SQQQ": 0.1,
        "TNA": 0.1,
        "TZA": 0.1,
        "FAS": 0.09,
        "FAZ": 0.1,
        "GUSH": 0.1,
        "DRIP": 0.1,
        "YINN": 0.1,
        "YANG": 0.1,
        "NAIL": 0.1,
        "DPST": 0.1,
        # ── Universe expansion ──
        "ADBE": 0.03, "BAC": 0.025, "CRM": 0.035, "DIA": 0.02, "DIS": 0.035,
        "INTC": 0.04, "IWM": 0.02, "JNJ": 0.03, "JPM": 0.03, "PG": 0.025,
        "QQQ": 0.02, "SPY": 0.018, "SSO": 0.035, "UNH": 0.035, "V": 0.03,
        "WMT": 0.03, "XLB": 0.025, "XLE": 0.03, "XLF": 0.02, "XLI": 0.025,
        "XLK": 0.025, "XLU": 0.03, "XLV": 0.025, "XLY": 0.03, "XOM": 0.04,
        # ── Phase 1 expansion ──
        "AA": 0.045, "AAL": 0.05, "ADI": 0.035, "AEM": 0.04, "BMY": 0.025,
        "BSX": 0.03, "CCL": 0.05, "CDE": 0.06, "CIEN": 0.035, "CMCSA": 0.025,
        "CSCO": 0.025, "EGO": 0.06, "ET": 0.035, "F": 0.035, "GLW": 0.03,
        "GOOG": 0.03, "GS": 0.035, "HOOD": 0.05, "HPE": 0.035, "IBM": 0.025,
        "KGC": 0.06, "KHC": 0.03, "KO": 0.02, "MET": 0.03, "MRVL": 0.04,
        "MS": 0.035, "MU": 0.045, "NCLH": 0.045, "NEM": 0.04, "NKE": 0.03,
        "NOW": 0.035, "NTR": 0.035, "ORCL": 0.03, "OVV": 0.04, "PCG": 0.03,
        "PFE": 0.025, "PYPL": 0.035, "QLD": 0.035, "RIG": 0.06, "SDS": 0.03,
        "SMCI": 0.05, "STLA": 0.04, "STM": 0.035, "SYF": 0.035, "T": 0.025,
        "THC": 0.04, "TSM": 0.035, "TTD": 0.045, "UAL": 0.045, "VZ": 0.025,
        # ── Phase 2 ──
        "ACN": 0.03, "ACAD": 0.04, "ACGL": 0.03, "ADSK": 0.035,
        "AGI": 0.04, "AGNC": 0.03, "AIG": 0.025, "ALL": 0.03,
        "ALLY": 0.03, "AMKR": 0.04, "APA": 0.04, "APH": 0.03,
        "APLD": 0.06, "AR": 0.04, "ARCC": 0.025, "AU": 0.045,
        "AUR": 0.06, "AXTA": 0.03, "BB": 0.06, "BBY": 0.03,
        "BCE": 0.025, "BE": 0.05, "BKNG": 0.03, "BSY": 0.03,
        "AEO": 0.04,
    }

    FALLBACK_SECTOR = {
        "NVDA": "Semiconductors",
        "AMD": "Semiconductors",
        "QCOM": "Semiconductors",
        "AVGO": "Semiconductors",
        "TSLA": "Consumer Discretionary",
        "AAPL": "Technology",
        "MSFT": "Technology",
        "GOOGL": "Communication Services",
        "META": "Communication Services",
        "AMZN": "Consumer Discretionary",
        "NFLX": "Communication Services",
        "UBER": "Technology",
        "LYFT": "Technology",
        "PLTR": "Technology",
        "QBTS": "Information Technology",
        "WULF": "Energy",
        "SOFI": "Financial Services",
        "NIO": "Consumer Discretionary",
        "SMR": "Energy",
        "SOXL": "Leveraged Semiconductor ETF",
        "SOXS": "Inverse Semiconductor ETF",
        "LABU": "Leveraged Biotechnology ETF",
        "LABD": "Inverse Biotechnology ETF",
        "TQQQ": "Leveraged Nasdaq ETF",
        "SQQQ": "Inverse Nasdaq ETF",
        "TNA": "Leveraged Small Cap ETF",
        "TZA": "Inverse Small Cap ETF",
        "FAS": "Leveraged Financial ETF",
        "FAZ": "Inverse Financial ETF",
        "GUSH": "Leveraged Energy ETF",
        "DRIP": "Inverse Energy ETF",
        "YINN": "Leveraged China ETF",
        "YANG": "Inverse China ETF",
        "NAIL": "Leveraged Homebuilders ETF",
        "DPST": "Leveraged Regional Banks ETF",
        # ── Universe expansion ──
        "ADBE": "Technology",
        "BAC": "Financial Services",
        "CRM": "Technology",
        "DIA": "Index ETF",
        "DIS": "Communication Services",
        "INTC": "Technology",
        "IWM": "Index ETF",
        "JNJ": "Healthcare",
        "JPM": "Financial Services",
        "PG": "Consumer Defensive",
        "QQQ": "Index ETF",
        "SPY": "Index ETF",
        "SSO": "Leveraged Index ETF",
        "UNH": "Healthcare",
        "V": "Financial Services",
        "WMT": "Consumer Defensive",
        "XLB": "Materials Sector ETF",
        "XLE": "Energy Sector ETF",
        "XLF": "Financial Sector ETF",
        "XLI": "Industrial Sector ETF",
        "XLK": "Technology Sector ETF",
        "XLU": "Utilities Sector ETF",
        "XLV": "Healthcare Sector ETF",
        "XLY": "Consumer Discretionary ETF",
        "XOM": "Energy",
        # ── Phase 1 expansion ──
        "AA": "Materials", "AAL": "Industrials", "ADI": "Semiconductors",
        "AEM": "Materials", "BMY": "Healthcare", "BSX": "Healthcare",
        "CCL": "Consumer Discretionary", "CDE": "Materials",
        "CIEN": "Technology", "CMCSA": "Communication Services",
        "CSCO": "Technology", "EGO": "Materials", "ET": "Energy",
        "F": "Consumer Discretionary", "GLW": "Technology",
        "GOOG": "Communication Services", "GS": "Financial Services",
        "HOOD": "Financial Services", "HPE": "Technology",
        "IBM": "Technology", "KGC": "Materials", "KHC": "Consumer Defensive",
        "KO": "Consumer Defensive", "MET": "Financial Services",
        "MRVL": "Semiconductors", "MS": "Financial Services",
        "MU": "Semiconductors", "NCLH": "Consumer Discretionary",
        "NEM": "Materials", "NKE": "Consumer Discretionary",
        "NOW": "Technology", "NTR": "Materials",
        "ORCL": "Technology", "OVV": "Energy",
        "PCG": "Utilities", "PFE": "Healthcare",
        "PYPL": "Financial Services", "QLD": "Leveraged Index ETF",
        "RIG": "Energy", "SDS": "Inverse Index ETF",
        "SMCI": "Technology", "STLA": "Consumer Discretionary",
        "STM": "Semiconductors", "SYF": "Financial Services",
        "T": "Communication Services", "THC": "Healthcare",
        "TSM": "Semiconductors", "TTD": "Technology",
        "UAL": "Industrials", "VZ": "Communication Services",
        # ── Phase 2 ──
        "ACN": "Technology", "ACAD": "Healthcare", "ACGL": "Financial Services",
        "ADSK": "Technology", "AGI": "Materials", "AGNC": "Financial Services",
        "AIG": "Financial Services", "ALL": "Financial Services",
        "ALLY": "Financial Services", "AMKR": "Semiconductors",
        "APA": "Energy", "APH": "Technology", "APLD": "Technology",
        "AR": "Energy", "ARCC": "Financial Services", "AU": "Materials",
        "AUR": "Technology", "AXTA": "Materials", "BB": "Technology",
        "BBY": "Consumer Discretionary", "BCE": "Communication Services",
        "BE": "Energy", "BKNG": "Consumer Discretionary", "BSY": "Technology",
        "AEO": "Consumer Discretionary",
    }

    FALLBACK_MARKET_CAP = {
        "AAPL": 3_000_000_000_000,
        "MSFT": 3_000_000_000_000,
        "NVDA": 3_000_000_000_000,
        "AMD": 250_000_000_000,
        "TSLA": 900_000_000_000,
        "META": 1_500_000_000_000,
        "AVGO": 1_200_000_000_000,
        "AMZN": 2_000_000_000_000,
        "GOOGL": 2_000_000_000_000,
        "NFLX": 500_000_000_000,
        "QCOM": 180_000_000_000,
        "UBER": 150_000_000_000,
        "LYFT": 5_000_000_000,
        "PLTR": 300_000_000_000,
        "SOFI": 15_000_000_000,
        "NIO": 10_000_000_000,
        # ── Universe expansion ──
        "ADBE": 210_000_000_000,
        "BAC": 280_000_000_000,
        "CRM": 150_000_000_000,
        "DIS": 170_000_000_000,
        "INTC": 400_000_000_000,
        "JNJ": 630_000_000_000,
        "JPM": 500_000_000_000,
        "PG": 350_000_000_000,
        "UNH": 390_000_000_000,
        "V": 650_000_000_000,
        "WMT": 870_000_000_000,
        "XOM": 700_000_000_000,
        # ── Phase 1 expansion (common stocks only; ETFs use aggregate mcap) ──
        "AA": 12_000_000_000, "AAL": 8_000_000_000, "ADI": 110_000_000_000,
        "AEM": 35_000_000_000, "BMY": 100_000_000_000, "BSX": 120_000_000_000,
        "CCL": 30_000_000_000, "CDE": 2_500_000_000, "CIEN": 8_000_000_000,
        "CMCSA": 160_000_000_000, "CSCO": 220_000_000_000, "EGO": 3_000_000_000,
        "ET": 65_000_000_000, "F": 45_000_000_000, "GLW": 35_000_000_000,
        "GOOG": 2_200_000_000_000, "GS": 180_000_000_000, "HOOD": 35_000_000_000,
        "HPE": 25_000_000_000, "IBM": 220_000_000_000, "KGC": 13_000_000_000,
        "KHC": 40_000_000_000, "KO": 290_000_000_000, "MET": 55_000_000_000,
        "MRVL": 85_000_000_000, "MS": 200_000_000_000, "MU": 120_000_000_000,
        "NCLH": 10_000_000_000, "NEM": 60_000_000_000, "NKE": 120_000_000_000,
        "NOW": 200_000_000_000, "NTR": 30_000_000_000, "ORCL": 450_000_000_000,
        "OVV": 13_000_000_000, "PCG": 50_000_000_000, "PFE": 150_000_000_000,
        "PYPL": 80_000_000_000, "RIG": 5_000_000_000, "SMCI": 40_000_000_000,
        "STLA": 50_000_000_000, "STM": 30_000_000_000, "SYF": 20_000_000_000,
        "T": 160_000_000_000, "THC": 22_000_000_000, "TSM": 900_000_000_000,
        "TTD": 55_000_000_000, "UAL": 20_000_000_000, "VZ": 180_000_000_000,
        # ── Gap fill: small-cap symbols missing from Phase 0 ──
        "QBTS": 4_000_000_000, "SMR": 5_000_000_000, "WULF": 2_500_000_000,
        # ── Phase 2 ──
        "ACN": 230_000_000_000, "ADSK": 55_000_000_000, "AGNC": 8_000_000_000,
        "AIG": 50_000_000_000, "ALL": 48_000_000_000, "ALLY": 14_000_000_000,
        "AMKR": 7_000_000_000, "APA": 12_000_000_000, "APH": 75_000_000_000,
        "APLD": 3_000_000_000, "AR": 9_000_000_000, "ARCC": 13_000_000_000,
        "AU": 30_000_000_000, "AUR": 20_000_000_000, "AXTA": 8_000_000_000,
        "BB": 2_500_000_000, "BBY": 20_000_000_000, "BCE": 35_000_000_000,
        "BE": 6_000_000_000, "BKNG": 150_000_000_000, "BSY": 16_000_000_000,
        "ACAD": 3_000_000_000, "ACGL": 38_000_000_000, "AGI": 5_000_000_000,
        "AEO": 4_000_000_000,
    }

    def __init__(self):
        self._cache_status, self._cache_error_message = _configure_yfinance_cache()
        self.min_price = self._env_float("OPENALPHA_MIN_PRICE", get_float_setting("min_price", self.MIN_PRICE))
        self.max_price = self._env_float("OPENALPHA_MAX_PRICE", get_float_setting("max_price", self.MAX_PRICE))
        self.market_timeout = self._env_float("OPENALPHA_MARKET_TIMEOUT", self.DEFAULT_MARKET_TIMEOUT)
        self.score_workers = max(1, self._env_int("OPENALPHA_SCORE_WORKERS", self.DEFAULT_SCORE_WORKERS))
        self.min_spread_pct = self._env_float("OPENALPHA_MIN_SPREAD_PCT", self.DEFAULT_MIN_SPREAD_PCT)
        self.allow_proxy_market = os.environ.get("OPENALPHA_ALLOW_PROXY_MARKET", "0") == "1"
        self._market_cap_cache: dict[str, float | None] = {}

    def _env_float(self, name: str, default: float) -> float:
        raw = os.environ.get(name)
        try:
            return float(raw) if raw not in (None, "") else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _env_int(self, name: str, default: int) -> int:
        raw = os.environ.get(name)
        try:
            return int(raw) if raw not in (None, "") else int(default)
        except (TypeError, ValueError):
            return int(default)

    def _longbridge_symbol(self, symbol: str) -> str:
        return symbol if "." in symbol else f"{symbol}.US"

    def _provider_symbol(self, symbol: str) -> str:
        return _provider_ticker(symbol)

    def _longbridge_value(self, obj, *names, default=None):
        if isinstance(obj, dict):
            for name in names:
                if name in obj and obj[name] is not None:
                    return obj[name]
            return default
        for name in names:
            if hasattr(obj, name):
                value = getattr(obj, name)
                if value is not None:
                    return value
        return default

    def _fetch_longbridge_snapshot(self, symbol: str) -> dict:
        if not has_longbridge_runtime_credentials():
            raise RuntimeError("longbridge credentials unavailable")

        import longbridge.openapi as lb

        config = lb.Config.from_apikey(
            get_runtime_env("LONGBRIDGE_APP_KEY") or get_runtime_env("LONGBRIDGE_API_KEY") or "",
            get_runtime_env("LONGBRIDGE_APP_SECRET") or get_runtime_env("LONGBRIDGE_API_SECRET") or "",
            get_runtime_env("LONGBRIDGE_ACCESS_TOKEN", ""),
            http_url=get_runtime_env("LONGBRIDGE_HTTP_URL") or get_runtime_env("LONGBRIDGE_BASE_URL"),
            quote_ws_url=get_runtime_env("LONGBRIDGE_QUOTE_WS_URL"),
            trade_ws_url=get_runtime_env("LONGBRIDGE_TRADE_WS_URL"),
            log_path=get_runtime_env("LONGBRIDGE_LOG_PATH"),
        )
        ctx = lb.QuoteContext(config)
        try:
            resp = ctx.quote(symbols=[self._longbridge_symbol(symbol)])
            items = resp if isinstance(resp, (list, tuple)) else [resp]
            item = items[0] if items else None
            if item is None:
                raise RuntimeError("longbridge quote unavailable")

            price = self._longbridge_value(item, "last_done", "price", "last_price", default=0.0)
            high = self._longbridge_value(item, "high", "day_high", default=price)
            low = self._longbridge_value(item, "low", "day_low", default=price)
            volume = self._longbridge_value(item, "volume", "turnover", default=0)
            price = float(price or 0.0)
            high = float(high or price or 0.0)
            low = float(low or price or 0.0)
            volume = int(float(volume or 0))
            if price <= 0:
                raise RuntimeError("longbridge quote missing price")

            return {
                "price": price,
                "recent_high": high if high > 0 else price,
                "recent_low": low if low > 0 else price,
                "volume": volume,
            }
        finally:
            for attr in ("close", "dispose", "release"):
                fn = getattr(ctx, attr, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

    def _fetch_chart_daily(self, symbol: str, days: int = 320) -> pd.DataFrame:
        if not _REQUESTS_AVAILABLE:
            raise ImportError(
                "chart data requires the 'requests' package. "
                "Install it with: pip install quantcairn[research]"
            )
        symbol = self._provider_symbol(symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "range": "1y" if days >= 250 else f"{max(days, 1)}d",
            "interval": "1d",
            "includePrePost": "false",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            )
        }
        last_error = None
        resp = None
        trust_env_options = (False, True) if self.allow_proxy_market else (False,)
        for trust_env in trust_env_options:
            session = None
            try:
                session = requests.Session()
                session.trust_env = trust_env
                resp = session.get(url, params=params, headers=headers, timeout=self.market_timeout)
                resp.raise_for_status()
                break
            except Exception as exc:
                last_error = exc
                resp = None
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        if resp is None:
            raise last_error
        payload = None
        try:
            payload = resp.json()
        except Exception:
            return pd.DataFrame()
        if not isinstance(payload, dict):
            return pd.DataFrame()
        chart = payload.get("chart")
        if not isinstance(chart, dict):
            return pd.DataFrame()
        result_list = chart.get("result")
        if not isinstance(result_list, list) or not result_list or result_list[0] is None:
            return pd.DataFrame()
        result = result_list[0]
        if not isinstance(result, dict):
            return pd.DataFrame()
        quote = (result.get("indicators", {}).get("quote") or [None])[0]
        ts = result.get("timestamp") or []
        if not quote or not ts:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "Open": quote.get("open") or [],
                "High": quote.get("high") or [],
                "Low": quote.get("low") or [],
                "Close": quote.get("close") or [],
                "Volume": quote.get("volume") or [],
            }
        )
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return self._standardize_history(df)

    def _load_history(self, symbol: str) -> pd.DataFrame:
        if os.environ.get("OPENALPHA_LIVE_DATA", "1") == "0":
            return pd.DataFrame()

        prefer_yfinance = os.environ.get("OPENALPHA_USE_YFINANCE", "0") == "1"
        allow_yfinance_fallback = os.environ.get("OPENALPHA_ALLOW_YFINANCE_FALLBACK", "0") == "1"
        if prefer_yfinance and allow_yfinance_fallback:
            try:
                df = yf.download(self._provider_symbol(symbol), period="260d", interval="1d", progress=False)
                if df is not None and not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    return self._standardize_history(df)
            except Exception:
                pass
        try:
            try:
                fetcher = PriceFetcher(self._provider_symbol(symbol), poll_interval=0)
                candles = fetcher.get_ohlcv(period="1y", interval="1d")
            finally:
                fetcher.close()
            if candles:
                df = pd.DataFrame(
                    {
                        "Open": [float(item.open) for item in candles],
                        "High": [float(item.high) for item in candles],
                        "Low": [float(item.low) for item in candles],
                        "Close": [float(item.close) for item in candles],
                        "Volume": [float(item.volume or 0.0) for item in candles],
                    }
                )
                return self._standardize_history(df)
        except Exception:
            pass
        try:
            df = self._fetch_chart_daily(self._provider_symbol(symbol))
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
        if allow_yfinance_fallback and not prefer_yfinance:
            try:
                df = yf.download(self._provider_symbol(symbol), period="260d", interval="1d", progress=False)
                if df is not None and not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    return self._standardize_history(df)
            except Exception:
                pass
        return pd.DataFrame()

    def _standardize_history(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        rename = {}
        for col in out.columns:
            low = str(col).lower()
            if low == "adj close":
                continue
            if low == "open":
                rename[col] = "Open"
            elif low == "high":
                rename[col] = "High"
            elif low == "low":
                rename[col] = "Low"
            elif low == "close":
                rename[col] = "Close"
            elif low == "volume":
                rename[col] = "Volume"
        out = out.rename(columns=rename)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        if "Volume" not in out.columns:
            out["Volume"] = 0.0
        out = out.dropna(subset=["High", "Low", "Close"])
        return out

    def _fetch_live_snapshot(self, symbol: str) -> dict:
        if not _REQUESTS_AVAILABLE:
            raise ImportError(
                "live snapshot requires the 'requests' package. "
                "Install it with: pip install quantcairn[research]"
            )
        symbol = self._provider_symbol(symbol)
        last_error = None
        trust_env_options = (False, True) if self.allow_proxy_market else (False,)
        for trust_env in trust_env_options:
            session = None
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                params = {"range": "5d", "interval": "1d", "includePrePost": "false"}
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                    )
                }
                session = requests.Session()
                session.trust_env = trust_env
                resp = session.get(url, params=params, headers=headers, timeout=self.market_timeout)
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"unexpected chart payload type={type(payload).__name__}")
                chart = payload.get("chart")
                if not isinstance(chart, dict):
                    raise ValueError("chart payload missing")
                result_list = chart.get("result")
                if not isinstance(result_list, list) or not result_list or result_list[0] is None:
                    raise ValueError("chart result missing")
                result = result_list[0]
                if not isinstance(result, dict):
                    raise ValueError(f"unexpected chart result type={type(result).__name__}")
                meta = result.get("meta") or {}
                quote = ((result.get("indicators") or {}).get("quote") or [None])[0] or {}

                closes = pd.to_numeric(pd.Series(quote.get("close") or []), errors="coerce").dropna()
                highs = pd.to_numeric(pd.Series(quote.get("high") or []), errors="coerce").dropna()
                lows = pd.to_numeric(pd.Series(quote.get("low") or []), errors="coerce").dropna()
                volumes = pd.to_numeric(pd.Series(quote.get("volume") or []), errors="coerce").dropna()

                last_close = float(closes.iloc[-1]) if not closes.empty else 0.0
                last_price = float(meta.get("regularMarketPrice") or last_close or 0.0)
                recent_high = float(highs.max()) if not highs.empty else last_price
                recent_low = float(lows.min()) if not lows.empty else last_price
                recent_volume = int(volumes.iloc[-1]) if not volumes.empty else 0

                if last_price > 0:
                    return {
                        "price": last_price,
                        "recent_high": recent_high,
                        "recent_low": recent_low,
                        "volume": recent_volume,
                    }
            except Exception as exc:
                last_error = exc
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        try:
            return self._fetch_longbridge_snapshot(symbol)
        except Exception as exc:
            if last_error is None:
                last_error = exc
        raise last_error

    def _market_cap_for_symbol(self, symbol: str) -> float | None:
        normalized = str(symbol or "").strip().upper().split(".")[0]
        if normalized in self._market_cap_cache:
            return self._market_cap_cache[normalized]
        if normalized in self.FALLBACK_MARKET_CAP:
            self._market_cap_cache[normalized] = float(self.FALLBACK_MARKET_CAP[normalized])
            return self._market_cap_cache[normalized]
        if infer_asset_type(normalized) != "common_stock":
            self._market_cap_cache[normalized] = None
            return None
        try:
            fetcher = PriceFetcher(normalized, poll_interval=0)
            try:
                parsed = fetcher.get_market_cap()
                self._market_cap_cache[normalized] = parsed if parsed and parsed > 0 else None
            finally:
                fetcher.close()
        except Exception:
            self._market_cap_cache[normalized] = None
        return self._market_cap_cache[normalized]

    def _fallback_profile_for_symbol(self, symbol: str) -> dict | None:
        profile = self.FALLBACK_PROFILES.get(symbol)
        if not profile:
            return None

        dynamic = dict(profile)
        if os.environ.get("OPENALPHA_LIVE_DATA", "1") == "0":
            return dynamic
        try:
            snapshot = self._fetch_live_snapshot(symbol)
            price = float(snapshot.get("price") or 0.0)
            if price > 0:
                band = float(self.FALLBACK_RANGE_PCT.get(symbol, 0.03))
                low = max(0.01, price * (1.0 - band))
                high = price * (1.0 + band)
                recent_low = float(snapshot.get("recent_low") or low)
                recent_high = float(snapshot.get("recent_high") or high)
                dynamic["range_low"] = round(min(low, recent_low), 2)
                dynamic["range_high"] = round(max(high, recent_high), 2)
                dynamic["volume"] = max(int(snapshot.get("volume") or 0), int(profile["volume"]))
        except Exception:
            pass
        return dynamic

    def score_universe(self, symbols: List[str], news_map: Dict[str, List[str]]):
        scored = []
        if len(symbols) <= 1:
            for symbol in symbols:
                item = self._score_symbol(symbol, news_map.get(symbol, []))
                if item:
                    scored.append(item)
        else:
            with ThreadPoolExecutor(max_workers=min(self.score_workers, len(symbols))) as executor:
                futures = {
                    executor.submit(self._score_symbol, symbol, news_map.get(symbol, [])): symbol
                    for symbol in symbols
                }
                for future in as_completed(futures):
                    try:
                        item = future.result()
                    except Exception:
                        item = None
                    if item:
                        scored.append(item)

        if not scored:
            return self._fallback_scores(symbols, news_map)

        return scored

    def _score_symbol(self, symbol: str, news_items: Sequence[str]) -> Optional[dict]:
        try:
            df = self._load_history(symbol)
            if df.empty or len(df) < self.MIN_HISTORY_ROWS:
                fallback = self._fallback_profile_for_symbol(symbol)
                if fallback:
                    return self._fallback_scored_item(symbol, fallback, news_items)
                return None
            return self.score_frame(symbol=symbol, df=df, news_items=list(news_items))
        except Exception:
            fallback = self._fallback_profile_for_symbol(symbol)
            if fallback:
                return self._fallback_scored_item(symbol, fallback, news_items)
            return None

    def score_frame(
        self,
        symbol: str,
        df: pd.DataFrame,
        news_items: Optional[List[str]] = None,
        sector: Optional[str] = None,
    ) -> Optional[dict]:
        df = self._standardize_history(df)
        if df.empty or len(df) < self.MIN_HISTORY_ROWS:
            return None

        news_items = news_items or []
        sector = sector or self._sector_for_symbol(symbol)

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        volume = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series([0.0] * len(df), index=df.index)
        open_ = df["Open"].astype(float) if "Open" in df.columns else close.shift(1).fillna(close.iloc[0])

        last_close = float(close.iloc[-1])
        last_open = float(open_.iloc[-1])
        last_volume = float(volume.iloc[-1]) if len(volume) else 0.0
        avg_volume_20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
        avg_volume_60 = float(volume.rolling(60).mean().iloc[-1]) if len(volume) >= 60 else float(volume.mean())

        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1] if len(df) >= 200 else np.nan
        sma20_prev = close.rolling(20).mean().iloc[-6] if len(df) >= 26 else np.nan
        sma50_prev = close.rolling(50).mean().iloc[-6] if len(df) >= 56 else np.nan
        sma200_prev = close.rolling(200).mean().iloc[-6] if len(df) >= 206 else np.nan

        rsi_val = float(rsi(close, window=14).iloc[-1])
        macd_hist = float(MACD(close).macd_diff().iloc[-1])
        atr = float(AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])
        atr_pct = (atr / last_close * 100.0) if last_close else 0.0
        average_dollar_volume_20d = avg_volume_20 * last_close
        market_cap = self._market_cap_for_symbol(symbol)
        universe_eval = evaluate_universe_candidate(
            {
                "ticker": symbol,
                "current_price": last_close,
                "asset_type": infer_asset_type(symbol),
                "market_cap": market_cap,
                "average_dollar_volume_20d": average_dollar_volume_20d,
                "atr_20_percentage": atr_pct,
            }
        )
        if universe_eval.rejected:
            return None

        returns = close.pct_change().dropna()
        return_vol_pct = float(returns.rolling(20).std().iloc[-1] * 100.0) if len(returns) >= 20 else 0.0
        rolling_high_20 = float(high.tail(20).max())
        rolling_low_20 = float(low.tail(20).min())
        range_width_pct = ((rolling_high_20 - rolling_low_20) / last_close * 100.0) if last_close else 0.0

        gaps = self._gap_stats(df)
        gap_rate = gaps["gap_rate"]
        max_gap_pct = gaps["max_gap_pct"]
        volume_spike = (last_volume / avg_volume_20) if avg_volume_20 > 0 else 0.0
        news_score = self._news_score(news_items)
        max_drawdown_pct = self._max_drawdown(close)

        reject_reasons = []
        if range_width_pct > self.MAX_RANGE_WIDTH_PCT:
            reject_reasons.append("range too wide")
        if range_width_pct < self.MIN_RANGE_WIDTH_PCT:
            reject_reasons.append("range too tight")
        if atr_pct < self.MIN_ATR_PCT:
            reject_reasons.append("volatility too low")
        if atr_pct > self.MAX_ATR_PCT:
            reject_reasons.append("volatility too high")
        if gap_rate > 0.20 or max_gap_pct > self.GAP_LIMIT_PCT:
            reject_reasons.append("frequent gap risk")
        if news_score >= self.EVENT_NEWS_SCORE:
            reject_reasons.append("event/news driven")
        if self._strong_trend(close, sma20, sma50, sma200, rsi_val, return_vol_pct):
            reject_reasons.append("strong trend")
        if self._too_flat(close, atr_pct, return_vol_pct):
            reject_reasons.append("insufficient movement")

        if reject_reasons:
            return None

        volatility_score = self._volatility_score(atr_pct, return_vol_pct, range_width_pct)
        volume_score = self._volume_score(last_volume, avg_volume_20, avg_volume_60, volume_spike)
        trend_fit_score = self._trend_fit_score(close, sma20, sma50, sma200, sma20_prev, sma50_prev, sma200_prev, rsi_val, macd_hist)
        repeatability_score = self._repeatability_score(close, high, low, sma20, sma50)
        drawdown_safety_score = self._drawdown_safety_score(close, max_drawdown_pct)
        base_score = (
            0.30 * volatility_score
            + 0.20 * volume_score
            + 0.20 * trend_fit_score
            + 0.15 * repeatability_score
            + 0.10 * drawdown_safety_score
        )

        support, resistance, support_meta, resistance_meta = self._estimate_range(df, atr)
        if ((resistance - support) / support * 100.0) < self.min_spread_pct:
            return None
        price_mid = (support + resistance) / 2.0 if resistance > support else last_close

        return {
            "ticker": symbol,
            "sector": sector,
            "score": float(round(base_score, 2)),
            "base_score": float(round(base_score, 2)),
            "volatility_score": float(round(volatility_score, 2)),
            "volume_score": float(round(volume_score, 2)),
            "trend_fit_score": float(round(trend_fit_score, 2)),
            "repeatability_score": float(round(repeatability_score, 2)),
            "drawdown_safety_score": float(round(drawdown_safety_score, 2)),
            "correlation_penalty": 0.0,
            "news_score": float(round(news_score, 2)),
            "range_low": float(round(support, 2)),
            "range_high": float(round(resistance, 2)),
            "suggested_range": f"${support:.2f} - ${resistance:.2f}",
            "support_source": support_meta,
            "resistance_source": resistance_meta,
            "risk": {
                "stop_loss_pct": self._stop_loss_pct(atr_pct, max_drawdown_pct),
            },
            "size": self._position_size_hint(last_close, avg_volume_20),
            "asset_type": universe_eval.asset_type,
            "market_cap": universe_eval.market_cap,
            "average_dollar_volume_20d": universe_eval.average_dollar_volume_20d,
            "atr_20_percentage": universe_eval.atr_20_percentage,
            "data_source": "live",
            "metrics": {
                "last_close": float(round(last_close, 4)),
                "atr_pct": float(round(atr_pct, 4)),
                "return_vol_pct": float(round(return_vol_pct, 4)),
                "range_width_pct": float(round(range_width_pct, 4)),
                "gap_rate": float(round(gap_rate, 4)),
                "max_gap_pct": float(round(max_gap_pct, 4)),
                "volume_spike": float(round(volume_spike, 4)),
                "max_drawdown_pct": float(round(max_drawdown_pct, 4)),
                "price_midpoint": float(round(price_mid, 4)),
            },
            "series": {
                "returns": self._series_tail_returns(close),
            },
        }

    def _fallback_scores(self, symbols: List[str], news_map: Dict[str, List[str]]):
        scored = []
        for symbol in symbols:
            profile = self._fallback_profile_for_symbol(symbol)
            if not profile:
                continue
            item = self._fallback_scored_item(symbol, profile, news_map.get(symbol, []))
            if item:
                scored.append(item)
        return scored

    def _fallback_scored_item(self, symbol: str, profile: dict, news_items: Sequence[str]):
        support = float(profile["range_low"])
        resistance = float(profile["range_high"])
        price_mid = (support + resistance) / 2.0
        band_pct = ((resistance - support) / price_mid * 100.0) if price_mid else 0.0
        market_cap = self.FALLBACK_MARKET_CAP.get(str(symbol or "").strip().upper().split(".")[0])
        universe_eval = evaluate_universe_candidate(
            {
                "ticker": symbol,
                "current_price": price_mid,
                "asset_type": infer_asset_type(symbol),
                "market_cap": market_cap,
                "average_dollar_volume_20d": float(profile["volume"]) * price_mid,
                "atr_20_percentage": band_pct / 2.0,
                "data_source": "fallback",
            },
            skip_atr_validation=True,
        )
        if universe_eval.rejected:
            return None
        if ((resistance - support) / support * 100.0) < self.min_spread_pct:
            return None
        news_score = self._news_score(list(news_items))
        volume_score = min(100.0, 35.0 + math.log10(max(float(profile["volume"]), 1.0) / 1_000_000.0 + 1.0) * 20.0)
        volatility_score = max(0.0, min(100.0, 55.0 + band_pct * 1.5))
        trend_fit_score = 58.0
        repeatability_score = 62.0
        drawdown_safety_score = 55.0
        base_score = (
            0.30 * volatility_score
            + 0.20 * volume_score
            + 0.20 * trend_fit_score
            + 0.15 * repeatability_score
            + 0.10 * drawdown_safety_score
        )
        return {
            "ticker": symbol,
            "sector": self._sector_for_symbol(symbol),
            "score": float(round(base_score, 2)),
            "base_score": float(round(base_score, 2)),
            "volatility_score": float(round(volatility_score, 2)),
            "volume_score": float(round(volume_score, 2)),
            "trend_fit_score": float(round(trend_fit_score, 2)),
            "repeatability_score": float(round(repeatability_score, 2)),
            "drawdown_safety_score": float(round(drawdown_safety_score, 2)),
            "correlation_penalty": 0.0,
            "news_score": float(round(news_score, 2)),
            "range_low": support,
            "range_high": resistance,
            "suggested_range": f"${support:.2f} - ${resistance:.2f}",
            "support_source": "fallback",
            "resistance_source": "fallback",
            "risk": {"stop_loss_pct": 1.5},
            "size": int(max(1, min(1000, profile["volume"] // 1000))),
            "asset_type": universe_eval.asset_type,
            "market_cap": universe_eval.market_cap,
            "average_dollar_volume_20d": universe_eval.average_dollar_volume_20d,
            "atr_20_percentage": universe_eval.atr_20_percentage,
            "data_source": "fallback",
            "avg_daily_volume_hint": int(profile["volume"]),
            "price_midpoint_hint": float(round(price_mid, 4)),
            "fallback_history_incomplete": True,
            "metrics": {
                "last_close": float(round(price_mid, 4)),
                "atr_pct": float(round(band_pct / 2.0, 4)),
                "return_vol_pct": float(round(band_pct / 3.0, 4)),
                "range_width_pct": float(round(band_pct, 4)),
                "gap_rate": 0.0,
                "max_gap_pct": 0.0,
                "volume_spike": 1.0,
                "max_drawdown_pct": 12.0,
                "price_midpoint": float(round(price_mid, 4)),
            },
            "series": {"returns": []},
        }

    def _sector_for_symbol(self, symbol: str) -> str:
        return self.FALLBACK_SECTOR.get(symbol, "Unknown")

    def _series_tail_returns(self, close: pd.Series, tail: int = 60) -> List[float]:
        series = close.pct_change().dropna().tail(tail)
        return [float(round(x, 6)) for x in series.tolist() if pd.notna(x)]

    def _gap_stats(self, df: pd.DataFrame) -> Dict[str, float]:
        if "Open" in df.columns:
            open_ = df["Open"].astype(float)
        else:
            open_ = df["Close"].shift(1).fillna(df["Close"].iloc[0])
        prev_close = df["Close"].shift(1).astype(float)
        gap_pct = ((open_ - prev_close).abs() / prev_close.replace(0, np.nan) * 100.0).dropna()
        if gap_pct.empty:
            return {"gap_rate": 0.0, "max_gap_pct": 0.0}
        gap_rate = float((gap_pct > self.GAP_LIMIT_PCT).mean())
        max_gap_pct = float(gap_pct.max())
        return {"gap_rate": gap_rate, "max_gap_pct": max_gap_pct}

    def _max_drawdown(self, close: pd.Series) -> float:
        rolling_max = close.cummax()
        drawdown = (close / rolling_max - 1.0) * 100.0
        min_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
        return abs(min_drawdown)

    def _strong_trend(
        self,
        close: pd.Series,
        sma20,
        sma50,
        sma200,
        rsi_val: float,
        return_vol_pct: float,
    ) -> bool:
        if any(pd.isna(x) for x in [sma20, sma50]):
            return False

        price = float(close.iloc[-1])
        trend_gap_1 = abs(float(sma20) - float(sma50)) / price * 100.0 if price else 0.0
        trend_gap_2 = abs(float(sma50) - float(sma200)) / price * 100.0 if price and not pd.isna(sma200) else 0.0
        up_stack = not pd.isna(sma200) and float(sma20) > float(sma50) > float(sma200)
        down_stack = not pd.isna(sma200) and float(sma20) < float(sma50) < float(sma200)
        stacked = up_stack or down_stack
        momentum_extreme = rsi_val >= 68.0 or rsi_val <= 32.0
        volatility_strong = return_vol_pct >= 2.8
        return bool(stacked and (trend_gap_1 >= 1.5 or trend_gap_2 >= 1.5) and momentum_extreme and volatility_strong)

    def _too_flat(self, close: pd.Series, atr_pct: float, return_vol_pct: float) -> bool:
        if atr_pct >= self.MIN_ATR_PCT or return_vol_pct >= 0.8:
            return False
        recent_range_pct = ((float(close.tail(20).max()) - float(close.tail(20).min())) / float(close.iloc[-1]) * 100.0) if len(close) >= 20 and float(close.iloc[-1]) else 0.0
        return recent_range_pct < 3.0

    def _volatility_score(self, atr_pct: float, return_vol_pct: float, range_width_pct: float) -> float:
        if atr_pct <= 0 or return_vol_pct <= 0:
            return 0.0
        combined = (atr_pct + return_vol_pct) / 2.0
        ideal = 3.5
        score = 100.0 - abs(combined - ideal) * 14.0
        if range_width_pct > 0:
            score += min(10.0, range_width_pct / 6.0)
        return float(max(0.0, min(100.0, score)))

    def _volume_score(self, last_volume: float, avg_volume_20: float, avg_volume_60: float, volume_spike: float) -> float:
        if avg_volume_20 <= 0:
            return 0.0
        base = math.log10(avg_volume_20 / 1_000_000.0 + 1.0) * 35.0
        activity = min(30.0, volume_spike * 10.0)
        persistence = 0.0
        if avg_volume_60 > 0:
            persistence = min(25.0, math.log10(avg_volume_60 / 1_000_000.0 + 1.0) * 10.0)
        score = 20.0 + base + activity + persistence
        return float(max(0.0, min(100.0, score)))

    def _trend_fit_score(
        self,
        close: pd.Series,
        sma20,
        sma50,
        sma200,
        sma20_prev,
        sma50_prev,
        sma200_prev,
        rsi_val: float,
        macd_hist: float,
    ) -> float:
        price = float(close.iloc[-1])
        if price <= 0:
            return 0.0

        def pct_gap(a, b) -> float:
            if pd.isna(a) or pd.isna(b) or price <= 0:
                return 0.0
            return abs(float(a) - float(b)) / price * 100.0

        alignment_penalty = pct_gap(sma20, sma50) * 8.0
        if not pd.isna(sma200):
            alignment_penalty += pct_gap(sma50, sma200) * 5.0

        slope_penalty = 0.0
        if not pd.isna(sma20_prev):
            slope_penalty += abs((float(sma20) - float(sma20_prev)) / price * 100.0) * 30.0
        if not pd.isna(sma50_prev):
            slope_penalty += abs((float(sma50) - float(sma50_prev)) / price * 100.0) * 18.0
        if not pd.isna(sma200_prev):
            slope_penalty += abs((float(sma200) - float(sma200_prev)) / price * 100.0) * 10.0

        rsi_score = max(0.0, 100.0 - abs(rsi_val - 50.0) * 2.6)
        macd_score = max(0.0, 100.0 - min(100.0, abs(macd_hist) / max(price, 1e-6) * 10000.0))
        score = 100.0 - alignment_penalty - slope_penalty
        score = score * 0.55 + rsi_score * 0.25 + macd_score * 0.20
        return float(max(0.0, min(100.0, score)))

    def _repeatability_score(self, close: pd.Series, high: pd.Series, low: pd.Series, sma20, sma50) -> float:
        window = min(60, len(close))
        if window < 20:
            return 0.0
        recent_close = close.tail(window)
        recent_high = high.tail(window)
        recent_low = low.tail(window)

        support_band = float(recent_low.min())
        resistance_band = float(recent_high.max())
        price = float(recent_close.iloc[-1])
        if price <= 0 or resistance_band <= support_band:
            return 0.0

        band_width = resistance_band - support_band
        support_touches = int((recent_close <= support_band + band_width * 0.12).sum())
        resistance_touches = int((recent_close >= resistance_band - band_width * 0.12).sum())
        middle_zone = support_band + band_width * 0.45
        middle_touches = int(((recent_close >= middle_zone - band_width * 0.08) & (recent_close <= middle_zone + band_width * 0.08)).sum())

        sign_series = np.sign((recent_close - recent_close.rolling(5).mean()).dropna())
        oscillations = int((sign_series.diff().fillna(0) != 0).sum()) if len(sign_series) else 0
        balance = min(support_touches, resistance_touches) / max(1, max(support_touches, resistance_touches))

        score = (
            min(40.0, (support_touches + resistance_touches) * 3.0)
            + min(25.0, middle_touches * 2.0)
            + min(20.0, oscillations * 1.8)
            + balance * 15.0
        )
        return float(max(0.0, min(100.0, score)))

    def _drawdown_safety_score(self, close: pd.Series, max_drawdown_pct: float) -> float:
        if close.empty:
            return 0.0
        recent = close.tail(60)
        low = float(recent.min())
        high = float(recent.max())
        last = float(recent.iloc[-1])
        if high <= low or last <= 0:
            return 0.0

        recovery_position = (last - low) / (high - low)
        drawdown_penalty = max(0.0, max_drawdown_pct - 8.0) * 2.0
        recovery_bonus = max(0.0, recovery_position * 30.0)
        stability_bonus = 25.0 if max_drawdown_pct <= 20.0 else max(0.0, 25.0 - (max_drawdown_pct - 20.0) * 2.5)
        score = 45.0 + recovery_bonus + stability_bonus - drawdown_penalty
        return float(max(0.0, min(100.0, score)))

    def _estimate_range(self, df: pd.DataFrame, atr: float) -> Tuple[float, float, str, str]:
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        recent = close.tail(60)
        recent_high = high.tail(60)
        recent_low = low.tail(60)
        last_close = float(close.iloc[-1])

        hist_support = float(recent_low.quantile(0.12))
        hist_resistance = float(recent_high.quantile(0.88))

        # Guard: NaN propagation would produce misleading support/resistance
        import math
        if math.isnan(hist_support) or math.isnan(hist_resistance):
            hist_support = max(0.01, last_close * 0.95)
            hist_resistance = last_close * 1.05

        if "Volume" in df.columns and df["Volume"].notna().any():
            volume = df["Volume"].astype(float).tail(60)
            valid = pd.DataFrame({"close": recent, "volume": volume}).dropna()
            if len(valid) >= 10:
                q1 = valid["close"].quantile(0.20)
                q2 = valid["close"].quantile(0.80)
                lower_vol = valid.loc[valid["close"] <= q1, "close"].mean()
                upper_vol = valid.loc[valid["close"] >= q2, "close"].mean()
            else:
                lower_vol = hist_support
                upper_vol = hist_resistance
        else:
            lower_vol = hist_support
            upper_vol = hist_resistance

        atr_adjust = max(atr * 1.2, last_close * 0.012)
        lower_atr = last_close - atr_adjust
        upper_atr = last_close + atr_adjust

        support = (hist_support * 0.45) + (lower_vol * 0.35) + (lower_atr * 0.20)
        resistance = (hist_resistance * 0.45) + (upper_vol * 0.35) + (upper_atr * 0.20)

        support = max(0.01, min(support, last_close * 0.98))
        resistance = max(last_close * 1.02, resistance)
        if resistance <= support:
            resistance = support * 1.08

        return round(float(support), 2), round(float(resistance), 2), "hist+volume+atr", "hist+volume+atr"

    def _news_score(self, news_items: List[str]):
        pos = ["beat", "beats", "raise", "upgrade", "positive", "growth", "beat expectations"]
        neg = ["miss", "falls", "downgrade", "negative", "lawsuit", "recall", "missed", "investigation"]
        s = 50.0
        text = " ".join(news_items).lower()
        for p in pos:
            s += text.count(p) * 2
        for n in neg:
            s -= text.count(n) * 3
        return max(0.0, min(100.0, s))

    def _stop_loss_pct(self, atr_pct: float, max_drawdown_pct: float) -> float:
        base = max(1.2, min(3.5, atr_pct * 1.3))
        if max_drawdown_pct > 20:
            base = max(base, 2.5)
        return float(round(base, 2))

    def _position_size_hint(self, last_close: float, avg_volume_20: float) -> int:
        if last_close <= 0:
            return 1
        liquidity_scale = max(1.0, min(12.0, avg_volume_20 / 2_000_000.0))
        size = int(max(1, min(1000, (liquidity_scale * 1000.0) / max(last_close, 1.0) * 0.25)))
        return max(1, size)
