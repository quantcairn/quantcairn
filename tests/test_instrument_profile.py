"""Tests for src.risk.instrument_profile."""
from src.risk.instrument_profile import get_profile, is_leveraged_etf


def test_leveraged_etf_identification():
    assert is_leveraged_etf("SOXS") is True
    assert is_leveraged_etf("SOXL") is True
    assert is_leveraged_etf("LABD") is True
    assert is_leveraged_etf("TQQQ") is True
    assert is_leveraged_etf("AAPL") is False
    assert is_leveraged_etf("NVDA") is False
    assert is_leveraged_etf("") is False
    assert is_leveraged_etf("SPY") is False


def test_profile_fields_for_leveraged_etf():
    profile = get_profile("SOXS")
    assert profile["instrument_type"] == "leveraged_etf"
    assert profile["leverage_factor"] == 3
    assert profile["inverse"] is True
    assert profile["overnight_allowed"] is False
    assert profile["max_position_pct"] == 0.15
    assert profile["max_total_group_exposure"] == 0.50
    assert profile["max_daily_loss_pct"] == 0.03
    assert profile["reduce_only_allowed"] is True


def test_profile_fields_for_non_leveraged():
    profile = get_profile("AAPL")
    assert profile["instrument_type"] == "equity"
    assert profile["leverage_factor"] == 1
    assert profile["overnight_allowed"] is True
    assert profile["max_position_pct"] == 0.30


def test_profile_leverage_tier():
    # 3x → 15%
    assert get_profile("SOXS")["max_position_pct"] == 0.15
    # 2x → 20%
    assert get_profile("YINN")["max_position_pct"] == 0.20


def test_profile_non_inverse_etf():
    profile = get_profile("TQQQ")
    assert profile["inverse"] is False
    assert profile["instrument_type"] == "leveraged_etf"


def test_profile_case_insensitive():
    assert get_profile("soxs")["instrument_type"] == "leveraged_etf"
    assert get_profile("Soxs")["instrument_type"] == "leveraged_etf"


def test_profile_empty_ticker():
    profile = get_profile("")
    assert profile["instrument_type"] == "equity"
    assert profile["leverage_factor"] == 1


def run_test_direct():
    test_leveraged_etf_identification()
    test_profile_fields_for_leveraged_etf()
    test_profile_fields_for_non_leveraged()
    test_profile_leverage_tier()
    test_profile_non_inverse_etf()
    test_profile_case_insensitive()
    test_profile_empty_ticker()
