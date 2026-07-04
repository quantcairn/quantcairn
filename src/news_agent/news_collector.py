import requests
from bs4 import BeautifulSoup
from typing import List, Dict
import time
import logging
import os


class NewsCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = os.environ.get("AI_SELECTOR_ALLOW_PROXY_NEWS", "0") == "1"
        self.logger = logging.getLogger('news_collector')
        self.timeout = float(os.environ.get("AI_SELECTOR_NEWS_TIMEOUT_SECONDS", "3") or 3)

    def fetch_news_snippets(self, ticker: str) -> List[str]:
        snippets = []
        # simple Yahoo finance news scrape
        try:
            url = f"https://finance.yahoo.com/quote/{ticker}"
            r = self.session.get(url, timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(r.text, "lxml")
            for item in soup.select("h3 a")[:10]:
                snippets.append(item.get_text(strip=True))
        except Exception:
            self.logger.exception('Yahoo news fetch failed for %s', ticker)
        # try reddit Pushshift search as fallback (no API key)
        try:
            reddit_search = f"https://api.pushshift.io/reddit/search/submission/?q={ticker}&size=5"
            r2 = self.session.get(reddit_search, timeout=self.timeout)
            if r2.status_code == 200:
                data = r2.json().get('data', [])
                for d in data:
                    title = d.get('title')
                    if title:
                        snippets.append(title)
        except Exception:
            self.logger.debug('Pushshift reddit fetch failed for %s', ticker)
        return snippets

    def collect_for_symbols(self, symbols: List[str]) -> Dict[str, List[str]]:
        out = {}
        if os.environ.get("AI_SELECTOR_FETCH_NEWS", "0") != "1":
            return {s: [] for s in symbols}
        delay = float(os.environ.get("AI_SELECTOR_NEWS_SLEEP_SECONDS", "0.1") or 0.1)
        for s in symbols:
            out[s] = self.fetch_news_snippets(s)
            if delay > 0:
                time.sleep(delay)
        return out
