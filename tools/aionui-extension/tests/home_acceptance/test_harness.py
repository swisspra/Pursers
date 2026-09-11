from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from . import harness as harness_module
from .harness import (
    CURRENT_OPERATOR_TOPOLOGY,
    CURRENT_OPERATOR_TIERS,
    MAX_EVIDENCE_FILE_BYTES,
    MUTATION_OPT_IN,
    REQUIRED_INVENTORY,
    REQUIRED_FINAL_GATES,
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
    _surface_for_identifier,
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


def _legacy_report() -> dict[str, object]:
    """Legacy single-surface report shape, kept only for legacy-path coverage."""
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
        "final_gates": [
            {
                "id": identifier,
                "status": "passed",
                "evidence": f"observations/item-{_safe_name(identifier)}.json",
            }
            for identifier in REQUIRED_FINAL_GATES
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


def _fixture_browser_assertions(identifier: str) -> list[dict[str, Any]]:
    assertions = list(harness_module._canonical_browser_assertions(identifier))
    if assertions:
        return assertions
    return [{
        "name": f"browser context: {identifier}",
        "path": ["nodes"],
        "operator": "ax_name_contains",
        "expected": identifier,
    }]


def _complete_report() -> dict[str, object]:
    """Complete final-train report: per-surface bindings and operator topology."""
    report = _legacy_report()
    report["operator_topology"] = {
        **{kind: dict(value) for kind, value in CURRENT_OPERATOR_TOPOLOGY.items()},
        "optional_opus_worker": {
            "enabled": False,
            "count": 0,
            "model": "opus",
            "tier_max": 2,
        },
    }
    report["surfaces"] = {
        "aionui": {
            "target": dict(TARGET),
            "runtime": {
                **HOST,
                "identity_source": "signed-aionui-webui-listener",
            },
            "candidate_commit": CANDIDATE_SHA,
        },
        "fleet": {
            "target": {
                "base_url": "http://127.0.0.1:8766",
                "board_id": TARGET["board_id"],
            },
            "runtime": {
                "product": "Pursers Fleet",
                "version": "candidate-72c4345be172",
                "build": "a" * 64,
                "identity_source": "verifier-pinned-process-artifact",
            },
            "candidate_commit": CANDIDATE_SHA,
        },
        "personal": {
            "target": dict(TARGET),
            "runtime": {
                "product": "Pursers Personal",
                "version": "5.0.0a25",
                "build": "b" * 64,
                "identity_source": "verifier-pinned-personal-mcp-runtime",
            },
            "candidate_commit": CANDIDATE_SHA,
        },
    }
    return report


# The per-surface report is now the only complete shape; keep the old name so
# existing per-surface tests keep reading naturally.
_complete_surface_report = _complete_report


def _retarget(
    report: dict[str, object], base_url: str, board_id: str = "sandbox-home"
) -> dict[str, object]:
    """Point the primary target and the AionUi-hosted surfaces at a live server."""
    target = {"base_url": base_url, "board_id": board_id}
    report["target"] = dict(target)
    surfaces = report.get("surfaces")
    if isinstance(surfaces, dict):
        for surface_id in ("aionui", "personal"):
            surfaces[surface_id]["target"] = dict(target)
        surfaces["fleet"]["target"] = {
            **surfaces["fleet"]["target"],
            "board_id": board_id,
        }
    return report


def test_report_without_surface_bindings_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    with pytest.raises(AcceptanceError, match="surface"):
        _validate(tmp_path, _legacy_report())


def test_report_with_surfaces_but_no_operator_topology_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report.pop("operator_topology")
    with pytest.raises(AcceptanceError):
        _validate(tmp_path, report)


def test_dashboard_ui_identifiers_bind_to_the_personal_surface() -> None:
    for identifier in (
        "dashboard-ui.logic",
        "dashboard-ui.styles",
        "dashboard-ui.shell",
        "personal-mcp.surface",
    ):
        assert _surface_for_identifier(identifier) == "personal"
    assert _surface_for_identifier("fleet-dashboard.surface") == "fleet"
    assert _surface_for_identifier("extension-join.surface") == "aionui"


def test_dashboard_ui_observation_bound_to_aionui_runtime_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report["surfaces"]["personal"]["runtime"] = {  # type: ignore[index]
        **HOST,
        "identity_source": "signed-aionui-webui-listener",
    }
    with pytest.raises(AcceptanceError):
        _validate(tmp_path, report)


def test_surface_report_rejects_stale_six_worker_two_reviewer_topology(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_surface_report()
    report["operator_topology"]["codex_worker"]["count"] = 6  # type: ignore[index]
    report["operator_topology"]["codex_reviewer"]["count"] = 2  # type: ignore[index]
    with pytest.raises(AcceptanceError, match="stale or invalid"):
        _validate(tmp_path, report)


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
        *report.get("final_gates", []),
    ]
    for item in observations:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id", "missing")
        reference = item.get("evidence")
        if not isinstance(identifier, str) or not isinstance(reference, str):
            continue
        surface_id = harness_module._surface_for_identifier(identifier)
        surfaces = report.get("surfaces")
        surface = surfaces.get(surface_id) if isinstance(surfaces, dict) else None
        observation_target = surface["target"] if isinstance(surface, dict) else target
        observation_base_url = str(observation_target["base_url"]).rstrip("/")
        screenshot = tmp_path / "screenshots" / f"{_safe_name(identifier)}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(_png_bytes(identifier))
        snapshot = tmp_path / "snapshots" / f"{_safe_name(identifier)}.json"
        assertions = _fixture_browser_assertions(identifier)
        _write_json(snapshot, {
            "schema_version": 1,
            "observation_id": identifier,
            "page_url": f"{observation_base_url}/dashboard",
            "captured_at": CAPTURED_AT,
            "snapshot": {
                "title": "Pursers Home acceptance",
                "viewport": {"w": 1280, "h": 800},
                "nodes": [
                    {"role": "heading", "name": assertions[0]["expected"]},
                    {"role": "status", "name": f"visible and verified: {identifier}"},
                    {"role": "main", "name": f"Acceptance surface for {identifier}"},
                ],
            },
        })
        receipt = {
            "schema_version": 1,
            "evidence_kind": "browser_observation",
            "observation_id": identifier,
            "target": observation_target,
            "candidate_commit": CANDIDATE_SHA,
            "captured_at": CAPTURED_AT,
            "page_url": f"{observation_base_url}/dashboard",
            "screenshot": _descriptor(screenshot, tmp_path),
            "accessibility_snapshot": _descriptor(snapshot, tmp_path),
            "assertions": assertions,
        }
        if isinstance(surface, dict):
            receipt["surface_id"] = surface_id
            receipt["runtime"] = surface["runtime"]
            receipt["attestation_nonce"] = _fixture_nonce(identifier)
            receipt["attestation"] = (
                _fixture_attestation(
                    surface["runtime"],
                    CANDIDATE_SHA,
                    observation_target["board_id"],
                    _fixture_nonce(identifier),
                )
                if surface_id == "personal"
                else None
            )
        else:
            receipt["host"] = {"version": HOST["version"], "build": HOST["build"]}
        _write_json(tmp_path / reference, receipt)
        typed_conjuncts = harness_module._canonical_typed_conjuncts(identifier)
        if typed_conjuncts:
            typed_references = []
            for index, conjunct in enumerate(typed_conjuncts, start=1):
                typed_reference = (
                    f"typed/{_safe_name(identifier)}-{index}.json"
                )
                _write_json(tmp_path / typed_reference, {
                    "schema_version": 1,
                    "kind": conjunct["kind"],
                    "observation_id": identifier,
                    "fixture_record": True,
                })
                typed_references.append({
                    "evidence": typed_reference,
                    "run_id": "acceptance-run-1",
                    "action_id": identifier,
                    "entity": identifier,
                    "causal_index": index,
                })
            item["typed_evidence"] = typed_references
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
    for item in [*report["steps"], *report["inventory"], *report["final_gates"]]:  # type: ignore[misc]
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
            trusted_typed_evidence_evaluator=_FixtureTrustedTypedEvidenceEvaluator(),
        )


CHALLENGE_KEY = b"\x44" * 32
PERSONAL_PID = 4242
PERSONAL_SOURCE = "/PATH/TO/candidate/apps_server.py"


def _fixture_nonce(identifier: str) -> str:
    """One distinct verifier nonce per observation, as the real flow requires."""
    return hashlib.sha256(f"nonce:{identifier}".encode()).hexdigest()


def _fixture_attestation(
    runtime: dict[str, Any],
    candidate_commit: str,
    board_id: str,
    nonce: str,
    *,
    key: bytes = CHALLENGE_KEY,
    **overrides: Any,
) -> dict[str, Any]:
    """Sign a claim the way apps_server.acceptance_attestation serializes it."""
    claim = {
        "schema_version": 1,
        "server_name": "On Board Personal",
        "version": runtime["version"],
        "build": runtime["build"],
        "candidate_commit": candidate_commit,
        "candidate_source": PERSONAL_SOURCE,
        "board_id": board_id,
        "pid": PERSONAL_PID,
        "transport": "stdio",
        "nonce": nonce,
    }
    claim.update(overrides)
    payload = json.dumps(claim, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**claim, "signature": hmac.new(key, payload, hashlib.sha256).hexdigest()}


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
            host_product=request.runtime_product,
            host_version=request.host_version,
            host_build=request.host_build,
            host_identity_source=request.runtime_identity_source,
            candidate_commit=request.candidate_commit,
            captured_at=request.captured_at,
            page_url=request.page_url,
            screenshot=(self.root / receipt["screenshot"]["path"]).read_bytes(),
            snapshot=snapshot_wrapper["snapshot"],
            surface_id=request.surface_id,
            attestation=receipt.get("attestation"),
            attestation_nonce=receipt.get("attestation_nonce", ""),
        )

    def personal_challenge(self) -> dict[str, Any]:
        return {
            "key": CHALLENGE_KEY,
            "pid": PERSONAL_PID,
            "candidate_source": PERSONAL_SOURCE,
        }


