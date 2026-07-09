from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

VENV_SITE_PACKAGES = next(
    (path for path in (Path(__file__).resolve().parents[1] / ".venv" / "lib").glob("python*/site-packages") if path.exists()),
    None,
)
if VENV_SITE_PACKAGES is not None and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from src.dashboard import combined as dashboard


def _write_order_state(path: Path, *, ticker: str, reason: str, count: int = 1, runtime_scope: str | None = None) -> None:
    payload = {
        "ticker": ticker,
        "updated_at": "2026-07-09T11:10:23.786345",
        "failed_orders_today": [
            {
                "ticker": ticker,
                "timestamp": f"2026-07-09T11:10:{idx:02d}.000000",
                "reason": reason,
                "quantity": 0,
                "price": 0.0,
                "buying_power": 0.0,
            }
            for idx in range(count)
        ],
    }
    if runtime_scope:
        payload["runtime_scope"] = runtime_scope
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_non_current_ticker_failed_orders_are_hidden_from_main_view(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        order_state_dir = project_dir / "state" / "order_state"
        order_state_dir.mkdir(parents=True, exist_ok=True)

        _write_order_state(order_state_dir / "SOFI.json", ticker="SOFI", reason="buying power", count=1)
        _write_order_state(order_state_dir / "YINN.json", ticker="YINN", reason="The order amount exceeds the maximum buying power", count=24)
        _write_order_state(order_state_dir / "TEST.json", ticker="TEST", reason="ignored", count=5, runtime_scope="test")

        monkeypatch.setattr(dashboard, "STATE_DIR", project_dir / "state")
        result = dashboard._load_order_states(active_symbols={"SOFI", "AAPL", "DRIP"})

        assert result["failed_orders_today"] == 1
        assert result["failed_orders_total_today"] == 1
        assert result["historical_failed_orders_today"] == 1
        assert result["historical_failed_orders_total_today"] == 24
        assert [item["ticker"] for item in result["ticker_details"]] == ["SOFI"]
        assert [item["ticker"] for item in result["historical_ticker_details"]] == ["YINN"]
        assert result["ticker_details"][0]["failed_count"] == 1
        assert result["historical_ticker_details"][0]["failed_count"] == 24


def test_yinn_shows_when_it_is_a_real_position_or_active_symbol(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        order_state_dir = project_dir / "state" / "order_state"
        order_state_dir.mkdir(parents=True, exist_ok=True)

        _write_order_state(order_state_dir / "YINN.json", ticker="YINN", reason="The order amount exceeds the maximum buying power", count=24)

        monkeypatch.setattr(dashboard, "STATE_DIR", project_dir / "state")
        result = dashboard._load_order_states(active_symbols={"YINN", "SOFI"})

        assert result["failed_orders_today"] == 1
        assert result["historical_failed_orders_today"] == 0
        assert [item["ticker"] for item in result["ticker_details"]] == ["YINN"]
        assert result["ticker_details"][0]["failed_count"] == 24
