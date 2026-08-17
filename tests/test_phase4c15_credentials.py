from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config.local_env import (
    LIVE_EXECUTION_CREDENTIAL_ENV_KEYS,
    load_live_execution_credentials,
    load_local_ai_env,
    sanitize_paper_environment,
)
from src.config.runtime_values import get_runtime_env, has_longbridge_runtime_credentials


def test_paper_sanitizer_removes_fake_credentials_and_blocks_fallback(monkeypatch):
    for key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS:
        monkeypatch.setenv(key, f"fake-{key.lower()}")
    monkeypatch.setenv("SOXS_DISABLE_LIVE_CREDENTIALS", "0")

    removed = sanitize_paper_environment()

    assert removed == len(LIVE_EXECUTION_CREDENTIAL_ENV_KEYS)
    assert all(key not in os.environ for key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS)
    assert get_runtime_env("LONGBRIDGE_APP_KEY") == ""
    assert has_longbridge_runtime_credentials() is False


def test_local_env_never_loads_execution_credentials(tmp_path, monkeypatch):
    env_path = Path(tmp_path) / ".env.ai_selector.local"
    env_path.write_text(
        "SOXS_OPENALPHA_ENABLED=1\n"
        "LONGBRIDGE_APP_KEY=fake-key\n"
        "LONGBRIDGE_APP_SECRET=fake-secret\n"
        "LONGBRIDGE_ACCESS_TOKEN=fake-token\n",
        encoding="utf-8",
    )
    for key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    assert load_local_ai_env(env_path) is True
    assert os.environ["SOXS_OPENALPHA_ENABLED"] == "1"
    assert all(key not in os.environ for key in LIVE_EXECUTION_CREDENTIAL_ENV_KEYS)


def test_live_loader_is_explicit_and_fail_closed():
    fake = {
        "LONGBRIDGE_APP_KEY": "fake-key",
        "LONGBRIDGE_APP_SECRET": "fake-secret",
        "LONGBRIDGE_ACCESS_TOKEN": "fake-token",
    }
    with pytest.raises(PermissionError):
        load_live_execution_credentials("PAPER", fake)
    values = load_live_execution_credentials("LIVE_EXECUTION", fake)
    assert values["LONGBRIDGE_APP_KEY"] == "fake-key"
    assert values["LONGBRIDGE_APP_SECRET"] == "fake-secret"
    assert values["LONGBRIDGE_ACCESS_TOKEN"] == "fake-token"


def test_paper_templates_do_not_source_secrets_or_embed_values():
    launchd = Path(__file__).resolve().parents[1] / "deploy" / "launchd"
    for path in launchd.glob("*.plist.template"):
        text = path.read_text(encoding="utf-8")
        assert "secrets.env" not in text
        assert "SOXS_DISABLE_LIVE_CREDENTIALS" in text