class _FixtureTrustedTypedEvidenceEvaluator:
    """Unit-test double for verifier-owned typed evidence evaluation."""

    def evaluate(
        self, request: harness_module.TypedEvidenceRequest
    ) -> harness_module.TrustedTypedEvidenceEvaluation:
        document = json.loads(request.evidence_path.read_text(encoding="utf-8"))
        passed = document.get("fixture_record") is True and "passed" not in document
        return harness_module.TrustedTypedEvidenceEvaluation(
            verifier_id="unit-test-fixture-typed-evaluator",
            observation_id=request.observation_id,
            run_id=request.run_id,
            action_id=request.action_id,
            entity=request.entity,
            causal_index=request.causal_index,
            surface_id=request.surface_id,
            board_id=request.board_id,
            candidate_commit=request.candidate_commit,
            kind=request.conjunct["kind"],
            passed=passed,
            predicate_sha256=harness_module._json_digest(request.conjunct),
            evidence_sha256=hashlib.sha256(request.evidence_path.read_bytes()).hexdigest(),
        )


def _write_verifier_observer(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3
import base64, hashlib, hmac, json, sys, zlib
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
    "title": "Pursers Home acceptance",
    "viewport": {"w": 1280, "h": 800},
    "nodes": [
        {"role": "heading", "name": request["assertions"][0]["expected"]},
        {"role": "status", "name": "visible and verified: " + identifier},
        {"role": "main", "name": "Acceptance surface for " + identifier},
    ],
}
attestation = None
nonce = hashlib.sha256(("nonce:" + identifier).encode()).hexdigest()
if request["surface_id"] == "personal":
    claim = {
        "schema_version": 1,
        "server_name": "On Board Personal",
        "version": request["host_version"],
        "build": request["host_build"],
        "candidate_commit": request["candidate_commit"],
        "candidate_source": "/PATH/TO/candidate/apps_server.py",
        "board_id": request["target"]["board_id"],
        "pid": 4242,
        "transport": "stdio",
        "nonce": nonce,
    }
    signed = json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()
    claim["signature"] = hmac.new(bytes.fromhex("44" * 32), signed, hashlib.sha256).hexdigest()
    attestation = claim
