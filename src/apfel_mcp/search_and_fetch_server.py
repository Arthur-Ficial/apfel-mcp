"""apfel-mcp-search-and-fetch: compound search + fetch MCP.

THE token-budget win for apfel's 4096-token context window. Chaining
separate search and fetch tool calls means the model pays the tool-schema
overhead twice, emits tool-calls JSON twice, and keeps intermediate state
in conversation history. This compound tool does everything in one round:

1. DDG search for the query
2. Fetch the top N result pages (Readability-extracted)
3. Return combined content in one response

The model asks "what's happening with X?" and gets an answer without
coaching through two tool calls. Saves ~400-600 tokens of conversation
state per use.

Protocol: MCP 2025-06-18 (stdio, JSON-RPC 2.0).
Shares `common.search`, `common.fetch`, and `common.arg_tolerance` with
the standalone MCPs. No code duplication.
"""

from __future__ import annotations

from typing import Any

from apfel_mcp import __version__
from apfel_mcp.common.arg_tolerance import extract_int, extract_string
from apfel_mcp.common.fetch import FetchError, fetch_and_extract
from apfel_mcp.common.mcp_protocol import ToolResult, run_server
from apfel_mcp.common.search import SearchError, search

SERVER_NAME = "apfel-mcp-search-and-fetch"
SERVER_VERSION = __version__

# --- Token budget constants ---

DEFAULT_RESULTS: int = 2
MIN_RESULTS: int = 1
MAX_RESULTS: int = 3  # hard max - more blows the token budget

DEFAULT_MAX_CHARS_PER_RESULT: int = 1800
MAX_CHARS_PER_RESULT_HARD_CAP: int = 2500
HARD_CAP_CHARS_COMBINED: int = 5000  # total output cannot exceed this


PRIMARY_TOOL_NAME = "search"
"""The primary tool name.

The 3B model's instinct for a search-intent tool is `search` or
`web_search`. We declare BOTH in tools/list because apfel's MCP client
filters tools/call by the declared names. Whatever the model picks,
our handler routes it to the same implementation.
"""

_PRIMARY_DESCRIPTION = (
    "Search the web and fetch the top result pages in one tool call. "
    "Uses DuckDuckGo for search and Readability to extract each page's "
    "clean article body. Returns search hits PLUS page content combined "
    "into one response - no need to chain a separate fetch call. "
    "Default 2 results, 1800 chars each, 5000 chars total - tuned for "
    "apfel's 4096-token context window. "
    "Example: search(query='Swift 7 release date', results=2)"
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "search keywords",
        },
        "results": {
            "type": "integer",
            "description": (
                f"How many top results to fetch. Default {DEFAULT_RESULTS}. "
                f"Max {MAX_RESULTS}."
            ),
        },
        "max_chars_per_result": {
            "type": "integer",
            "description": (
                f"Chars of article body per result. Default "
                f"{DEFAULT_MAX_CHARS_PER_RESULT}. Hard cap "
                f"{MAX_CHARS_PER_RESULT_HARD_CAP}."
            ),
        },
    },
    "required": ["query"],
}

_ACCEPTED_TOOL_NAMES: frozenset[str] = frozenset(
    {"search", "web_search", "search_and_fetch"}
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": _PRIMARY_DESCRIPTION,
        "inputSchema": _INPUT_SCHEMA,
    },
    {
        # Alias: the 3B model sometimes hallucinates `web_search` when
        # the user prompt says "search the web". Apfel's MCP client
        # filters tools/call by declared names, so we must declare this
        # alias upstream. Description is minimal to save schema tokens.
        "name": "web_search",
        "description": "Alias for 'search'. Same behavior. Same arguments.",
        "inputSchema": _INPUT_SCHEMA,
    },
]


