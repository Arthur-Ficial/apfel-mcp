"""Tests for apfel_mcp.common.mcp_protocol.

The MCP protocol module implements a shared JSON-RPC 2.0 stdio dispatcher
that the three entry-point servers (url-fetch, ddg-search, search-and-fetch)
all reuse. Each server passes its own server_name, server_version, tool
definitions, and tool handler; the dispatcher handles initialize, tools/list,
tools/call, ping, and error paths identically.

Tests drive the dispatcher via injectable in-memory stdin/stdout so no real
subprocess is needed.
"""

import io
import json

from apfel_mcp.common.mcp_protocol import (
    PROTOCOL_VERSION,
    ToolResult,
    run_server,
)


def _run(requests: list[dict], tools: list[dict], tool_handler) -> list[dict]:
    """Helper: feed `requests` through run_server and parse the JSON-RPC responses."""
    stdin_text = "".join(json.dumps(r) + "\n" for r in requests)
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    run_server(
        server_name="test-server",
        server_version="1.2.3",
        tools=tools,
        tool_handler=tool_handler,
        stdin=stdin,
        stdout=stdout,
    )
    # Parse each newline-delimited JSON response
    responses: list[dict] = []
    for line in stdout.getvalue().splitlines():
        if line.strip():
            responses.append(json.loads(line))
    return responses


def _noop_handler(name: str, args: dict) -> ToolResult:
    return ToolResult(text=f"called {name} with {args}", is_error=False)


def test_initialize_returns_serverInfo_with_name_version_protocol():
    """initialize must return serverInfo and the MCP protocolVersion constant."""
    responses = _run(
        [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}],
        tools=[],
        tool_handler=_noop_handler,
    )
    assert len(responses) == 1
    result = responses[0]["result"]
    assert result["serverInfo"]["name"] == "test-server"
    assert result["serverInfo"]["version"] == "1.2.3"
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "capabilities" in result


def test_protocol_version_is_2025_06_18():
    """Hard assertion that we stay on the version apfel's client expects."""
    assert PROTOCOL_VERSION == "2025-06-18"


def test_tools_list_returns_the_provided_tool_definitions():
    """tools/list must echo back the server's configured tools verbatim."""
    tools = [
        {
            "name": "foo",
            "description": "example tool",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    responses = _run(
        [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
        tools=tools,
        tool_handler=_noop_handler,
    )
    assert len(responses) == 1
    assert responses[0]["result"]["tools"] == tools


def test_tools_call_dispatches_to_handler_with_name_and_args():
    """tools/call must invoke tool_handler(name, arguments) and wrap the result."""
    seen: list[tuple[str, dict]] = []

    def handler(name: str, args: dict) -> ToolResult:
        seen.append((name, args))
        return ToolResult(text="handled!", is_error=False)

    tools = [{"name": "foo", "description": "", "inputSchema": {}}]
    responses = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "foo", "arguments": {"a": 1, "b": "hello"}},
            }
        ],
        tools=tools,
        tool_handler=handler,
    )
    assert seen == [("foo", {"a": 1, "b": "hello"})]
    assert len(responses) == 1
    result = responses[0]["result"]
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "handled!"
    assert result["isError"] is False


def test_tools_call_wraps_error_result_with_isError_true():
    """When the handler returns is_error=True, the response carries isError: true."""
    def handler(name, args):
        return ToolResult(text="something broke", is_error=True)

    tools = [{"name": "foo", "description": "", "inputSchema": {}}]
    responses = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "foo", "arguments": {}},
            }
        ],
        tools=tools,
        tool_handler=handler,
    )
    result = responses[0]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "something broke"


def test_tools_call_unknown_tool_returns_jsonrpc_error():
    """Calling a tool that isn't in the server's tool list must return JSON-RPC error -32602."""
    tools = [{"name": "foo", "description": "", "inputSchema": {}}]
    responses = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "nonexistent", "arguments": {}},
            }
        ],
        tools=tools,
        tool_handler=_noop_handler,
    )
    assert "error" in responses[0]
    assert responses[0]["error"]["code"] == -32602
    assert "nonexistent" in responses[0]["error"]["message"]


def test_ping_returns_empty_result():
    """MCP spec: ping method must be answered with an empty result object."""
    responses = _run(
        [{"jsonrpc": "2.0", "id": 6, "method": "ping"}],
        tools=[],
        tool_handler=_noop_handler,
    )
    assert responses[0]["result"] == {}


def test_notifications_initialized_is_no_op_no_response_sent():
    """MCP notifications have no id and no response. The dispatcher must silently consume them."""
    responses = _run(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ],
        tools=[],
        tool_handler=_noop_handler,
    )
    # Only the ping gets a response; the notification is silently consumed.
    assert len(responses) == 1
    assert responses[0]["id"] == 7


def test_unknown_method_returns_jsonrpc_error():
    """Unknown RPC methods (with an id) must return JSON-RPC error -32601 (Method not found)."""
    responses = _run(
        [{"jsonrpc": "2.0", "id": 8, "method": "nonsense/method"}],
        tools=[],
        tool_handler=_noop_handler,
    )
    assert "error" in responses[0]
    assert responses[0]["error"]["code"] == -32601


def test_malformed_json_line_does_not_crash_server():
    """Malformed JSON on stdin must not crash the dispatcher - it should skip and continue."""
    stdin = io.StringIO('not valid json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
    stdout = io.StringIO()
    # Must not raise
    run_server(
        server_name="test",
        server_version="1.0",
        tools=[],
        tool_handler=_noop_handler,
        stdin=stdin,
        stdout=stdout,
    )
    # The subsequent valid ping is still answered
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) >= 1
    response = json.loads(lines[-1])
    assert response["id"] == 9
    assert response["result"] == {}
