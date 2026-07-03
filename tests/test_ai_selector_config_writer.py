from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from src.ai_selector.config_writer import write_top_configs


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = {}

    def setattr(self, target, value):
        module_name, attr_name = target.rsplit(".", 1)
        module = __import__(module_name, fromlist=[attr_name])
        key = (module, attr_name)
        if key not in self._originals:
            self._originals[key] = getattr(module, attr_name)
        setattr(module, attr_name, value)

    def restore(self):
        for (module, attr_name), original in self._originals.items():
            setattr(module, attr_name, original)


def test_config_writer_preserves_live_mode_and_enabled_broker(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()

    monkeypatch.setattr("src.ai_selector.config_writer.BASE", str(repo_root))

    existing = {
        "ticker": "OLD",
        "mode": "live",
        "broker": {"longbridge": {"enabled": True}},
        "range": {"support_price": 10, "resistance_price": 12},
        "position": {"initial_capital": 1000},
    }
    (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    write_top_configs([
        {
            "ticker": "NVDA",
            "range_low": 118.0,
            "range_high": 154.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 5,
        },
        {
            "ticker": "TSLA",
            "range_low": 220.0,
            "range_high": 260.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 4,
        },
        {
            "ticker": "AAPL",
            "range_low": 170.0,
            "range_high": 190.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 3,
        },
        {
            "ticker": "MSFT",
            "range_low": 390.0,
            "range_high": 420.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 2,
        },
        {
            "ticker": "AMZN",
            "range_low": 120.0,
            "range_high": 140.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 1,
        },
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    assert updated["mode"] == "live"
    assert updated["broker"]["longbridge"]["enabled"] is True
    assert updated["ticker"] == "NVDA"
    assert updated["range"]["mode"] == "auto"
    assert updated["range"]["support_price"] is None
    assert updated["range"]["resistance_price"] is None
    assert updated["position"]["initial_capital"] == 700.0
    updated5 = yaml.safe_load((configs_dir / "TOP5.yaml").read_text(encoding="utf-8"))
    assert updated5["ticker"] == "AMZN"


def test_config_writer_uses_auto_refresh_env_override(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.ai_selector.config_writer.BASE", str(repo_root))
    monkeypatch.setenv("AI_SELECTOR_AUTO_REFRESH_MINUTES", "12")

    write_top_configs([
        {
            "ticker": "NVDA",
            "range_low": 118.0,
            "range_high": 154.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 5,
        }
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    assert updated["range"]["auto_refresh_minutes"] == 12


def test_config_writer_persists_reduce_only_for_preserved_positions(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.ai_selector.config_writer.BASE", str(repo_root))

    write_top_configs([
        {
            "ticker": "SOXS",
            "range_low": 3.5,
            "range_high": 4.2,
            "risk": {"stop_loss_pct": 1.5},
            "size": 50,
            "reduce_only": True,
        }
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    assert updated["position"]["reduce_only"] is True


def test_config_writer_scales_min_profit_for_lower_priced_stocks(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.ai_selector.config_writer.BASE", str(repo_root))

    write_top_configs([
        {
            "ticker": "SOFI",
            "range_low": 17.0,
            "range_high": 19.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 10,
        },
        {
            "ticker": "NVDA",
            "range_low": 190.0,
            "range_high": 200.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 2,
        },
    ])

    low_price = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    high_price = yaml.safe_load((configs_dir / "TOP2.yaml").read_text(encoding="utf-8"))
    assert low_price["range"]["min_profit_per_trade"] == 0.72
    assert high_price["range"]["min_profit_per_trade"] == 1.0


def test_config_writer_honors_global_reduce_only_flag(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    state_dir = repo_root / "state"
    configs_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "trading_flags.json").write_text('{"reduce_only_all": true}', encoding="utf-8")
    monkeypatch.setattr("src.ai_selector.config_writer.BASE", str(repo_root))
    monkeypatch.setenv("SOXS_STATE_DIR", str(state_dir))

    write_top_configs([
        {
            "ticker": "SOFI",
            "range_low": 17.0,
            "range_high": 19.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 10,
        }
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    assert updated["position"]["reduce_only"] is True


def run_test_direct():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory(prefix="config-writer-test-") as tmpdir:
            test_config_writer_preserves_live_mode_and_enabled_broker(
                Path(tmpdir),
                monkeypatch,
            )
    finally:
        monkeypatch.restore()


if __name__ == "__main__":
    run_test_direct()
