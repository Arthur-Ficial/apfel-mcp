"""apfel-mcp-fs: MCP stdio server that reads a bounded slice of a local file.

Thin entry-point wrapper around apfel_mcp.common.fs. Read-only: it cannot
write, move, or delete anything. Only files under the allowed roots
(APFEL_MCP_FS_ROOTS, or the working directory if unset) can be read.

Usage from apfel:

    APFEL_MCP_FS_ROOTS="$HOME/Downloads" apfel --mcp $(which apfel-mcp-fs) \\
        "Read ~/Downloads/app.log and tell me what failed"

Protocol: MCP 2025-06-18 (stdio, JSON-RPC 2.0). See
apfel_mcp.common.mcp_protocol for the dispatcher.
"""

from __future__ import annotations

import os
from typing import Any

from apfel_mcp import __version__
from apfel_mcp.common.arg_tolerance import extract_int, extract_string
from apfel_mcp.common.fs import (
    DEFAULT_MAX_CHARS,
    HARD_CAP,
    ROOTS_ENV,
    FsError,
    read_file_slice,
    resolve_roots,
)
from apfel_mcp.common.mcp_protocol import ToolResult, run_server

SERVER_NAME = "apfel-mcp-fs"
SERVER_VERSION = __version__

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a bounded slice of a local text file and return it as plain "
            "text. Read-only: it cannot write, move, or delete anything. Only "
            "files under the allowed roots (set via APFEL_MCP_FS_ROOTS, "
            "otherwise the working directory) can be read; paths outside are "
            "refused, and binary files are refused. Optimized for apfel's "
            "4096-token context window - output is hard-capped at 6000 "
            "characters. "
            "Example: read_file(path='~/Downloads/app.log', max_chars=3000)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to a text file to read",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Soft cap on output length in characters. "
                        f"Default {DEFAULT_MAX_CHARS}. Hard cap {HARD_CAP}."
                    ),
                },
            },
            "required": ["path"],
        },
    }
]


_PATH_KEYS = (
    "path", "file", "filename", "filepath", "file_path",
    "name", "target", "location", "f", "source", "src",
)
"""Known argument-key synonyms for `path`.

Anything not in this list is still accepted via `extract_string`'s fallback:
any non-empty string value under any key not in COUNT_KEYS.
"""


def _roots() -> list:
    """Resolve the allowlist fresh per call (cheap; reflects env at call time)."""
    return resolve_roots(os.environ.get(ROOTS_ENV), os.getcwd())


def _handle_tool_call(name: str, args: dict[str, Any]) -> ToolResult:
    """Tool handler for the read_file tool.

    Any reasonable key the model invents for the path argument is accepted
    (see `extract_string`). All errors are wrapped as ToolResult(is_error=True)
    so the model can react rather than crashing the MCP subprocess.
    """
    if name != "read_file":
        return ToolResult(text=f"unknown tool: {name}", is_error=True)

    path = extract_string(args, _PATH_KEYS)
    if not path:
        return ToolResult(
            text=(
                "read_file: missing 'path' argument. "
                "Call read_file(path='/path/to/file')."
            ),
            is_error=True,
        )

    max_chars = extract_int(args, ("max_chars",), default=DEFAULT_MAX_CHARS)

    try:
        result = read_file_slice(path, _roots(), max_chars=max_chars)
    except FsError as exc:
        return ToolResult(text=str(exc), is_error=True)
    except Exception as exc:
        return ToolResult(
            text=f"unexpected error in fs: {type(exc).__name__}: {exc}",
            is_error=True,
        )

    return ToolResult(text=result.body, is_error=False)


def main() -> None:
    """Entry point declared in pyproject.toml as apfel-mcp-fs."""
    from apfel_mcp._test_mode import install_if_requested

    install_if_requested()
    run_server(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        tool_handler=_handle_tool_call,
    )


if __name__ == "__main__":
    main()
