# apfel-mcp — Project Instructions

Token-budget-optimized MCP servers for [apfel](https://github.com/Arthur-Ficial/apfel). apfel's context window is **4096 tokens**. Everything in this repo exists to fit that budget.

## Golden goal

Ship user-facing MCP tools that work when the apfel conversation already has 2000+ tokens of history. Every new MCP here must answer: *"Does this tool result still fit in an already-crowded context?"*

## Four MCPs

| MCP | Binary | Hard cap | Purpose |
|---|---|---|---|
| `url-fetch` | `apfel-mcp-url-fetch` | 6000 chars (~1500 tokens) | Readability-based URL → clean markdown |
| `ddg-search` | `apfel-mcp-ddg-search` | 2000 chars (~500 tokens) | DuckDuckGo search (experimental, scraping) |
| `search-and-fetch` | `apfel-mcp-search-and-fetch` | 5000 chars (~1250 tokens) | Compound: search + fetch top N in one tool call |
| `fs` | `apfel-mcp-fs` | 6000 chars (~1500 tokens) | Read-only bounded slice of a local text file, path allowlist |

## Non-negotiable principles

1. **TDD always.** Red → green → refactor. Tests first. No implementation ahead of a failing test.
2. **Tool results are hard-capped.** Users can override the soft cap via tool argument, but NEVER the hard cap.
3. **SSRF guards on anything that fetches URLs.** `http`/`https` scheme allowlist. Private-network blocklist (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, `::1`). 10-second timeout. 2 MB download cap. Honest `User-Agent`.
4. **Clean Python.** Type hints on public functions. `ruff` clean. No ad-hoc dependencies — if you add a library, add it to `pyproject.toml` and justify in the commit message.
5. **No persistent state.** MCPs are restartable subprocesses. In-memory caches only (`ddg-search` has a 60s cache for same-session deduplication).
6. **Plain-text output, not JSON.** JSON structure overhead costs tokens the model could use for actual content.

## Architecture

One Python package, shared `common/` module, four entry-point scripts:

```
src/apfel_mcp/
├── common/
│   ├── mcp_protocol.py      # stdio JSON-RPC dispatcher (reused by all servers)
│   ├── fetch.py             # httpx + readability-lxml + markdownify + SSRF guards
│   ├── search.py            # ddgs wrapper + 60s in-memory cache + format
│   ├── fs.py                # path-allowlist + bounded read-only file slice
│   └── budget.py            # hard-cap enforcement + truncation suffix
├── url_fetch_server.py      # `apfel-mcp-url-fetch` main()
├── ddg_search_server.py     # `apfel-mcp-ddg-search` main()
├── search_and_fetch_server.py  # `apfel-mcp-search-and-fetch` main()
└── fs_server.py             # `apfel-mcp-fs` main()
```

The four entry-point scripts are **thin wrappers** (~80-100 lines each). All the logic lives in `common/`. New MCPs added here should follow the same pattern: implement logic in `common/`, add a one-file entry-point.

## Build & Test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
ruff check src tests
pytest -v
```

CI runs the same on Python 3.10, 3.11, and 3.12 via `.github/workflows/ci.yml`.

## Verifying against apfel

The real test is end-to-end through apfel itself:

```bash
apfel --mcp $(which apfel-mcp-url-fetch) "Summarize https://www.apple.com/newsroom/"
apfel --mcp $(which apfel-mcp-ddg-search) "Search for Swift 7 release notes"
apfel --mcp $(which apfel-mcp-search-and-fetch) "What did Apple announce this week?"
```

With `--debug`, apfel prints the input token count to stderr. Verify it stays under ~3000 tokens even after multiple tool calls in chat mode.

## Handling Pull Requests

Follow the same playbook as apfel's `CLAUDE.md`: vet the author, classify the PR type (tests / code / docs), read every changed file, run the full test suite on the branch, review with structured P0/P1/P2 findings, squash-merge only after CI green and all P0/P1 resolved. See [apfel/CLAUDE.md](https://github.com/Arthur-Ficial/apfel/blob/main/CLAUDE.md) for the full 12-step process.

## Credit

The `ddg-search` MCP's design and "experimental, unofficial, expect breakage" framing is adapted from [OpenClaw's DuckDuckGo extension](https://github.com/openclaw/openclaw/tree/main/extensions/duckduckgo), MIT-licensed.
