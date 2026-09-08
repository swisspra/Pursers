from __future__ import annotations

import hashlib
import json
import subprocess
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

CANDIDATE_SHA = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=Path(__file__).resolve().parents[4],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
TARGET = {
    "base_url": "http://127.0.0.1:8765",
    "board_id": "sandbox-home",
}
HOST = {"product": "AionUi", "version": "2.2.1", "build": "2026.09.08.1"}
CAPTURED_AT = "2026-09-08T12:00:00+00:00"


def _safe_name(value: str) -> str:
    return value.replace(".", "-").replace("_", "-")


def _complete_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": "real_browser_host",
        "mocked": False,
        "synthetic": False,
        "target": dict(TARGET),
        "host": {**HOST, "evidence": "host.json"},
        "steps": [
            {
                "id": identifier,
                "status": "passed",
                "evidence": f"observations/step-{_safe_name(identifier)}.json",
            }
            for identifier in SEQUENCE
        ],
        "inventory": [
            {
                "id": identifier,
                "status": "passed",
                "evidence": f"observations/item-{_safe_name(identifier)}.json",
            }
            for identifier in sorted(REQUIRED_INVENTORY)
        ],
        "suites": [
            {
                "name": name,
                "command": command,
                "status": "passed",
                "commit": CANDIDATE_SHA,
                "evidence": f"suites/{_safe_name(name)}.json",
            }
            for name, command in REQUIRED_SUITES.items()
        ],
        "all_existing_suites_passed": True,
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _descriptor(path: Path, root: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _suite_output(name: str) -> str:
    if name == "repository-python":
        return "central: 1 passed\nclient: 1 passed\npersonal: 1 passed\nwait-bridge: 1 passed\nseat-kit: 1 passed\n"
    if name.startswith("extension-"):
        return "TAP version 13\n# tests 1\n# pass 1\n# fail 0\n"
    if name == "dashboard-typecheck":
        return "> typecheck\n> tsc --noEmit\n"
    if name == "dashboard-build":
        return "> build\nbuilt in 1ms\n"
    if name == "repository-leak-scan":
        return "leak_scan: clean (0 violations)\n"
    return ""


def _write_report(tmp_path: Path, report: dict[str, object]) -> Path:
    screenshot = tmp_path / "browser.png"
    screenshot.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
    )
    host = report.get("host") if isinstance(report.get("host"), dict) else {}
    host_reference = host.get("evidence", "host.json")
    if isinstance(host_reference, str):
        _write_json(tmp_path / host_reference, {
            "schema_version": 1,
            "evidence_kind": "host_identity",
            "target": dict(TARGET),
            "product": "AionUi",
            "version": host.get("version", HOST["version"]),
            "build": host.get("build", HOST["build"]),
            "candidate_commit": CANDIDATE_SHA,
            "captured_at": CAPTURED_AT,
            "source": "aionui-about",
        })
    observations = [
        *report.get("steps", []),
        *report.get("inventory", []),
    ]
    for item in observations:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id", "missing")
        reference = item.get("evidence")
        if not isinstance(identifier, str) or not isinstance(reference, str):
            continue
        snapshot = tmp_path / "snapshots" / f"{_safe_name(identifier)}.json"
        _write_json(snapshot, {
            "schema_version": 1,
            "observation_id": identifier,
            "snapshot": {"role": "status", "name": "visible"},
        })
        _write_json(tmp_path / reference, {
            "schema_version": 1,
            "evidence_kind": "browser_observation",
            "observation_id": identifier,
            "target": dict(TARGET),
            "host": {"version": HOST["version"], "build": HOST["build"]},
            "candidate_commit": CANDIDATE_SHA,
            "captured_at": CAPTURED_AT,
            "page_url": "http://127.0.0.1:8765/dashboard",
            "screenshot": _descriptor(screenshot, tmp_path),
            "accessibility_snapshot": _descriptor(snapshot, tmp_path),
            "assertions": [
                {"name": "visible state", "passed": True, "actual": "visible"}
            ],
        })
    for suite in report.get("suites", []):
        if not isinstance(suite, dict):
            continue
        name = suite.get("name", "missing")
        reference = suite.get("evidence")
        if not isinstance(name, str) or not isinstance(reference, str):
            continue
        output = tmp_path / "suite-output" / f"{_safe_name(name)}.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_suite_output(name), encoding="utf-8")
        _write_json(tmp_path / reference, {
            "schema_version": 1,
            "evidence_kind": "suite_run",
            "name": name,
            "command": suite.get("command"),
            "commit": suite.get("commit"),
            "target": dict(TARGET),
            "started_at": CAPTURED_AT,
            "finished_at": "2026-09-08T12:01:00+00:00",
            "exit_code": 0,
            "output": _descriptor(output, tmp_path),
        })
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
    report = _complete_report()
    report["steps"][0]["evidence"] = "missing.json"  # type: ignore[index]
    path = _write_report(tmp_path, report)
    (tmp_path / "missing.json").unlink()
    with pytest.raises(AcceptanceError, match="missing or escapes"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


@pytest.mark.parametrize("field", ("version", "build"))
def test_missing_exact_host_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    del report["host"][field]  # type: ignore[index]
    with pytest.raises(AcceptanceError, match=f"host {field} must be"):
        _validate(tmp_path, report)


@pytest.mark.parametrize(("field", "value"), (("version", "1"), ("build", "1")))
def test_placeholder_host_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: str
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report["host"][field] = value  # type: ignore[index]
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
    report = _complete_report()
    path = _write_report(tmp_path, report)
    reference = report["steps"][0]["evidence"]  # type: ignore[index]
    receipt = tmp_path / reference
    receipt.unlink()
    outside = tmp_path.parent / "outside-evidence.json"
    outside.write_text("{}", encoding="utf-8")
    receipt.symlink_to(outside)
    with pytest.raises(AcceptanceError, match="missing or escapes"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


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
    reference = report["steps"][0]["evidence"]  # type: ignore[index]
    (tmp_path / reference).write_text(
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
    report = _complete_report()
    path = _write_report(tmp_path, report)
    reference = report["steps"][0]["evidence"]  # type: ignore[index]
    receipt = tmp_path / reference
    receipt.unlink()
    receipt.mkdir()
    with pytest.raises(AcceptanceError, match="regular file"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_oversized_evidence_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    reference = report["steps"][0]["evidence"]  # type: ignore[index]
    (tmp_path / reference).write_bytes(b"x" * (MAX_EVIDENCE_FILE_BYTES + 1))
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


def test_review_attack_reusing_two_byte_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    (tmp_path / "artifact.txt").write_bytes(b"ok")
    report = _complete_report()
    report["host"] = {  # type: ignore[assignment]
        "product": "AionUi",
        "version": "1",
        "build": "1",
        "evidence": "artifact.txt",
    }
    for item in [*report["steps"], *report["inventory"], *report["suites"]]:  # type: ignore[misc]
        item["evidence"] = "artifact.txt"
    for suite in report["suites"]:  # type: ignore[union-attr]
        suite["commit"] = "b" * 40
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(AcceptanceError):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            "b" * 40,
        )


def test_nonexistent_candidate_commit_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    with pytest.raises(AcceptanceError, match="unavailable"):
        validate_evidence_report(
            _write_report(tmp_path, _complete_report()),
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            "b" * 40,
        )


def test_reused_primary_receipt_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report["inventory"][0]["evidence"] = report["steps"][0]["evidence"]  # type: ignore[index]
    with pytest.raises(AcceptanceError, match="distinct receipt"):
        _validate(tmp_path, report)


def test_observation_receipt_id_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    reference = report["steps"][0]["evidence"]  # type: ignore[index]
    receipt_path = tmp_path / reference
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["observation_id"] = "different"
    _write_json(receipt_path, receipt)
    with pytest.raises(AcceptanceError, match="does not bind"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_suite_output_without_success_marker_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    suite = report["suites"][0]  # type: ignore[index]
    receipt_path = tmp_path / suite["evidence"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    output_path = tmp_path / receipt["output"]["path"]
    output_path.write_text("ok\n", encoding="utf-8")
    receipt["output"] = _descriptor(output_path, tmp_path)
    _write_json(receipt_path, receipt)
    with pytest.raises(AcceptanceError, match="lacks success marker"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_suite_output_hash_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    suite = report["suites"][0]  # type: ignore[index]
    receipt_path = tmp_path / suite["evidence"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    output_path = tmp_path / receipt["output"]["path"]
    output_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="sha256 does not match"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_host_receipt_version_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    receipt_path = tmp_path / "host.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["version"] = "9.9.9"
    _write_json(receipt_path, receipt)
    with pytest.raises(AcceptanceError, match="does not match"):
        validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )
