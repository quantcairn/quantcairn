from __future__ import annotations

from pathlib import Path

import yaml

from src.openalpha import config_writer
from src.config.runtime_paths import resolve_top_config_dir


def _items() -> list[dict[str, object]]:
    return [
        {"ticker": "AIG", "range_low": 50.0, "range_high": 60.0, "final_score": 90.0},
        {"ticker": "ACGL", "range_low": 70.0, "range_high": 80.0, "final_score": 85.0},
    ]


def _write_configs(writer: object, monkeypatch, *, project: Path) -> None:
    monkeypatch.setattr(writer, "BASE", str(project))
    writer.write_top_configs(  # type: ignore[attr-defined]
        _items(),
        selection_run_id="selection-runtime-path-test",
        selection_date="2026-08-28",
        generated_at="2026-08-28T10:00:00+08:00",
        research_admission="PAPER_ELIGIBLE",
        slot_limit=3,
    )


def test_explicit_top_config_root_wins_and_release_stays_untouched(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "releases" / "sha-a"
    external = tmp_path / "runtime" / "top_configs_paper"
    release.mkdir(parents=True)
    monkeypatch.setenv("SOXS_TOP_CONFIG_DIR", str(external))
    monkeypatch.delenv("SOXS_CONFIG_DIR", raising=False)

    assert resolve_top_config_dir(release) == external.resolve()
    _write_configs(config_writer, monkeypatch, project=release)

    assert (external / "TOP1.yaml").exists()
    assert (external / "TOP2.yaml").exists()
    assert (external / "TOP3.yaml").exists()
    assert not (release / "configs").exists()


def test_config_root_fallback_is_used_when_explicit_top_root_is_absent(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "code"
    config_root = tmp_path / "runtime" / "config"
    release.mkdir()
    monkeypatch.delenv("SOXS_TOP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("SOXS_CONFIG_DIR", str(config_root))

    _write_configs(config_writer, monkeypatch, project=release)

    assert (config_root / "top_configs" / "TOP1.yaml").exists()
    assert not (release / "configs").exists()


def test_project_config_fallback_remains_available_for_dev(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "code"
    project.mkdir()
    monkeypatch.delenv("SOXS_TOP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SOXS_CONFIG_DIR", raising=False)

    _write_configs(config_writer, monkeypatch, project=project)

    assert (project / "configs" / "TOP1.yaml").exists()
    assert config_writer._runtime_top_dir() == str((project / "configs").resolve())


def test_two_candidates_keep_identity_and_disable_top3(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "immutable-release"
    external = tmp_path / "runtime" / "top_configs_paper"
    project.mkdir()
    monkeypatch.setenv("SOXS_TOP_CONFIG_DIR", str(external))
    monkeypatch.setenv("QUANTCAIRN_EXECUTION_MODE", "PAPER")

    _write_configs(config_writer, monkeypatch, project=project)

    payloads = [
        yaml.safe_load((external / f"TOP{slot}.yaml").read_text(encoding="utf-8"))
        for slot in range(1, 4)
    ]
    assert [payload["ticker"] for payload in payloads] == ["AIG", "ACGL", None]
    assert [payload["enabled"] for payload in payloads] == [True, True, False]
    assert all(payload["selection_run_id"] == "selection-runtime-path-test" for payload in payloads)
    assert all(payload["mode"] == "paper" for payload in payloads)
    assert not list(project.rglob("TOP*.yaml"))
