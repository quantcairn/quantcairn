from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from src.dashboard import combined
from src.reports import daily_report


class SimpleMonkeyPatch:
    def __init__(self):
        self._attrs = []

    def setattr(self, obj, name, value):
        self._attrs.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._attrs):
            setattr(obj, name, original)


class FakeBroker:
    def __init__(self, positions=None, account=None):
        self._positions = list(positions or [])
        self._account = account or SimpleNamespace(cash=700.0, equity=1500.0, buying_power=350.0)

    def connect(self):
        return True

    def disconnect(self):
        return None

    def get_positions(self):
        return list(self._positions)

    def get_account(self):
        return self._account


def _position(symbol: str, quantity: int, avg_cost: float, current_price: float):
    market_value = quantity * current_price
    unrealized_pnl = (current_price - avg_cost) * quantity
    unrealized_pnl_pct = (unrealized_pnl / (avg_cost * quantity) * 100.0) if quantity and avg_cost else 0.0
    return SimpleNamespace(
        ticker=symbol,
        quantity=quantity,
        avg_entry_price=avg_cost,
        current_price=current_price,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
    )


def _write_log(log_dir: Path, day: str, records: list[dict]) -> None:
    path = log_dir / f"trades-{day}.jsonl"
    payload = "\n".join(json.dumps(record) for record in records)
    path.write_text(payload, encoding="utf-8")


def test_report_generates_even_with_no_trades():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reports_dir = root / "reports"
        log_dir = root / "logs"
        log_dir.mkdir()
        trade_day = date(2026, 7, 2)
        broker = FakeBroker()

        report = daily_report.generate_daily_report(
            trade_day,
            broker=broker,
            reports_dir=reports_dir,
            log_dir=log_dir,
            now_et=datetime(2026, 7, 2, 16, 5),
        )

        assert report["realized_pnl"]["total"] == 0.0
        assert report["trades"]["total_trades_today"] == 0
        assert report["trades"]["win_rate"] is None
        assert "no_trades_today" in report["warnings"]
        assert (reports_dir / "daily_2026-07-02.json").exists()


def test_report_includes_current_holdings():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reports_dir = root / "reports"
        log_dir = root / "logs"
        log_dir.mkdir()
        broker = FakeBroker(
            positions=[
                _position("SOFI", 30, 18.09, 17.93),
                _position("SOXS", 132, 3.80, 3.92),
            ]
        )

        report = daily_report.generate_daily_report(
            date(2026, 7, 2),
            broker=broker,
            reports_dir=reports_dir,
            log_dir=log_dir,
            now_et=datetime(2026, 7, 2, 16, 5),
        )

        assert [row["symbol"] for row in report["current_holdings"]] == ["SOFI", "SOXS"]
        assert report["unrealized_pnl"]["by_symbol"]["SOFI"] == -4.8


def test_report_calculates_total_realized_pnl_from_audit_fills():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reports_dir = root / "reports"
        log_dir = root / "logs"
        log_dir.mkdir()
        _write_log(
            log_dir,
            "20260702",
            [
                {
                    "timestamp": "2026-07-02T14:00:00Z",
                    "action": "get_order",
                    "response": {
                        "mapped_status": "FILLED",
                        "order": 'OrderDetail { order_id: "1", executed_quantity: 10, executed_price: Some(10.0), submitted_at: "2026-07-02T14:00:00Z", side: Buy, symbol: "SOFI.US" }',
                    },
                },
                {
                    "timestamp": "2026-07-02T15:00:00Z",
                    "action": "get_order",
                    "response": {
                        "mapped_status": "FILLED",
                        "order": 'OrderDetail { order_id: "2", executed_quantity: 10, executed_price: Some(11.0), submitted_at: "2026-07-02T15:00:00Z", side: Sell, symbol: "SOFI.US" }',
                    },
                },
            ],
        )
        broker = FakeBroker()

        report = daily_report.generate_daily_report(
            date(2026, 7, 2),
            broker=broker,
            reports_dir=reports_dir,
            log_dir=log_dir,
            now_et=datetime(2026, 7, 2, 16, 5),
        )

        assert report["realized_pnl"]["by_symbol"]["SOFI"] == 10.0
        assert report["realized_pnl"]["total"] == 10.0
        assert report["trades"]["profitable_trades"] == 1


