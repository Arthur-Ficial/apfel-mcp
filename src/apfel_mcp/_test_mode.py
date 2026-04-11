"""Test-mode hook for subprocess-based integration tests.

When the `APFEL_MCP_TEST_MODE` environment variable is set, importing
this module replaces the network-touching functions in `common.search`
and `common.fetch` with canned responses. Used by
`tests/test_integration_stdio.py` so subprocess round-trip tests don't
hit the real network.

This module has no effect unless `APFEL_MCP_TEST_MODE` is truthy. Each
MCP server's `main()` imports and calls `install_if_requested()` before
launching the dispatcher.
"""

from __future__ import annotations

import os
from typing import Any


def install_if_requested() -> None:
    """Patch network callers if APFEL_MCP_TEST_MODE is set.

    Callers: every `main()` in the three server entry points. Safe to
    call unconditionally - does nothing when the env var is unset.

    NOTE on the patching strategy: the server modules use
    `from apfel_mcp.common.fetch import fetch_and_extract`, which binds
    a local name in the server module. Patching `fetch_mod.fetch_and_extract`
    does not update the already-bound name in the server module. So we
    must ALSO patch each server module's globals directly (by importing
    them and reassigning). This is ugly but it's an integration-test
    scaffold, not production code.
    """
    if not os.environ.get("APFEL_MCP_TEST_MODE"):
        return

    import apfel_mcp.common.fetch as fetch_mod
    import apfel_mcp.common.search as search_mod

    def fake_ddgs_text(query: str, max_results: int) -> list[dict[str, Any]]:
        return [
            {
                "title": f"TEST_MODE result {i + 1} for {query}",
                "href": f"https://test.example/{i + 1}",
                "body": (
                    f"TEST_MODE snippet {i + 1} for {query}. "
                    "This is canned data for integration tests."
                ),
            }
            for i in range(max_results)
        ]

    def fake_fetch_and_extract(url: str, max_chars: int = 4000) -> Any:
        return fetch_mod.FetchResult(
            title=f"TEST_MODE title for {url}",
            final_url=url,
            body=(
                f"TEST_MODE fetched body for {url}. "
                "This is canned data for integration tests. "
                "The real fetch_and_extract would go over the network."
            ),
            was_truncated=False,
        )

    # Patch the source modules.
    search_mod._ddgs_text = fake_ddgs_text  # type: ignore[assignment]
    fetch_mod.fetch_and_extract = fake_fetch_and_extract  # type: ignore[assignment]

    # Patch the server modules' locally-bound names too. `from ... import`
    # creates a local binding that doesn't follow later updates to the
    # source module. We catch both forms:
    #
    # 1. The package-path form (sys.modules["apfel_mcp.url_fetch_server"]),
    #    which is the form we see when the module is imported normally.
    # 2. sys.modules["__main__"] - the form we see when running under
    #    `python -m apfel_mcp.url_fetch_server`. In that mode Python
    #    loads the module as `__main__` and does NOT populate the
    #    package-path entry. We duck-type: if __main__ has a
    #    `fetch_and_extract` attribute, it IS one of our server modules.
    import sys
    module_candidates: list[Any] = []
    for server_module_name in (
        "apfel_mcp.url_fetch_server",
        "apfel_mcp.search_and_fetch_server",
        "__main__",
    ):
        module = sys.modules.get(server_module_name)
        if module is not None:
            module_candidates.append(module)

    for module in module_candidates:
        if hasattr(module, "fetch_and_extract"):
            module.fetch_and_extract = fake_fetch_and_extract  # type: ignore[attr-defined]
    # We don't need to patch `search` - it's the wrapper in common.search
    # which calls the module-level `_ddgs_text` at call time, and we've
    # already patched _ddgs_text above. Any caller that imported
    # `search` still gets the same function object, which now reads
    # the patched `_ddgs_text` through its own globals.
