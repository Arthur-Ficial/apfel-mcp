"""Tests for apfel_mcp.common.budget.

The budget module enforces the hard-cap on tool-result text. Every MCP in
this repo must truncate its output before returning to apfel, because
apfel's 4096-token context window will crash ("contextOverflow") if the
tool result blows the budget.
"""

import pytest

from apfel_mcp.common.budget import truncate_to


def test_short_text_passes_through_unchanged():
    """Text below the cap is returned verbatim with was_truncated=False."""
    text = "hello world"
    out, was_truncated = truncate_to(text, hard_cap=100)
    assert out == text
    assert was_truncated is False


def test_text_exactly_at_cap_passes_through_unchanged():
    """Text exactly at the cap is returned verbatim (no suffix added)."""
    text = "a" * 100
    out, was_truncated = truncate_to(text, hard_cap=100)
    assert out == text
    assert was_truncated is False


def test_text_over_cap_is_truncated_and_suffixed():
    """Text over the cap is truncated AND gets a visible suffix."""
    text = "a" * 10_000
    out, was_truncated = truncate_to(text, hard_cap=1000)
    assert was_truncated is True
    assert len(out) <= 1000, f"output exceeds hard_cap: {len(out)} > 1000"
    assert "truncated" in out.lower(), "suffix must mention truncation"


def test_empty_text_passes_through():
    """Empty input is a valid case, returns empty, was_truncated=False."""
    out, was_truncated = truncate_to("", hard_cap=100)
    assert out == ""
    assert was_truncated is False


def test_unicode_text_is_sliced_by_code_points_not_bytes():
    """Multi-byte unicode should be sliced cleanly without corruption."""
    # 10 emojis, each is 1 code point but multiple bytes in UTF-8
    text = "🍎" * 100
    out, was_truncated = truncate_to(text, hard_cap=50)
    assert was_truncated is True
    assert len(out) <= 50
    # The output must still be a valid string (no broken multi-byte sequences)
    assert isinstance(out, str)


def test_negative_hard_cap_raises():
    """Non-positive hard_cap is a programmer error."""
    with pytest.raises(ValueError):
        truncate_to("hello", hard_cap=0)
    with pytest.raises(ValueError):
        truncate_to("hello", hard_cap=-1)
