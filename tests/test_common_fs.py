"""Tests for apfel_mcp.common.fs - bounded read-only file reading.

These use real files under pytest's tmp_path rather than mocks: the whole
point of this module is correct path-allowlist enforcement (traversal,
symlink escape) and binary/cap handling, which only a real filesystem
exercises faithfully.
"""

from __future__ import annotations

import os

import pytest

from apfel_mcp.common.fs import (
    DEFAULT_MAX_CHARS,
    HARD_CAP,
    FsError,
    read_file_slice,
    resolve_roots,
)

# --- resolve_roots ---------------------------------------------------------

def test_resolve_roots_defaults_to_cwd_when_env_empty(tmp_path):
    roots = resolve_roots(None, str(tmp_path))
    assert roots == [tmp_path.resolve()]


def test_resolve_roots_parses_colon_separated_env(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    roots = resolve_roots(f"{a}{os.pathsep}{b}", str(tmp_path))
    assert a.resolve() in roots
    assert b.resolve() in roots


def test_resolve_roots_drops_nonexistent_entries(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    missing = tmp_path / "does-not-exist"
    roots = resolve_roots(f"{a}{os.pathsep}{missing}", str(tmp_path))
    assert roots == [a.resolve()]


def test_resolve_roots_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    roots = resolve_roots("~", str(tmp_path))
    assert roots == [tmp_path.resolve()]


# --- read_file_slice happy path -------------------------------------------

def test_read_file_within_root_returns_content(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello apfel")
    result = read_file_slice(str(f), [tmp_path.resolve()], max_chars=DEFAULT_MAX_CHARS)
    assert result.body == "hello apfel"
    assert result.was_truncated is False
    assert result.path == str(f.resolve())


def test_read_file_empty_file_returns_empty_body(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("")
    result = read_file_slice(str(f), [tmp_path.resolve()], max_chars=DEFAULT_MAX_CHARS)
    assert result.body == ""
    assert result.was_truncated is False


# --- allowlist enforcement -------------------------------------------------

def test_read_file_outside_root_raises(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    with pytest.raises(FsError) as exc:
        read_file_slice(str(outside), [allowed.resolve()], max_chars=DEFAULT_MAX_CHARS)
    assert "outside" in str(exc.value).lower()


def test_read_file_parent_traversal_outside_root_raises(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    traversal = str(allowed / ".." / "secret.txt")
    with pytest.raises(FsError):
        read_file_slice(traversal, [allowed.resolve()], max_chars=DEFAULT_MAX_CHARS)


def test_read_file_symlink_escape_raises(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    link = allowed / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(FsError):
        read_file_slice(str(link), [allowed.resolve()], max_chars=DEFAULT_MAX_CHARS)


def test_read_file_no_roots_raises(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    with pytest.raises(FsError) as exc:
        read_file_slice(str(f), [], max_chars=DEFAULT_MAX_CHARS)
    assert "root" in str(exc.value).lower()


# --- not-a-file / missing --------------------------------------------------

def test_read_file_directory_raises(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(FsError) as exc:
        read_file_slice(str(d), [tmp_path.resolve()], max_chars=DEFAULT_MAX_CHARS)
    assert "directory" in str(exc.value).lower()


def test_read_file_missing_raises(tmp_path):
    with pytest.raises(FsError) as exc:
        read_file_slice(
            str(tmp_path / "ghost.txt"), [tmp_path.resolve()], max_chars=DEFAULT_MAX_CHARS
        )
    assert "no such file" in str(exc.value).lower()


# --- binary rejection ------------------------------------------------------

def test_read_file_binary_raises(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"PK\x03\x04\x00\x00binary\x00data")
    with pytest.raises(FsError) as exc:
        read_file_slice(str(f), [tmp_path.resolve()], max_chars=DEFAULT_MAX_CHARS)
    assert "binary" in str(exc.value).lower()


# --- caps ------------------------------------------------------------------

def test_read_file_truncates_to_hard_cap(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("A" * 50_000)
    result = read_file_slice(str(f), [tmp_path.resolve()], max_chars=HARD_CAP)
    assert result.was_truncated is True
    assert len(result.body) <= HARD_CAP


def test_read_file_max_chars_cannot_exceed_hard_cap(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("A" * 50_000)
    result = read_file_slice(str(f), [tmp_path.resolve()], max_chars=999_999)
    assert len(result.body) <= HARD_CAP


def test_read_file_respects_smaller_soft_cap(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("A" * 50_000)
    result = read_file_slice(str(f), [tmp_path.resolve()], max_chars=500)
    assert result.was_truncated is True
    assert len(result.body) <= 500
