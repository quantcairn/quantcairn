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


def run_test_direct():
    test_position_size_defaults_to_auto_when_omitted()


if __name__ == "__main__":
    run_test_direct()
