"""Maximum-tolerance argument extraction for MCP tools.

apfel's 3B on-device model invents argument-key names constantly. It
might call `search(term='...')` instead of `search(query='...')`, or
`fetch(link='...')` instead of `fetch(url='...')`. It might pass a
count as `limit` instead of `max_results`. Every "missing argument"
error is a wasted tool-call round-trip the user pays for in tokens
and latency.

The golden goal says tools must be **usable**. That means:

1. Try all plausible synonyms for a named argument.
2. If none match, fall back to "any string value under any key that
   isn't obviously something else" (for string arguments).
3. For integer/count arguments, accept any int-convertible value
   from a generous key list.

As long as we can **make sense** of what the model is passing,
we accept it. One shared helper used by every MCP.
"""

from __future__ import annotations

from typing import Any

# Keys that clearly refer to something other than a primary string argument.
# If the model passes `max_results=5` and we're extracting the query, we
# must not return "5" as the query.
COUNT_KEYS: frozenset[str] = frozenset(
    {
        "max_results",
        "max_chars",
        "max",
        "limit",
        "count",
        "n",
        "results",
        "num",
        "number",
        "top",
        "top_n",
        "size",
    }
)


def extract_string(
    args: dict[str, Any],
    primary_keys: tuple[str, ...],
) -> str | None:
    """Extract a string argument the model intended to pass.

    Tries, in order:
    1. Each key in `primary_keys` (the known synonyms).
    2. Any string-valued key in `args` that isn't in `COUNT_KEYS`.
       If multiple qualify, the longest string wins - the real query
       or URL is almost always the largest string value.

    Returns the stripped string, or None if nothing usable was found.
    """
    for key in primary_keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    candidates: list[str] = []
    for key, value in args.items():
        if key in COUNT_KEYS:
            continue
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    if candidates:
        return max(candidates, key=len)
    return None


def extract_int(
    args: dict[str, Any],
    primary_keys: tuple[str, ...],
    default: int,
    exclude_keys: tuple[str, ...] = (),
) -> int:
    """Extract an integer argument the model intended to pass.

    Tries, in order:
    1. Each key in `primary_keys`.
    2. Any key in `COUNT_KEYS` that isn't in `primary_keys` or
       `exclude_keys`.

    `exclude_keys` lets a caller that has TWO int parameters (e.g.
    search-and-fetch's `results` and `max_chars_per_result`) avoid the
    fallback picking up a key already claimed by the other parameter.
    Without this, extracting `max_chars_per_result` would grab the
    `results` value (since `results` is in COUNT_KEYS), producing
    very wrong caps.

    Accepts int, or string that parses as int. Falls back to `default`
    on missing / malformed / non-int values. A zero or negative value
    also falls back to `default` - a zero count is never what the
    model meant.
    """
    excluded = frozenset(exclude_keys)
    for key in primary_keys:
        if key in args:
            parsed = _coerce_int(args[key])
            if parsed is not None and parsed > 0:
                return parsed

    for key in COUNT_KEYS:
        if key in primary_keys or key in excluded:
            continue
        if key in args:
            parsed = _coerce_int(args[key])
            if parsed is not None and parsed > 0:
                return parsed

    return default


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion. Returns None on failure."""
    if isinstance(value, bool):
        # bool is a subclass of int in Python - reject it explicitly.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
