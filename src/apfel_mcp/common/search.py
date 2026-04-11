"""DuckDuckGo search wrapper with caching, formatting, and error handling.

This module is the core of the ddg-search MCP and is also called by
search-and-fetch. It wraps the unofficial `ddgs` library (formerly
`duckduckgo-search`, MIT) that scrapes DuckDuckGo's HTML endpoint.

DuckDuckGo has NO public web-search API. The Instant Answer API
(`api.duckduckgo.com`) returns only summary boxes. Scraping
`html.duckduckgo.com/html` is the only way to get real 10-blue-links
results. It is unsanctioned but widely used - OpenClaw's DDG extension
(MIT) takes the same approach and documents it as "experimental,
unofficial, fragile". We adopt that framing.

On top of ddgs this module adds:
1. A 60-second in-memory cache keyed by (query, max_results). Reduces
   DDG load when the model searches the same thing twice in one session.
2. Token-budget-aware formatting: title truncated to 80 chars, snippet
   to 160 chars, total output hard-capped at 2000 chars.
3. Friendly error messages for rate-limit / bot-challenge exceptions
   so the model sees "wait a moment" rather than a raw stack trace.

The cache is process-local (plain module-level dict). Lives for the
lifetime of the MCP subprocess. No persistence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ddgs import DDGS

# --- Configuration constants ---

HARD_CAP_CHARS: int = 2000
CACHE_TTL_SECONDS: int = 60
DEFAULT_MAX_RESULTS: int = 5
MIN_MAX_RESULTS: int = 1
MAX_MAX_RESULTS: int = 10

TITLE_MAX_CHARS: int = 80
SNIPPET_MAX_CHARS: int = 160


class SearchError(Exception):
    """User-facing search error. The message becomes the tool result text."""


@dataclass
class ResultRow:
    """One parsed ddgs result row, normalized and truncated."""

    title: str
    url: str
    snippet: str
    domain: str


@dataclass
class SearchResult:
    """The full search output returned by `search()`.

    `results` is the structured list of rows (useful for programmatic
    callers like search_and_fetch). `formatted_text` is the plain-text
    block the MCP server returns to the model.
    """

    query: str
    results: list[ResultRow] = field(default_factory=list)
    formatted_text: str = ""
    from_cache: bool = False


# --- Cache ---

# Module-level cache: { (query_normalized, max_results): (inserted_at, SearchResult) }
_CACHE: dict[tuple[str, int], tuple[float, SearchResult]] = {}


def _clear_cache() -> None:
    """Drop all cache entries. Exposed for tests."""
    _CACHE.clear()


def _cache_get(key: tuple[str, int]) -> SearchResult | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    inserted_at, response = entry
    if time.monotonic() - inserted_at > CACHE_TTL_SECONDS:
        del _CACHE[key]
        return None
    return response


def _cache_set(key: tuple[str, int], response: SearchResult) -> None:
    _CACHE[key] = (time.monotonic(), response)


# --- ddgs indirection (so tests can patch) ---

def _ddgs_text(query: str, max_results: int) -> list[dict[str, Any]]:
    """Thin wrapper around `ddgs.DDGS().text(...)`.

    Exists as a module-level function so tests can patch it without
    touching the ddgs class. On failure, the underlying ddgs exception
    propagates; `search()` wraps it into a SearchError.
    """
    with DDGS() as ddgs:
        return ddgs.text(query, max_results=max_results, region="wt-wt", safesearch="moderate")


# --- Formatting ---

def _truncate(text: str, limit: int) -> str:
    """Truncate with an ellipsis if longer than limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _domain_from_url(url: str) -> str:
    """Extract the bare domain from an href, stripping www. prefix."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.netloc or parsed.path or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _parse_row(row: dict[str, Any]) -> ResultRow:
    """Normalize one ddgs row into our internal struct."""
    raw_title = (row.get("title") or "").strip()
    raw_url = (row.get("href") or row.get("url") or "").strip()
    raw_body = (row.get("body") or row.get("snippet") or "").strip()

    return ResultRow(
        title=_truncate(raw_title, TITLE_MAX_CHARS),
        url=raw_url,
        snippet=_truncate(raw_body, SNIPPET_MAX_CHARS),
        domain=_domain_from_url(raw_url),
    )


def _format_results(query: str, rows: list[ResultRow]) -> str:
    """Render the formatted_text block. Empty rows list gets a friendly message."""
    if not rows:
        return f"No results for: {query}"

    lines = [f"Search: {query}", ""]
    for idx, row in enumerate(rows, start=1):
        lines.append(f"{idx}. {row.title}")
        if row.snippet:
            lines.append(f"   {row.snippet}")
        if row.domain:
            lines.append(f"   {row.domain}")
        lines.append("")

    body = "\n".join(lines).rstrip()

    # Hard cap on the joined output. Simple truncation because the per-row
    # caps already keep us well under 2000 chars in practice.
    if len(body) > HARD_CAP_CHARS:
        body = body[: HARD_CAP_CHARS - 1] + "\u2026"
    return body


# --- Main entry point ---

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _looks_like_ratelimit(exc: BaseException) -> bool:
    """Heuristic: does this exception message mention rate-limiting?

    ddgs raises `DuckDuckGoSearchException` with messages like "Ratelimit"
    or "202 Ratelimit" when DDG throttles. We also treat "429" the same
    way.
    """
    msg = str(exc).lower()
    return "ratelimit" in msg or "rate limit" in msg or "429" in msg


def _looks_like_bot_challenge(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "bot" in msg or "challenge" in msg or "captcha" in msg


def search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> SearchResult:
    """Run a DuckDuckGo search and return formatted results.

    Args:
        query: search keywords. Leading/trailing whitespace is stripped.
        max_results: how many results to return. Clamped to [1, 10].

    Returns:
        A SearchResult with the normalized result rows, the plain-text
        block to return to the model, and a `from_cache` flag.

    Raises:
        SearchError: empty query, or ddgs raised an exception. The error
            message is user-facing and will be surfaced as the MCP tool
            result text with `isError=true`.
    """
    stripped = (query or "").strip()
    if not stripped:
        raise SearchError("search: query must be non-empty")

    clamped = _clamp(int(max_results), MIN_MAX_RESULTS, MAX_MAX_RESULTS)
    cache_key = (stripped, clamped)

    cached = _cache_get(cache_key)
    if cached is not None:
        # Return a shallow copy with from_cache=True so callers can see
        # they got a hit without mutating the cached entry.
        return SearchResult(
            query=cached.query,
            results=list(cached.results),
            formatted_text=cached.formatted_text,
            from_cache=True,
        )

    try:
        raw_rows = _ddgs_text(stripped, clamped)
    except SearchError:
        raise
    except Exception as exc:
        if _looks_like_ratelimit(exc):
            raise SearchError(
                "search unavailable: DuckDuckGo rate-limited this request. "
                "Try again in a moment."
            ) from exc
        if _looks_like_bot_challenge(exc):
            raise SearchError(
                "search temporarily unavailable: DuckDuckGo is serving a "
                "bot-challenge page. This happens when the unofficial scraping "
                "endpoint detects automated traffic. Wait a few minutes."
            ) from exc
        raise SearchError(f"search failed: {exc}") from exc

    rows = [_parse_row(row) for row in (raw_rows or [])]
    formatted = _format_results(stripped, rows)

    response = SearchResult(
        query=stripped,
        results=rows,
        formatted_text=formatted,
        from_cache=False,
    )
    _cache_set(cache_key, response)
    return response
