from __future__ import annotations

import json
from pathlib import Path

import pytest

from .harness import (
    CURRENT_OPERATOR_TIERS,
    MUTATION_OPT_IN,
    SEQUENCE,
    AcceptanceError,
    RepositoryCapabilities,
    discover_repository_capabilities,
    redact,
    require_mutation_opt_in,
    validate_evidence_report,
    validate_live_target,
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
    assert capabilities.api_routes == ("/pursers/join", "/pursers/status")
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
        )
