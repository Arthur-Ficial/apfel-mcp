"""Tests for apfel_mcp.common.search.

The search module wraps the unofficial `ddgs` library that scrapes
DuckDuckGo's HTML endpoint. DuckDuckGo has no public web-search API, so
this is the only key-free option. We document it as experimental and
adopt the same framing OpenClaw uses for their DDG extension.

The search module adds three things on top of ddgs:
1. A 60-second in-memory cache (reduces DDG load on repeated queries)
2. Token-budget-aware formatting (title 80 chars, snippet 160 chars,
   hard cap 2000 chars total)
3. Friendly error messages when ddgs raises rate-limit or bot-challenge
   exceptions

Tests mock ddgs.DDGS.text so no real network traffic happens.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from apfel_mcp.common.search import (
    CACHE_TTL_SECONDS,
    HARD_CAP_CHARS,
    SearchError,
    SearchResult,
    _clear_cache,
    search,
)


def _mk_results(n: int) -> list[dict]:
    """Build N fake ddgs results with predictable titles, urls, bodies."""
    return [
        {
            "title": f"Result {i+1} title",
            "href": f"https://example{i+1}.com/path",
            "body": f"Result {i+1} snippet body content for testing purposes.",
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _reset_cache():
    """Ensure each test starts with an empty cache (the module has a module-level dict)."""
    _clear_cache()
    yield
    _clear_cache()


# --- Input validation ---

def test_empty_query_raises_search_error():
    with pytest.raises(SearchError, match=r"empty|query"):
        search("")


def test_whitespace_only_query_raises_search_error():
    with pytest.raises(SearchError, match=r"empty|query"):
        search("   \n\t  ")


def test_max_results_below_one_clamps_to_one():
    """max_results=0 is silently clamped to 1 instead of returning nothing."""
    with patch("apfel_mcp.common.search._ddgs_text", return_value=_mk_results(1)) as mock_text:
        result = search("test", max_results=0)
    mock_text.assert_called_once()
    # ddgs should have been asked for 1 result
    args, kwargs = mock_text.call_args
    assert kwargs.get("max_results", args[1] if len(args) > 1 else None) == 1
    assert len(result.results) == 1


def test_max_results_above_ten_clamps_to_ten():
    """max_results=99 is silently clamped to 10 (our configured maximum)."""
    with patch("apfel_mcp.common.search._ddgs_text", return_value=_mk_results(10)) as mock_text:
        result = search("test", max_results=99)
    args, kwargs = mock_text.call_args
    assert kwargs.get("max_results", args[1] if len(args) > 1 else None) == 10
    assert len(result.results) == 10


# --- Output formatting ---

def test_search_returns_SearchResult_with_query_and_results():
    with patch("apfel_mcp.common.search._ddgs_text", return_value=_mk_results(3)):
        result = search("test query", max_results=3)
    assert isinstance(result, SearchResult)
    assert result.query == "test query"
    assert len(result.results) == 3
    assert result.from_cache is False


def test_formatted_text_contains_each_result_block():
    with patch("apfel_mcp.common.search._ddgs_text", return_value=_mk_results(3)):
        result = search("test", max_results=3)
    # formatted_text is the plain-text output the MCP server returns to the model
    assert "Search: test" in result.formatted_text
    assert "Result 1 title" in result.formatted_text
    assert "Result 2 title" in result.formatted_text
    assert "Result 3 title" in result.formatted_text


def test_long_titles_are_truncated_to_80_chars():
    long_title_results = [
        {
            "title": "x" * 500,  # 500 char title
            "href": "https://example.com/",
            "body": "body",
        }
    ]
    with patch("apfel_mcp.common.search._ddgs_text", return_value=long_title_results):
        result = search("test")
    # No single line should contain 500 'x's in a row
    for line in result.formatted_text.splitlines():
        if "x" in line:
            # The title line should be truncated with an ellipsis or similar
            xs_in_a_row = line.count("x")
            assert xs_in_a_row <= 85  # 80 char cap + a little slack for suffix


def test_long_snippets_are_truncated_to_160_chars():
    results = [
        {
            "title": "Short title",
            "href": "https://example.com/",
            "body": "y" * 1000,
        }
    ]
    with patch("apfel_mcp.common.search._ddgs_text", return_value=results):
        result = search("test")
    # The snippet line should be truncated
    for line in result.formatted_text.splitlines():
        if "y" in line:
            ys = line.count("y")
            assert ys <= 165


def test_domain_is_extracted_from_href():
    results = [
        {
            "title": "Page",
            "href": "https://www.example.com/some/long/path?q=1",
            "body": "body",
        }
    ]
    with patch("apfel_mcp.common.search._ddgs_text", return_value=results):
        result = search("test")
    # The formatted output should include the domain (stripped of path and query)
    assert "example.com" in result.formatted_text
    # But NOT the long path
    assert "/some/long/path" not in result.formatted_text


def test_hard_cap_enforced_on_total_output():
    huge_results = [
        {
            "title": "t" * 100,
            "href": "https://example.com/",
            "body": "s" * 500,
        }
        for _ in range(10)
    ]
    with patch("apfel_mcp.common.search._ddgs_text", return_value=huge_results):
        result = search("test", max_results=10)
    assert len(result.formatted_text) <= HARD_CAP_CHARS


def test_empty_results_returns_friendly_message():
    with patch("apfel_mcp.common.search._ddgs_text", return_value=[]):
        result = search("nonexistent query xyzzy")
    assert len(result.results) == 0
    assert "No results for" in result.formatted_text
    assert "nonexistent query xyzzy" in result.formatted_text


# --- Error handling ---

def test_ddgs_exception_becomes_search_error():
    with patch(
        "apfel_mcp.common.search._ddgs_text",
        side_effect=RuntimeError("something broke"),
    ), pytest.raises(SearchError, match=r"search failed|something broke"):
        search("test")


def test_ratelimit_hint_in_error_message_when_ddgs_signals_ratelimit():
    """When ddgs raises an exception containing 'ratelimit' or '429', our error message
    mentions it so the user knows to wait."""
    with patch(
        "apfel_mcp.common.search._ddgs_text",
        side_effect=Exception("DuckDuckGoSearchException: Ratelimit"),
    ), pytest.raises(SearchError, match=r"rate|limit"):
        search("test")


# --- Cache ---

def test_cache_hit_on_second_call_with_same_query():
    with patch(
        "apfel_mcp.common.search._ddgs_text",
        return_value=_mk_results(3),
    ) as mock_text:
        result1 = search("same query", max_results=3)
        result2 = search("same query", max_results=3)
    # The underlying ddgs call should only happen once
    assert mock_text.call_count == 1
    assert result1.from_cache is False
    assert result2.from_cache is True
    assert result2.results == result1.results


def test_cache_miss_for_different_query():
    with patch(
        "apfel_mcp.common.search._ddgs_text",
        return_value=_mk_results(3),
    ) as mock_text:
        search("first query")
        search("second query")
    assert mock_text.call_count == 2


def test_cache_miss_for_different_max_results():
    """Different max_results counts should hit different cache entries."""
    with patch(
        "apfel_mcp.common.search._ddgs_text",
        return_value=_mk_results(10),
    ) as mock_text:
        search("query", max_results=3)
        search("query", max_results=5)
    assert mock_text.call_count == 2


def test_cache_expires_after_ttl(monkeypatch):
    """Cache entries older than CACHE_TTL_SECONDS are evicted on access."""
    # Simulate time passing by monkey-patching time.monotonic
    fake_time = [1000.0]

    def fake_monotonic():
        return fake_time[0]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    with patch(
        "apfel_mcp.common.search._ddgs_text",
        return_value=_mk_results(3),
    ) as mock_text:
        search("query")
        # Advance past TTL
        fake_time[0] += CACHE_TTL_SECONDS + 1
        search("query")
    assert mock_text.call_count == 2  # cache expired, second call re-hit ddgs


def test_cache_ttl_constant_is_60_seconds():
    """Regression check for the documented TTL."""
    assert CACHE_TTL_SECONDS == 60
