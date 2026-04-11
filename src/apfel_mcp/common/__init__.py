"""Shared modules used by the three apfel-mcp entry-point servers.

- mcp_protocol: stdio JSON-RPC 2.0 dispatcher (initialize / tools/list / tools/call / ping)
- fetch: URL fetch + Readability extraction + truncation, with SSRF guards
- search: DuckDuckGo web search via ddgs, with in-memory cache and formatting
- budget: hard-cap truncation enforcement
"""
