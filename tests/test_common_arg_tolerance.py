"""Tests for common.arg_tolerance.

The golden-goal invariant: if a reasonable human could look at the args
dict and say "obviously the query is X", `extract_string` must return X.
Same for `extract_int`.
"""

from __future__ import annotations

from apfel_mcp.common.arg_tolerance import extract_int, extract_string

# --- extract_string ---

def test_returns_value_under_primary_key():
    result = extract_string({"query": "hello"}, ("query", "q"))
    assert result == "hello"


def test_returns_value_under_synonym_key():
    result = extract_string({"q": "hello"}, ("query", "q"))
    assert result == "hello"


def test_falls_back_to_unknown_key_with_string_value():
    """Model invents a new key like `term`. We still find the query."""
    result = extract_string({"term": "hello world"}, ("query",))
    assert result == "hello world"


def test_falls_back_to_longest_string_among_unknown_keys():
    """Given several unknown string keys, the longest wins - it's the real query."""
    args = {"mode": "text", "xxxx": "this is the actual query i care about"}
    result = extract_string(args, ("query",))
    assert result == "this is the actual query i care about"


def test_ignores_count_keys_in_fallback():
    """A numeric-sounding key with a string value must not be treated as the query."""
    args = {"max_results": "5", "topic": "swift 7 release notes"}
    result = extract_string(args, ("query",))
    assert result == "swift 7 release notes"


def test_strips_whitespace():
    result = extract_string({"query": "  hello  "}, ("query",))
    assert result == "hello"


def test_returns_none_for_empty_dict():
    assert extract_string({}, ("query",)) is None


def test_returns_none_when_only_count_keys_present():
    """Query is genuinely missing. Don't invent one from count keys."""
    assert extract_string({"max_results": 5}, ("query",)) is None


def test_returns_none_when_all_strings_are_whitespace():
    assert extract_string({"query": "   "}, ("query",)) is None


def test_primary_keys_checked_in_order():
    """First matching primary key wins over later primary keys."""
    args = {"q": "fallback", "query": "primary"}
    result = extract_string(args, ("query", "q"))
    assert result == "primary"


# --- extract_int ---

def test_extract_int_from_primary_key():
    assert extract_int({"max_results": 7}, ("max_results",), default=5) == 7


def test_extract_int_parses_string_value():
    assert extract_int({"max_results": "7"}, ("max_results",), default=5) == 7


def test_extract_int_falls_back_to_known_count_synonym():
    """Model passes `limit` instead of `max_results`."""
    assert extract_int({"limit": 3}, ("max_results",), default=5) == 3


def test_extract_int_rejects_bool_values():
    """bool is a subclass of int in Python - must not count as a valid int."""
    assert extract_int({"max_results": True}, ("max_results",), default=5) == 5


def test_extract_int_rejects_non_numeric_strings():
    assert extract_int({"max_results": "lots"}, ("max_results",), default=5) == 5


def test_extract_int_rejects_zero_and_negative():
    """Zero or negative doesn't make sense for a count arg - use default."""
    assert extract_int({"max_results": 0}, ("max_results",), default=5) == 5
    assert extract_int({"max_results": -3}, ("max_results",), default=5) == 5


def test_extract_int_returns_default_on_empty_dict():
    assert extract_int({}, ("max_results",), default=5) == 5


def test_extract_int_respects_exclude_keys():
    """Regression: search_and_fetch has TWO int params.

    Without exclude_keys, extracting `max_chars_per_result` would pick
    up the `results` value (since `results` is in COUNT_KEYS), which is
    obviously wrong. `exclude_keys` prevents that cross-contamination.
    """
    args = {"query": "x", "results": 2}
    # Without exclude: would wrongly return 2 as max_chars_per_result.
    # With exclude=("results",): falls through to default.
    got = extract_int(
        args,
        ("max_chars_per_result", "max_chars"),
        default=1800,
        exclude_keys=("results",),
    )
    assert got == 1800
