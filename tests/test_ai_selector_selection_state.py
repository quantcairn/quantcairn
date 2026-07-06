from __future__ import annotations

from src.ai_selector import selection_state


def test_selection_state_verifies_same_day_top_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "TOP1.yaml").write_text("ticker: SOFI\nmode: live\n", encoding="utf-8")

    selection_state.write_selection_state(
        et_date="2026-07-02",
        generated_at="2026-07-02T08:30:00-04:00",
        selected_symbols=["SOFI"],
        report_path="/tmp/ai_selection_latest.json",
    )

    ok, reason, state = selection_state.verify_selection_state(required_et_date="2026-07-02")

    assert ok is True
    assert reason == "ok"
    assert state is not None


def test_selection_state_detects_top_config_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "TOP1.yaml").write_text("ticker: NVDA\nmode: live\n", encoding="utf-8")

    selection_state.write_selection_state(
        et_date="2026-07-02",
        generated_at="2026-07-02T08:30:00-04:00",
        selected_symbols=["SOFI"],
        report_path="/tmp/ai_selection_latest.json",
    )

    ok, reason, _ = selection_state.verify_selection_state(required_et_date="2026-07-02")

    assert ok is False
    assert reason == "top_config_symbols_mismatch"


def test_verify_live_startup_selection_skips_when_no_live_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "TOP1.yaml").write_text("ticker: SOFI\nmode: paper\n", encoding="utf-8")

    ok, reason, _ = selection_state.verify_live_startup_selection(required_et_date="2026-07-06")

    assert ok is True
    assert reason == "no_live_top_configs"


def test_verify_live_startup_selection_blocks_stale_live_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(selection_state, "PROJECT_DIR", tmp_path)
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "TOP1.yaml").write_text("ticker: SOFI\nmode: live\n", encoding="utf-8")

    selection_state.write_selection_state(
        et_date="2026-07-05",
        generated_at="2026-07-05T08:30:00-04:00",
        selected_symbols=["SOFI"],
        report_path="/tmp/ai_selection_latest.json",
    )

    ok, reason, _ = selection_state.verify_live_startup_selection(required_et_date="2026-07-06")

    assert ok is False
    assert reason.startswith("selection_state_date_mismatch")
