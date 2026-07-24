from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from src.openalpha.config_writer import write_top_configs, _existing_mode_is_live, _load_existing_mode


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

    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

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
            "ai_score": 91.5,
            "range_score": 88.0,
            "final_score": 90.1,
            "confidence": 0.77,
            "trade_filter_passed": True,
            "reject_reason": "",
            "fallback_used": False,
            "reason": "protected live position",
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
    assert updated["selection"]["score"] == 90.1
    assert updated["selection"]["ai_score"] == 91.5
    assert updated["selection"]["range_score"] == 88.0
    assert updated["selection"]["final_score"] == 90.1
    assert updated["selection"]["confidence"] == 0.77
    assert updated["selection"]["trade_filter_passed"] is True
    assert updated["selection"]["reject_reason"] == ""
    assert updated["selection"]["fallback_used"] is False
    assert updated["selection"]["reason"] == "protected live position"
    updated5 = yaml.safe_load((configs_dir / "TOP5.yaml").read_text(encoding="utf-8"))
    assert updated5["ticker"] == "AMZN"


def test_config_writer_uses_auto_refresh_env_override(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))
    monkeypatch.setenv("OPENALPHA_AUTO_REFRESH_MINUTES", "12")

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
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

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
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

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
    assert low_price["range"]["min_profit_per_trade"] == 0.54
    assert high_price["range"]["min_profit_per_trade"] == 0.9


def test_config_writer_honors_global_reduce_only_flag(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    state_dir = repo_root / "state"
    configs_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "trading_flags.json").write_text('{"reduce_only_all": true}', encoding="utf-8")
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))
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


