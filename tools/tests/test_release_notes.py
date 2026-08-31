"""Regression tests for GitHub Release note extraction."""

from __future__ import annotations

import pytest

from tools.release_notes import ReleaseNotesError, extract_release_notes


def test_extracts_exact_release_section() -> None:
    changelog = """# Changelog

## [2.0.0] - 2026-08-31

### Added

- Current release.

## [1.0.0] - 2026-08-30

- Previous release.
"""

    assert extract_release_notes(changelog, "2.0.0") == (
        "### Added\n\n- Current release."
    )


def test_missing_section_fails() -> None:
    with pytest.raises(ReleaseNotesError, match=r"no \[9\.9\.9] release section"):
        extract_release_notes("# Changelog\n", "9.9.9")


def test_version_with_suffix_matches_exactly() -> None:
    changelog = """# Changelog

## [5.0.0a7] - 2026-08-31

- Alpha seven.

## [5.0.0] - 2026-08-30

- Final release.
"""

    assert extract_release_notes(changelog, "5.0.0a7") == "- Alpha seven."
