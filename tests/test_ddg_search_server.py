"""Tests for the apfel-mcp-ddg-search server entry point.

Protocol-level tests that drive run_server via io.StringIO and mock the
underlying common.search.search call. The heavy lifting is tested in
test_common_search.py; this file verifies that the MCP wrapper correctly
dispatches to search, handles error paths, and tolerates the argument-key
synonyms that small models sometimes invent.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from apfel_mcp.common.search import ResultRow, SearchError, SearchResult
from apfel_mcp.ddg_search_server import SERVER_NAME, TOOLS, _handle_tool_call, main


def _fake_result(query: str = "test", n: int = 2) -> SearchResult:
    rows = [
        ResultRow(
            title=f"Result {i+1}",
            url=f"https://example{i+1}.com/",
            snippet=f"Snippet {i+1}",
            domain=f"example{i+1}.com",
        )
        for i in range(n)
    ]
    formatted = f"Search: {query}\n\n" + "\n".join(
        f"{i+1}. {r.title}\n   {r.snippet}\n   {r.domain}" for i, r in enumerate(rows)
    )
    return SearchResult(
        query=query,
        results=rows,
        formatted_text=formatted,
        from_cache=False,
    )


def test_server_name_matches_entry_point():
    assert SERVER_NAME == "apfel-mcp-ddg-search"


def test_tools_list_contains_single_search_tool():
    assert len(TOOLS) == 1
    assert TOOLS[0]["name"] == "search"
    assert "query" in TOOLS[0]["inputSchema"]["required"]


def test_tool_description_mentions_duckduckgo_and_experimental():
    """The description shown to the model must flag the unofficial nature."""
    desc = TOOLS[0]["description"].lower()
    assert "duckduckgo" in desc
    assert "unofficial" in desc or "experimental" in desc


def test_handle_tool_call_runs_search_and_returns_formatted_text():
    """A successful search is wrapped as an MCP ToolResult with is_error=False."""
    fake = _fake_result("apfel", n=2)
    with patch(
        "apfel_mcp.ddg_search_server.search", return_value=fake
    ) as mock_search:
        result = _handle_tool_call("search", {"query": "apfel"})

    mock_search.assert_called_once_with("apfel", max_results=5)
    assert result.is_error is False
    assert "Search: apfel" in result.text
    assert "Result 1" in result.text


def test_handle_tool_call_missing_query_returns_error():
    result = _handle_tool_call("search", {})
    assert result.is_error is True
    assert "query" in result.text.lower()


def test_handle_tool_call_tolerates_q_synonym_for_query():
    """If the model calls search(q=...) instead of search(query=...), it still works."""
    fake = _fake_result("apfel", n=1)
    with patch("apfel_mcp.ddg_search_server.search", return_value=fake):
        result = _handle_tool_call("search", {"q": "apfel"})
    assert result.is_error is False


def test_handle_tool_call_tolerates_text_synonym_for_query():
    fake = _fake_result("apfel", n=1)
    with patch("apfel_mcp.ddg_search_server.search", return_value=fake):
        result = _handle_tool_call("search", {"text": "apfel"})
    assert result.is_error is False


def test_handle_tool_call_respects_max_results_argument():
    """Custom max_results is forwarded to search()."""
    fake = _fake_result("apfel", n=3)
    with patch(
        "apfel_mcp.ddg_search_server.search", return_value=fake
    ) as mock_search:
        _handle_tool_call("search", {"query": "apfel", "max_results": 3})
    mock_search.assert_called_once_with("apfel", max_results=3)


def test_handle_tool_call_search_error_becomes_is_error_true():
    """A SearchError from the underlying search is returned as is_error=True."""
    with patch(
        "apfel_mcp.ddg_search_server.search",
        side_effect=SearchError("DuckDuckGo rate-limited this request"),
    ):
        result = _handle_tool_call("search", {"query": "apfel"})
    assert result.is_error is True
    assert "rate" in result.text.lower()


def test_handle_tool_call_unexpected_exception_becomes_is_error_true():
    """Any non-SearchError crash is wrapped as a tool error, not propagated."""
    with patch(
        "apfel_mcp.ddg_search_server.search",
        side_effect=RuntimeError("boom"),
    ):
        result = _handle_tool_call("search", {"query": "apfel"})
    assert result.is_error is True
    assert "unexpected error" in result.text
    assert "boom" in result.text


def test_handle_tool_call_unknown_tool_name_returns_error():
    result = _handle_tool_call("not_search", {"query": "x"})
    assert result.is_error is True
    assert "unknown tool" in result.text.lower()


def test_handle_tool_call_tolerates_term_synonym():
    """Regression: the 3B model called search(term='apfel macos') in real E2E."""
    fake = _fake_result("apfel macos", n=1)
    with patch(
        "apfel_mcp.ddg_search_server.search", return_value=fake
    ) as mock_search:
        result = _handle_tool_call("search", {"term": "apfel macos"})
    mock_search.assert_called_once_with("apfel macos", max_results=5)
    assert result.is_error is False


def test_handle_tool_call_tolerates_arbitrary_unknown_key():
    """Max tolerance: any non-empty string under any unknown key counts as the query."""
    fake = _fake_result("hello world", n=1)
    with patch(
        "apfel_mcp.ddg_search_server.search", return_value=fake
    ) as mock_search:
        result = _handle_tool_call("search", {"wibble": "hello world"})
    mock_search.assert_called_once_with("hello world", max_results=5)
    assert result.is_error is False


def test_handle_tool_call_tolerates_limit_synonym_for_max_results():
    """Model passes `limit` instead of `max_results`. Should still work."""
    fake = _fake_result("test", n=1)
    with patch(
        "apfel_mcp.ddg_search_server.search", return_value=fake
    ) as mock_search:
        _handle_tool_call("search", {"query": "test", "limit": 3})
    mock_search.assert_called_once_with("test", max_results=3)


def test_server_stdio_round_trip_initialize_then_search(monkeypatch):
    """End-to-end: drive the full run_server loop via StringIO."""
    fake = _fake_result("apfel", n=1)
    monkeypatch.setattr(
        "apfel_mcp.ddg_search_server.search",
        lambda query, max_results: fake,
    )

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "apfel"}},
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
    assert responses[0]["result"]["serverInfo"]["name"] == "apfel-mcp-ddg-search"

    # tools/list
    tools = responses[1]["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "search"

    # tools/call
    call = responses[2]["result"]
    assert call["isError"] is False
    assert "Search: apfel" in call["content"][0]["text"]
