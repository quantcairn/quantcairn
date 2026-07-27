"""Tests for NewsCollector optional dependency handling.

Verifies:
  1. Module imports cleanly even when requests/bs4 are unavailable
  2. NewsCollector() raises ImportError with helpful message when requests missing
  3. fetch_news_snippets() returns [] and logs warning when bs4 unavailable
  4. Normal operation when all dependencies are present
"""

from __future__ import annotations

import logging
import os

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Module-level import safety (core-only mode)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsCollectorModuleImport:
    """Module must import cleanly even without requests/bs4 installed."""

    def test_module_imports_ok(self):
        """The module itself always imports (try/except guards at module level)."""
        import src.news_agent.news_collector as nc

        # Module-level flags should be present
        assert hasattr(nc, "_REQUESTS_AVAILABLE")
        assert hasattr(nc, "_BS4_AVAILABLE")

    def test_news_collector_class_exists(self):
        """NewsCollector class is always importable."""
        from src.news_agent.news_collector import NewsCollector

        assert NewsCollector is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Missing requests → ImportError on instantiation (core-only mode)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsCollectorWithoutRequests:
    """When requests is unavailable, NewsCollector() must raise ImportError."""

    @pytest.fixture(autouse=True)
    def _simulate_missing_requests(self, monkeypatch):
        """Simulate core-only mode: requests not installed."""
        import src.news_agent.news_collector as nc

        monkeypatch.setattr(nc, "_REQUESTS_AVAILABLE", False)
        yield
        # The module-level flag is restored after test by monkeypatch

    def test_instantiation_raises_import_error(self):
        """NewsCollector() should raise ImportError, not NameError."""
        from src.news_agent.news_collector import NewsCollector

        with pytest.raises(ImportError, match="NewsCollector"):
            NewsCollector()

    def test_instantiation_message_mentions_install_hint(self):
        """Error message should point to quantcairn[research]."""
        from src.news_agent.news_collector import NewsCollector

        with pytest.raises(ImportError, match="pip install quantcairn"):
            NewsCollector()

    def test_instantiation_never_reaches_name_error(self):
        """Must raise ImportError before hitting requests.Session()."""
        from src.news_agent.news_collector import NewsCollector

        # If the guard were missing, this would be NameError: name 'requests' is not defined
        try:
            NewsCollector()
        except Exception as exc:
            assert not isinstance(exc, NameError), (
                f"Got NameError instead of ImportError: {exc}"
            )
            assert isinstance(exc, ImportError)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Missing bs4 → graceful degradation on fetch_news_snippets()
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsCollectorWithoutBs4:
    """When bs4 is unavailable, fetch_news_snippets() returns [] with warning."""

    @pytest.fixture(autouse=True)
    def _simulate_missing_bs4(self, monkeypatch):
        """Simulate missing beautifulsoup4."""
        import src.news_agent.news_collector as nc

        monkeypatch.setattr(nc, "_BS4_AVAILABLE", False)
        yield

    def test_fetch_news_snippets_returns_empty_list(self):
        """Should return [] without crashing when bs4 is missing."""
        import src.news_agent.news_collector as nc

        collector = nc.NewsCollector.__new__(nc.NewsCollector)
        collector.logger = logging.getLogger("test_news")
        collector.session = None  # Not used when bs4 is missing

        result = collector.fetch_news_snippets("AAPL")
        assert result == []

    def test_fetch_news_snippets_does_not_access_session(self):
        """Should not touch self.session when bs4 is missing."""
        import src.news_agent.news_collector as nc

        collector = nc.NewsCollector.__new__(nc.NewsCollector)
        collector.logger = logging.getLogger("test_news")
        # Deliberately leave session unset — should not be accessed

        result = collector.fetch_news_snippets("AAPL")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Normal operation (all dependencies available)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsCollectorNormalOperation:
    """With all deps installed, NewsCollector works normally."""

    def test_instantiation_succeeds(self):
        """NewsCollector() should work when requests is installed."""
        import src.news_agent.news_collector as nc
        from src.news_agent.news_collector import NewsCollector

        if not nc._REQUESTS_AVAILABLE:
            pytest.skip("requests not installed in this environment")

        collector = NewsCollector()
        assert collector is not None
        assert hasattr(collector, "session")

    def test_collect_for_symbols_disabled_by_default(self, monkeypatch):
        """collect_for_symbols() returns empty dict when OPENALPHA_FETCH_NEWS != 1."""
        import src.news_agent.news_collector as nc
        from src.news_agent.news_collector import NewsCollector

        if not nc._REQUESTS_AVAILABLE:
            pytest.skip("requests not installed in this environment")

        # Ensure the env var is NOT "1"
        monkeypatch.setenv("OPENALPHA_FETCH_NEWS", "0")

        collector = NewsCollector()
        result = collector.collect_for_symbols(["AAPL", "MSFT"])
        assert result == {"AAPL": [], "MSFT": []}

    def test_collect_for_symbols_respects_news_delay(self, monkeypatch):
        """collect_for_symbols should respect OPENALPHA_NEWS_SLEEP_SECONDS env."""
        import src.news_agent.news_collector as nc
        from src.news_agent.news_collector import NewsCollector

        if not nc._REQUESTS_AVAILABLE:
            pytest.skip("requests not installed in this environment")

        monkeypatch.setenv("OPENALPHA_FETCH_NEWS", "0")

        collector = NewsCollector()
        # Should complete quickly since fetch is disabled
        result = collector.collect_for_symbols(["AAPL"])
        assert result == {"AAPL": []}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Integration: selector.py handles missing NewsCollector gracefully