json.dump({
    "observer_id": "verifier-session-1",
    "observation_id": identifier,
    "surface_id": request["surface_id"],
    "target": request["target"],
    "host_product": request["runtime_product"],
    "host_version": request["host_version"],
    "host_build": request["host_build"],
    "host_identity_source": request["runtime_identity_source"],
    "candidate_commit": request["candidate_commit"],
    "captured_at": request["captured_at"],
    "page_url": request["page_url"],
    "screenshot_base64": base64.b64encode(screenshot(identifier)).decode(),
    "snapshot": snapshot,
    "attestation": attestation,
    "attestation_nonce": nonce,
}, sys.stdout)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    key_path = path.parent / "acceptance-challenge.key"
    key_path.write_bytes(CHALLENGE_KEY)
    key_path.chmod(0o600)
    pid_path = path.parent / "personal.pid"
    pid_path.write_text(f"{PERSONAL_PID}\n", encoding="utf-8")
    (path.parent / "observer.json").write_text(
        json.dumps({
            "schema_version": 1,
            "observer_id": "verifier-session-1",
            "repository_root": str(Path(PERSONAL_SOURCE).parent),
            "surfaces": {
                "personal": {
                    "runtime": {
                        "artifact": Path(PERSONAL_SOURCE).name,
                        "challenge_key": str(key_path),
                        "pid_file": str(pid_path),
                    }
                }
            },
        }),
        encoding="utf-8",
    )


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
        "five_workers_three_reviewers",
        "ticket_offer_claim",
        "ticket_submit_independent_review",
        "result_visible",
        "pause_resume_stop",
        "clean_reconnect_after_rotation",
    )
    assert CURRENT_OPERATOR_TIERS == {
        "goose_worker": 1,
        "codex_worker": 2,
        "codex_reviewer": 2,
        "optional_opus_worker": 2,
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
        report = _retarget(_complete_report(), base_url)
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
    assert capture.snapshot["nodes"][0]["name"] == "ready"
    assert capture.candidate_commit == CANDIDATE_SHA


def test_public_validation_requires_typed_evaluator_after_browser_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    verifier = tmp_path / "verifier" / "browser-observer"
    _write_verifier_observer(verifier)
    with _minimal_status_server() as base_url:
        report = _retarget(_complete_report(), base_url)
        path = _write_report(evidence, report)
        with (
            patch.object(harness_module, "_execute_required_suite", return_value=None),
            pytest.raises(
                AcceptanceCapabilityUnavailable,
                match="trusted typed evidence evaluator is unavailable",
            ),
        ):
            validate_evidence_report(
                path,
                validate_live_target(base_url, "sandbox-home"),
                RepositoryCapabilities((), (), (), (), ()),
                CANDIDATE_SHA,
                verifier,
            )


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
        "final_gates_passed": len(REQUIRED_FINAL_GATES),
        "suites_passed": len(REQUIRED_SUITES),
    }


