from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from src.ai_selector import config_writer


def test_top_yaml_contains_selection_metadata(tmp_path, monkeypatch):
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
                "reason": "protected live position",
                "selection_date": "2026-07-06",
                "protected_position": True,
                "reduce_only": True,
                "size": 30,
                "risk": {"stop_loss_pct": 1.5},
            }
        ]
    )

    payload = yaml.safe_load((tmp_path / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))

    assert payload["selection"]["source"] == "ai_selector"
    assert payload["selection"]["selection_date"] == "2026-07-06"
    assert payload["selection"]["score"] == 81.2
    assert payload["selection"]["confidence"] == 0.77
    assert payload["selection"]["protected_position"] is True
    assert payload["selection"]["reduce_only"] is True
    assert payload["selection"]["reason"] == "protected live position"
