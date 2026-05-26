"""Bounded, read-only file reading for apfel-mcp-fs.

Resolve a path allowlist, enforce it (defeating ``..`` traversal and symlink
escapes via realpath), read at most a bounded slice of a UTF-8 text file,
reject binaries, and hard-cap the output to fit apfel's 4096-token context
window.

No writes, ever. No network. The only configuration is the allowlist, derived
from the ``APFEL_MCP_FS_ROOTS`` environment variable (colon-separated absolute
paths) or, if unset, the process working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from apfel_mcp.common.budget import truncate_to

DEFAULT_MAX_CHARS = 4000
HARD_CAP = 6000
ROOTS_ENV = "APFEL_MCP_FS_ROOTS"

# Read at most this many bytes off disk before truncation. Bounds memory on a
# huge file; far larger than HARD_CAP chars so it never affects the visible cap.
_READ_BYTE_BUDGET = 256 * 1024
# A NUL byte in the first chunk is a reliable "this is binary" signal.
_BINARY_SNIFF_BYTES = 8192


class FsError(Exception):
    """Raised for any read failure the model should see as a tool error."""


@dataclass
class FsResult:
    path: str  # the resolved absolute path that was read
    body: str  # the (possibly truncated) file slice, plain text
    was_truncated: bool


def resolve_roots(env_value: str | None, cwd: str) -> list[Path]:
    """Build the read allowlist.

    ``env_value`` is the raw ``APFEL_MCP_FS_ROOTS`` value (colon-separated) or
    None. When empty, the allowlist is just ``[cwd]``. Entries are ``~``-expanded
    and realpath-resolved; entries that are not existing directories are dropped.
    """
    raw = [p for p in (env_value or "").split(os.pathsep) if p.strip()]
    if not raw:
        raw = [cwd]
    roots: list[Path] = []
    for entry in raw:
        try:
            resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _resolve_within(path_str: str, roots: list[Path]) -> Path:
    """Resolve ``path_str`` and confirm it sits inside one of ``roots``.

    Resolution follows symlinks, so a symlink that points outside an allowed
    root resolves to its outside target and is rejected.
    """
    if not roots:
        raise FsError(
            "fs: no readable roots configured. Set APFEL_MCP_FS_ROOTS to a directory."
        )
    try:
        target = Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise FsError(f"fs: cannot resolve path '{path_str}': {exc}") from exc
    for root in roots:
        if target == root or target.is_relative_to(root):
            return target
    allowed = ", ".join(str(r) for r in roots)
    raise FsError(
        f"fs: path '{path_str}' is outside the allowed roots ({allowed}). "
        "Set APFEL_MCP_FS_ROOTS to authorize a directory."
    )


def read_file_slice(path_str: str, roots: list[Path], max_chars: int) -> FsResult:
    """Read a bounded slice of a text file under the allowlist.

    Raises FsError for: path outside roots, missing file, a directory, a binary
    file, or an unreadable file. Output is hard-capped at HARD_CAP characters
    regardless of ``max_chars``.
    """
    target = _resolve_within(path_str, roots)
    if target.is_dir():
        raise FsError(f"fs: '{target}' is a directory, not a file")
    if not target.exists():
        raise FsError(f"fs: no such file: {target}")

    try:
        with open(target, "rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                raise FsError(
                    f"fs: '{target.name}' looks binary (contains NUL); "
                    "only text files can be read"
                )
            rest = fh.read(max(0, _READ_BYTE_BUDGET - len(head)))
    except FsError:
        raise
    except OSError as exc:
        raise FsError(f"fs: cannot read {target}: {exc}") from exc

    text = (head + rest).decode("utf-8", errors="replace")
    cap = min(max(max_chars, 1), HARD_CAP)
    body, truncated = truncate_to(text, cap)
    return FsResult(path=str(target), body=body, was_truncated=truncated)
