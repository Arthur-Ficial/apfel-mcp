"""Tests for apfel_mcp.common.fetch.

The fetch module is the core of the url-fetch MCP. It takes a URL, fetches
the HTML, extracts the main article via Readability (with a BeautifulSoup
fallback), converts to Markdown, and truncates to fit apfel's 4096-token
context.

Critical: this module has SSRF guards. Any URL fetch from an LLM-driven
tool is a potential SSRF vector (the model can be prompted to fetch
internal URLs). We validate the scheme (http/https only), resolve the
hostname, and block any resolved IP in a private network range. These
tests mock DNS so they are hermetic.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from apfel_mcp.common.fetch import (
    DEFAULT_MAX_CHARS,
    HARD_CAP_CHARS,
    FetchError,
    fetch_and_extract,
)

# A small real-looking HTML fixture with header, nav, article, footer.
# Readability should extract the article and drop the boilerplate.
_REAL_HTML = """
<!DOCTYPE html>
<html>
<head><title>Example Article Title</title></head>
<body>
<header>
  <nav><a href="/">Home</a> | <a href="/about">About</a></nav>
</header>
<article>
<h1>Example Article Title</h1>
<p>This is the first paragraph of the main article body. It should be extracted
by Readability while the nav and footer are dropped. The article contains
multiple sentences across several paragraphs to make it clear that this is
the main content area of the page.</p>
<p>The second paragraph continues the article. Readability uses heuristics to
identify the main content block by scoring text density, link density, and
tag types. Boilerplate elements like nav and footer have high link density
and low text density, so they get dropped.</p>
<p>A third paragraph with additional content to ensure the article has enough
material to survive extraction, conversion to Markdown, and any truncation
that might otherwise produce an empty result.</p>
</article>
<footer>Copyright 2026. All rights reserved.</footer>
</body>
</html>
"""

# Fake DNS resolution: map hostnames to IP addresses we control in tests.
_FAKE_DNS = {
    "example.com": "93.184.216.34",  # real public IP of example.com
    "public.example.com": "93.184.216.34",
    "internal.example.com": "10.1.2.3",  # private network
    "metadata.local": "169.254.169.254",  # AWS metadata
}


def _fake_getaddrinfo(host: str, port, *args, **kwargs):
    """Replacement for socket.getaddrinfo that consults _FAKE_DNS."""
    import socket
    if host in _FAKE_DNS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_FAKE_DNS[host], port or 0))]
    # Unknown host
    raise OSError("name resolution failed")


# --- Scheme allowlist tests ---

@pytest.mark.parametrize(
    ("bad_url", "_reason"),
    [
        ("file:///etc/passwd", "file scheme"),
        ("javascript:alert(1)", "javascript scheme"),
        ("data:text/html,<h1>x</h1>", "data scheme"),
        ("ftp://ftp.example.com/file.txt", "ftp scheme"),
        ("gopher://example.com/", "gopher scheme"),
    ],
)
def test_non_http_schemes_are_rejected(bad_url, _reason):
    """Only http and https schemes are allowed. Everything else raises FetchError."""
    with pytest.raises(FetchError, match=r"only http"):
        fetch_and_extract(bad_url)


def test_url_with_missing_host_raises():
    """A URL with no host component is invalid."""
    with pytest.raises(FetchError):
        fetch_and_extract("http:///path")


# --- Private network blocklist tests ---

@pytest.mark.parametrize(
    "blocked_host",
    [
        "localhost",
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "::1",
    ],
)
def test_private_network_literal_ips_and_localhost_are_blocked(blocked_host):
    """Literal private-network IPs in the URL must be blocked."""
    url = f"http://[{blocked_host}]/" if ":" in blocked_host else f"http://{blocked_host}/"
    with patch("socket.getaddrinfo", _fake_getaddrinfo), pytest.raises(
        FetchError, match=r"private network|localhost"
    ):
        fetch_and_extract(url)


def test_hostname_resolving_to_private_ip_is_blocked():
    """A public-looking hostname that DNS-resolves to a private IP is blocked."""
    with patch("socket.getaddrinfo", _fake_getaddrinfo), pytest.raises(
        FetchError, match=r"private network"
    ):
        fetch_and_extract("https://internal.example.com/")


def test_private_network_blocked_for_aws_metadata_via_hostname():
    """Hostname that resolves to 169.254.169.254 (AWS metadata service) is blocked."""
    with patch("socket.getaddrinfo", _fake_getaddrinfo), pytest.raises(
        FetchError, match=r"private network|link-local"
    ):
        fetch_and_extract("https://metadata.local/")


def test_env_var_bypasses_private_network_blocklist(monkeypatch):
    """URL_FETCH_ALLOW_PRIVATE_NETWORKS=1 disables the blocklist for localhost dev."""
    monkeypatch.setenv("URL_FETCH_ALLOW_PRIVATE_NETWORKS", "1")
    # Still needs a working HTTP mock - use respx
    with respx.mock(base_url="http://127.0.0.1") as respx_mock:
        respx_mock.get("/").mock(
            return_value=httpx.Response(
                200,
                html=_REAL_HTML,
                headers={"content-type": "text/html"},
            )
        )
        with patch("socket.getaddrinfo", _fake_getaddrinfo):
            result = fetch_and_extract("http://127.0.0.1/")
    assert "Example Article" in result.body


# --- Successful fetch + extraction tests ---

@respx.mock
def test_successful_fetch_returns_title_final_url_body():
    """A normal HTML page is fetched, Readability extracts the article, Markdown is returned."""
    respx.get("https://example.com/article").mock(
        return_value=httpx.Response(
            200,
            html=_REAL_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo):
        result = fetch_and_extract("https://example.com/article")

    assert "Example Article Title" in result.body
    assert "https://example.com/article" in result.body
    # Boilerplate must be dropped
    assert "Home" not in result.body
    assert "About" not in result.body
    assert "Copyright 2026" not in result.body
    # Article body must be present
    assert "Readability" in result.body
    assert result.was_truncated is False


@respx.mock
def test_non_html_content_type_is_rejected():
    """Content-Type that isn't HTML triggers a FetchError."""
    respx.get("https://example.com/data.json").mock(
        return_value=httpx.Response(
            200,
            json={"hello": "world"},
            headers={"content-type": "application/json"},
        )
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo), pytest.raises(
        FetchError, match=r"not HTML"
    ):
        fetch_and_extract("https://example.com/data.json")


