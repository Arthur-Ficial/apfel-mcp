"""Tests for the apfel-mcp-url-fetch server entry point.

Protocol-level tests that drive run_server via io.StringIO and mock the
underlying fetch_and_extract call. The heavy lifting is tested in
test_common_fetch.py; this file verifies that the MCP wrapper correctly
dispatches to fetch, handles error paths, and tolerates the tool-argument
synonyms that small models sometimes invent.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from apfel_mcp.common.fetch import FetchError, FetchResult
from apfel_mcp.url_fetch_server import SERVER_NAME, TOOLS, _handle_tool_call, main


def test_server_name_matches_entry_point():
    assert SERVER_NAME == "apfel-mcp-url-fetch"


def test_tools_list_contains_single_fetch_tool():
    assert len(TOOLS) == 1
    assert TOOLS[0]["name"] == "fetch"
    assert "url" in TOOLS[0]["inputSchema"]["required"]


def test_tool_description_mentions_token_budget_and_SSRF():
    """The description shown to the model must set expectations about limits."""
    desc = TOOLS[0]["description"].lower()
    assert "4096" in desc
    assert "6000" in desc
    assert "http" in desc


def test_handle_tool_call_fetches_url_and_wraps_result():
    """A successful fetch is wrapped as an MCP ToolResult with is_error=False."""
    fake_result = FetchResult(
        title="Hello",
        final_url="https://example.com/",
        body="Hello\nhttps://example.com/\n\nbody content",
        was_truncated=False,
    )
    with patch(
        "apfel_mcp.url_fetch_server.fetch_and_extract", return_value=fake_result
    ) as mock_fetch:
        result = _handle_tool_call("fetch", {"url": "https://example.com/"})

    mock_fetch.assert_called_once_with("https://example.com/", max_chars=4000)
    assert result.is_error is False
    assert "body content" in result.text


def test_handle_tool_call_missing_url_returns_error():
    result = _handle_tool_call("fetch", {})
    assert result.is_error is True
    assert "url" in result.text.lower()


def test_handle_tool_call_tolerates_link_synonym_for_url():
    """If the model calls fetch(link=...) instead of fetch(url=...), it still works."""
    fake_result = FetchResult(
        title="X", final_url="https://x.com/", body="body", was_truncated=False
    )
    with patch(
        "apfel_mcp.url_fetch_server.fetch_and_extract", return_value=fake_result
    ) as mock_fetch:
        result = _handle_tool_call("fetch", {"link": "https://x.com/"})
    mock_fetch.assert_called_once()
    assert result.is_error is False


def test_handle_tool_call_tolerates_page_synonym_for_url():
    fake_result = FetchResult(
        title="X", final_url="https://x.com/", body="body", was_truncated=False
    )
    with patch(
        "apfel_mcp.url_fetch_server.fetch_and_extract", return_value=fake_result
    ):
        result = _handle_tool_call("fetch", {"page": "https://x.com/"})
    assert result.is_error is False


def test_handle_tool_call_respects_max_chars_argument():
    """Custom max_chars is forwarded to fetch_and_extract."""
    fake_result = FetchResult(
        title="X", final_url="https://x.com/", body="body", was_truncated=False
    )
    with patch(
        "apfel_mcp.url_fetch_server.fetch_and_extract", return_value=fake_result
    ) as mock_fetch:
        _handle_tool_call("fetch", {"url": "https://x.com/", "max_chars": 2000})
    mock_fetch.assert_called_once_with("https://x.com/", max_chars=2000)


def test_handle_tool_call_fetch_error_becomes_is_error_true():
    """A FetchError from the underlying fetch is returned as is_error=True."""
    with patch(
        "apfel_mcp.url_fetch_server.fetch_and_extract",
        side_effect=FetchError("private network blocked"),
    ):
        result = _handle_tool_call("fetch", {"url": "http://127.0.0.1/"})
    assert result.is_error is True
    assert "private network" in result.text


def test_handle_tool_call_unexpected_exception_becomes_is_error_true():
    """Any non-FetchError crash is wrapped as a tool error, not propagated."""
    with patch(
        "apfel_mcp.url_fetch_server.fetch_and_extract",
        side_effect=RuntimeError("boom"),
    ):
        result = _handle_tool_call("fetch", {"url": "https://example.com/"})
    assert result.is_error is True
    assert "unexpected error" in result.text
    assert "boom" in result.text


def test_server_stdio_round_trip_initialize_then_fetch(monkeypatch):
    """End-to-end: drive the full run_server loop via StringIO."""
    fake_result = FetchResult(
        title="Stdio Test",
        final_url="https://example.com/",
        body="hello from stdio",
        was_truncated=False,
    )
    monkeypatch.setattr(
        "apfel_mcp.url_fetch_server.fetch_and_extract",
        lambda url, max_chars: fake_result,
    )

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "fetch", "arguments": {"url": "https://example.com/"}},
        },
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()

    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    main()

    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(responses) == 3

    # initialize
    assert responses[0]["result"]["serverInfo"]["name"] == "apfel-mcp-url-fetch"

    # tools/list
    tools = responses[1]["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "fetch"

    # tools/call
    call = responses[2]["result"]
    assert call["isError"] is False
    assert "hello from stdio" in call["content"][0]["text"]
