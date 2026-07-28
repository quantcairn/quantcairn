import os
import pandas as pd
import time
from io import StringIO
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
# Module-level optional imports: try/except'd so the module loads even
# in core-only mode, and captures real module objects at import time
# to be immune to sys.modules monkeypatching from other tests.
try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False


class Universe:
    """Builds a tradable universe from S&P500 (fallback) and filters by basic criteria.

    Notes:
    - This implementation fetches S&P500 tickers from Wikipedia and then filters
      using `yfinance`. It uses a thread pool to parallelize requests.
    - For full-market coverage, replace the source with an official exchange list.
    """

    def __init__(self):
        self.min_price = 4.0
        self.min_avg_volume = 1_000_000
        self.min_market_cap = 1_000_000_000

    def _fetch_sp500_tickers(self) -> List[str]:
        if not _REQUESTS_AVAILABLE:
            raise ImportError(
                "S&P 500 fetch requires the 'requests' package. "
                "Install it with: pip install quantcairn[research]"
            )
        if not _BS4_AVAILABLE:
            raise ImportError(
                "S&P 500 fetch requires the 'beautifulsoup4' package. "
                "Install it with: pip install quantcairn[research]"
            )
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        try:
            r = requests.get(url, timeout=10)
            soup = BeautifulSoup(r.text, "lxml")
            table = soup.find("table", {"id": "constituents"}) or soup.find("table", {"class": "wikitable"})
            tickers = []
            if table:
                for row in table.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if cols:
                        t = cols[0].get_text(strip=True)
                        t = t.replace('.', '-')
                        tickers.append(t)
            # if fetch failed or parsed no tickers, fallback to local snapshot
            if not tickers:
                local = self._load_local_snapshot()
                if local:
                    return local
            return tickers
        except Exception:
            # fallback to local snapshot
            local = self._load_local_snapshot()
            if local:
                return local
            return ["AAPL", "MSFT", "AMZN", "TSLA", "NVDA", "AMD", "NFLX"]

    def _load_local_snapshot(self) -> List[str]:
        # look for data/sp500_sample.txt in project
        try:
            tried = []
            candidates = []
            # relative to this file: up two/three levels
            here = os.path.dirname(__file__)
            candidates.append(os.path.abspath(os.path.join(here, '..', '..', 'data', 'sp500_sample.txt')))
            candidates.append(os.path.abspath(os.path.join(here, '..', '..', '..', 'data', 'sp500_sample.txt')))
            # repository root as cwd
            candidates.append(os.path.abspath(os.path.join(os.getcwd(), 'data', 'sp500_sample.txt')))

            for path in candidates:
                tried.append(path)
                if os.path.exists(path):
                    with open(path, 'r') as f:
                        return [line.strip() for line in f if line.strip()]
            # debug: no file found
            # print tried paths to help debugging
            # fallback empty
            return []
        except Exception:
            pass
        return []

    def _fetch_chart_daily(self, symbol: str, days: int = 7):
        if not _REQUESTS_AVAILABLE:
            raise ImportError(
                "chart data requires the 'requests' package. "
                "Install it with: pip install quantcairn[research]"
            )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "range": f"{max(days, 1)}d",
            "interval": "1d",
            "includePrePost": "false",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            )
        }
        session = requests.Session()
        session.trust_env = False
        try:
            resp = session.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except Exception:
                return None
            if not isinstance(payload, dict):
                return None
            chart = payload.get("chart")
            if not isinstance(chart, dict):
                return None
            result_list = chart.get("result")
            if not isinstance(result_list, list) or not result_list or result_list[0] is None:
                return None
            result = result_list[0]
            if not isinstance(result, dict):
                return None
            quote = (result.get("indicators", {}).get("quote") or [None])[0]
            ts = result.get("timestamp") or []
            if not quote or not ts:
                return None
            df = pd.DataFrame({
                "Close": quote.get("close") or [],
                "Volume": quote.get("volume") or [],
            })
            df = df.dropna(subset=["Close"])
            return df if not df.empty else None
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _check_symbol(self, symbol: str) -> bool:
        if not _REQUESTS_AVAILABLE or not _YF_AVAILABLE:
            raise ImportError(
                "symbol check requires 'requests' and 'yfinance' packages. "
                "Install them with: pip install quantcairn[research]"
            )
        try:
            # 1) Try yfinance download
            df = None
            try:
                df = yf.download(symbol, period='7d', interval='1d', progress=False)
            except Exception:
                df = None

            # 2) If no data, try direct Yahoo CSV download with browser UA
            if df is None or df.empty:
                end = int(time.time())
                start = end - 7 * 24 * 3600
                url = f"https://query1.finance.yahoo.com/v7/finance/download/{symbol}?period1={start}&period2={end}&interval=1d&events=history&includeAdjustedClose=true"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36'
                }
                try:
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200 and resp.text.strip():
                        df = pd.read_csv(StringIO(resp.text), parse_dates=['Date'])
                        df.set_index('Date', inplace=True)
                except Exception:
                    df = None

            if df is None or df.empty:
                try:
                    df = self._fetch_chart_daily(symbol, days=10)
                except Exception:
                    df = None

            if df is None or df.empty:
                return False

            price = float(df['Close'].iloc[-1])
            vol = int(df['Volume'].iloc[-1]) if 'Volume' in df.columns else 0

            # marketCap via yfinance Ticker.info may require additional calls; approximate by price*shares if available
            mkt = 0
            try:
                t = yf.Ticker(symbol)
                info = {}
                try:
                    info = t.info
                except Exception:
                    info = {}
                mkt = info.get('marketCap') or 0
                if not mkt:
                    shares = info.get('sharesOutstanding') or 0
                    try:
                        mkt = float(shares) * float(price) if shares and price else 0
                    except Exception:
                        mkt = 0
            except Exception:
                mkt = 0

            # If market cap is missing, allow symbol to pass based on price+volume (we'll mark it as unknown market cap)
            if mkt == 0:
                return (price > self.min_price) and (vol >= self.min_avg_volume)
            return (price > self.min_price) and (vol >= self.min_avg_volume) and (mkt >= self.min_market_cap)
        except Exception:
            return False

    def build_universe(self, source: str = 'sp500') -> List[str]:
        tickers = []
        if source == 'sp500':
            tickers = self._fetch_sp500_tickers()

        result = []
        # parallel check
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(self._check_symbol, t): t for t in tickers}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    ok = fut.result()
                    if ok:
                        result.append(sym)
                except Exception:
                    continue

        if result:
            return sorted(result)

        # If market data filtering fails across the board, fall back to the
        # local liquid-stock sample and let the scorer reject symbols without
        # usable history. This keeps the opening selector from producing an
        # empty TOP list because of one data-provider outage.
        local = self._load_local_snapshot()
        return sorted(local) if local else []
