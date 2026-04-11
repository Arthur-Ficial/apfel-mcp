# apfel-mcp

Token-budget-optimized MCP (Model Context Protocol) servers for [apfel](https://github.com/Arthur-Ficial/apfel), the command-line wrapper for Apple's on-device FoundationModels LLM.

apfel's context window is **4096 tokens**. These MCPs are designed from the ground up to produce tiny, useful tool results that fit that budget — not to truncate afterward.

## The three MCPs

- **`apfel-mcp-url-fetch`** — fetch a web page, extract the main article body via Readability, return clean markdown. Default ~4000 chars, hard cap 6000 chars (~1500 tokens). SSRF guards: `http`/`https` only, private-network blocklist, 10-second timeout, 2 MB download cap.
- **`apfel-mcp-ddg-search`** — DuckDuckGo web search, no API key required. Returns top 5 results in ~300 tokens. **Experimental, unofficial, scraping-based** — DuckDuckGo does not provide a public search API, so this uses their HTML endpoint. Expect occasional breakage. See [ddg-search caveats](#ddg-search-caveats).
- **`apfel-mcp-search-and-fetch`** — compound tool: search then fetch the top N results in a single tool call. Saves ~500 tokens of tool-call schema and conversation overhead versus calling search + fetch separately. Hard cap 5000 chars (~1250 tokens).

## Why a separate repo

apfel itself ships `mcp/calculator/` and `mcp/http-test-server/` as **test fixtures** that exercise apfel's own MCP client. This repo is for **user-facing utility MCPs**: things real users want, maintained at their own cadence, with their own Python dep graph.

## Install

### Homebrew (recommended)

```bash
brew install Arthur-Ficial/tap/apfel-mcp
```

Installs three binaries to `/opt/homebrew/bin/`:

```
apfel-mcp-url-fetch
apfel-mcp-ddg-search
apfel-mcp-search-and-fetch
```

### Pip

```bash
pip install apfel-mcp
```

Or from source:

```bash
git clone https://github.com/Arthur-Ficial/apfel-mcp.git
cd apfel-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Usage with apfel

```bash
# URL fetch
apfel --mcp $(which apfel-mcp-url-fetch) "Summarize https://www.apple.com/newsroom/ in 3 bullets"

# DDG search
apfel --mcp $(which apfel-mcp-ddg-search) "Search for Swift 7 release notes"

# Compound (one tool call, one answer)
apfel --mcp $(which apfel-mcp-search-and-fetch) "What did Apple announce this week?"
```

All three attached in chat mode:

```bash
apfel --mcp $(which apfel-mcp-url-fetch) \
      --mcp $(which apfel-mcp-ddg-search) \
      --mcp $(which apfel-mcp-search-and-fetch) \
      --context-strategy sliding-window \
      --context-max-turns 4 \
      --chat
```

The `sliding-window` strategy is recommended because these tools return a lot of text relative to apfel's 4096-token context — a long session without windowing will overflow.

## ddg-search caveats

DuckDuckGo **does not provide a public web-search API**. The Instant Answer API at `api.duckduckgo.com` returns only the summary box at the top of a DDG page, not the 10-blue-links results. The only way to get real search results programmatically is to scrape their HTML endpoint (`html.duckduckgo.com/html`), which DDG's Terms of Service gently discourage.

`apfel-mcp-ddg-search` uses the [`ddgs`](https://pypi.org/project/ddgs/) Python library, which handles the HTML scraping, bot-challenge detection, and URL unwrapping. This approach is:

- **Unofficial** — not endorsed by or affiliated with DuckDuckGo
- **Fragile** — DDG changes their HTML layout from time to time, which breaks scrapers
- **Rate-limited** — DDG detects and blocks automated traffic. Expect occasional "bot challenge" errors
- **Best-effort** — if it stops working, open an issue. We may switch backends (SearXNG self-hosted, Brave API with a key) in the future

If reliability matters more to you than zero configuration, run your own [SearXNG](https://github.com/searxng/searxng) and point `apfel-mcp-url-fetch` at it.

**This pattern is directly modeled on [OpenClaw's DDG extension](https://github.com/openclaw/openclaw/tree/main/extensions/duckduckgo)**, which uses the same approach with the same caveats. Credit where due.

## Development

```bash
git clone https://github.com/Arthur-Ficial/apfel-mcp.git
cd apfel-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
ruff check src tests
pytest -v
```

See [CLAUDE.md](CLAUDE.md) for the non-negotiable principles (TDD, hard caps, SSRF guards, no persistent state).

## Manual testing via MCP Inspector

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the official interactive testing UI for MCP servers:

```bash
npx @modelcontextprotocol/inspector python -m apfel_mcp.url_fetch_server
npx @modelcontextprotocol/inspector python -m apfel_mcp.ddg_search_server
npx @modelcontextprotocol/inspector python -m apfel_mcp.search_and_fetch_server
```

## License

[MIT](LICENSE). Built for [apfel](https://github.com/Arthur-Ficial/apfel).

DDG search approach and framing: credit to [OpenClaw](https://github.com/openclaw/openclaw) (MIT).
