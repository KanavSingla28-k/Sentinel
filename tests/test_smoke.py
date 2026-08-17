"""Smoke tests for the package skeleton (Phase 0)."""

import re

from sentinel import __version__


def test_version_is_semver() -> None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?", __version__)
    assert match is not None, __version__
    assert all(part.isdigit() for part in match.groups())
