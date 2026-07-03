from __future__ import annotations

from pathlib import Path

import yaml

from src.config import runtime_values


def test_runtime_values_fall_back_to_config_local(tmp_path, monkeypatch):
    config_local = tmp_path / "config.local.yaml"
    config_local.write_text(
        yaml.safe_dump(
            {
                "broker": {
                    "longbridge": {
                        "app_key": "local-key",
                        "app_secret": "local-secret",
                        "access_token": "local-token",
                        "region": "cn",
                        "environment": "prod",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_values, "PROJECT_DIR", tmp_path)
    runtime_values.clear_private_config_cache()
    monkeypatch.delenv("LONGBRIDGE_APP_KEY", raising=False)
    monkeypatch.delenv("LONGBRIDGE_APP_SECRET", raising=False)
    monkeypatch.delenv("LONGBRIDGE_ACCESS_TOKEN", raising=False)

    assert runtime_values.get_runtime_env("LONGBRIDGE_APP_KEY") == "local-key"
    assert runtime_values.get_runtime_env("LONGBRIDGE_APP_SECRET") == "local-secret"
    assert runtime_values.get_runtime_env("LONGBRIDGE_ACCESS_TOKEN") == "local-token"
    assert runtime_values.has_longbridge_runtime_credentials() is True