def test_report_handles_missing_previous_daily_report():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reports_dir = root / "reports"
        log_dir = root / "logs"
        log_dir.mkdir()
        broker = FakeBroker()

        report = daily_report.generate_daily_report(
            date(2026, 7, 2),
            broker=broker,
            reports_dir=reports_dir,
            log_dir=log_dir,
            now_et=datetime(2026, 7, 2, 16, 5),
        )

        assert report["account"]["equity_change_vs_yesterday"] is None
        assert "previous_daily_report_missing" in report["warnings"]


def test_report_handles_incomplete_fill_data_without_inventing_values():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reports_dir = root / "reports"
        log_dir = root / "logs"
        log_dir.mkdir()
        _write_log(
            log_dir,
            "20260702",
            [
                {
                    "timestamp": "2026-07-02T15:00:00Z",
                    "action": "get_order",
                    "response": {
                        "mapped_status": "FILLED",
                        "order": 'OrderDetail { order_id: "3", executed_quantity: 10, executed_price: Some(11.0), submitted_at: "2026-07-02T15:00:00Z", side: Sell, symbol: "SOFI.US" }',
                    },
                }
            ],
        )
        broker = FakeBroker()

        report = daily_report.generate_daily_report(
            date(2026, 7, 2),
            broker=broker,
            reports_dir=reports_dir,
            log_dir=log_dir,
            now_et=datetime(2026, 7, 2, 16, 5),
        )

        assert report["realized_pnl"]["by_symbol"]["SOFI"] is None
        assert report["realized_pnl"]["total"] is None
        assert "incomplete_fill_data" in report["warnings"]


def test_daily_report_endpoint_returns_latest_report_json():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            reports_dir.mkdir()
            report_path = reports_dir / "daily_2026-07-01.json"
            report_path.write_text(
                json.dumps({"date": "2026-07-01", "account": {"equity": 1500.0}}, ensure_ascii=False),
                encoding="utf-8",
            )
            monkeypatch.setattr(daily_report, "REPORTS_DIR", reports_dir)
            monkeypatch.setattr(daily_report, "_ny_now", lambda: datetime(2026, 7, 2, 12, 0))
            client = combined.app.test_client()
            response = client.get("/daily-report")
            payload = response.get_json()

            assert response.status_code == 200
            assert payload["date"] == "2026-07-01"
            assert payload["is_latest_trading_day_report"] is False
    finally:
        monkeypatch.restore()


def test_daily_report_endpoint_handles_no_report_available():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            monkeypatch.setattr(daily_report, "REPORTS_DIR", reports_dir)
            client = combined.app.test_client()
            response = client.get("/daily-report")
            payload = response.get_json()

            assert response.status_code == 404
            assert payload["status"] == "no_report_available"
    finally:
        monkeypatch.restore()


def test_scheduler_only_triggers_at_1605_et_on_trading_days():
    with tempfile.TemporaryDirectory() as tmp:
        reports_dir = Path(tmp) / "reports"
        reports_dir.mkdir()
        assert daily_report.should_generate_daily_report(datetime(2026, 7, 2, 16, 5), reports_dir) is True
        assert daily_report.should_generate_daily_report(datetime(2026, 7, 2, 16, 4), reports_dir) is False
        assert daily_report.should_generate_daily_report(datetime(2026, 7, 4, 16, 5), reports_dir) is False
        assert daily_report.should_generate_daily_report(datetime(2026, 7, 5, 16, 5), reports_dir) is False


def run_test_direct():
    test_report_generates_even_with_no_trades()
    test_report_includes_current_holdings()
    test_report_calculates_total_realized_pnl_from_audit_fills()
    test_report_handles_missing_previous_daily_report()
    test_report_handles_incomplete_fill_data_without_inventing_values()
    test_daily_report_endpoint_returns_latest_report_json()
    test_daily_report_endpoint_handles_no_report_available()
    test_scheduler_only_triggers_at_1605_et_on_trading_days()


if __name__ == "__main__":
    run_test_direct()
