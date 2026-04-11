"""URL fetch + Readability extraction + token-budget truncation.

This module is the core of the url-fetch MCP and is also called by
search-and-fetch. It takes a URL, validates it against the SSRF
blocklist, fetches the HTML via httpx, extracts the main article
body via Readability (falling back to BeautifulSoup for pages with
no <article> structure), converts to Markdown via markdownify, and
hard-caps the output via apfel_mcp.common.budget.

SSRF protection: `http` and `https` schemes only, private-network
blocklist for 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
127.0.0.0/8, 169.254.0.0/16 (link-local / AWS metadata), and IPv6
::1, fc00::/7, fe80::/10. Hostnames are resolved via
socket.getaddrinfo before the fetch and each resolved IP is checked
against the blocklist. The blocklist can be disabled for local-dev
use via the URL_FETCH_ALLOW_PRIVATE_NETWORKS=1 environment variable.

Download limits: 10-second timeout, 2 MB max response body. Cookies
are not stored. The User-Agent identifies the client honestly.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from readability import Document

from apfel_mcp.common.budget import truncate_to

# httpx's default `verify=True` builds its own SSLContext from certifi.
# On macOS + Python 3.14 + Homebrew OpenSSL, that path fails with
# "unable to get local issuer certificate". Using `ssl.create_default_context()`
# directly picks up the OS trust store (Homebrew OpenSSL's cert.pem on macOS,
# system CA bundle on Linux) and Just Works on both platforms.
_DEFAULT_SSL_CONTEXT = ssl.create_default_context()

# --- Configuration constants ---

DEFAULT_MAX_CHARS: int = 4000
HARD_CAP_CHARS: int = 6000
MAX_DOWNLOAD_BYTES: int = 2_000_000
REQUEST_TIMEOUT_SECONDS: float = 10.0
USER_AGENT: str = "apfel-mcp/url-fetch/0.1.0 (+https://github.com/Arthur-Ficial/apfel-mcp)"

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, incl. AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
)

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost"}
)


class FetchError(Exception):
    """User-facing fetch error. The message becomes the tool result text."""


@dataclass
class FetchResult:
    """The result of a fetch + extract operation.

    `body` is the final formatted text that should be returned to the
    LLM: it includes the title, final URL, the extracted article body,
    and (if truncated) the truncation suffix.
    """

    title: str
    final_url: str
    body: str
    was_truncated: bool


def fetch_and_extract(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> FetchResult:
    """Fetch a URL and return its cleaned article body as Markdown.

    Args:
        url: http:// or https:// URL to fetch.
        max_chars: soft cap on output length. Capped at HARD_CAP_CHARS
            regardless of what the caller supplies.

    Returns:
        FetchResult with title, final URL, Markdown body (truncated if
        needed), and was_truncated flag.

    Raises:
        FetchError: scheme rejected, SSRF blocklist hit, network error,
            non-HTML content type, oversize response, or empty extraction.
    """
    _validate_url(url)
    effective_cap = min(max_chars, HARD_CAP_CHARS)

    # Fetch. See the _DEFAULT_SSL_CONTEXT docstring above for why we pass an
    # explicit SSLContext instead of relying on httpx's default verify=True.
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            cookies=None,
            headers={"User-Agent": USER_AGENT},
            verify=_DEFAULT_SSL_CONTEXT,
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise FetchError(f"fetch failed: {exc}") from exc

    if response.status_code >= 400:
        raise FetchError(
            f"HTTP {response.status_code} from {url}: "
            f"{response.reason_phrase or 'error'}"
        )

    final_url = str(response.url)

    # Content-type check: must be HTML-ish
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        raise FetchError(
            f"not HTML: content-type={content_type or '(missing)'}"
        )

    # Size check
    if len(response.content) > MAX_DOWNLOAD_BYTES:
        raise FetchError(
            f"response too large: {len(response.content)} bytes > "
            f"{MAX_DOWNLOAD_BYTES} limit"
        )

    html = response.text
    title, body_md = _extract(html)

    if not body_md.strip():
        raise FetchError("page has no extractable text content")

    # Assemble final output: title, URL, blank line, body
    header = f"{title}\n{final_url}\n\n"
    full = header + body_md
    truncated_text, was_truncated = truncate_to(full, effective_cap)

    return FetchResult(
        title=title,
        final_url=final_url,
        body=truncated_text,
        was_truncated=was_truncated,
    )


_NOISY_TAGS = ("script", "style", "nav", "footer", "aside", "header", "form")


def _extract(html: str) -> tuple[str, str]:
    """Extract (title, body_markdown) from raw HTML.

    Tries Readability first. In BOTH code paths (Readability and fallback),
    we run a BeautifulSoup pass to strip noisy tags (script, style, nav,
    footer, aside, header, form) before producing the final Markdown.
    Readability's content scoring is good but not perfect — it sometimes
    includes footer content when the main article is short. Stripping
    noisy tags as a post-processing step gives us cleaner output in all
    cases.

    Falls back to pure BeautifulSoup plain-text if Readability produced
    nothing useful (< 200 chars after stripping).
    """
    title = "Untitled"
    body_md = ""

    # Readability pass
    try:
        doc = Document(html)
        readability_title = (doc.title() or "").strip()
        if readability_title:
            title = readability_title
        body_html = doc.summary() or ""
        if body_html:
            # Strip noisy tags from Readability's output before converting
            # to Markdown. Readability sometimes keeps a <footer> if the
            # article is short relative to the boilerplate.
            stripped_html = _strip_noisy_tags_from_html(body_html)
            body_md = markdownify(stripped_html).strip()
    except Exception:
        # Readability can raise on malformed HTML. Fall through to BS4.
        pass

    # BeautifulSoup fallback if Readability produced nothing useful
    if len(body_md) < 200:
        soup = BeautifulSoup(html, "html.parser")

        if title == "Untitled":
            title_tag = soup.find("title")
            if title_tag and title_tag.text.strip():
                title = title_tag.text.strip()

        for tag in soup(_NOISY_TAGS):
            tag.decompose()

        text = soup.get_text(separator=" ")
        body_md = " ".join(text.split())

    return title, body_md


def _strip_noisy_tags_from_html(html_fragment: str) -> str:
    """Remove script/style/nav/footer/aside/header/form from an HTML string.

    Applied to Readability's output before Markdown conversion so boilerplate
    that survived Readability's content-scoring gets removed.
    """
    soup = BeautifulSoup(html_fragment, "html.parser")
    for tag in soup(_NOISY_TAGS):
        tag.decompose()
    return str(soup)


def _validate_url(url: str) -> None:
    """Validate scheme, extract host, check SSRF blocklist.

    Raises FetchError with a user-visible message on any violation.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise FetchError(f"invalid URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise FetchError(
            f"url-fetch: only http(s) URLs allowed, got scheme: "
            f"{scheme or '(none)'}"
        )

    host = parsed.hostname
    if not host:
        raise FetchError(f"invalid URL: missing host in {url}")

    # Blocklist bypass for local dev
    if os.environ.get("URL_FETCH_ALLOW_PRIVATE_NETWORKS"):
        return

    # Explicit hostname blocklist (localhost variants)
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise FetchError(
            "url-fetch: localhost blocked "
            "(set URL_FETCH_ALLOW_PRIVATE_NETWORKS=1 to override)"
        )

    # Resolve all addresses for this host and check each
    resolved_ips = _resolve_host(host)
    for ip_str in resolved_ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for network in _PRIVATE_NETWORKS:
            if ip_obj in network:
                raise FetchError(
                    f"url-fetch: {host} resolves to private network "
                    f"({ip_obj} in {network}) - blocked "
                    f"(set URL_FETCH_ALLOW_PRIVATE_NETWORKS=1 to override)"
                )


def _resolve_host(host: str) -> list[str]:
    """Return all IP addresses a host resolves to, or raise FetchError.

    If the host IS a literal IP address (v4 or v6), returns [host] without
    consulting DNS. Otherwise calls socket.getaddrinfo and returns all
    resolved addresses (A + AAAA).
    """
    # Strip IPv6 brackets if present: [::1] → ::1
    clean_host = host
    if clean_host.startswith("[") and clean_host.endswith("]"):
        clean_host = clean_host[1:-1]

    # Is it already a literal IP?
    try:
        ipaddress.ip_address(clean_host)
        return [clean_host]
    except ValueError:
        pass

    # Not a literal IP - resolve via DNS
    try:
        addrinfo = socket.getaddrinfo(clean_host, None)
    except OSError as exc:
        raise FetchError(f"could not resolve host: {host} ({exc})") from exc

    # sockaddr[0] is the address string for both IPv4 and IPv6 (first tuple element).
    # Typeshed types it as str | int for historical reasons; at runtime it's always str.
    return [str(sockaddr[0]) for _, _, _, _, sockaddr in addrinfo]
