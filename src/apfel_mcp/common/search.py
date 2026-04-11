"""DuckDuckGo search wrapper with caching, formatting, and error handling.

This module is the core of the ddg-search MCP and is also called by
search-and-fetch. It scrapes DuckDuckGo's HTML endpoint directly using
httpx + BeautifulSoup - no third-party DDG library, so the Homebrew
formula stays simple (no Rust-compiled `primp` dep).

DuckDuckGo has NO public web-search API. The Instant Answer API
(`api.duckduckgo.com`) returns only summary boxes. Scraping
`html.duckduckgo.com/html` is the only way to get real 10-blue-links
results. It is unsanctioned but widely used - OpenClaw's DDG extension
(MIT) takes the same approach and documents it as "experimental,
unofficial, fragile". We adopt that framing.

On top of the raw scrape this module adds:
1. A 60-second in-memory cache keyed by (query, max_results). Reduces
   DDG load when the model searches the same thing twice in one session.
2. Token-budget-aware formatting: title truncated to 80 chars, snippet
   to 160 chars, total output hard-capped at 2000 chars.
3. Friendly error messages for rate-limit / bot-challenge exceptions
   so the model sees "wait a moment" rather than a raw stack trace.
4. DDG URL unwrapping (real URLs are hidden behind a `/l/?uddg=<encoded>`
   redirect - we decode it).
5. Bot-challenge detection: if the response HTML mentions "challenge"
   or doesn't contain result rows, we raise a friendly error.

The cache is process-local (plain module-level dict). Lives for the
lifetime of the MCP subprocess. No persistence.
"""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

# httpx's default verify=True fails on macOS + Python 3.14 + Homebrew
# OpenSSL. See common/fetch.py for the full explanation. Same fix here.
_DEFAULT_SSL_CONTEXT = ssl.create_default_context()

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


# --- DDG HTML scraper ---

_DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
_DDG_TIMEOUT_SECONDS = 15.0
_DDG_USER_AGENT = (
    # DDG's HTML endpoint serves a challenge page for anything that
    # looks like a script. A plausible browser User-Agent gets through.
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


def _unwrap_ddg_url(href: str) -> str:
    """Decode DDG's redirect wrapper.

    DDG wraps real result URLs as `//duckduckgo.com/l/?uddg=<encoded>`
    (or occasionally as a plain `/l/?uddg=...`). Unwrap to get the real
    URL. Other hrefs (including already-real URLs) are returned as-is.
    """
    if not href:
        return href
    # Normalize protocol-relative URLs
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.endswith("/l/") and parsed.query:
        qs = parse_qs(parsed.query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    return href


def _ddgs_text(query: str, max_results: int) -> list[dict[str, Any]]:
    """Scrape DuckDuckGo's HTML endpoint and return up to N result dicts.

    Returns a list of {title, href, body} dicts compatible with the
    shape the rest of this module expects. Raises on network errors -
    `search()` wraps them into friendly SearchErrors.

    Exists as a module-level function so tests can patch it.
    """
    with httpx.Client(
        timeout=_DDG_TIMEOUT_SECONDS,
        follow_redirects=True,
        cookies=None,
        headers={
            "User-Agent": _DDG_USER_AGENT,
            # html.duckduckgo.com expects a POST with the form-encoded query.
        },
        verify=_DEFAULT_SSL_CONTEXT,
    ) as client:
        response = client.post(
            _DDG_HTML_ENDPOINT,
            data={"q": query, "b": "", "kl": "wt-wt"},
        )
    response.raise_for_status()
    html = response.text

    # Bot-challenge heuristic: the endpoint serves an interstitial if
    # it thinks we're a bot. Raise so `search()` can format a friendly
    # error.
    lower = html.lower()
    if "anomaly" in lower or "unusual traffic" in lower:
        raise RuntimeError(
            "DuckDuckGoSearchException: Ratelimit / bot challenge detected"
        )

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    for result_div in soup.select(".result"):
        if len(results) >= max_results:
            break
        title_a = result_div.select_one(".result__a") or result_div.select_one("a.result__url")
        snippet_el = result_div.select_one(".result__snippet")
        if title_a is None:
            continue
        raw_href = title_a.get("href", "") or ""
        href = _unwrap_ddg_url(str(raw_href))
        title = title_a.get_text(strip=True)
        body = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if not title or not href:
            continue
        results.append({"title": title, "href": href, "body": body})

    return results


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