@respx.mock
def test_http_error_status_returns_fetch_error():
    """A 5xx response from the server surfaces as a FetchError with the status."""
    respx.get("https://example.com/dead").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo), pytest.raises(
        FetchError, match=r"500|HTTP"
    ):
        fetch_and_extract("https://example.com/dead")


@respx.mock
def test_network_error_returns_fetch_error():
    """An httpx RequestError (connection refused, timeout, etc.) surfaces as FetchError."""
    respx.get("https://example.com/unreachable").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo), pytest.raises(
        FetchError, match=r"fetch failed|Connection"
    ):
        fetch_and_extract("https://example.com/unreachable")


# --- Truncation tests ---

@respx.mock
def test_short_content_is_not_truncated():
    """Content shorter than DEFAULT_MAX_CHARS passes through without truncation suffix."""
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            html=_REAL_HTML,
            headers={"content-type": "text/html"},
        )
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo):
        result = fetch_and_extract("https://example.com/")
    assert result.was_truncated is False
    assert "[... truncated" not in result.body


@respx.mock
def test_long_content_is_truncated_to_max_chars():
    """Content longer than max_chars gets truncated with a visible suffix."""
    huge_html = "<html><head><title>Big</title></head><body><article>"
    huge_html += "<p>" + ("Long sentence with actual words. " * 200) + "</p>"
    huge_html += "</article></body></html>"
    respx.get("https://example.com/huge").mock(
        return_value=httpx.Response(
            200,
            html=huge_html,
            headers={"content-type": "text/html"},
        )
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo):
        result = fetch_and_extract("https://example.com/huge", max_chars=1000)
    assert len(result.body) <= 1000
    assert result.was_truncated is True
    assert "truncated" in result.body.lower()


@respx.mock
def test_hard_cap_cannot_be_exceeded_even_if_max_chars_is_larger():
    """User can request max_chars=999999 but the hard cap still applies."""
    huge_html = "<html><body><article>" + "<p>word " * 5000 + "</p></article></body></html>"
    respx.get("https://example.com/huger").mock(
        return_value=httpx.Response(
            200,
            html=huge_html,
            headers={"content-type": "text/html"},
        )
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo):
        result = fetch_and_extract("https://example.com/huger", max_chars=999_999)
    assert len(result.body) <= HARD_CAP_CHARS


@respx.mock
def test_beautifulsoup_fallback_when_readability_returns_empty():
    """If Readability returns < 200 chars, fall back to BeautifulSoup plaintext extraction."""
    # A page with no article tag, just a body with some text
    plain_html = """
<!DOCTYPE html>
<html>
<head><title>Plain Page</title></head>
<body>
<script>var x = 1;</script>
<p>This is the only paragraph on a page that has no article structure.
It should still be extractable via the BeautifulSoup fallback path even
though Readability might not find a scored content block here.</p>
</body>
</html>
"""
    respx.get("https://example.com/plain").mock(
        return_value=httpx.Response(
            200,
            html=plain_html,
            headers={"content-type": "text/html"},
        )
    )
    with patch("socket.getaddrinfo", _fake_getaddrinfo):
        result = fetch_and_extract("https://example.com/plain")
    assert "paragraph" in result.body.lower()
    # Script content must be stripped
    assert "var x" not in result.body


def test_default_max_chars_is_4000():
    """Regression check for the constant used throughout the plan."""
    assert DEFAULT_MAX_CHARS == 4000


def test_hard_cap_is_6000():
    """Regression check for the constant used throughout the plan."""
    assert HARD_CAP_CHARS == 6000
