"""apfel-mcp-ddg-search: MCP stdio server for DuckDuckGo web search.

Thin entry-point wrapper around apfel_mcp.common.search. The heavy
lifting (ddgs call, caching, formatting, token-budget enforcement)
lives in `common/search.py` so that `search_and_fetch_server.py` can
reuse it.

EXPERIMENTAL / UNOFFICIAL: DuckDuckGo has no public web-search API. The
underlying `ddgs` library scrapes `html.duckduckgo.com/html`, which is
unsanctioned but widely used. Expect occasional breakage from
bot-challenge pages or HTML changes. OpenClaw's DDG extension
(https://github.com/openclaw/openclaw/tree/main/extensions/duckduckgo,
MIT) takes the same approach and frames it the same way.

Usage from apfel:

    apfel --mcp $(which apfel-mcp-ddg-search) --chat

Protocol: MCP 2025-06-18 (stdio, JSON-RPC 2.0).
"""

from __future__ import annotations

from typing import Any

from apfel_mcp import __version__
from apfel_mcp.common.arg_tolerance import extract_int, extract_string
from apfel_mcp.common.mcp_protocol import ToolResult, run_server
from apfel_mcp.common.search import (
    DEFAULT_MAX_RESULTS,
    SearchError,
    search,
)

SERVER_NAME = "apfel-mcp-ddg-search"
SERVER_VERSION = __version__

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Search the web via DuckDuckGo and return the top results as a "
            "compact list (title, snippet, domain). Use for recent-news, "
            "fact-checking, or exploratory queries. EXPERIMENTAL and UNOFFICIAL: "
            "DuckDuckGo has no public API, so this scrapes their HTML endpoint. "
            "May occasionally fail with a rate-limit or bot-challenge error - "
            "just wait a moment and try again. Output is hard-capped at 2000 "
            "characters to fit apfel's 4096-token context. "
            "Example: search(query='Swift 7 release notes', max_results=5)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "search keywords",
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"How many results to return. Default {DEFAULT_MAX_RESULTS}. "
                        "Min 1, max 10."
                    ),
                },
            },
            "required": ["query"],
        },
    }
]


_QUERY_KEYS = (
    "query", "q", "text", "search", "keywords", "terms", "term",
    "input", "prompt", "question", "ask", "phrase", "string", "s",
    "topic", "subject", "about", "for",
)
"""Known argument-key synonyms for `query`.

Anything not in this list is still accepted via `extract_string`'s
fallback: any non-empty string value under any key not in COUNT_KEYS.
"""


def _handle_tool_call(name: str, args: dict[str, Any]) -> ToolResult:
    """Tool handler for the search tool.

    Tolerates the common argument-key synonyms the model invents
    (`q`, `text`, `search`, etc.) and wraps all errors as
    ToolResult(is_error=True) so the model can react rather than
    crashing the MCP subprocess.
    """
    if name != "search":
        return ToolResult(text=f"unknown tool: {name}", is_error=True)

    query = extract_string(args, _QUERY_KEYS)
    if not query:
        return ToolResult(
            text=(
                "search: missing 'query' argument. "
                "Call search(query='...') with your search keywords."
            ),
            is_error=True,
        )

    max_results = extract_int(args, ("max_results",), default=DEFAULT_MAX_RESULTS)

    try:
        result = search(query, max_results=max_results)
    except SearchError as exc:
        return ToolResult(text=str(exc), is_error=True)
    except Exception as exc:
        return ToolResult(
            text=f"unexpected error in ddg-search: {type(exc).__name__}: {exc}",
            is_error=True,
        )

    return ToolResult(text=result.formatted_text, is_error=False)


def main() -> None:
    """Entry point declared in pyproject.toml as apfel-mcp-ddg-search."""
    run_server(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        tool_handler=_handle_tool_call,
    )


if __name__ == "__main__":
    main()
