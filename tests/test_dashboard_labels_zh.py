from src.dashboard.labels_zh import (
    format_bool,
    format_optional,
    format_timestamp,
    translate_reason,
    translate_status,
    truncate_identifier,
)


def test_status_translation_preserves_original_enum():
    assert translate_status("FINALIZED") == "已完成（FINALIZED）"
    assert translate_status("DEGRADED") == "降级（DEGRADED）"
    assert translate_status("RESEARCH_ONLY") == "仅供研究（RESEARCH_ONLY）"
    assert translate_status("paper") == "虚拟盘（paper）"
    assert translate_status("NEW_STATE") == "NEW_STATE"
    assert translate_status(None) == "暂无数据"


def test_reason_and_optional_value_formatting():
    assert translate_reason("low_dollar_volume") == "成交额过低"
    assert translate_reason("insufficient_candidates") == "候选数量不足"
    assert translate_reason("custom_reason") == "未识别原因（custom_reason）"
    assert format_bool(True) == "是"
    assert format_bool(False) == "否"
    assert format_bool(None) == "未知"
    assert format_optional(None) == "暂无数据"
    assert format_optional("") == "暂无数据"


def test_timestamp_is_displayed_in_beijing_time_and_invalid_input_is_safe():
    assert format_timestamp("2026-07-19T13:35:08Z") == "2026-07-19 21:35:08"
    assert format_timestamp("2026-07-19T21:35:08+08:00") == "2026-07-19 21:35:08"
    assert format_timestamp("2026-07-19 21:35:08") == "2026-07-19 21:35:08"
    assert format_timestamp("not-a-time") == "not-a-time"
    assert format_timestamp(None) == "时间未知"


def test_identifier_truncation_keeps_short_values_and_shortens_long_values():
    assert truncate_identifier("abc123") == "abc123"
    assert truncate_identifier("abcdefgh1234567890") == "abcdefgh…567890"