def test_generic_assertion_cannot_replace_id_specific_required_fact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    reference = report["steps"][0]["evidence"]  # type: ignore[index]
    receipt_path = tmp_path / reference
    receipt = json.loads(receipt_path.read_text())
    receipt["assertions"] = [{
        "name": "generic title", "path": ["title"],
        "operator": "contains", "expected": "Pursers",
    }]
    _write_json(receipt_path, receipt)
    with pytest.raises(AcceptanceError, match="canonical visual fact"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def _inventory_item(report: dict[str, object], identifier: str) -> dict[str, Any]:
    return next(
        item for item in report["inventory"]  # type: ignore[index]
        if item["id"] == identifier
    )


def test_all_of_nonvisual_fact_without_typed_evidence_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    _inventory_item(report, "dashboard-ui.styles").pop("typed_evidence", None)
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(AcceptanceError, match="typed evidence reference"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_report_owned_passed_result_cannot_replace_typed_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    typed = _inventory_item(report, "dashboard-ui.styles")["typed_evidence"][0]
    evidence_path = tmp_path / typed["evidence"]
    forged = json.loads(evidence_path.read_text(encoding="utf-8"))
    forged["passed"] = True
    _write_json(evidence_path, forged)
    with pytest.raises(AcceptanceError, match="does not prove canonical conjunct"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


@pytest.mark.parametrize(
    ("field", "wrong"),
    (
        ("observation_id", "wrong-observation"),
        ("run_id", "wrong-run"),
        ("action_id", "wrong-action"),
        ("entity", "wrong-entity"),
        ("causal_index", 999),
        ("surface_id", "fleet"),
        ("board_id", "wrong-board"),
        ("candidate_commit", "f" * 40),
    ),
)
def test_typed_evaluation_must_match_full_correlation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    wrong: object,
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    path = _write_report(tmp_path, _complete_report())
    fixture = _FixtureTrustedTypedEvidenceEvaluator()

    class WrongCorrelationEvaluator:
        def evaluate(
            self, request: harness_module.TypedEvidenceRequest
        ) -> harness_module.TrustedTypedEvidenceEvaluation:
            return replace(fixture.evaluate(request), **{field: wrong})

    with (
        patch.object(harness_module, "probe_host_identity", return_value=dict(HOST)),
        patch.object(harness_module, "_execute_required_suite", return_value=None),
        pytest.raises(AcceptanceError, match="does not prove canonical conjunct"),
    ):
        harness_module._validate_evidence_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
            trusted_browser_observer=_FixtureTrustedBrowserObserver(tmp_path),
            trusted_typed_evidence_evaluator=WrongCorrelationEvaluator(),
        )


def test_typed_only_fact_still_requires_screenshot_and_ax_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    item = _inventory_item(report, "dashboard-ui.styles")
    receipt_path = tmp_path / item["evidence"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["assertions"] = []
    _write_json(receipt_path, receipt)
    with pytest.raises(AcceptanceError, match="explicit assertions"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_ax_name_assertion_ignores_hidden_accessibility_nodes() -> None:
    assertion = {
        "name": "required fact: hidden-state",
        "path": ["nodes"],
        "operator": "ax_name_contains",
        "expected": "Only hidden",
    }
    with pytest.raises(AcceptanceError, match="assertion failed"):
        harness_module._evaluate_browser_assertions(
            {"nodes": [{"name": "Only hidden", "ignored": True}]}, [assertion]
        )


def test_wrapper_metadata_cannot_make_identical_accessibility_states_unique(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    first = {"nodes": [{"role": "status", "name": "same"}], "nodeId": "one"}
    second = {
        "nodes": [{"role": "status", "name": "same", "nodeId": "child"}],
        "nodeId": "two",
        "captured_at": "later",
    }
    assert harness_module._snapshot_identity(first) == harness_module._snapshot_identity(second)


def test_final_quickstart_fleet503_and_o1_gates_are_mandatory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    report.pop("final_gates")
    with pytest.raises(AcceptanceError, match="final acceptance gates"):
        _validate(tmp_path, report)


def test_surface_report_binds_fleet_to_distinct_real_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    result = _validate(tmp_path, _complete_surface_report())
    assert result["inventory_passed"] == len(REQUIRED_INVENTORY)


def test_surface_report_rejects_fleet_relabelled_as_aionui_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_surface_report()
    report["surfaces"]["fleet"]["target"] = dict(TARGET)  # type: ignore[index]
    with pytest.raises(AcceptanceError, match="actual distinct loopback origin"):
        _validate(tmp_path, report)


def test_surface_report_rejects_fleet_signed_aionui_identity_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_surface_report()
    report["surfaces"]["fleet"]["runtime"][  # type: ignore[index]
        "identity_source"
    ] = "signed-aionui-webui-listener"
    with pytest.raises(AcceptanceError, match="pinned-process-artifact trust"):
        _validate(tmp_path, report)


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
            trusted_typed_evidence_evaluator=_FixtureTrustedTypedEvidenceEvaluator(),
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
            trusted_typed_evidence_evaluator=_FixtureTrustedTypedEvidenceEvaluator(),
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


# --- ground A: the Personal live transport challenge at validate time -------


def _personal_observations(report: dict[str, object]) -> list[dict[str, object]]:
    rows = [
        *report.get("steps", []),  # type: ignore[list-item]
        *report.get("inventory", []),  # type: ignore[list-item]
        *report.get("final_gates", []),  # type: ignore[list-item]
    ]
    return [
        row
        for row in rows
        if harness_module._surface_for_identifier(row["id"]) == "personal"
    ]


def test_personal_attestation_signed_with_a_foreign_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hand-authored attestation cannot be produced without the verifier key."""
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    item = _personal_observations(report)[0]
    receipt_path = tmp_path / item["evidence"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["attestation"] = _fixture_attestation(
        receipt["runtime"],
        CANDIDATE_SHA,
        receipt["target"]["board_id"],
        receipt["attestation_nonce"],
        key=b"\x99" * 32,
    )
    _write_json(receipt_path, receipt)

    with pytest.raises(AcceptanceError, match="not signed"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_personal_observation_without_an_attestation_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dropping the block is a refusal, not a surface that carries no challenge."""
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    item = _personal_observations(report)[0]
    receipt_path = tmp_path / item["evidence"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["attestation"] = None
    _write_json(receipt_path, receipt)

    with pytest.raises(AcceptanceError, match="no live transport attestation"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_correctly_signed_attestation_for_another_pid_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The signature is valid, but the pid is not the runtime the verifier holds."""
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    item = _personal_observations(report)[0]
    receipt_path = tmp_path / item["evidence"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["attestation"] = _fixture_attestation(
        receipt["runtime"],
        CANDIDATE_SHA,
        receipt["target"]["board_id"],
        receipt["attestation_nonce"],
        pid=PERSONAL_PID + 1,
    )
    _write_json(receipt_path, receipt)

    with pytest.raises(AcceptanceError, match="names a pid"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_reused_challenge_nonce_across_personal_observations_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two observations answering one nonce prove one moment, not two."""
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    first, second = _personal_observations(report)[:2]
    first_receipt = json.loads((tmp_path / first["evidence"]).read_text(encoding="utf-8"))
    second_path = tmp_path / second["evidence"]
    second_receipt = json.loads(second_path.read_text(encoding="utf-8"))
    second_receipt["attestation_nonce"] = first_receipt["attestation_nonce"]
    second_receipt["attestation"] = _fixture_attestation(
        second_receipt["runtime"],
        CANDIDATE_SHA,
        second_receipt["target"]["board_id"],
        first_receipt["attestation_nonce"],
    )
    _write_json(second_path, second_receipt)

    with pytest.raises(AcceptanceError, match="distinct challenge nonce"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


# --- uniqueness: what the observation shows, not how it was encoded --------


def _reference_for(report: dict[str, object], identifier: str) -> str:
    rows = [
        *report.get("steps", []),  # type: ignore[list-item]
        *report.get("inventory", []),  # type: ignore[list-item]
        *report.get("final_gates", []),  # type: ignore[list-item]
    ]
    return next(row["evidence"] for row in rows if row["id"] == identifier)


def _duplicate_expected_pair(report: dict[str, object]) -> tuple[str, str]:
    """Two ids whose canonical required fact is the same expected value.

    Only such a pair can hold identical accessibility state and still satisfy
    both assertions, which is exactly the persistent-label case the uniqueness
    gate has to catch.
    """
    rows = [
        *report.get("steps", []),  # type: ignore[list-item]
        *report.get("inventory", []),  # type: ignore[list-item]
        *report.get("final_gates", []),  # type: ignore[list-item]
    ]
    seen: dict[str, str] = {}
    for row in rows:
        identifier = row["id"]
        for assertion in harness_module._canonical_browser_assertions(identifier):
            expected = assertion["expected"]
            key = json.dumps(expected, sort_keys=True)
            if key in seen:
                return seen[key], identifier
            seen[key] = identifier
    pytest.skip("no two canonical facts share an expected value")


def _snapshot_of(tmp_path: Path, report: dict[str, object], identifier: str) -> Any:
    receipt = json.loads(
        (tmp_path / _reference_for(report, identifier)).read_text(encoding="utf-8")
    )
    wrapper = json.loads(
        (tmp_path / receipt["accessibility_snapshot"]["path"]).read_text(encoding="utf-8")
    )
    return wrapper["snapshot"]


def _replace_snapshot(
    tmp_path: Path, report: dict[str, object], identifier: str, snapshot: Any
) -> None:
    receipt_path = tmp_path / _reference_for(report, identifier)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    snapshot_path = tmp_path / receipt["accessibility_snapshot"]["path"]
    wrapper = json.loads(snapshot_path.read_text(encoding="utf-8"))
    wrapper["snapshot"] = snapshot
    snapshot_path.write_text(json.dumps(wrapper), encoding="utf-8")
    receipt["accessibility_snapshot"] = _descriptor(snapshot_path, tmp_path)
    _write_json(receipt_path, receipt)


def _with_node_ids(value: Any, counter: list[int]) -> Any:
    if isinstance(value, dict):
        counter[0] += 1
        return {
            "nodeId": f"cdp-{counter[0]}",
            **{key: _with_node_ids(item, counter) for key, item in value.items()},
        }
    if isinstance(value, list):
        return [_with_node_ids(item, counter) for item in value]
    return value


def _reversed_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed_keys(item) for item in value]
    return value


def _reencode_png(data: bytes) -> bytes:
    """Same pixels, different filter and compression level."""
    width, height, channels, pixels = harness_module.png_pixels.decode_png(data)
    stride = width * channels
    raw = bytearray()
    for row in range(height):
        line = pixels[row * stride:(row + 1) * stride]
        raw.append(1)
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            raw.append((line[index] - left) & 0xFF)
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, 2 if channels == 3 else 6, 0, 0, 0))
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def test_snapshots_differing_only_in_node_ids_are_one_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    source, target = _duplicate_expected_pair(report)
    relabelled = _with_node_ids(_snapshot_of(tmp_path, report, source), [0])
    _replace_snapshot(tmp_path, report, target, relabelled)

    with pytest.raises(AcceptanceError, match="unique underlying"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_snapshots_differing_only_in_key_order_are_one_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    source, target = _duplicate_expected_pair(report)
    reordered = _reversed_keys(_snapshot_of(tmp_path, report, source))
    _replace_snapshot(tmp_path, report, target, reordered)

    with pytest.raises(AcceptanceError, match="unique underlying"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_identical_pixels_reencoded_are_one_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PURSERS_HOME_ACCEPTANCE_MUTATE", MUTATION_OPT_IN)
    report = _complete_report()
    path = _write_report(tmp_path, report)
    first, second = report["steps"][:2]  # type: ignore[index]
    first_receipt = json.loads(
        (tmp_path / first["evidence"]).read_text(encoding="utf-8")
    )
    original = (tmp_path / first_receipt["screenshot"]["path"]).read_bytes()
    reencoded = _reencode_png(original)
    assert reencoded != original
    second_receipt_path = tmp_path / second["evidence"]
    second_receipt = json.loads(second_receipt_path.read_text(encoding="utf-8"))
    screenshot_path = tmp_path / second_receipt["screenshot"]["path"]
    screenshot_path.write_bytes(reencoded)
    second_receipt["screenshot"] = _descriptor(screenshot_path, tmp_path)
    _write_json(second_receipt_path, second_receipt)

    with pytest.raises(AcceptanceError, match="unique underlying"):
        _validate_report(
            path,
            validate_live_target("http://127.0.0.1:8765", "sandbox-home"),
            RepositoryCapabilities((), (), (), (), ()),
            CANDIDATE_SHA,
        )


def test_undecodable_screenshot_is_refused_rather_than_compared_by_bytes() -> None:
    """A 16-bit PNG is refused; the byte comparison is not a fallback."""
    header = (
        (320).to_bytes(4, "big") + (180).to_bytes(4, "big") + bytes((16, 2, 0, 0, 0))
    )
    sixteen_bit = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 64))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(AcceptanceError, match="cannot be compared"):
        harness_module._screenshot_identity(sixteen_bit)
