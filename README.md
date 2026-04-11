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

## Contributing new MCPs

**This repo is open for new apfel-optimized MCPs.** If you have an idea for a token-budget-aware MCP that fits apfel's 4096-token context window, open an issue or a PR. The shared `src/apfel_mcp/common/` module gives you everything you need to build one:

> **#agentswelcome** - AI agent contributions welcome. Claude Code, Codex, Cursor, Aider, any autonomous coding agent: if you can read [CLAUDE.md](CLAUDE.md), run `pytest`, and open a pull request, you can ship an apfel-mcp. The contribution rules below are written to be unambiguous enough for an agent to follow without human translation. Credit your tool in the commit trailer (`Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` or equivalent), include a passing test suite, and submit. Humans and agents are reviewed on the same bar: token budget first, tests second, honesty about limits third.


- `common/budget.py` — `truncate_to(text, hard_cap)` enforces hard output caps with a visible truncation suffix.
- `common/mcp_protocol.py` — `run_server(...)` is the shared JSON-RPC 2.0 stdio dispatcher. Your entry point is ~30 lines.
- `common/arg_tolerance.py` — `extract_string(args, keys)` and `extract_int(args, keys, default)` absorb the 3B model's argument-key hallucinations so your tool never returns a "missing argument" error that a reasonable human would have understood.
- `common/fetch.py` — if you need an HTTP client, use this one: SSRF blocklist, 2 MB download cap, 10-second timeout, honest User-Agent, Readability extraction.

### Ideas we'd love to see

These don't exist yet. All welcome as issues or PRs. Each one should land with tests, a hard token cap, and honest documentation of its limits.

| Tool | What it does | Why apfel needs it |
|---|---|---|
| `apfel-mcp-sqlite-query` | Read-only SQL query against a local SQLite file, result count capped | Ask apfel questions about local data without uploading it anywhere |
| `apfel-mcp-git` | Recent commits, file blame, branch info, read-only | Let apfel explain your repo history from the command line |
| `apfel-mcp-man` | Fetch a section of a man page or built-in help text | Turn `man ls` into a conversation |
| `apfel-mcp-fs` | Bounded slice of a local file with path allowlist and byte cap | Read code/config files into apfel without pasting them |
| `apfel-mcp-datetime` | Current date, timezone conversion, relative date parsing | Fix the "3B model doesn't know today's date" problem |
| `apfel-mcp-shell` | Allowlisted read-only shell commands (`df`, `uname`, `uptime`, `ps`) | Let apfel diagnose the machine it runs on |
| `apfel-mcp-clipboard` | Read the current macOS pasteboard (with an always-on privacy warning) | "Summarize what I just copied" |
| `apfel-mcp-screenshot-ocr` | Capture a region of screen and OCR it via Apple Vision | Bridge what's on-screen into the chat |
| `apfel-mcp-brew` | Read-only wrapper over `brew info`, `brew list`, `brew outdated` | Answer "what do I have installed, what's outdated" |

### Contribution rules

1. **Token budget first.** Pick a hard cap *before* writing code. 2000-6000 chars is the sweet spot for apfel.
2. **Test-driven.** Write failing tests before the implementation. Mock all network and subprocess calls. Existing tests in `tests/` are the pattern.
3. **Use `common/`.** Reuse `budget.truncate_to`, `mcp_protocol.run_server`, `arg_tolerance.extract_string`. Don't reinvent.
4. **Honest limits.** Document exactly where the MCP stops being useful - in the tool description the model sees, and in the README.
5. **One tool, one purpose.** Don't build multi-tool mega-servers. Compound tools are the exception: they exist to save tool-call round-trips, not to add features.
6. **No persistence.** In-memory caches are fine. SQLite databases, filesystem state, and long-lived daemons are not.
7. **Read-only by default.** Any MCP that can modify user state (files, clipboard, shell) needs an explicit opt-in flag and a very loud README warning.
8. **Ship it as `src/apfel_mcp/<name>_server.py`** with a console-script entry in `pyproject.toml`. Homebrew formula updates are part of the PR.

If you're not sure whether your idea fits, **open an issue first** and we'll talk about scope. Small, focused, honest. That's the bar.

## License

[MIT](LICENSE). Built for [apfel](https://github.com/Arthur-Ficial/apfel).

DDG search approach and framing: credit to [OpenClaw](https://github.com/openclaw/openclaw) (MIT).
