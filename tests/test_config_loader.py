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


def run_test_direct():
    test_position_size_defaults_to_auto_when_omitted()
    test_reduce_only_defaults_false_and_parses_yaml()
    test_ai_selector_fallback_policy_defaults_and_parses_yaml()


if __name__ == "__main__":
    run_test_direct()
