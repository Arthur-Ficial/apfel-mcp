"""Hard-cap truncation for tool-result text.

apfel's context window is 4096 tokens. Tool-result text that exceeds the
budget triggers `contextOverflow` in apfel and crashes the request. Every
MCP in this repo must call `truncate_to` before returning output.

The suffix format "[... truncated at N chars]" makes the truncation visible
to the model so it can account for the missing content in its answer.
"""

from __future__ import annotations

_SUFFIX_TEMPLATE = "\n\n[... truncated at {n} chars]"
# Reserve worst-case suffix length (7-digit cap covers hard_caps up to 9,999,999).
_SUFFIX_RESERVE = len(_SUFFIX_TEMPLATE.format(n=9_999_999))


def truncate_to(text: str, hard_cap: int) -> tuple[str, bool]:
    """Truncate text to at most `hard_cap` characters, with a visible suffix.

    Args:
        text: text to truncate.
        hard_cap: maximum total length of the returned string (including any
            appended truncation suffix). Must be positive.

    Returns:
        A tuple of (possibly-truncated-text, was_truncated). If the input was
        shorter than or equal to `hard_cap`, the text is returned verbatim
        and was_truncated is False. Otherwise, the text is cut to leave room
        for a "[... truncated at N chars]" suffix and was_truncated is True.

    Raises:
        ValueError: if hard_cap is not positive.
    """
    if hard_cap <= 0:
        raise ValueError(f"hard_cap must be positive, got {hard_cap}")

    if len(text) <= hard_cap:
        return text, False

    # Reserve room for the worst-case suffix. If the hard_cap is so small that
    # we cannot even fit the suffix, fall back to a plain truncation with no
    # suffix at all (rare edge case, typically a caller mistake).
    body_budget = hard_cap - _SUFFIX_RESERVE
    if body_budget <= 0:
        return text[:hard_cap], True

    body = text[:body_budget]
    suffix = _SUFFIX_TEMPLATE.format(n=body_budget)
    return body + suffix, True
