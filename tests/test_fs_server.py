"""Tests for the apfel-mcp-fs server entry point.

Handler-dispatch and protocol tests mock read_file_slice (the path logic is
tested in test_common_fs.py); two end-to-end tests use a real tmp file +
APFEL_MCP_FS_ROOTS to prove the allowlist wiring works through the handler.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from apfel_mcp.common.fs import FsError, FsResult
from apfel_mcp.fs_server import SERVER_NAME, TOOLS, _handle_tool_call, main


def test_server_name_matches_entry_point():
    assert SERVER_NAME == "apfel-mcp-fs"


def test_tools_list_contains_single_read_file_tool():
    assert len(TOOLS) == 1
    assert TOOLS[0]["name"] == "read_file"
    assert "path" in TOOLS[0]["inputSchema"]["required"]


def test_tool_description_mentions_budget_and_readonly():
    desc = TOOLS[0]["description"].lower()
    assert "4096" in desc
    assert "6000" in desc
    assert "read-only" in desc


def test_handle_reads_file_and_wraps_result():
    fake = FsResult(path="/tmp/x.txt", body="file body", was_truncated=False)
    with patch("apfel_mcp.fs_server.read_file_slice", return_value=fake) as m:
        result = _handle_tool_call("read_file", {"path": "/tmp/x.txt"})
    m.assert_called_once()
    assert result.is_error is False
    assert "file body" in result.text


def test_handle_missing_path_returns_error():
    result = _handle_tool_call("read_file", {})
    assert result.is_error is True
    assert "path" in result.text.lower()


def test_handle_tolerates_file_synonym_for_path():
    fake = FsResult(path="/tmp/x.txt", body="b", was_truncated=False)
    with patch("apfel_mcp.fs_server.read_file_slice", return_value=fake) as m:
        result = _handle_tool_call("read_file", {"file": "/tmp/x.txt"})
    m.assert_called_once()
    assert result.is_error is False


def test_handle_tolerates_arbitrary_unknown_key_for_path():
    fake = FsResult(path="/tmp/x.txt", body="b", was_truncated=False)
    with patch("apfel_mcp.fs_server.read_file_slice", return_value=fake) as m:
        result = _handle_tool_call("read_file", {"wibble": "/tmp/x.txt"})
    m.assert_called_once()
    assert result.is_error is False


def test_handle_respects_max_chars_argument():
    fake = FsResult(path="/tmp/x.txt", body="b", was_truncated=False)
    with patch("apfel_mcp.fs_server.read_file_slice", return_value=fake) as m:
        _handle_tool_call("read_file", {"path": "/tmp/x.txt", "max_chars": 1500})
    _, kwargs = m.call_args
    assert kwargs["max_chars"] == 1500


def test_handle_tolerates_limit_synonym_for_max_chars():
    fake = FsResult(path="/tmp/x.txt", body="b", was_truncated=False)
    with patch("apfel_mcp.fs_server.read_file_slice", return_value=fake) as m:
        _handle_tool_call("read_file", {"path": "/tmp/x.txt", "limit": 1200})
    _, kwargs = m.call_args
    assert kwargs["max_chars"] == 1200


def test_handle_fs_error_becomes_is_error_true():
    with patch(
        "apfel_mcp.fs_server.read_file_slice",
        side_effect=FsError("path '/etc/passwd' is outside the allowed roots"),
    ):
        result = _handle_tool_call("read_file", {"path": "/etc/passwd"})
    assert result.is_error is True
    assert "outside" in result.text.lower()


def test_handle_unexpected_exception_becomes_is_error_true():
    with patch(
        "apfel_mcp.fs_server.read_file_slice", side_effect=RuntimeError("boom")
    ):
        result = _handle_tool_call("read_file", {"path": "/tmp/x.txt"})
    assert result.is_error is True
    assert "unexpected error" in result.text
    assert "boom" in result.text


def test_handle_unknown_tool_returns_error():
    result = _handle_tool_call("write_file", {"path": "/tmp/x.txt"})
    assert result.is_error is True


# --- end-to-end through the real allowlist (no mock) -----------------------

def test_end_to_end_reads_file_within_env_root(monkeypatch, tmp_path):
    f = tmp_path / "config.ini"
    f.write_text("[server]\nport=11434\n")
    monkeypatch.setenv("APFEL_MCP_FS_ROOTS", str(tmp_path))
    result = _handle_tool_call("read_file", {"path": str(f)})
    assert result.is_error is False
    assert "port=11434" in result.text


def test_end_to_end_refuses_file_outside_env_root(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("classified")
    monkeypatch.setenv("APFEL_MCP_FS_ROOTS", str(allowed))
    result = _handle_tool_call("read_file", {"path": str(outside)})
    assert result.is_error is True
    assert "classified" not in result.text


def test_server_stdio_round_trip_initialize_then_read(monkeypatch, tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello from fs stdio")
    monkeypatch.setenv("APFEL_MCP_FS_ROOTS", str(tmp_path))

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": str(f)}},
        },
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    main()

    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(responses) == 3
    assert responses[0]["result"]["serverInfo"]["name"] == "apfel-mcp-fs"
    assert responses[1]["result"]["tools"][0]["name"] == "read_file"
    call = responses[2]["result"]
    assert call["isError"] is False
    assert "hello from fs stdio" in call["content"][0]["text"]
