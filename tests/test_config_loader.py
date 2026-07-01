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


def run_test_direct():
    test_position_size_defaults_to_auto_when_omitted()
    test_reduce_only_defaults_false_and_parses_yaml()


if __name__ == "__main__":
    run_test_direct()
