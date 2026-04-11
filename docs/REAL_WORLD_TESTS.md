# Real-world smoke tests

Verified against live network services on 2026-04-11 before the
v0.1.0 release. Every MCP was exercised in two ways:

1. Direct stdio round-trip (canonical JSON-RPC into the compiled
   binary, read the response off stdout).
2. End-to-end through `apfel --chat` / `apfel --mcp ... "<prompt>"`
   so the real 3B on-device model drives the tool call.

The goal of this phase was to catch gaps that mocks don't: actual
DDG HTML changes, Readability extraction quality on live pages, SSL
behavior, the 3B model's tool-use vocabulary.

## url-fetch

### Direct stdio

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"fetch","arguments":{"url":"https://en.wikipedia.org/wiki/Apple_silicon"}}}' \
  | apfel-mcp-url-fetch
```

Result: initialize response, then a `tools/call` response containing
the Wikipedia article as clean Markdown. Title, URL, and body all
present. Under the hard cap. Nav/footer boilerplate removed.

### Known working URLs

- `https://en.wikipedia.org/wiki/Apple_silicon` - Wikipedia long-form
- `https://apfel.franzai.com/` - marketing landing page
- `https://www.apple.com/newsroom/2025/06/macos-tahoe-26-makes-the-mac-more-capable-productive-and-intelligent-than-ever/` - Apple press release

### Known limitations

- JavaScript-heavy SPAs return only the skeleton HTML. Readability
  extracts what it can; often short. That's the limit of any
  non-browser fetch tool.
- GitHub repo root URLs (`https://github.com/owner/repo`) return a
  very short Readability extract - GitHub renders most of the page
  via JS after load. Users who want README content should fetch
  `https://raw.githubusercontent.com/owner/repo/main/README.md`
  instead (which url-fetch handles because the Content-Type is
  text/plain... actually no, we reject non-HTML content types).
  Known gap.

## ddg-search

### Direct stdio

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"macos tahoe release date","max_results":3}}}' \
  | apfel-mcp-ddg-search
```

Result: three real results from DuckDuckGo - Wikipedia, MacRumors,
BetaWiki. Formatted as title + snippet + domain, under 2000 chars total.

### Rate-limit resilience

DDG occasionally throttles. When it does, `ddgs` raises an exception
whose message contains "Ratelimit" or "429"; our `SearchError` wraps
that into a friendly "wait a moment" message. We intentionally do
NOT implement automatic retry - the caller can just try again.

### Known limitations

- Bot-challenge pages: if DDG serves a captcha, `ddgs` detects it
  and returns an empty result set. We surface this as "No results
  for: \<query>" which is indistinguishable from a genuine zero-result
  query. Acceptable for v0.1.0.
- 60-second in-memory cache is process-local. When apfel restarts
  the MCP subprocess, the cache is empty again. By design.

## search-and-fetch (the flagship compound tool)

### Direct stdio

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"python ddgs library github","results":2}}}' \
  | apfel-mcp-search-and-fetch
```

Result: DDG search for the query, then Readability-extracted bodies
from the top 2 results (`github.com/deedy5/ddgs`, `pypi.org/project/ddgs`)
in ONE response. Under the 5000-char hard cap.

### E2E through apfel --chat

```bash
apfel --mcp $(which apfel-mcp-search-and-fetch) \
  "use the search tool to find what macos tahoe version number means"
```

Result: model invoked `search(query='macos tahoe version number')`,
received combined content from two live sources (Wikipedia +
apple.com), produced an accurate multi-sentence summary naming the
September 15, 2025 release date, the Intel-supported Mac list,
Spotlight changes, and Apple Intelligence updates.

This is the golden-goal UX - a single tool call turns a topic
question into a research-backed answer, within apfel's 4096-token
context window.

### The hallucination gotcha

The 3B model occasionally ignores the declared tool name and
invents `web_search`, `search_function`, or `search_wikipedia`.
Apfel's MCP client filters tools/call by the names in tools/list,
so our flagship tool declares BOTH `search` and `web_search` as
top-level entries. The compound MCP handler accepts either name,
plus `search_and_fetch` as a legacy alias.

Adding more alias entries is possible but each one costs schema
tokens in the prompt. Two entries are the sweet spot for v0.1.0.

## Conclusion

All three MCPs are live-verified and produce useful output via the
3B on-device model. No blocker bugs. Ship it.
