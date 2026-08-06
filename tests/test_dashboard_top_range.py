"""Tests for TOP engine range data resolution in the dashboard.

Verifies:
  1. Engine online with range → range_ready=True, engine values used
  2. Engine online without range → YAML config fallback
  3. Engine offline → YAML config fallback if YAML has valid range
  4. Engine offline + YAML null → range_ready=False
  5. TICKERS ports match documented engine ports
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


def _make_engine_status():
    """Return a fresh combined module reference for each test."""
    import src.dashboard.combined as combined
    return combined


# ---------------------------------------------------------------------------
# 1. Engine online with full range data
# ---------------------------------------------------------------------------

def test_engine_online_range_ready_uses_engine_values(monkeypatch, tmp_path: Path):
    """When engine is online and returns range data, use engine values."""
    _write_project_fixture(tmp_path, range_support=42.0, range_resistance=48.0)
    monkeypatch.setattr("src.dashboard.combined.PROJECT_DIR", tmp_path)
    monkeypatch.setattr("src.dashboard.combined.urllib.request.build_opener", lambda *a, **kw: _FakeEngineOpener(
        {"support": 43.0, "resistance": 45.17, "range_ready": True, "range_source": "bar_extrema",
         "last_signal": "HOLD", "price": 44.265, "mode": "paper"},
    ))

    c = _make_engine_status()
    result = c._top_engine_status(
        {"port": 8080, "config": "TOP1.yaml"}, rank=1, ticker="ALLY", mode="paper",
    )
    assert result["online"] is True
    assert result["range_ready"] is True
    assert result["support"] == 43.0
    assert result["resistance"] == 45.17
    assert result["range_source"] == "bar_extrema"


# ---------------------------------------------------------------------------
# 2. Engine online but range data missing → YAML fallback
# ---------------------------------------------------------------------------

def test_engine_online_no_range_falls_back_to_yaml(monkeypatch, tmp_path: Path):
    """When engine is online but returns no support/resistance, use YAML."""
    _write_project_fixture(tmp_path, range_support=42.0, range_resistance=48.0)
    monkeypatch.setattr("src.dashboard.combined.PROJECT_DIR", tmp_path)
    monkeypatch.setattr("src.dashboard.combined.urllib.request.build_opener", lambda *a, **kw: _FakeEngineOpener(
        {"last_signal": "HOLD", "price": 44.3, "mode": "paper",
         "range_ready": False, "support": None, "resistance": None},
    ))

    c = _make_engine_status()
    result = c._top_engine_status(
        {"port": 8080, "config": "TOP1.yaml"}, rank=1, ticker="ALLY", mode="paper",
    )
    assert result["online"] is True
    assert result["range_ready"] is False  # engine says not ready
    assert result["support"] == 42.0  # YAML fallback
    assert result["resistance"] == 48.0  # YAML fallback


# ---------------------------------------------------------------------------
# 3. Engine offline → YAML fallback
# ---------------------------------------------------------------------------

def test_engine_offline_yaml_has_valid_range(monkeypatch, tmp_path: Path):
    """When engine is offline but YAML has valid range, use YAML values."""
    _write_project_fixture(tmp_path, range_support=42.0, range_resistance=48.0)
    monkeypatch.setattr("src.dashboard.combined.PROJECT_DIR", tmp_path)
    _clear_status_caches()

    c = _make_engine_status()
    monkeypatch.setattr(c, "_fetch_status", lambda port: None)
    result = c._top_engine_status(
        {"port": 8080, "config": "TOP1.yaml"}, rank=1, ticker="ALLY", mode="paper",
    )
    assert result["online"] is False
    assert result["range_ready"] is True  # YAML fallback has valid range
    assert result["support"] == 42.0
    assert result["resistance"] == 48.0
    assert result["range_source"] == "yaml_config"


# ---------------------------------------------------------------------------
# 4. Engine offline + YAML null range → range_ready=False
# ---------------------------------------------------------------------------

def test_engine_offline_yaml_null_range(monkeypatch, tmp_path: Path):
    """When engine is offline AND YAML has null range, range_ready=False."""
    _write_project_fixture(tmp_path, range_support=None, range_resistance=None)
    monkeypatch.setattr("src.dashboard.combined.PROJECT_DIR", tmp_path)
    _clear_status_caches()

    c = _make_engine_status()
    monkeypatch.setattr(c, "_fetch_status", lambda port: None)
    result = c._top_engine_status(
        {"port": 8080, "config": "TOP1.yaml"}, rank=1, ticker="ALLY", mode="paper",
    )
    assert result["online"] is False
    assert result["range_ready"] is False
    assert result["support"] is None
    assert result["resistance"] is None


# ---------------------------------------------------------------------------
# 5. TICKERS ports match actual engine ports
# ---------------------------------------------------------------------------

def test_tickers_ports_match_engine_ports():
    """Dashboard TICKERS config must have ports that match run_top_engine.sh."""
    c = _make_engine_status()
    expected = [(1, 8080), (2, 8081), (3, 8082)]
    for (rank, expected_port), item in zip(expected, c.TICKERS):
        assert item["port"] == expected_port, (
            f"TOP{rank} port mismatch: TICKERS has {item['port']}, "
            f"expected {expected_port} (from run_top_engine.sh)"
        )


# ---------------------------------------------------------------------------
# 6. Existing _fetch_status integration
# ---------------------------------------------------------------------------

def test_fetch_status_returns_engine_data(monkeypatch):
    """_fetch_status returns real dict from engine API when available."""
    monkeypatch.setattr("src.dashboard.combined.urllib.request.build_opener", lambda *a, **kw: _FakeEngineOpener(
        {"support": 43.0, "resistance": 45.17, "range_ready": True, "last_signal": "BUY", "price": 44.0},
    ))
    c = _make_engine_status()
    c._STATUS_CACHE.pop(8080, None)
    c._STATUS_FAILURES.pop(8080, None)
    data = c._fetch_status(8080)
    assert isinstance(data, dict)
    assert data["support"] == 43.0
    assert data["resistance"] == 45.17
    assert data["range_ready"] is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_status_caches():
    """Reset fetch_status caches to avoid stale cached results leaking between tests."""
    import src.dashboard.combined as combined
    combined._STATUS_CACHE.clear()
    combined._STATUS_FAILURES.clear()

def _write_project_fixture(tmp_path: Path, *, range_support=None, range_resistance=None):
    """Write minimal project fixtures so _top_engine_status works."""
    configs = tmp_path / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    # Also need state dir
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)

    range_cfg = {"mode": "auto"}
    if range_support is not None:
        range_cfg["support_price"] = float(range_support)
    if range_resistance is not None:
        range_cfg["resistance_price"] = float(range_resistance)

    top_config = {
        "ticker": "ALLY",
        "mode": "paper",
        "range": range_cfg,
        "position": {"initial_capital": 700.0, "reduce_only": False},
    }
    (configs / "TOP1.yaml").write_text(yaml.dump(top_config), encoding="utf-8")


class _FakeEngineOpener:
    """Mimics urllib opener for a running engine."""
    def __init__(self, data: dict):
        self._data = data

    def open(self, url, timeout=1):
        return _FakeResponse(json.dumps(self._data).encode())


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
