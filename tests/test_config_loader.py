from src.config.loader import _parse_config


def test_position_size_defaults_to_auto_when_omitted():
    config = _parse_config(
        {
            "ticker": "SOXS",
            "mode": "paper",
            "range": {"mode": "auto"},
        }
    )

    assert config.position.size_per_trade == 0


def test_reduce_only_defaults_false_and_parses_yaml():
    config = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "live",
            "range": {"mode": "auto"},
            "position": {"reduce_only": True},
        }
    )

    assert config.position.reduce_only is True


def test_ai_selector_fallback_policy_defaults_and_parses_yaml():
    config = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
            "ai_selector": {
                "allow_fallback_paper_entries": True,
                "allow_fallback_live_entries": False,
                "fallback_paper_position_multiplier": 0.25,
            },
        }
    )

    assert config.ai_selector.allow_fallback_paper_entries is True
    assert config.ai_selector.allow_fallback_live_entries is False
    assert config.ai_selector.fallback_paper_position_multiplier == 0.25

    defaults = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
        }
    )

    assert defaults.ai_selector.allow_fallback_paper_entries is False
    assert defaults.ai_selector.allow_fallback_live_entries is False
    assert defaults.ai_selector.fallback_paper_position_multiplier == 0.25


def test_ai_selector_entry_proximity_defaults_and_parses_yaml():
    config = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
            "ai_selector": {
                "entry_proximity_enabled": True,
                "entry_proximity_weight": 0.15,
            },
        }
    )

    assert config.ai_selector.entry_proximity_enabled is True
    assert config.ai_selector.entry_proximity_weight == 0.15

    defaults = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
        }
    )

    assert defaults.ai_selector.entry_proximity_enabled is True
    assert defaults.ai_selector.entry_proximity_weight == 0.0


def test_strategy_feature_flags_default_false_and_parse_yaml():
    config = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
            "strategy": {
                "dynamic_range_enabled": True,
                "scaled_entry_enabled": "false",
                "scaled_exit_enabled": "1",
                "inventory_aware_sizing_enabled": 0,
                "trend_guard_enabled": "yes",
                "cost_filter_enabled": "",
                "time_stop_enabled": False,
            },
        }
    )

    assert config.strategy.dynamic_range_enabled is True
    assert config.strategy.scaled_entry_enabled is False
    assert config.strategy.scaled_exit_enabled is True
    assert config.strategy.inventory_aware_sizing_enabled is False
    assert config.strategy.trend_guard_enabled is True
    assert config.strategy.cost_filter_enabled is False
    assert config.strategy.time_stop_enabled is False

    defaults = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
        }
    )

    assert defaults.strategy.dynamic_range_enabled is False
    assert defaults.strategy.scaled_entry_enabled is False
    assert defaults.strategy.scaled_exit_enabled is False
    assert defaults.strategy.inventory_aware_sizing_enabled is False
    assert defaults.strategy.trend_guard_enabled is False
    assert defaults.strategy.cost_filter_enabled is False
    assert defaults.strategy.time_stop_enabled is False


def test_portfolio_enabled_string_false_is_not_truthy():
    config = _parse_config(
        {
            "ticker": "SOFI",
            "mode": "paper",
            "range": {"mode": "auto"},
            "portfolio": {"enabled": "false"},
        }
    )

    assert config.portfolio.enabled is False


def test_longbridge_account_type_defaults_and_parses_yaml():
    import os
    from src.config import loader as config_loader

    original_env_account_type = os.environ.get("LONGBRIDGE_ACCOUNT_TYPE")
    original_runtime_loader = config_loader.load_private_longbridge_config
    os.environ.pop("LONGBRIDGE_ACCOUNT_TYPE", None)
    config_loader.load_private_longbridge_config = lambda: {}
    try:
        config = _parse_config(
            {
                "ticker": "SOFI",
                "mode": "sandbox",
                "range": {"mode": "auto"},
                "broker": {
                    "longbridge": {
                        "enabled": True,
                        "environment": "sandbox",
                        "account_type": "paper",
                        "sandbox": {
                            "allow_live_order": False,
                        },
                    }
                },
            }
        )

        assert config.broker.longbridge.account_type == "paper"

        defaults = _parse_config({"ticker": "SOFI", "mode": "paper", "range": {"mode": "auto"}})

        assert defaults.broker.longbridge.account_type == ""
    finally:
        if original_env_account_type is None:
            os.environ.pop("LONGBRIDGE_ACCOUNT_TYPE", None)
        else:
            os.environ["LONGBRIDGE_ACCOUNT_TYPE"] = original_env_account_type
        config_loader.load_private_longbridge_config = original_runtime_loader


def test_notifications_split_trade_and_ai_selector_channels():
    import os

    original_trade_token = os.environ.get("SOXS_TELEGRAM_BOT_TOKEN")
    original_trade_chat = os.environ.get("SOXS_TELEGRAM_CHAT_ID")
    original_ai_token = os.environ.get("SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN")
    original_ai_chat = os.environ.get("SOXS_OPENALPHA_TELEGRAM_CHAT_ID")
    os.environ.pop("SOXS_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("SOXS_TELEGRAM_CHAT_ID", None)
    os.environ.pop("SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("SOXS_OPENALPHA_TELEGRAM_CHAT_ID", None)
    try:
        config = _parse_config(
            {
                "ticker": "SOFI",
                "mode": "paper",
                "range": {"mode": "auto"},
                "notifications": {
                    "telegram_bot_token": "trade-token",
                    "telegram_chat_id": "trade-chat",
                    "ai_selector": {
                        "telegram_bot_token": "ai-token",
                        "telegram_chat_id": "ai-chat",
                        "webhook_url": "https://example.com/ai",
                    },
                },
            }
        )

        assert config.notifications.telegram_bot_token == "trade-token"
        assert config.notifications.telegram_chat_id == "trade-chat"
        assert config.notifications.ai_selector_telegram_bot_token == "ai-token"
        assert config.notifications.ai_selector_telegram_chat_id == "ai-chat"
        assert config.notifications.ai_selector_webhook_url == "https://example.com/ai"
    finally:
        if original_trade_token is None:
            os.environ.pop("SOXS_TELEGRAM_BOT_TOKEN", None)
        else:
            os.environ["SOXS_TELEGRAM_BOT_TOKEN"] = original_trade_token
        if original_trade_chat is None:
            os.environ.pop("SOXS_TELEGRAM_CHAT_ID", None)
        else:
            os.environ["SOXS_TELEGRAM_CHAT_ID"] = original_trade_chat
        if original_ai_token is None:
            os.environ.pop("SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN", None)
        else:
            os.environ["SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN"] = original_ai_token
        if original_ai_chat is None:
            os.environ.pop("SOXS_OPENALPHA_TELEGRAM_CHAT_ID", None)
        else:
            os.environ["SOXS_OPENALPHA_TELEGRAM_CHAT_ID"] = original_ai_chat


def run_test_direct():
    test_position_size_defaults_to_auto_when_omitted()
    test_reduce_only_defaults_false_and_parses_yaml()
    test_ai_selector_fallback_policy_defaults_and_parses_yaml()
    test_ai_selector_entry_proximity_defaults_and_parses_yaml()
    test_strategy_feature_flags_default_false_and_parse_yaml()
    test_portfolio_enabled_string_false_is_not_truthy()
    test_longbridge_account_type_defaults_and_parses_yaml()
    test_notifications_split_trade_and_ai_selector_channels()


if __name__ == "__main__":
    run_test_direct()
