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
        }
    ])

    updated = yaml.safe_load((configs_dir / "TOP1.yaml").read_text(encoding="utf-8"))
    assert updated["mode"] == "live"
    assert updated["broker"]["longbridge"]["enabled"] is True
    assert updated["ticker"] == "NVDA"


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