def test_config_writer_includes_portfolio_from_local_over_config(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    (repo_root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "portfolio": {
                    "enabled": False,
                    "max_positions": 1,
                    "max_total_exposure": 0.25,
                    "max_total_risk": 0.01,
                    "leveraged_etf_max_single_position": 0.05,
                    "leveraged_etf_max_group_exposure": 0.10,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (repo_root / "config.local.yaml").write_text(
        yaml.safe_dump(
            {
                "portfolio": {
                    "enabled": True,
                    "max_positions": 3,
                    "max_total_exposure": 1.0,
                    "max_total_risk": 0.05,
                    "leveraged_etf_max_single_position": 0.15,
                    "leveraged_etf_max_group_exposure": 0.50,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

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
    assert updated["portfolio"]["enabled"] is True
    assert updated["portfolio"]["max_positions"] == 3
    assert updated["portfolio"]["max_total_exposure"] == 1.0
    assert updated["portfolio"]["max_total_risk"] == 0.05
    assert updated["portfolio"]["leveraged_etf_max_single_position"] == 0.15
    assert updated["portfolio"]["leveraged_etf_max_group_exposure"] == 0.50


def test_config_writer_includes_ai_selector_fallback_policy(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    (repo_root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "ai_selector": {
                    "allow_fallback_paper_entries": False,
                    "allow_fallback_live_entries": False,
                    "fallback_paper_position_multiplier": 0.10,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (repo_root / "config.local.yaml").write_text(
        yaml.safe_dump(
            {
                "ai_selector": {
                    "allow_fallback_paper_entries": True,
                    "allow_fallback_live_entries": False,
                    "fallback_paper_position_multiplier": 0.25,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

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
    assert updated["ai_selector"]["allow_fallback_paper_entries"] is True
    assert updated["ai_selector"]["allow_fallback_live_entries"] is False
    assert updated["ai_selector"]["fallback_paper_position_multiplier"] == 0.25
    assert updated["ai_selector"]["entry_proximity_enabled"] is True
    assert updated["ai_selector"]["entry_proximity_weight"] == 0.0
    assert "selection" in updated
    assert "allocation" in updated


def test_config_writer_includes_entry_diagnostics(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    write_top_configs([
        {
            "ticker": "SOFI",
            "range_low": 17.0,
            "range_high": 19.0,
            "risk": {"stop_loss_pct": 1.5},
            "size": 10,
            "entry": {
                "entry_proximity_score": 80.0,
                "good_for_entry_now": True,
                "entry_quality": "good",
                "entry_reason": "near_support",
                "range_position": 35.0,
                "dist_to_support": 4.2,
                "dist_to_resistance": 18.8,
            },
            "entry_proximity_score": 80.0,
            "good_for_entry_now": True,
            "entry_quality": "good",
            "entry_reason": "near_support",
            "range_position": 35.0,
            "dist_to_support": 4.2,
            "dist_to_resistance": 18.8,
        }
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    entry = updated["selection"]["entry"]
    assert entry["entry_proximity_score"] == 80.0
    assert entry["good_for_entry_now"] is True
    assert entry["entry_quality"] == "good"
    assert entry["entry_reason"] == "near_support"
    assert entry["range_position"] == 35.0
    assert entry["dist_to_support"] == 4.2
    assert entry["dist_to_resistance"] == 18.8


def test_config_writer_includes_composition_metadata(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    write_top_configs([
        {
            "ticker": "SOXS",
            "range_low": 3.5,
            "range_high": 4.2,
            "risk": {"stop_loss_pct": 1.5},
            "size": 50,
            "ai_score": 90.0,
            "range_score": 80.0,
            "final_score": 88.0,
            "confidence": 0.75,
            "trade_filter_passed": True,
            "reject_reason": "",
            "fallback_used": False,
            "leveraged_etf": True,
            "composition_filter_passed": True,
            "composition_reject_reason": "",
            "final_rank": 1,
        }
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    selection = updated["selection"]
    assert selection["leveraged_etf"] is True
    assert selection["composition_filter_passed"] is True
    assert selection["composition_reject_reason"] == ""
    assert selection["final_rank"] == 1


def test_config_writer_defaults_portfolio_when_missing(tmp_path, monkeypatch):
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

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
    assert updated["portfolio"]["enabled"] is False
    assert updated["portfolio"]["max_positions"] == 3
    assert updated["portfolio"]["max_total_exposure"] == 1.0
    assert updated["portfolio"]["max_total_risk"] == 0.05
    assert updated["portfolio"]["leveraged_etf_max_single_position"] == 0.15
    assert updated["portfolio"]["leveraged_etf_max_group_exposure"] == 0.50


def test_live_top_preserved_when_no_candidates(tmp_path, monkeypatch):
    """Live TOP config must NOT be overwritten by disabled slot when 0 candidates."""
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    # Pre-create a live TOP1 with allow_live_order=false, reduce_only=true
    existing = {
        "ticker": "SOXS",
        "mode": "live",
        "selection": {"source": "manual_override", "selection_date": "2026-07-21"},
        "broker": {
            "longbridge": {
                "enabled": True,
                "environment": "prod",
                "account_type": "live",
                "allow_live_order": False,
                "reduce_only": True,
            }
        },
    }
    (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    # Zero candidates → disabled-slot path
    write_top_configs([])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    assert updated["mode"] == "live"
    assert updated["ticker"] == "SOXS"
    assert updated["broker"]["longbridge"]["allow_live_order"] is False
    assert updated["broker"]["longbridge"]["reduce_only"] is True
    assert updated["broker"]["longbridge"]["environment"] == "prod"
    assert updated["broker"]["longbridge"]["account_type"] == "live"


def test_paper_top_normal_disabled_write_when_no_candidates(tmp_path, monkeypatch):
    """Paper TOP config SHOULD be overwritten by disabled slot when 0 candidates."""
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    existing = {
        "ticker": "SOFI",
        "mode": "paper",
        "broker": {"longbridge": {"enabled": False}},
    }
    (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    write_top_configs([])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    # Paper mode gets overwritten to disabled
    assert updated.get("mode") is None or updated["mode"] != "live"


def test_sandbox_top_normal_disabled_write_when_no_candidates(tmp_path, monkeypatch):
    """Sandbox TOP config SHOULD be overwritten by disabled slot when 0 candidates."""
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    existing = {
        "ticker": "LABD",
        "mode": "sandbox",
        "broker": {"longbridge": {"enabled": True, "environment": "sandbox", "account_type": "paper"}},
    }
    (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    write_top_configs([])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    # Sandbox mode gets overwritten to disabled — not preserved
    assert updated.get("mode") is None or updated["mode"] != "sandbox"


def test_existing_mode_is_live_returns_true_for_live():
    """_existing_mode_is_live(1) should return True when TOP1.yaml has mode=live."""
    import tempfile, os
    from unittest.mock import patch

    td = tempfile.mkdtemp()
    try:
        configs_dir = Path(td) / "configs"
        configs_dir.mkdir(parents=True)
        existing = {"ticker": "SOXS", "mode": "live"}
        (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

        with patch("src.openalpha.config_writer.BASE", td):
            assert _existing_mode_is_live(1) is True
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_existing_mode_is_live_returns_false_for_paper():
    """_existing_mode_is_live(1) should return False when TOP1.yaml has mode=paper."""
    import tempfile
    from unittest.mock import patch

    td = tempfile.mkdtemp()
    try:
        configs_dir = Path(td) / "configs"
        configs_dir.mkdir(parents=True)
        existing = {"ticker": "SOFI", "mode": "paper"}
        (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

        with patch("src.openalpha.config_writer.BASE", td):
            assert _existing_mode_is_live(1) is False
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_live_top_allows_candidate_write(tmp_path, monkeypatch):
    """Live TOP with candidates present: normal write still works."""
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir()
    monkeypatch.setattr("src.openalpha.config_writer.BASE", str(repo_root))

    existing = {
        "ticker": "OLD_SYMBOL",
        "mode": "live",
        "broker": {"longbridge": {"enabled": True, "environment": "prod", "account_type": "live"}},
    }
    (configs_dir / "TOP1.yaml").write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    # Has candidates → normal write path
    write_top_configs([{
        "ticker": "SOXS",
        "range_low": 20.0,
        "range_high": 30.0,
        "risk": {"stop_loss_pct": 1.5},
        "size": 10,
    }])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    # Normal write: preserves mode but updates ticker
    assert updated["mode"] == "live"
    assert updated["ticker"] == "SOXS"


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
