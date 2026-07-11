from pathlib import Path

from scripts.test_weekend_paper_lifecycle import run_weekend_paper_lifecycle


def test_weekend_paper_lifecycle_is_fully_offline(tmp_path):
    report = run_weekend_paper_lifecycle(audit_dir=tmp_path / "audit")

    assert report["bootstrap_confirmed"] is True
    assert report["start_position_zero"] is True
    assert report["buy"]["status"] == "FILLED"
    assert report["buy"]["filled_quantity"] == 1
    assert report["buy"]["avg_fill_price"] == 10.0
    assert report["checks"]["buy_fill_confirmed"] is True
    assert report["checks"]["position_increased_after_buy"] is True
    assert report["sell"]["status"] == "FILLED"
    assert report["sell"]["filled_quantity"] == 1
    assert report["sell"]["avg_fill_price"] == 10.1
    assert report["checks"]["sell_fill_confirmed"] is True
    assert report["checks"]["position_returned_to_zero"] is True
    assert report["checks"]["audit_log_confirmed"] is True
    assert report["checks"]["overall"] is True

    audit_path = Path(report["audit"]["path"])
    assert audit_path.exists()
    assert report["audit"]["execution_count"] == 2
    assert report["audit"]["buy_count"] == 1
    assert report["audit"]["sell_count"] == 1
    assert report["audit"]["tickers"] == ["TEST"]


def run_test_direct():
    test_weekend_paper_lifecycle_is_fully_offline(Path("/tmp/weekend-paper-lifecycle-test"))


if __name__ == "__main__":
    run_test_direct()
