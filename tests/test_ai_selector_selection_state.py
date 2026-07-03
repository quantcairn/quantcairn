from __future__ import annotations

import yaml

from src.ai_selector import selection_state


def _write_top_config(configs_dir, index: int, ticker: str) -> None:
    payload = {
        "ticker": ticker,
        "mode": "live",
        "range": {"mode": "auto"},
        "position": {"reduce_only": True},
    }
    (configs_dir / f"TOP{index}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_selection_state_verifies_same_day_top_configs(tmp_path, monkeypatch):
    configs_dir = tmp_path / "configs"
    state_dir = tmp_path / "state"
    configs_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))

    for index, ticker in enumerate(["SOFI", "NVDA", "SMR", "QBTS", "PLTR"], start=1):
        _write_top_config(configs_dir, index, ticker)

    selection_state.write_selection_state(
        et_date="2026-07-02",
        generated_at="2026-07-02T21:00:00-04:00",
        selected_symbols=["SOFI", "NVDA", "SMR", "QBTS", "PLTR"],
        report_path=str(tmp_path / "reports" / "ai_selection_latest.json"),
    )

    ok, reason, state = selection_state.verify_selection_state(required_et_date="2026-07-02")

    assert ok is True
    assert reason == "ok"
    assert state["selected_symbols"] == ["SOFI", "NVDA", "SMR", "QBTS", "PLTR"]


def test_selection_state_detects_top_config_mismatch(tmp_path, monkeypatch):
    configs_dir = tmp_path / "configs"
    state_dir = tmp_path / "state"
    configs_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))

    for index, ticker in enumerate(["SOFI", "NVDA", "SMR", "QBTS", "PLTR"], start=1):
        _write_top_config(configs_dir, index, ticker)

    selection_state.write_selection_state(
        et_date="2026-07-02",
        generated_at="2026-07-02T21:00:00-04:00",
        selected_symbols=["SOFI", "NVDA", "SMR", "QBTS", "PLTR"],
    )

    _write_top_config(configs_dir, 5, "TSLA")
    ok, reason, _ = selection_state.verify_selection_state(required_et_date="2026-07-02")

    assert ok is False
    assert reason == "top_config_symbols_mismatch"
