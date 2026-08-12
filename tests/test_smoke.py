"""Smoke tests for the package skeleton (Phase 0)."""

from sentinel import __version__


def test_version_is_semver() -> None:
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
