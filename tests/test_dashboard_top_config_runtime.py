from __future__ import annotations

from pathlib import Path

import yaml

import src.dashboard.combined as dashboard
from src.config.runtime_paths import resolve_top_config_dir


def _write_top_config(root: Path, slot: int, ticker: str = "AIG") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"TOP{slot}.yaml").write_text(
        yaml.safe_dump(
            {
                "ticker": ticker,
                "mode": "paper",
                "enabled": True,
                "range": {"support_price": 40.0, "resistance_price": 50.0},
            }
        ),
        encoding="utf-8",
    )


def test_dashboard_reads_external_top_config_root(monkeypatch, tmp_path: Path) -> None:
    release_root = tmp_path / "immutable-release"
    external_root = tmp_path / "runtime" / "top_configs"
    release_root.mkdir()
    _write_top_config(external_root, 1)

    monkeypatch.setattr(dashboard, "PROJECT_DIR", release_root)
    monkeypatch.setenv("SOXS_TOP_CONFIG_DIR", str(external_root))

    slots = dashboard._current_top_config_slots_cached(limit=3, request_cache={})

    assert dashboard._top_config_dir() == external_root.resolve()
    assert resolve_top_config_dir(release_root) == external_root.resolve()
    assert slots[0]["path"] == (external_root / "TOP1.yaml").resolve()
    assert slots[0]["ticker"] == "AIG"
    assert slots[0]["enabled"] is True
    assert not (release_root / "configs").exists()


def test_dashboard_readiness_uses_external_config_when_release_has_no_configs(
    monkeypatch, tmp_path: Path
) -> None:
    release_root = tmp_path / "immutable-release"
    external_root = tmp_path / "runtime" / "top_configs"
    release_root.mkdir()
    _write_top_config(external_root, 1, ticker="AGNC")

    monkeypatch.setattr(dashboard, "PROJECT_DIR", release_root)
    monkeypatch.setenv("SOXS_TOP_CONFIG_DIR", str(external_root))
    monkeypatch.setattr(
        dashboard,
        "_fetch_status",
        lambda port: {
            "mode": "paper",
            "range_ready": True,
            "support": 41.0,
            "resistance": 49.0,
        },
    )

    result = dashboard._top_engine_status(
        {"port": 8080, "config": "TOP1.yaml"},
        rank=1,
        ticker="AGNC",
        mode="paper",
    )

    assert result["online"] is True
    assert result["runtime_status"] == "online"
    assert result["range_ready"] is True
    assert result["support"] == 41.0
    assert result["resistance"] == 49.0
    assert not (release_root / "configs").exists()


def test_dashboard_top_config_fallback_order(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "code"
    config_root = tmp_path / "runtime" / "config"
    project_root.mkdir()
    _write_top_config(config_root / "top_configs", 1, ticker="ACGL")

    monkeypatch.setattr(dashboard, "PROJECT_DIR", project_root)
    monkeypatch.delenv("SOXS_TOP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("SOXS_CONFIG_DIR", str(config_root))
    assert dashboard._top_config_dir() == (config_root / "top_configs").resolve()

    monkeypatch.delenv("SOXS_CONFIG_DIR", raising=False)
    project_config = project_root / "configs"
    _write_top_config(project_config, 1, ticker="AIG")
    assert dashboard._top_config_dir() == project_config.resolve()
