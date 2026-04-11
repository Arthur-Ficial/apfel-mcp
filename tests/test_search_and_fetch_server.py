"""Tests for the apfel-mcp-search-and-fetch compound server.

search_and_fetch is the user-facing win on apfel's 4096-token context:
one tool call performs a DDG search AND fetches the top N result pages,
returning combined content. The model gets the answer in one round trip
instead of chaining search → fetch with the schema overhead doubled.

Tests mock `common.search.search` and `common.fetch.fetch_and_extract`
independently so we can validate:
- successful combined output
- partial-failure: 1 of 2 fetches fails, the good one still comes through
- total-failure: all fetches fail → is_error=true
- no-results: empty DDG result set → friendly message
- hard cap enforced on combined output
- per-result cap is respected
- search-level errors (rate-limit) propagate as is_error=true
- arg-key tolerance for query synonyms
- MCP stdio round-trip
"""

from __future__ import annotations

import io
import json

import pytest

from apfel_mcp.common.fetch import FetchError, FetchResult
from apfel_mcp.common.search import ResultRow, SearchError, SearchResult
from apfel_mcp.search_and_fetch_server import (
    HARD_CAP_CHARS_COMBINED,
    SERVER_NAME,
    TOOLS,
    _handle_tool_call,
    main,
    search_and_fetch,
)


def _mk_search_result(query: str, urls: list[str]) -> SearchResult:
    rows = [
        ResultRow(
            title=f"Title for {u}",
            url=u,
            snippet=f"Snippet for {u}",
            domain=u.replace("https://", "").split("/")[0],
        )
        for u in urls
    ]
    return SearchResult(
        query=query,
        results=rows,
        formatted_text=f"Search: {query}\n(summary)",
        from_cache=False,
    )


def _mk_fetch_result(url: str, body: str = "article body text") -> FetchResult:
    return FetchResult(
        title=f"Title for {url}",
        final_url=url,
        body=f"Title for {url}\n{url}\n\n{body}",
        was_truncated=False,
    )


# --- Tool schema / server identity ---

def test_server_name_matches_entry_point():
    assert SERVER_NAME == "apfel-mcp-search-and-fetch"


def test_tools_list_contains_search_and_web_search():
    """The compound MCP exposes two declared tool names.

    - `search` is the primary entry, with full description.
    - `web_search` is an alias the 3B model frequently hallucinates
      when the user says "search the web". Apfel's MCP client filters
      tools/call by declared names, so the alias must be declared
      upstream - handler-level aliases would be unreachable.
    """
    names = {t["name"] for t in TOOLS}
    assert names == {"search", "web_search"}
    for tool in TOOLS:
        assert "query" in tool["inputSchema"]["required"]


def test_primary_tool_description_explains_the_compound_behavior():
    primary = next(t for t in TOOLS if t["name"] == "search")
    desc = primary["description"].lower()
    assert "search" in desc
    assert "fetch" in desc
    assert "duckduckgo" in desc


# --- Core search_and_fetch logic ---

def test_empty_query_raises():
    with pytest.raises(SearchError, match=r"empty|query"):
        search_and_fetch("")


def test_results_clamped_to_maximum_of_three(monkeypatch):
    """results=99 clamps to 3. Otherwise we'd blow the token budget."""
    call_log: dict = {}

    def fake_search(query, max_results):
        call_log["max_results"] = max_results
        return _mk_search_result(query, [f"https://ex{i}.com/" for i in range(max_results)])

    def fake_fetch(url, max_chars):
        return _mk_fetch_result(url)

    monkeypatch.setattr("apfel_mcp.search_and_fetch_server.search", fake_search)
    monkeypatch.setattr("apfel_mcp.search_and_fetch_server.fetch_and_extract", fake_fetch)

    search_and_fetch("test", results=99)
    assert call_log["max_results"] == 3


def test_all_successful_fetches_returns_combined_output(monkeypatch):
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(
            query, ["https://a.com/", "https://b.com/"]
        ),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url, body=f"body of {url}"),
    )

    result = search_and_fetch("test", results=2)
    assert "Search: test" in result
    assert "https://a.com/" in result
    assert "https://b.com/" in result
    assert "body of https://a.com/" in result
    assert "body of https://b.com/" in result


def test_partial_fetch_failure_inlines_error_and_continues(monkeypatch):
    """1 of 2 fetches fails. The good one still renders; the bad one gets a note."""
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(
            query, ["https://good.com/", "https://bad.com/"]
        ),
    )

    def fake_fetch(url, max_chars):
        if "bad" in url:
            raise FetchError("HTTP 503 from bad.com")
        return _mk_fetch_result(url, body="good content")

    monkeypatch.setattr("apfel_mcp.search_and_fetch_server.fetch_and_extract", fake_fetch)

    result = search_and_fetch("test", results=2)
    assert "good content" in result
    assert "https://bad.com/" in result
    assert "fetch failed" in result.lower() or "503" in result


