from __future__ import annotations

import json
from pathlib import Path

import pytest

from .harness import (
    CURRENT_OPERATOR_TIERS,
    MAX_EVIDENCE_FILE_BYTES,
    MUTATION_OPT_IN,
    REQUIRED_INVENTORY,
    REQUIRED_SUITES,
    SEQUENCE,
    AcceptanceError,
    RepositoryCapabilities,
    discover_repository_capabilities,
    redact,
    require_mutation_opt_in,
    validate_evidence_report,
    validate_live_target,
)

CANDIDATE_SHA = "a" * 40


def _complete_report(evidence: str = "artifact.txt") -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": "real_browser_host",
        "mocked": False,
        "synthetic": False,
        "target": {
            "base_url": "http://127.0.0.1:8765",
            "board_id": "sandbox-home",
        },
        "host": {"product": "AionUi", "version": "2.2.1", "build": "2026.09.08.1"},
        "steps": [
            {"id": identifier, "status": "passed", "evidence": evidence}
            for identifier in SEQUENCE
        ],
        "inventory": [
            {"id": identifier, "status": "passed", "evidence": evidence}
            for identifier in sorted(REQUIRED_INVENTORY)
        ],
        "suites": [
            {
                "name": name,
                "command": command,
                "status": "passed",
                "commit": CANDIDATE_SHA,
                "evidence": evidence,
            }
            for name, command in REQUIRED_SUITES.items()
        ],
        "all_existing_suites_passed": True,
    }


def _write_report(tmp_path: Path, report: dict[str, object]) -> Path:
    (tmp_path / "artifact.txt").write_text("bounded real-host evidence", encoding="utf-8")
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _validate(tmp_path: Path, report: dict[str, object]) -> dict[str, object]:
    return validate_evidence_report(
        _write_report(tmp_path, report),
        validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
        RepositoryCapabilities((), (), (), (), ()),
        CANDIDATE_SHA,
    )


def test_acceptance_sequence_and_current_operator_tiers_are_explicit() -> None:
    assert SEQUENCE == (
        "fresh_install",
        "door_connect",
        "team_setup",
        "six_workers_two_reviewers",
        "ticket_offer_claim",
        "ticket_submit_independent_review",
        "result_visible",
        "pause_resume_stop",
        "clean_reconnect_after_rotation",
    )
    assert CURRENT_OPERATOR_TIERS == {
        "gemini_worker": 1,
        "glm_worker": 1,
        "qwen_worker": 2,
        "codex_worker": 2,
        "reviewer": 2,
    }


def test_repository_discovery_reports_current_read_only_contracts() -> None:
    capabilities = discover_repository_capabilities()
    assert capabilities.api_routes == (
        "/pursers/join",
        "/pursers/onboarding/connect",
        "/pursers/onboarding/recover",
        "/pursers/onboarding/rotate",
        "/pursers/onboarding/status",
        "/pursers/onboarding/validate",
        "/pursers/status",
    )
    assert set(capabilities.assistants) == {
        "pursers-reviewer-claude",
        "pursers-reviewer-codex",
        "pursers-worker-claude",
        "pursers-worker-codex",
    }
    assert set(capabilities.personal_views) == {
        "activity",
        "agents",
        "fleet",
        "links",
        "today",
        "work",
    }
    assert "read_only_discovery" in capabilities.capabilities
    assert "door_rotation" in capabilities.capabilities
    assert capabilities.missing_mutation_capabilities


@pytest.mark.parametrize(
    ("url", "board", "error"),
    (
        ("https://example.com", "sandbox-home", "loopback-only"),
        ("http://127.0.0.1:8765/path", "sandbox-home", "without a path"),
        ("http://127.0.0.1:8765", "pursers", "production board refused"),
        ("http://127.0.0.1:8765", "customer", "sandbox- or test- prefix"),
    ),
)
def test_live_target_refuses_unsafe_destinations(url: str, board: str, error: str) -> None:
    with pytest.raises(AcceptanceError, match=error):
        validate_live_target(url, board)


def test_live_target_accepts_explicit_loopback_sandbox() -> None:
    target = validate_live_target("http://127.0.0.1:8765/", "sandbox-home-acceptance")
    assert target.base_url == "http://127.0.0.1:8765"
    assert target.board_id == "sandbox-home-acceptance"


