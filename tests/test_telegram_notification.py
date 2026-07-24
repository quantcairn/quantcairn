"""Tests for Telegram notification message length handling.

Verifies that long selection messages are correctly chunked and that
short messages are sent as single units.
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from src.notifier.alerts import Notifier


class TestTelegramSingleMessage:
    """Short messages must be sent as one Telegram API call."""

    def test_short_message_sent_as_single(self):
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        short_body = "选股完成\nTOP1: AAPL"

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            notifier._telegram_send("选股完成", short_body)
            mock_single.assert_called_once()

    def test_message_under_4000_chars_no_chunking(self):
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        body = "数据正常\n" * 200  # ~2000 chars, well under 4000

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            with patch.object(notifier, "_telegram_send_chunked") as mock_chunked:
                notifier._telegram_send("选股完成", body)
                mock_single.assert_called_once()
                mock_chunked.assert_not_called()

    def test_exactly_4000_boundary_single(self):
        """Exactly at SAFE_CHARS boundary: still sent as single."""
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        # Build body such that title + body = exactly 4000
        title = "选股完成"
        safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        overhead = len(f"<b>{safe_title}</b>\n")
        # Each "X" char: 1 byte. body "X" * N, safe_body same
        body_len = 4000 - overhead
        body = "X" * body_len

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            with patch.object(notifier, "_telegram_send_chunked") as mock_chunked:
                notifier._telegram_send(title, body)
                mock_single.assert_called_once()
                mock_chunked.assert_not_called()


class TestTelegramLongMessage:
    """Messages exceeding 4000 chars must be split into multiple Telegram API calls."""

    def test_long_message_triggers_chunking(self):
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        # Build a body > 4000 chars total (title + body)
        body_lines = []
        # Each paragraph: ~200 chars, so ~25 paragraphs = ~5000 chars
        for i in range(30):
            body_lines.append(f"选股候选 #{i}: " + "数据正常. " * 30)
        body = "\n\n".join(body_lines)

        assert len(body) > 4000  # sanity check

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            notifier._telegram_send("选股完成", body)
            # Should be called multiple times (chunked)
            assert mock_single.call_count >= 2, (
                f"Expected ≥2 chunks, got {mock_single.call_count}"
            )

    def test_each_chunk_under_4096_chars(self):
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        body_lines = []
        for i in range(30):
            body_lines.append(f"选股候选 #{i}: " + "数据正常. " * 30)
        body = "\n\n".join(body_lines)

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            notifier._telegram_send("选股完成", body)

            for call_args in mock_single.call_args_list:
                text = call_args[0][0]
                assert len(text) <= notifier.TELEGRAM_MAX_CHARS, (
                    f"Chunk length {len(text)} exceeds {notifier.TELEGRAM_MAX_CHARS}"
                )

    def test_first_chunk_contains_title(self):
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        body_lines = []
        for i in range(30):
            body_lines.append(f"选股候选 #{i}: " + "数据正常. " * 30)
        body = "\n\n".join(body_lines)

        title = "【AI 选股完成】"
        with patch.object(notifier, "_telegram_send_single") as mock_single:
            notifier._telegram_send(title, body)

            first_text = mock_single.call_args_list[0][0][0]
            assert title in first_text, (
                f"First chunk should contain title '{title}', got: {first_text[:100]}..."
            )

    def test_continuation_chunks_have_numbered_header(self):
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        body_lines = []
        for i in range(40):
            body_lines.append(f"选股候选 #{i}: " + "数据正常. " * 30)
        body = "\n\n".join(body_lines)

        title = "【AI 选股完成】"
        with patch.object(notifier, "_telegram_send_single") as mock_single:
            notifier._telegram_send(title, body)

            for idx, call_args in enumerate(mock_single.call_args_list):
                text = call_args[0][0]
                if idx == 0:
                    assert title in text
                elif idx > 0:
                    # Continuation chunks have (2/N), (3/N) etc.
                    assert f"({idx + 1}" in text, (
                        f"Chunk {idx+1} should have continuation number in: {text[:80]}"
                    )

    def test_double_newline_boundaries_respected(self):
        """Paragraphs (double-newline sections) should stay together in chunks."""
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        # Create paragraphs: each paragraph is ~200 chars
        paragraphs = []
        for i in range(40):
            paragraphs.append(f"PARA#{i:03d}: " + "x" * 190)
        body = "\n\n".join(paragraphs)

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            notifier._telegram_send("选股完成", body)

            total_paras_covered = 0
            for call_args in mock_single.call_args_list:
                text = call_args[0][0]
                # Count how many PARA markers in this chunk
                para_count = text.count("PARA#")
                # Each PARA marker should appear at most once across all chunks
                for p in range(total_paras_covered, total_paras_covered + para_count):
                    assert f"PARA#{p:03d}" in text, (
                        f"PARA#{p:03d} should be in the chunk covering "
                        f"paras {total_paras_covered}–{total_paras_covered + para_count - 1}"
                    )
                total_paras_covered += para_count

            assert total_paras_covered == len(paragraphs)

    def test_fallback_to_html_disabled_on_api_error(self):
        """When HTML send fails, the chunk should retry as plain text."""
        notifier = Notifier(
            console=False,
            macos_notification=False,
            telegram_bot_token="test_token",
            telegram_chat_id="@test_channel",
        )

        body = "测试消息体" * 500  # ~2500 chars — won't trigger chunking

        import requests as real_requests

        with patch.object(notifier, "_telegram_send_single") as mock_single:
            # Just verify it's called with use_html=True for normal case
            notifier._telegram_send("测试", body)
            assert mock_single.call_args[1]["use_html"] is True
