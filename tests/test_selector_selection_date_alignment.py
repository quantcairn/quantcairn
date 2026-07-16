from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from scripts import run_ai_selector
from src.ai_selector import config_writer
from src.utils.market_calendar import required_selection_date


def test_required_selection_date_uses_previous_completed_session_for_premarket_weekend_and_holiday():
    premarket_monday = datetime(2026, 7, 13, 8, 55, tzinfo=ZoneInfo("America/New_York"))
    regular_monday = datetime(2026, 7, 13, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    after_hours_monday = datetime(2026, 7, 13, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    weekend_saturday = datetime(2026, 7, 11, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    holiday_friday = datetime(2026, 7, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    assert required_selection_date(premarket_monday) == "2026-07-10"
    assert required_selection_date(regular_monday) == "2026-07-10"
    assert required_selection_date(after_hours_monday) == "2026-07-13"
    assert required_selection_date(weekend_saturday) == "2026-07-10"
    assert required_selection_date(holiday_friday) == "2026-07-02"
    assert required_selection_date(regular_monday, selection_completed=True) == "2026-07-13"


def test_run_ai_selector_and_config_writer_share_required_selection_date(monkeypatch, tmp_path):
    monkeypatch.setattr(run_ai_selector, "required_selection_date", lambda now_et=None, selection_completed=False: "2026-07-15")
    monkeypatch.setattr(config_writer, "required_selection_date", lambda now_et=None, selection_completed=False: "2026-07-15")

    assert run_ai_selector._selection_date() == "2026-07-15"
    assert config_writer._selection_date() == "2026-07-15"

    monkeypatch.setattr(config_writer, "BASE", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    config_writer.write_top_configs(
        [
            {
                "ticker": "SOFI",
                "range_low": 17.0,
                "range_high": 19.0,
                "score": 81.2,
                "confidence": 0.77,
                "reason": "date check",
                "protected_position": False,
                "reduce_only": False,
                "size": 10,
            }
        ],
        selection_date="2026-07-15",
        generated_at="2026-07-15T08:30:00-04:00",
        selection_run_id="run-1",
        result_quality="DEGRADED",
        research_admission="RESEARCH_ONLY",
    )

    payload = yaml.safe_load((Path(tmp_path) / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))
    assert payload["selection"]["selection_date"] == "2026-07-15"
    assert payload["selection_date"] == "2026-07-15"