def test_all_fetches_fail_raises_search_error(monkeypatch):
    """Every fetch fails. The model should see an error, not an empty result."""
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(
            query, ["https://a.com/", "https://b.com/"]
        ),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: (_ for _ in ()).throw(FetchError("boom")),
    )

    with pytest.raises(SearchError, match=r"all fetches failed"):
        search_and_fetch("test", results=2)


def test_no_search_results_returns_no_results_message(monkeypatch):
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: SearchResult(
            query=query,
            results=[],
            formatted_text=f"No results for: {query}",
            from_cache=False,
        ),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url),
    )
    result = search_and_fetch("nothingburger xyzzy")
    assert "No results" in result
    assert "nothingburger xyzzy" in result


def test_hard_cap_enforced_on_combined_output(monkeypatch):
    """Even with 3 maxed-out results, total output must stay under the hard cap."""
    huge_body = "word " * 5000  # ~25000 chars per result

    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(
            query, [f"https://ex{i}.com/" for i in range(3)]
        ),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url, body=huge_body[:max_chars]),
    )

    result = search_and_fetch("test", results=3, max_chars_per_result=2500)
    assert len(result) <= HARD_CAP_CHARS_COMBINED


def test_max_chars_per_result_respected(monkeypatch):
    """Per-result cap is forwarded to fetch_and_extract."""
    call_log: list = []

    def fake_fetch(url, max_chars):
        call_log.append(max_chars)
        return _mk_fetch_result(url)

    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(query, ["https://a.com/"]),
    )
    monkeypatch.setattr("apfel_mcp.search_and_fetch_server.fetch_and_extract", fake_fetch)

    search_and_fetch("test", results=1, max_chars_per_result=1500)
    assert call_log[0] == 1500


def test_search_rate_limit_propagates_as_search_error(monkeypatch):
    def raise_ratelimit(query, max_results):
        raise SearchError("search unavailable: DuckDuckGo rate-limited this request.")

    monkeypatch.setattr("apfel_mcp.search_and_fetch_server.search", raise_ratelimit)

    with pytest.raises(SearchError, match=r"rate"):
        search_and_fetch("test")


# --- MCP protocol layer ---

def test_handle_tool_call_runs_compound_and_returns_text(monkeypatch):
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(query, ["https://a.com/"]),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url, body="hello"),
    )
    result = _handle_tool_call("search",{"query": "apfel"})
    assert result.is_error is False
    assert "hello" in result.text


def test_handle_tool_call_missing_query_returns_error():
    result = _handle_tool_call("search",{})
    assert result.is_error is True
    assert "query" in result.text.lower()


def test_handle_tool_call_tolerates_topic_synonym(monkeypatch):
    """Max tolerance: model invents `topic` instead of `query`."""
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(query, ["https://a.com/"]),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url),
    )
    result = _handle_tool_call("search",{"topic": "apfel"})
    assert result.is_error is False


def test_handle_tool_call_unknown_tool_returns_error():
    result = _handle_tool_call("not_search_and_fetch", {"query": "x"})
    assert result.is_error is True
    assert "unknown tool" in result.text.lower()


def test_handle_tool_call_search_error_becomes_is_error_true(monkeypatch):
    def raise_err(query, max_results):
        raise SearchError("DuckDuckGo rate-limited this request")

    monkeypatch.setattr("apfel_mcp.search_and_fetch_server.search", raise_err)
    result = _handle_tool_call("search",{"query": "apfel"})
    assert result.is_error is True
    assert "rate" in result.text.lower()


def test_handle_tool_call_accepts_search_and_fetch_alias(monkeypatch):
    """Fallback: model calls the old name. Should still dispatch."""
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(query, ["https://a.com/"]),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url, body="alias body"),
    )
    result = _handle_tool_call("search_and_fetch", {"query": "apfel"})
    assert result.is_error is False
    assert "alias body" in result.text


def test_handle_tool_call_accepts_web_search_alias(monkeypatch):
    """Fallback: model hallucinated `web_search`. Should still dispatch."""
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(query, ["https://a.com/"]),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url, body="web_search body"),
    )
    result = _handle_tool_call("web_search", {"query": "apfel"})
    assert result.is_error is False
    assert "web_search body" in result.text


def test_server_stdio_round_trip_initialize_then_compound_call(monkeypatch):
    """Full run_server drive via StringIO."""
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.search",
        lambda query, max_results: _mk_search_result(query, ["https://a.com/"]),
    )
    monkeypatch.setattr(
        "apfel_mcp.search_and_fetch_server.fetch_and_extract",
        lambda url, max_chars: _mk_fetch_result(url, body="stdio body"),
    )

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": "apfel"},
            },
        },
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()

    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    main()

    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(responses) == 3
    assert responses[0]["result"]["serverInfo"]["name"] == "apfel-mcp-search-and-fetch"
    tools = responses[1]["result"]["tools"]
    assert tools[0]["name"] == "search"
    call = responses[2]["result"]
    assert call["isError"] is False
    assert "stdio body" in call["content"][0]["text"]
