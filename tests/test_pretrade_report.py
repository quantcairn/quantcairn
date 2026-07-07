"""Tests for src.reports.pretrade_report."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.reports.pretrade_report import PretradeReport


def test_generate_empty_context():
    report = PretradeReport.generate({})
    assert report.today is not None
    assert report.mode == ""
    assert report.account_equity == 0.0
    assert isinstance(report.top_configs, list)
    assert isinstance(report.errors, list)
    assert isinstance(report.warnings, list)
    assert report.allowed_to_open_new_positions is False
    assert report.allowed_reduce_only is True


def test_generate_with_live_guard_verdict():
    ctx = {
        "mode": "live",
        "live_guard_verdict": {
            "allowed_to_open_new_positions": False,
            "allowed_reduce_only": True,
            "errors": ["TOP config expired"],
            "warnings": ["Weekend trading"],
        },
    }
    report = PretradeReport.generate(ctx)
    assert report.allowed_to_open_new_positions is False
    assert report.allowed_reduce_only is True
    assert "TOP config expired" in report.errors
    assert "Weekend trading" in report.warnings


def test_generate_with_broker():
    fake_broker = MagicMock()
    fake_acct = MagicMock()
    fake_acct.equity = 15000.0
    fake_acct.cash = 8000.0
    fake_broker.get_account.return_value = fake_acct
    fake_broker.get_positions.return_value = []
    ctx = {"mode": "live", "broker": fake_broker}
    report = PretradeReport.generate(ctx)
    assert report.account_equity == 15000.0
    assert report.account_cash == 8000.0


def test_write_creates_file():
    tmpdir = Path(tempfile.mkdtemp(prefix="pretrade_test_"))
    report = PretradeReport.generate({"mode": "live"})
    path = report.write(reports_dir=tmpdir)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mode"] == "live"
    assert "today" in data
    assert "errors" in data
    assert "warnings" in data


def test_all_fields_present():
    report = PretradeReport.generate({"mode": "paper"})
    d = report.__dict__
    for field in ("today", "mode", "account_equity", "top_configs",
                  "allowed_to_open_new_positions", "allowed_reduce_only",
                  "errors", "warnings", "reduce_only_symbols"):
        assert field in d, f"Missing field: {field}"


def run_test_direct():
    test_generate_empty_context()
    test_generate_with_live_guard_verdict()
    test_generate_with_broker()
    test_write_creates_file()
    test_all_fields_present()
