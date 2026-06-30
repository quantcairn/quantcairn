import requests
from bs4 import BeautifulSoup
from typing import List, Dict
import time
import logging


class NewsCollector:
    def __init__(self):
        self.session = requests.Session()
        self.logger = logging.getLogger('news_collector')

    def fetch_news_snippets(self, ticker: str) -> List[str]:
        snippets = []
        # simple Yahoo finance news scrape
        try:
            url = f"https://finance.yahoo.com/quote/{ticker}"
            r = self.session.get(url, timeout=5)
            soup = BeautifulSoup(r.text, "lxml")
            for item in soup.select("h3 a")[:10]:
                snippets.append(item.get_text(strip=True))
        except Exception:
            self.logger.exception('Yahoo news fetch failed for %s', ticker)
        # try reddit Pushshift search as fallback (no API key)
        try:
            reddit_search = f"https://api.pushshift.io/reddit/search/submission/?q={ticker}&size=5"
            r2 = self.session.get(reddit_search, timeout=5)
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
        for s in symbols:
            out[s] = self.fetch_news_snippets(s)
            time.sleep(0.5)
        return out