_QUERY_KEYS = (
    "query", "q", "text", "search", "keywords", "terms", "term",
    "input", "prompt", "question", "ask", "phrase", "string", "s",
    "topic", "subject", "about", "for",
)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def search_and_fetch(
    query: str,
    results: int = DEFAULT_RESULTS,
    max_chars_per_result: int = DEFAULT_MAX_CHARS_PER_RESULT,
) -> str:
    """Run search + fetch for the top N results and return combined text.

    Raises:
        SearchError: empty query, DDG-level error, or ALL fetches failed.
        (FetchError on individual results is caught and inlined as a
        per-result error note; the overall call still succeeds as long
        as at least one fetch worked.)
    """
    stripped = (query or "").strip()
    if not stripped:
        raise SearchError("search_and_fetch: query must be non-empty")

    n_results = _clamp(int(results), MIN_RESULTS, MAX_RESULTS)
    per_result_cap = _clamp(
        int(max_chars_per_result), 200, MAX_CHARS_PER_RESULT_HARD_CAP
    )

    # Step 1: search. Any SearchError propagates to the caller.
    search_result = search(stripped, max_results=n_results)

    if not search_result.results:
        # DDG returned nothing. Give the model a friendly no-results block.
        return f"Search: {stripped}\n\nNo results for: {stripped}"

    # Step 2: fetch each URL. Track successes and failures separately so
    # we can tell whether EVERYTHING failed (which raises) vs partial
    # failure (which inlines error notes).
    blocks: list[str] = [f"Search: {stripped}", ""]
    successes = 0
    failures = 0

    for idx, row in enumerate(search_result.results[:n_results], start=1):
        header = f"=== Result {idx}: {row.title} ==="
        blocks.append(header)

        if not row.url:
            blocks.append("[fetch failed: result had no url]")
            blocks.append("")
            failures += 1
            continue

        try:
            fetched = fetch_and_extract(row.url, max_chars=per_result_cap)
        except FetchError as exc:
            blocks.append(row.url)
            blocks.append(f"[fetch failed: {exc}]")
            blocks.append("")
            failures += 1
            continue
        except Exception as exc:  # unexpected - still recover
            blocks.append(row.url)
            blocks.append(f"[fetch failed: {type(exc).__name__}: {exc}]")
            blocks.append("")
            failures += 1
            continue

        blocks.append(fetched.body)
        blocks.append("")
        successes += 1

    if successes == 0:
        raise SearchError(
            f"search_and_fetch: all fetches failed for query {stripped!r} "
            f"({failures} attempted)"
        )

    combined = "\n".join(blocks).rstrip()

    # Step 3: hard cap on combined output. Even if each per-result cap was
    # respected, the combined text could still overshoot the total cap.
    if len(combined) > HARD_CAP_CHARS_COMBINED:
        combined = (
            combined[: HARD_CAP_CHARS_COMBINED - 40]
            + "\n\n[... truncated to fit token budget]"
        )
    return combined


def _handle_tool_call(name: str, args: dict[str, Any]) -> ToolResult:
    """MCP tool handler for search_and_fetch.

    All errors wrapped as ToolResult(is_error=True). Partial failures
    (some fetches succeed) are NOT errors - the model gets the successful
    ones plus inline notes about the failures.
    """
    if name not in _ACCEPTED_TOOL_NAMES:
        return ToolResult(text=f"unknown tool: {name}", is_error=True)

    query = extract_string(args, _QUERY_KEYS)
    if not query:
        return ToolResult(
            text=(
                "search: the 'query' argument is required. "
                "Please retry with your actual search keywords, "
                "e.g. search(query='apple silicon m4 release date')."
            ),
            is_error=True,
        )

    n_results = extract_int(
        args,
        ("results",),
        default=DEFAULT_RESULTS,
        exclude_keys=("max_chars_per_result", "max_chars"),
    )
    per_result_cap = extract_int(
        args,
        ("max_chars_per_result", "max_chars"),
        default=DEFAULT_MAX_CHARS_PER_RESULT,
        exclude_keys=("results",),
    )
    # extract_int ignores values <= 0; a missing-but-present key means
    # we really do want the fetch default rather than the user's value.
    if per_result_cap > MAX_CHARS_PER_RESULT_HARD_CAP:
        per_result_cap = MAX_CHARS_PER_RESULT_HARD_CAP

    try:
        body = search_and_fetch(
            query,
            results=n_results,
            max_chars_per_result=per_result_cap,
        )
    except SearchError as exc:
        return ToolResult(text=str(exc), is_error=True)
    except Exception as exc:
        return ToolResult(
            text=f"unexpected error in search-and-fetch: {type(exc).__name__}: {exc}",
            is_error=True,
        )

    return ToolResult(text=body, is_error=False)


def main() -> None:
    """Entry point declared in pyproject.toml as apfel-mcp-search-and-fetch."""
    from apfel_mcp._test_mode import install_if_requested

    install_if_requested()
    run_server(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        tool_handler=_handle_tool_call,
    )


if __name__ == "__main__":
    main()