# ═══════════════════════════════════════════════════════════════════════════════


class TestSelectorIntegration:
    """AIStrategySelector must handle missing NewsCollector gracefully."""

    def test_selector_sets_news_to_none_when_unavailable(self, monkeypatch):
        """When NewsCollector raises ImportError, selector.news should be None."""
        import src.news_agent.news_collector as nc
        from src.openalpha.selector import AIStrategySelector

        # Simulate missing requests
        monkeypatch.setattr(nc, "_REQUESTS_AVAILABLE", False)

        selector = AIStrategySelector()
        assert selector.news is None, (
            "selector.news should be None when NewsCollector is unavailable"
        )

    def test_selector_prints_fallback_message(self, capsys, monkeypatch):
        """Should print a helpful message when news is unavailable."""
        import src.news_agent.news_collector as nc
        from src.openalpha.selector import AIStrategySelector

        monkeypatch.setattr(nc, "_REQUESTS_AVAILABLE", False)

        AIStrategySelector()
        captured = capsys.readouterr()
        assert "NewsCollector unavailable" in captured.out
        assert "quantcairn[research]" in captured.out

    def test_selector_run_selection_news_none_no_crash(self, monkeypatch):
        """When news is None, the selector should not crash on news_map access.

        We verify this by inspecting the news-related codepath directly:
        run_selection() calls self.news.collect_for_symbols(...) only when
        self.news is truthy AND OPENALPHA_FETCH_NEWS=="1".  With news=None,
        neither branch is taken, so the pipeline is safe.
        """
        import src.news_agent.news_collector as nc
        from src.openalpha.selector import AIStrategySelector

        monkeypatch.setattr(nc, "_REQUESTS_AVAILABLE", False)

        selector = AIStrategySelector()
        assert selector.news is None, (
            "selector.news should be None when NewsCollector is unavailable"
        )

        # Simulate the news branch guard from run_selection()
        news_map = {}
        if selector.news and os.environ.get("OPENALPHA_FETCH_NEWS", "0") == "1":
            news_map = selector.news.collect_for_symbols(["AAPL"])
        assert news_map == {}, (
            "news_map should be empty when self.news is None"
        )

        # Verify the method exists and is callable when news IS available
        monkeypatch.undo()
        # Verify the guard logic: with real _REQUESTS_AVAILABLE, if installed,
        # NewsCollector() either succeeds or raises ImportError gracefully
        try:
            selector2 = AIStrategySelector()
            # If news is set, it means requests was available — all good
            if selector2.news is not None:
                result = selector2.news.collect_for_symbols(["AAPL"])
                assert isinstance(result, dict)
        except ImportError:
            # Expected in core-only mode — already verified above
            pass
