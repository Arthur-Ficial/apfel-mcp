"""Subprocess-based stdio round-trip tests for all three MCP servers.

These tests launch each `apfel-mcp-*` entry point as a real subprocess
and drive the JSON-RPC protocol through its stdin/stdout. This catches
regressions that unit tests (which use StringIO) cannot:

- The entry point is wired correctly in `pyproject.toml`.
- `main()` actually blocks on stdin rather than exiting immediately.
- `run_server` flushes stdout after each response.
- Malformed JSON doesn't kill the server.
- The server exits cleanly when stdin is closed.

To keep the tests hermetic, the MCP servers are invoked through the
`APFEL_MCP_TEST_MODE` environment variable which replaces the network-
touching functions with canned responses. See `src/apfel_mcp/_test_mode.py`
for the injection point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# --- helpers ---

def _spawn(module: str) -> subprocess.Popen[str]:
    """Launch an MCP server as a subprocess with test-mode mocks enabled.

    We inherit the parent environment (so the venv's site-packages
    stays on sys.path) and add `APFEL_MCP_TEST_MODE=1` which flips the
    server into canned-response mode. Running under a stripped env
    breaks Python startup because HOME, USER, and the PYTHONPATH that
    locates site-packages all need to be preserved.
    """
    env = dict(os.environ)
    env["APFEL_MCP_TEST_MODE"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _send(proc: subprocess.Popen[str], obj: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[str]) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, "server closed stdout unexpectedly"
    return json.loads(line)


def _shutdown(proc: subprocess.Popen[str]) -> tuple[int, str]:
    """Close stdin and wait for the subprocess to exit. Returns (rc, stderr)."""
    assert proc.stdin is not None
    proc.stdin.close()
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
    stderr = proc.stderr.read() if proc.stderr else ""
    return rc, stderr


# --- url-fetch ---

def test_url_fetch_subprocess_initialize_list_call_exit():
    proc = _spawn("apfel_mcp.url_fetch_server")
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        resp = _recv(proc)
        assert resp["result"]["serverInfo"]["name"] == "apfel-mcp-url-fetch"
        assert resp["result"]["protocolVersion"] == "2025-06-18"

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "fetch"

        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "fetch",
                    "arguments": {"url": "https://test.example/"},
                },
            },
        )
        resp = _recv(proc)
        body_text = resp["result"]["content"][0]["text"]
        assert resp["result"]["isError"] is False, f"tool returned error: {body_text}"
        assert "TEST_MODE" in body_text
    finally:
        rc, stderr = _shutdown(proc)
    assert rc == 0, f"server exited with code {rc}\nstderr:\n{stderr}"


def test_url_fetch_subprocess_survives_malformed_json():
    """Malformed JSON lines are silently skipped (per `run_server` docstring),
    and the server must still handle valid follow-up requests."""
    proc = _spawn("apfel_mcp.url_fetch_server")
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        _recv(proc)

        # Send garbage. Per the protocol's malformed-JSON policy, the
        # server consumes the line and produces no response.
        assert proc.stdin is not None
        proc.stdin.write("this is not valid json\n")
        proc.stdin.flush()

        # Valid follow-up must still work.
        _send(proc, {"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
        resp = _recv(proc)
        assert resp["result"]["tools"][0]["name"] == "fetch"
    finally:
        rc, stderr = _shutdown(proc)
    assert rc == 0, f"server exited with code {rc}\nstderr:\n{stderr}"


# --- ddg-search ---

def test_ddg_search_subprocess_initialize_list_call_exit():
    proc = _spawn("apfel_mcp.ddg_search_server")
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        resp = _recv(proc)
        assert resp["result"]["serverInfo"]["name"] == "apfel-mcp-ddg-search"

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "search"

        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {"query": "test query"},
                },
            },
        )
        resp = _recv(proc)
        assert resp["result"]["isError"] is False
        assert "TEST_MODE" in resp["result"]["content"][0]["text"]
    finally:
        rc, stderr = _shutdown(proc)
    assert rc == 0, f"server exited with code {rc}\nstderr:\n{stderr}"


def test_ddg_search_subprocess_tolerates_term_synonym():
    """Regression: the 3B model called search(term='...'). Must not regress."""
    proc = _spawn("apfel_mcp.ddg_search_server")
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        _recv(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {"term": "apfel macos"},
                },
            },
        )
        resp = _recv(proc)
        assert resp["result"]["isError"] is False
        assert "apfel macos" in resp["result"]["content"][0]["text"]
    finally:
        rc, stderr = _shutdown(proc)
    assert rc == 0, f"server exited with code {rc}\nstderr:\n{stderr}"


# --- search-and-fetch ---

def test_search_and_fetch_subprocess_initialize_list_call_exit():
    proc = _spawn("apfel_mcp.search_and_fetch_server")
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        resp = _recv(proc)
        assert resp["result"]["serverInfo"]["name"] == "apfel-mcp-search-and-fetch"

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        assert tool_names == {"search", "web_search"}

        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {"query": "apfel"},
                },
            },
        )
        resp = _recv(proc)
        assert resp["result"]["isError"] is False
        body = resp["result"]["content"][0]["text"]
        assert "Search: apfel" in body
        assert "TEST_MODE" in body
    finally:
        rc, stderr = _shutdown(proc)
    assert rc == 0, f"server exited with code {rc}\nstderr:\n{stderr}"


def test_search_and_fetch_subprocess_web_search_alias_works():
    """The `web_search` alias must be dispatchable as a declared tool."""
    proc = _spawn("apfel_mcp.search_and_fetch_server")
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        _recv(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "web_search",
                    "arguments": {"query": "test"},
                },
            },
        )
        resp = _recv(proc)
        assert resp["result"]["isError"] is False
    finally:
        rc, stderr = _shutdown(proc)
    assert rc == 0, f"server exited with code {rc}\nstderr:\n{stderr}"


# --- cross-cutting: clean EOF shutdown ---

@pytest.mark.parametrize(
    "module",
    [
        "apfel_mcp.url_fetch_server",
        "apfel_mcp.ddg_search_server",
        "apfel_mcp.search_and_fetch_server",
    ],
)
def test_server_exits_cleanly_on_stdin_close(module):
    """Closing stdin without sending anything should make the server exit 0."""
    proc = _spawn(module)
    assert proc.stdin is not None
    proc.stdin.close()
    rc = proc.wait(timeout=5)
    stderr = proc.stderr.read() if proc.stderr else ""
    assert rc == 0, f"{module} exited with code {rc}\nstderr:\n{stderr}"
