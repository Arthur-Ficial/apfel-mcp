"""apfel-mcp-url-fetch: MCP stdio server that fetches URLs and returns clean Markdown.

Thin entry-point wrapper around apfel_mcp.common.fetch. The heavy lifting
(SSRF guards, Readability extraction, token-budget truncation) lives in
`common/fetch.py` so that `search_and_fetch_server.py` can reuse it
without duplication.

Usage from apfel:

    apfel --mcp $(which apfel-mcp-url-fetch) "Summarize https://example.com"

Protocol: MCP 2025-06-18 (stdio, JSON-RPC 2.0). See
apfel_mcp.common.mcp_protocol for the dispatcher.
"""

from __future__ import annotations

from typing import Any

from apfel_mcp import __version__
from apfel_mcp.common.arg_tolerance import extract_int, extract_string
from apfel_mcp.common.fetch import (
    DEFAULT_MAX_CHARS,
    FetchError,
    fetch_and_extract,
)
from apfel_mcp.common.mcp_protocol import ToolResult, run_server

SERVER_NAME = "apfel-mcp-url-fetch"
SERVER_VERSION = __version__

TOOLS: list[dict[str, Any]] = [
    {
        "name": "fetch",
        "description": (
            "Fetch a web page and return its main article body as clean "
            "Markdown. Uses Readability to drop navigation, ads, and "
            "boilerplate. Optimized for apfel's 4096-token context "
            "window - output is hard-capped at 6000 characters. Only "
            "http:// and https:// URLs are accepted; private-network "
            "IPs are blocked as an SSRF guard. "
            "Example: fetch(url='https://example.com/article', max_chars=4000)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "http:// or https:// URL to fetch",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Soft cap on output length in characters. "
                        f"Default {DEFAULT_MAX_CHARS}. Hard cap 6000."
                    ),
                },
            },
            "required": ["url"],
        },
    }
]


_URL_KEYS = (
    "url", "link", "page", "address", "uri", "u",
    "href", "website", "site", "source", "target", "endpoint", "location",
)
"""Known argument-key synonyms for `url`.

Anything not in this list is still accepted via `extract_string`'s
fallback: any non-empty string value under any key not in COUNT_KEYS.
"""


def _handle_tool_call(name: str, args: dict[str, Any]) -> ToolResult:
    """Tool handler for the fetch tool.

    Maximum tolerance: any reasonable key the model invents for the URL
    argument gets accepted (see `extract_string`). Same for max_chars.
    All errors wrapped as ToolResult(is_error=True) so the model can
    react rather than crashing the MCP subprocess.
    """
    if name != "fetch":
        return ToolResult(text=f"unknown tool: {name}", is_error=True)

    url = extract_string(args, _URL_KEYS)
    if not url:
        return ToolResult(
            text=(
                "fetch: missing 'url' argument. "
                "Call fetch(url='https://...') with an http or https URL."
            ),
            is_error=True,
        )

    max_chars = extract_int(args, ("max_chars",), default=DEFAULT_MAX_CHARS)

    try:
        result = fetch_and_extract(url, max_chars=max_chars)
    except FetchError as exc:
        return ToolResult(text=str(exc), is_error=True)
    except Exception as exc:
        return ToolResult(
            text=f"unexpected error in url-fetch: {type(exc).__name__}: {exc}",
            is_error=True,
        )

    return ToolResult(text=result.body, is_error=False)


def main() -> None:
    """Entry point declared in pyproject.toml as apfel-mcp-url-fetch."""
    run_server(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        tool_handler=_handle_tool_call,
    )


if __name__ == "__main__":
    main()
