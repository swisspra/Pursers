from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from . import harness as harness_module
from .harness import (
    CURRENT_OPERATOR_TIERS,
    MAX_EVIDENCE_FILE_BYTES,
    MUTATION_OPT_IN,
    REQUIRED_INVENTORY,
    REQUIRED_SUITES,
    SEQUENCE,
    AcceptanceCapabilityUnavailable,
    AcceptanceError,
    LiveTarget,
    RepositoryCapabilities,
    discover_repository_capabilities,
    probe_host_identity,
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


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return len(payload).to_bytes(4, "big") + kind + payload + checksum.to_bytes(4, "big")


def _png_bytes(identifier: str) -> bytes:
    width, height = 320, 180
    seed = hashlib.sha256(identifier.encode()).digest()
    pixel = seed[:3]
    rows = (b"\x00" + pixel * width) * height
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, 2, 0, 0, 0))
    )
    annotation = b"observation\x00" + identifier.encode() + b":" + seed.hex().encode() * 4
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", annotation)
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


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
    report_target = report.get("target")
    target = dict(report_target) if isinstance(report_target, dict) else dict(TARGET)
    base_url = str(target["base_url"]).rstrip("/")
    host = report.get("host") if isinstance(report.get("host"), dict) else {}
    host_reference = host.get("evidence", "host.json")
    if isinstance(host_reference, str):
        _write_json(tmp_path / host_reference, {
            "schema_version": 1,
            "evidence_kind": "host_identity",
            "target": target,
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
        screenshot = tmp_path / "screenshots" / f"{_safe_name(identifier)}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(_png_bytes(identifier))
        snapshot = tmp_path / "snapshots" / f"{_safe_name(identifier)}.json"
        _write_json(snapshot, {
            "schema_version": 1,
            "observation_id": identifier,
            "page_url": f"{base_url}/dashboard",
            "captured_at": CAPTURED_AT,
            "snapshot": {
                "role": "document",
                "name": "Pursers Home acceptance",
                "children": [
                    {"role": "heading", "name": identifier},
                    {"role": "status", "name": "visible and verified"},
                    {"role": "main", "name": f"Acceptance surface for {identifier}"},
                ],
            },
        })
        _write_json(tmp_path / reference, {
            "schema_version": 1,
            "evidence_kind": "browser_observation",
            "observation_id": identifier,
            "target": target,
            "host": {"version": HOST["version"], "build": HOST["build"]},
            "candidate_commit": CANDIDATE_SHA,
            "captured_at": CAPTURED_AT,
            "page_url": f"{base_url}/dashboard",
            "screenshot": _descriptor(screenshot, tmp_path),
            "accessibility_snapshot": _descriptor(snapshot, tmp_path),
            "assertions": [
                {
                    "name": "observation heading",
                    "path": ["children", 0, "name"],
                    "operator": "equals",
                    "expected": identifier,
                }
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
            "target": target,
            "started_at": CAPTURED_AT,
            "finished_at": "2026-09-08T12:01:00+00:00",
            "exit_code": 0,
            "output": _descriptor(output, tmp_path),
        })
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _write_forged_report(tmp_path: Path, report: dict[str, object]) -> Path:
    path = _write_report(tmp_path, report)
    shared = tmp_path / "shared-minimal.png"
    shared.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
    )
    for item in [*report["steps"], *report["inventory"]]:  # type: ignore[misc]
        reference = item["evidence"]
        receipt_path = tmp_path / reference
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        snapshot_path = tmp_path / receipt["accessibility_snapshot"]["path"]
        _write_json(snapshot_path, {
            "schema_version": 1,
            "observation_id": item["id"],
            "page_url": receipt["page_url"],
            "captured_at": receipt["captured_at"],
            "snapshot": {"role": "status", "name": "visible"},
        })
        receipt["screenshot"] = _descriptor(shared, tmp_path)
        receipt["accessibility_snapshot"] = _descriptor(snapshot_path, tmp_path)
        receipt["assertions"] = [
            {"name": "visible state", "passed": True, "actual": "visible"}
        ]
        _write_json(receipt_path, receipt)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _validate(tmp_path: Path, report: dict[str, object]) -> dict[str, object]:
    return _validate_report(
        _write_report(tmp_path, report),
        validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
        RepositoryCapabilities((), (), (), (), ()),
        CANDIDATE_SHA,
    )


def _validate_report(
    path: Path,
    target: LiveTarget,
    capabilities: RepositoryCapabilities,
    candidate_commit: str,
) -> dict[str, object]:
    with (
        patch.object(harness_module, "probe_host_identity", return_value=dict(HOST)),
        patch.object(harness_module, "_execute_required_suite", return_value=None),
    ):
        return harness_module._validate_evidence_report(
            path,
            target,
            capabilities,
            candidate_commit,
            trusted_browser_observer=_FixtureTrustedBrowserObserver(path.parent),
        )


class _FixtureTrustedBrowserObserver:
    """Unit-test double only; it is never used by the public acceptance path."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def capture(
        self, request: harness_module.BrowserObservationRequest
    ) -> harness_module.TrustedBrowserCapture:
        prefix = "step" if request.observation_id in SEQUENCE else "item"
        receipt = json.loads(
            (
                self.root
                / "observations"
                / f"{prefix}-{_safe_name(request.observation_id)}.json"
            ).read_text(encoding="utf-8")
        )
        snapshot_wrapper = json.loads(
            (
                self.root / receipt["accessibility_snapshot"]["path"]
            ).read_text(encoding="utf-8")
        )
        return harness_module.TrustedBrowserCapture(
            observer_id="unit-test-fixture-observer",
            observation_id=request.observation_id,
            target=request.target,
            host_product="AionUi",
            host_version=request.host_version,
            host_build=request.host_build,
            host_identity_source="host-api",
            candidate_commit=request.candidate_commit,
            captured_at=request.captured_at,
            page_url=request.page_url,
            screenshot=(self.root / receipt["screenshot"]["path"]).read_bytes(),
            snapshot=snapshot_wrapper["snapshot"],
        )


def _write_verifier_observer(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3
import base64, hashlib, json, sys, zlib
def chunk(kind, payload):
    checksum = zlib.crc32(kind + payload) & 0xffffffff
    return len(payload).to_bytes(4, "big") + kind + payload + checksum.to_bytes(4, "big")
def screenshot(identifier):
    width, height = 320, 180
    seed = hashlib.sha256(identifier.encode()).digest()
    rows = (b"\\x00" + seed[:3] * width) * height
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    annotation = b"observation\\x00" + identifier.encode() + b":" + seed.hex().encode() * 4
    return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header) + chunk(b"tEXt", annotation) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
request = json.load(sys.stdin)
identifier = request["observation_id"]
snapshot = {
    "role": "document",
    "name": "Pursers Home acceptance",
    "children": [
        {"role": "heading", "name": identifier},
        {"role": "status", "name": "visible and verified"},
        {"role": "main", "name": "Acceptance surface for " + identifier},
    ],
}
json.dump({
    "observer_id": "verifier-session-1",
    "observation_id": identifier,
    "target": request["target"],
    "host_product": "AionUi",
    "host_version": request["host_version"],
    "host_build": request["host_build"],
    "host_identity_source": "host-api",
    "candidate_commit": request["candidate_commit"],
    "captured_at": request["captured_at"],
    "page_url": request["page_url"],
    "screenshot_base64": base64.b64encode(screenshot(identifier)).decode(),
    "snapshot": snapshot,
}, sys.stdout)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


@contextmanager
def _minimal_status_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/pursers/status":
                self.send_error(404)
                return
            body = json.dumps({"ok": True, "host": HOST}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


def test_repository_discovery_reports_assembled_contracts() -> None:
    capabilities = discover_repository_capabilities()
    assert capabilities.api_routes == (
        "/pursers/groups",
        "/pursers/groups/create",
        "/pursers/groups/remove",
        "/pursers/groups/status",
        "/pursers/groups/update",
        "/pursers/join",
        "/pursers/onboarding/connect",
        "/pursers/onboarding/recover",
        "/pursers/onboarding/rotate",
        "/pursers/onboarding/status",
        "/pursers/onboarding/validate",
        "/pursers/results",
        "/pursers/seat-lifecycle/disconnect",
        "/pursers/seat-lifecycle/join",
        "/pursers/seat-lifecycle/status",
        "/pursers/status",
        "/pursers/team/apply",
        "/pursers/team/plan",
        "/pursers/team/seat/pause",
        "/pursers/team/seat/stop",
        "/pursers/team/status",
        "/pursers/tickets",
        "/pursers/tickets/cancel",
        "/pursers/tickets/create",
        "/pursers/tickets/get",
        "/pursers/tickets/status",
    )
    assert set(capabilities.assistants) == {
        "pursers-reviewer-claude",
        "pursers-reviewer-codex",
        "pursers-worker-claude",
        "pursers-worker-codex",
    }
    # Derived from tools/dashboard-ui/dashboard-entry.html data-view attributes.
    # The dashboard rework promoted seven top-level views; the former agents,
    # fleet, links and today panels are now data-subview panels beneath them.
    assert set(capabilities.personal_views) == {
        "activity",
        "approvals",
        "home",
        "projects",
        "settings",
        "team",
        "work",
    }
    assert "read_only_discovery" in capabilities.capabilities
    assert "door_rotation" in capabilities.capabilities
    assert "team_lifecycle" in capabilities.capabilities
    assert "ticket_lifecycle" in capabilities.capabilities
    assert "result_visibility" in capabilities.capabilities
    assert "seat_lifecycle" in capabilities.capabilities
    assert capabilities.missing_mutation_capabilities == ()


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


def test_host_identity_probe_reports_current_status_contract_gap() -> None:
    target = validate_live_target("http://127.0.0.1:8765")
    with (
        patch.object(
            harness_module,
            "_read_extension_status",
            return_value={"ok": True, "push_mode": "push", "seats": []},
        ),
        pytest.raises(
            AcceptanceCapabilityUnavailable,
            match="lacks verifiable AionUi host version/build identity",
        ),
    ):
        probe_host_identity(target)


def test_mutation_requires_exact_opt_in() -> None:
    with pytest.raises(AcceptanceError, match="mutation opt-in"):
        require_mutation_opt_in(None)
    with pytest.raises(AcceptanceError, match="mutation opt-in"):
        require_mutation_opt_in("yes")
    require_mutation_opt_in(MUTATION_OPT_IN)


def test_redaction_removes_sensitive_keys_and_values() -> None:
    value = {
        "authorization": "Bearer abc",
        "nested": [
            "prs1.not-a-real-door-value",
            {"jwt": "e" + "yJabc.def.ghi"},
        ],
        "safe": "worker",
    }
    assert redact(value) == {
        "authorization": "[REDACTED]",
        "nested": ["[REDACTED]", {"jwt": "[REDACTED]"}],
        "safe": "worker",
    }


def test_evidence_validation_reaches_schema_after_all_sibling_contracts_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = tmp_path / "evidence.json"
    report.write_text("{}", encoding="utf-8")
    with pytest.raises(AcceptanceError, match="evidence schema_version must be 1"):
        _validate_report(
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
        _validate_report(
            report,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            capabilities,
            CANDIDATE_SHA,
        )


def test_complete_offline_bundle_and_self_selected_status_cannot_pass_without_observer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    with _minimal_status_server() as base_url:
        report = _complete_report()
        report["target"] = {
            "base_url": base_url,
            "board_id": "sandbox-home",
        }
        path = _write_report(tmp_path, report)
        with (
            patch.object(harness_module, "_execute_required_suite", return_value=None),
            pytest.raises(
                AcceptanceCapabilityUnavailable,
                match="trusted browser observer is unavailable",
            ),
        ):
            validate_evidence_report(
                path,
                validate_live_target(base_url, "sandbox-home"),
                RepositoryCapabilities((), (), (), (), ()),
                CANDIDATE_SHA,
            )


def test_verifier_browser_observer_replays_bounded_independent_capture(
    tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    verifier = tmp_path / "verifier" / "browser-observer"
    _write_verifier_observer(verifier)
    observer = harness_module.VerifierBrowserObserver(verifier, evidence)
    request = harness_module.BrowserObservationRequest(
        observation_id="live-dashboard",
        target=LiveTarget("http://127.0.0.1:8765", "sandbox-home"),
        host_version=HOST["version"],
        host_build=HOST["build"],
        candidate_commit=CANDIDATE_SHA,
        captured_at=CAPTURED_AT,
        page_url="http://127.0.0.1:8765/dashboard",
        assertions=({
            "name": "ready",
            "path": ["children", 0, "name"],
            "operator": "equals",
            "expected": "ready",
        },),
    )

    capture = observer.capture(request)

    assert capture.observer_id == "verifier-session-1"
    assert capture.screenshot.startswith(b"\x89PNG")
    assert capture.snapshot["children"][0]["name"] == "live-dashboard"
    assert capture.candidate_commit == CANDIDATE_SHA


def test_public_validation_uses_verifier_owned_browser_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    verifier = tmp_path / "verifier" / "browser-observer"
    _write_verifier_observer(verifier)
    with _minimal_status_server() as base_url:
        report = _complete_report()
        report["target"] = {"base_url": base_url, "board_id": "sandbox-home"}
        path = _write_report(evidence, report)
        with patch.object(harness_module, "_execute_required_suite", return_value=None):
            result = validate_evidence_report(
                path,
                validate_live_target(base_url, "sandbox-home"),
                RepositoryCapabilities((), (), (), (), ()),
                CANDIDATE_SHA,
                verifier,
            )
    assert result["candidate_commit"] == CANDIDATE_SHA
    assert result["steps_passed"] == len(SEQUENCE)


def test_report_owned_observer_cannot_make_self_authored_artifacts_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _write_report(tmp_path, _complete_report())
    forged_observer = tmp_path / "self-authored-observer"
    forged_observer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    forged_observer.chmod(0o700)

    with pytest.raises(AcceptanceError, match="verifier-owned outside"):
        validate_evidence_report(
            report,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
            forged_observer,
        )


def test_structurally_valid_report_passes_with_trusted_observers(
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


def test_trusted_observer_capture_must_match_report_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    path = _write_report(tmp_path, _complete_report())
    fixture = _FixtureTrustedBrowserObserver(tmp_path)

    class MismatchingObserver:
        def capture(
            self, request: harness_module.BrowserObservationRequest
        ) -> harness_module.TrustedBrowserCapture:
            capture = fixture.capture(request)
            return replace(capture, screenshot=capture.screenshot + b"tampered")

    with (
        patch.object(harness_module, "probe_host_identity", return_value=dict(HOST)),
        patch.object(harness_module, "_execute_required_suite", return_value=None),
        pytest.raises(AcceptanceError, match="trusted observer capture"),
    ):
        harness_module._validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
            trusted_browser_observer=MismatchingObserver(),
        )


def test_missing_evidence_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report["steps"][0]["evidence"] = "missing.json"  # type: ignore[index]
    path = _write_report(tmp_path, report)
    (tmp_path / "missing.json").unlink()
    with pytest.raises(AcceptanceError, match="missing or escapes"):
        _validate_report(
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
        _validate_report(
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
        _validate_report(
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
        _validate_report(
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
        _validate_report(
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
        _validate_report(
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
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            "b" * 40,
        )


def test_structured_offline_forgery_with_shared_minimal_browser_evidence_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_forged_report(tmp_path, report)

    with pytest.raises(AcceptanceError, match="substantive PNG"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_reused_valid_screenshot_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    first, second = report["steps"][:2]  # type: ignore[index]
    first_receipt = json.loads(
        (tmp_path / first["evidence"]).read_text(encoding="utf-8")
    )
    second_receipt_path = tmp_path / second["evidence"]
    second_receipt = json.loads(second_receipt_path.read_text(encoding="utf-8"))
    second_receipt["screenshot"] = first_receipt["screenshot"]
    _write_json(second_receipt_path, second_receipt)

    with pytest.raises(AcceptanceError, match="distinct screenshot"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_generic_accessibility_snapshot_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    receipt_path = tmp_path / report["steps"][0]["evidence"]  # type: ignore[index]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    snapshot_path = tmp_path / receipt["accessibility_snapshot"]["path"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["snapshot"] = {"role": "status", "name": "visible"}
    _write_json(snapshot_path, snapshot)
    receipt["accessibility_snapshot"] = _descriptor(snapshot_path, tmp_path)
    _write_json(receipt_path, receipt)

    with pytest.raises(AcceptanceError, match="does not bind"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_self_declared_browser_assertion_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    receipt_path = tmp_path / report["steps"][0]["evidence"]  # type: ignore[index]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["assertions"] = [
        {"name": "visible state", "passed": True, "actual": "visible"}
    ]
    _write_json(receipt_path, receipt)

    with pytest.raises(AcceptanceError, match="not verifiable"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_report_host_identity_must_match_active_loopback_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    path = _write_report(tmp_path, _complete_report())

    with (
        patch.object(
            harness_module,
            "probe_host_identity",
            return_value={
                "product": "AionUi",
                "version": "9.9.9",
                "build": "different-build",
            },
        ),
        patch.object(harness_module, "_execute_required_suite", return_value=None),
        pytest.raises(AcceptanceError, match="active loopback host"),
    ):
        harness_module._validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
            trusted_browser_observer=_FixtureTrustedBrowserObserver(tmp_path),
        )


def test_marker_only_suite_receipts_do_not_replace_independent_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    path = _write_report(tmp_path, _complete_report())

    def reject_marker_only_run(name: str, _command: str, _commit: str) -> None:
        raise AcceptanceError(f"independent suite execution failed: {name}")

    with (
        patch.object(harness_module, "probe_host_identity", return_value=dict(HOST)),
        patch.object(
            harness_module,
            "_execute_required_suite",
            side_effect=reject_marker_only_run,
        ),
        pytest.raises(
            AcceptanceError,
            match="independent suite execution failed: repository-python",
        ),
    ):
        harness_module._validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
            trusted_browser_observer=_FixtureTrustedBrowserObserver(tmp_path),
        )


def test_nonexistent_candidate_commit_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    with pytest.raises(AcceptanceError, match="unavailable"):
        _validate_report(
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
        _validate_report(
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
        _validate_report(
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
        _validate_report(
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
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )
