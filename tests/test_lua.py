"""Lua registry unit tests (Phase 4)."""

import pytest
from sentinel.lua import (
    SCRIPT_NAMES,
    SLIDING_WINDOW_SCRIPT,
    TOKEN_BUCKET_SCRIPT,
    script_source,
)


def test_script_names_are_stable_and_ordered() -> None:
    assert SCRIPT_NAMES == (TOKEN_BUCKET_SCRIPT, SLIDING_WINDOW_SCRIPT)


def test_token_bucket_source_is_lua() -> None:
    source = script_source(TOKEN_BUCKET_SCRIPT)
    assert "redis.call" in source
    assert "KEYS[1]" in source
    assert "ARGV[1]" in source
    assert "ARGV[2]" in source


def test_sliding_window_source_is_lua() -> None:
    source = script_source(SLIDING_WINDOW_SCRIPT)
    assert "redis.call" in source
    assert "KEYS[1]" in source
    assert "ARGV[1]" in source
    assert "ARGV[2]" in source


def test_unknown_script_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown script"):
        script_source("missing")
