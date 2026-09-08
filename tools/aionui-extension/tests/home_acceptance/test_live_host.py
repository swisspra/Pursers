from __future__ import annotations

import os
from pathlib import Path

import pytest

from .harness import (
    AcceptanceError,
    discover_repository_capabilities,
    probe_extension_status,
    validate_evidence_report,
    validate_live_target,
)


def _host_url() -> str:
    value = os.environ.get("PURSERS_HOME_ACCEPTANCE_HOST_URL")
    if not value:
        pytest.skip(
            "real host unavailable: PURSERS_HOME_ACCEPTANCE_HOST_URL is unset"
        )
    return value


def test_real_host_read_only_capability_probe() -> None:
    result = probe_extension_status(validate_live_target(_host_url()))
    assert set(result) == {"ok", "push_mode", "roles", "seat_count"}


def test_real_browser_host_acceptance_evidence() -> None:
    board = os.environ.get("PURSERS_HOME_ACCEPTANCE_BOARD")
    report = os.environ.get("PURSERS_HOME_ACCEPTANCE_EVIDENCE")
    if not board or not report:
        pytest.skip(
            "real browser/host evidence unavailable: set explicit sandbox board and evidence report"
        )
    capabilities = discover_repository_capabilities()
    if capabilities.missing_mutation_capabilities:
        pytest.skip(
            "sibling interface capabilities unavailable: "
            + ", ".join(capabilities.missing_mutation_capabilities)
        )
    candidate_commit = os.environ.get("PURSERS_HOME_ACCEPTANCE_COMMIT")
    if not candidate_commit:
        pytest.fail("real browser/host evidence requires PURSERS_HOME_ACCEPTANCE_COMMIT")
    try:
        validate_evidence_report(
            Path(report),
            validate_live_target(_host_url(), board),
            capabilities,
            candidate_commit,
        )
    except AcceptanceError as error:
        pytest.fail(str(error))
