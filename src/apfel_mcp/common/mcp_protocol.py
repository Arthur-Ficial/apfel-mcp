"""Shared MCP (Model Context Protocol) JSON-RPC 2.0 stdio dispatcher.

All three apfel-mcp servers (url-fetch, ddg-search, search-and-fetch) use
this module to handle the MCP protocol layer. Each server passes its own
server name, version, tool definitions, and a tool handler callback; the
dispatcher takes care of the rest (initialize, tools/list, tools/call,
ping, notifications/initialized, error responses, malformed JSON recovery).

Protocol reference: https://spec.modelcontextprotocol.io/
Current protocol version: 2025-06-18 (matching apfel's Swift client at
Sources/Core/MCPProtocol.swift).

The dispatcher is side-effect-free if given in-memory streams for stdin
and stdout (useful for unit testing). The default production streams are
sys.stdin and sys.stdout.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

PROTOCOL_VERSION = "2025-06-18"


@dataclass
class ToolResult:
    """Return type for a tool handler.

    `text` is the plain-text tool result shown to the model. `is_error`
    becomes the top-level `isError` field in the MCP response, letting the
    model know the tool call failed so it can adjust its next step.
    """

    text: str
    is_error: bool = False


ToolHandler = Callable[[str, dict[str, Any]], ToolResult]
"""A tool handler receives (tool_name, arguments_dict) and returns a ToolResult."""


def run_server(
    server_name: str,
    server_version: str,
    tools: list[dict[str, Any]],
    tool_handler: ToolHandler,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Run the MCP stdio server loop until stdin reaches EOF.

    Args:
        server_name: identifier shown in the initialize response (e.g.,
            "apfel-url-fetch"). Must be set per-server.
        server_version: semver string, per-server.
        tools: list of MCP tool definitions. Each dict must have at least
            `name`, `description`, and `inputSchema` fields per the MCP
            spec. These are returned verbatim in tools/list.
        tool_handler: callable invoked when the client requests tools/call.
            Receives (tool_name, arguments_dict). Must return a ToolResult.
        stdin: input stream, defaults to sys.stdin.
        stdout: output stream, defaults to sys.stdout.

    The loop reads one JSON-RPC message per line from stdin, dispatches it,
    and writes the response (if any) as one JSON-RPC message per line to
    stdout. stdout is flushed after every response so the client (apfel)
    sees it immediately. Malformed JSON lines are skipped silently (the
    loop does not crash). The loop exits cleanly when stdin closes.
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout

    tool_names = {t["name"] for t in tools}

    while True:
        line = in_stream.readline()
        if not line:
            # EOF - client closed stdin
            return

        stripped = line.strip()
        if not stripped:
            continue

        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            # Malformed JSON - silently skip this line and continue
            continue

        if not isinstance(msg, dict):
            continue

        response = _dispatch(
            msg=msg,
            server_name=server_name,
            server_version=server_version,
            tools=tools,
            tool_names=tool_names,
            tool_handler=tool_handler,
        )
        if response is not None:
            out_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            out_stream.flush()


def _dispatch(
    msg: dict[str, Any],
    server_name: str,
    server_version: str,
    tools: list[dict[str, Any]],
    tool_names: set[str],
    tool_handler: ToolHandler,
) -> dict[str, Any] | None:
    """Dispatch a single parsed JSON-RPC message.

    Returns the JSON-RPC response to send, or None for notifications
    (methods that don't expect a response).
    """
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {}) or {}

    # Notifications (no id) get no response regardless of method.
    is_notification = msg_id is None

    if method == "initialize":
        if is_notification:
            return None
        return _respond(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server_name, "version": server_version},
            },
        )

    if method == "notifications/initialized":
        # MCP spec: notifications have no response
        return None

    if method == "ping":
        if is_notification:
            return None
        return _respond(msg_id, {})

    if method == "tools/list":
        if is_notification:
            return None
        return _respond(msg_id, {"tools": tools})

    if method == "tools/call":
        if is_notification:
            return None
        name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        if name not in tool_names:
            return _error(msg_id, -32602, f"Unknown tool: {name}")
        try:
            result = tool_handler(name, arguments)
        except Exception as exc:
            # Tool handler raised - convert to JSON-RPC error
            return _error(msg_id, -32603, f"Tool '{name}' raised: {exc}")
        return _respond(
            msg_id,
            {
                "content": [{"type": "text", "text": result.text}],
                "isError": result.is_error,
            },
        )

    # Unknown method
    if is_notification:
        return None
    return _error(msg_id, -32601, f"Method not found: {method}")


def _respond(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