def test_mutation_requires_exact_opt_in() -> None:
    with pytest.raises(AcceptanceError, match="mutation opt-in"):
        require_mutation_opt_in(None)
    with pytest.raises(AcceptanceError, match="mutation opt-in"):
        require_mutation_opt_in("yes")
    require_mutation_opt_in(MUTATION_OPT_IN)


def test_redaction_removes_sensitive_keys_and_values() -> None:
    value = {
        "authorization": "Bearer abc",
        "nested": ["prs1.not-a-real-door-value", {"jwt": "eyJabc.def.ghi"}],
        "safe": "worker",
    }
    assert redact(value) == {
        "authorization": "[REDACTED]",
        "nested": ["[REDACTED]", {"jwt": "[REDACTED]"}],
        "safe": "worker",
    }


def test_evidence_validation_stops_when_sibling_contracts_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = tmp_path / "evidence.json"
    report.write_text("{}", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="sibling interface capabilities unavailable"):
        validate_evidence_report(
            report,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            discover_repository_capabilities(),
            CANDIDATE_SHA,
        )


def test_synthetic_report_cannot_establish_gui_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = tmp_path / "evidence.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_kind": "synthetic",
                "mocked": True,
                "synthetic": True,
            }
        ),
        encoding="utf-8",
    )
    capabilities = RepositoryCapabilities((), (), (), (), ())
    with pytest.raises(AcceptanceError, match="real_browser_host"):
        validate_evidence_report(
            report,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            capabilities,
            CANDIDATE_SHA,
        )


def test_complete_real_evidence_report_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    result = _validate(tmp_path, _complete_report())
    assert result == {
        "evidence_kind": "real_browser_host",
        "host_product": "AionUi",
        "host_version": "2.2.1",
        "host_build": "2026.09.08.1",
        "candidate_commit": CANDIDATE_SHA,
        "steps_passed": len(SEQUENCE),
        "inventory_passed": len(REQUIRED_INVENTORY),
        "suites_passed": len(REQUIRED_SUITES),
    }


def test_missing_evidence_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report("missing.txt")
    with pytest.raises(AcceptanceError, match="missing or escapes"):
        _validate(tmp_path, report)


@pytest.mark.parametrize("field", ("version", "build"))
def test_missing_exact_host_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    del report["host"][field]  # type: ignore[index]
    with pytest.raises(AcceptanceError, match=f"host {field} must be"):
        _validate(tmp_path, report)


@pytest.mark.parametrize("field", ("name", "command", "commit", "evidence"))
def test_missing_suite_provenance_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    del report["suites"][0][field]  # type: ignore[index]
    with pytest.raises(AcceptanceError):
        _validate(tmp_path, report)


def test_wrong_suite_candidate_sha_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report["suites"][0]["commit"] = "b" * 40  # type: ignore[index]
    with pytest.raises(AcceptanceError, match="full candidate commit"):
        _validate(tmp_path, report)


def test_evidence_symlink_path_escape_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    outside = tmp_path.parent / "outside-evidence.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "escaped.txt").symlink_to(outside)
    report = _complete_report("escaped.txt")
    with pytest.raises(AcceptanceError, match="missing or escapes"):
        _validate(tmp_path, report)


def test_incomplete_per_item_inventory_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report["inventory"].pop()  # type: ignore[union-attr]
    with pytest.raises(AcceptanceError, match="authoritative set"):
        _validate(tmp_path, report)


def test_secret_bearing_evidence_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    (tmp_path / "artifact.txt").write_text(
        "Be" + "arer " + "this-is-a-sensitive-runtime-value", encoding="utf-8"
    )
    with pytest.raises(AcceptanceError, match="secret or private path"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_non_regular_evidence_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    (tmp_path / "artifact-dir").mkdir()
    report = _complete_report("artifact-dir")
    with pytest.raises(AcceptanceError, match="regular file"):
        _validate(tmp_path, report)


def test_oversized_evidence_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    (tmp_path / "artifact.txt").write_bytes(b"x" * (MAX_EVIDENCE_FILE_BYTES + 1))
    with pytest.raises(AcceptanceError, match="exceeds"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_short_candidate_sha_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    with pytest.raises(AcceptanceError, match="full lowercase 40-hex SHA"):
        validate_evidence_report(
            _write_report(tmp_path, _complete_report()),
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            "abc123",
        )
