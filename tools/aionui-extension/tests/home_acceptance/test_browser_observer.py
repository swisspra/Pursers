"""Trust-boundary regressions for the verifier-owned browser observer.

These tests never skip on missing capability: they drive the real observer
executable with a scripted capture backend, so healthy replay, forged
artifacts, mismatched bindings, stale captures, unreachable hosts and missing
host identity all assert a concrete outcome.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from . import browser_observer as observer_module
from . import harness as harness_module
from . import runner as runner_module
from . import typed_evidence
from .harness import (
    AcceptanceCapabilityUnavailable,
    AcceptanceError,
    BrowserObservationRequest,
    LiveTarget,
    VerifierBrowserObserver,
    _validate_trusted_browser_observations,
)

BOARD = "sandbox-home-observer"
COMMIT = "0" * 40
HOST_VERSION = "2.2.1"
HOST_BUILD = "2026.09.08.1"
STATUS_PAYLOAD = json.dumps(
    {
        "schema_version": 1,
        "ok": True,
        "host": {"product": "AionUi", "version": HOST_VERSION, "build": HOST_BUILD},
        "extension": {"candidate_commit": COMMIT},
        "seats": [],
    }
).encode("utf-8")
HEALTH_PAYLOAD = json.dumps(
    {"status": "ok", "version": "0.2.1", "build_time": "1788252518"}
).encode("utf-8")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big")
        + kind
        + payload
        + zlib.crc32(kind + payload).to_bytes(4, "big")
    )


def _png(identifier: str, width: int = 320, height: int = 180) -> bytes:
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 0, 0, 0, 0))
    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(
            b"tEXt", b"observation\x00" + identifier.encode("ascii") + b"." * 512
        )
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def _snapshot() -> dict[str, object]:
    return {
        "title": "Pursers Home",
        "viewport": {"w": 1280, "h": 800},
        "nodes": [
            {"nodeId": index, "role": "button", "name": f"seat {index}", "ignored": False}
            for index in range(6)
        ],
    }


@contextmanager
def _status_host(payload: bytes, content_type: str = "application/json") -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(HEALTH_PAYLOAD)))
                self.end_headers()
                self.wfile.write(HEALTH_PAYLOAD)
                return
            if self.path != "/pursers/status":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_backend(
    tmp_path: Path,
    identifier: str,
    *,
    observed_commit: str = COMMIT,
    observed_board: str = BOARD,
) -> Path:
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    command = backend_dir / "capture_backend.py"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, json, sys\n"
        f"SCREENSHOT = {base64.b64encode(_png(identifier)).decode('ascii')!r}\n"
        f"SNAPSHOT = {json.dumps(_snapshot())!r}\n"
        f"HOST_STATUS = {json.dumps(json.loads(STATUS_PAYLOAD))!r}\n"
        f"OBSERVED_COMMIT = {observed_commit!r}\n"
        f"OBSERVED_BOARD = {observed_board!r}\n"
        "request = json.loads(sys.stdin.read() or '{}')\n"
        "host_status = json.loads(HOST_STATUS)\n"
        "host_status['extension']['candidate_commit'] = OBSERVED_COMMIT\n"
        "print(json.dumps({\n"
        "    'page_url': request['page_url'],\n"
        "    'screenshot_base64': SCREENSHOT,\n"
        "    'snapshot': json.loads(SNAPSHOT),\n"
        "    'host_status': {\n"
        "        'http_status': 200,\n"
        "        'content_type': 'application/json; charset=utf-8',\n"
        "        'payload': host_status,\n"
        "    },\n"
        "    'candidate_status': {\n"
        "        'http_status': 200,\n"
        "        'content_type': 'application/json; charset=utf-8',\n"
        "        'payload': {'schema_version': 1, 'candidate_commit': OBSERVED_COMMIT},\n"
        "    },\n"
        "    'selected_board': OBSERVED_BOARD,\n"
        "    'page_sha256': None,\n"
        "}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    return command


def _install(tmp_path: Path, backend: Path, *, max_age_s: int = 43_200) -> Path:
    observer_dir = tmp_path / "verifier"
    test_source = tmp_path / "browser_observer_with_signed_listener.py"
    marker = '\nif __name__ == "__main__":\n'
    signed_listener = (
        "\ndef _probe_signed_bundle_listener(_base_url: str) -> dict[str, str]:\n"
        f"    return {{'product': 'AionUi', 'version': {HOST_VERSION!r}, "
        f"'build': {HOST_BUILD!r}, 'source': 'signed-aionui-webui-listener'}}\n"
    )
    source = runner_module.OBSERVER_SOURCE.read_text(encoding="utf-8")
    assert source.count(marker) == 1
    test_source.write_text(source.replace(marker, signed_listener + marker), encoding="utf-8")
    original_source = runner_module.OBSERVER_SOURCE
    runner_module.OBSERVER_SOURCE = test_source
    try:
        exit_code = runner_module.main(
            [
                "runner.py",
                "install-observer",
                "--dir",
                str(observer_dir),
                "--backend-command",
                str(backend),
                "--max-age-s",
                str(max_age_s),
            ]
        )
    finally:
        runner_module.OBSERVER_SOURCE = original_source
    assert exit_code == 0
    return observer_dir


def _write_surface_manifest(tmp_path: Path, challenge_key: str) -> Path:
    runtime_dir = tmp_path / "personal-runtime"
    runtime_dir.mkdir(exist_ok=True)
    manifest = {
        "schema_version": 1,
        "candidate_commit": COMMIT,
        "surfaces": {
            "aionui": {
                "adapter": "signed-aionui",
                "target": {
                    "base_url": "http://127.0.0.1:18822",
                    "board_id": BOARD,
                },
            },
            "fleet": {
                "adapter": "pinned-process-artifact",
                "target": {
                    "base_url": "http://127.0.0.1:18821",
                    "board_id": BOARD,
                },
                "artifact": "tools/fleet-dashboard/fleet_dashboard.py",
            },
            "personal": {
                "adapter": "pinned-signed-aionui-personal-mcp",
                "target": {
                    "base_url": "http://127.0.0.1:18822",
                    "board_id": BOARD,
                },
                "artifact": (
                    "packages/personal/src/pursers_personal/resources/dashboard.html"
                ),
                "runtime": {
                    "artifact": "packages/personal/src/pursers_personal/apps_server.py",
                    "challenge_key": challenge_key,
                    "pid_file": str(runtime_dir / "personal.pid"),
                    "receipt": str(runtime_dir / "personal-runtime.json"),
                },
            },
        },
    }
    path = tmp_path / "surface-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _clean_candidate_git(*arguments: str) -> str:
    if arguments == ("rev-parse", "HEAD"):
        return COMMIT
    if arguments == ("status", "--porcelain"):
        return ""
    if arguments[:2] == ("ls-files", "--error-unmatch"):
        return arguments[-1]
    raise AssertionError(f"unexpected git invocation: {arguments}")


def _assertions_file(tmp_path: Path) -> Path:
    path = tmp_path / "assertions.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "first accessibility node is a button",
                    "path": ["nodes", 0, "role"],
                    "operator": "equals",
                    "expected": "button",
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _capture(
    tmp_path: Path, observer_dir: Path, base_url: str, observation: str = "door_connect"
) -> dict[str, object]:
    exit_code, evidence = _capture_exit(tmp_path, observer_dir, base_url, observation)
    assert exit_code == 0
    receipt = json.loads(
        (evidence / "observations" / f"{observation}.json").read_text(encoding="utf-8")
    )
    return {"evidence_root": evidence, "receipt": receipt}


def _capture_exit(
    tmp_path: Path,
    observer_dir: Path,
    base_url: str,
    observation: str = "door_connect",
    *,
    requested_board: str = BOARD,
    requested_commit: str = COMMIT,
) -> tuple[int, Path]:
    evidence = tmp_path / "evidence"
    exit_code = runner_module.main(
        [
            "runner.py",
            "capture",
            "--observer",
            str(observer_dir),
            "--evidence",
            str(evidence),
            "--observation",
            observation,
            "--target",
            base_url,
            "--board",
            requested_board,
            "--commit",
            requested_commit,
            "--page",
            f"{base_url}/",
            "--assertions",
            str(_assertions_file(tmp_path)),
        ]
    )
    return exit_code, evidence


def _replay(observer_dir: Path, request: dict[str, object]) -> subprocess.CompletedProcess[str]:
    command = observer_dir / "browser_observer.py"
    return subprocess.run(
        [str(command)],
        input=json.dumps(request, sort_keys=True),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        cwd=command.parent,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )


def _request_from(receipt: dict[str, object]) -> dict[str, object]:
    runtime = receipt["runtime"]
    return {
        "observation_id": receipt["observation_id"],
        "surface_id": receipt["surface_id"],
        "target": receipt["target"],
        "host_version": runtime["version"],
        "host_build": runtime["build"],
        "runtime_product": runtime["product"],
        "runtime_identity_source": runtime["identity_source"],
        "candidate_commit": receipt["candidate_commit"],
        "captured_at": receipt["captured_at"],
        "page_url": receipt["page_url"],
        "assertions": receipt["assertions"],
    }


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_surface_observer(tmp_path: Path) -> tuple[Path, dict[str, dict[str, object]]]:
    observer_dir = tmp_path / "surface-observer"
    observer_dir.mkdir(mode=0o700)
    command = observer_dir / "browser_observer.py"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o700)
    surfaces: dict[str, dict[str, object]] = {}
    for index, (surface_id, product) in enumerate(runner_module.SURFACE_PRODUCTS.items()):
        surfaces[surface_id] = {
            "adapter": "signed-aionui",
            "target": {
                "base_url": f"http://127.0.0.1:{9100 + index}",
                "board_id": BOARD,
            },
            "product": product,
            "candidate_commit": COMMIT,
        }
    (observer_dir / "observer.json").write_text(
        json.dumps({
            "schema_version": 1,
            "observer_id": "observer-surface-test",
            "store_dir": "captures",
            "max_age_s": 3600,
            "repository_root": str(runner_module.REPOSITORY_ROOT),
            "backend": {"kind": "command", "command": str(command)},
            "surfaces": surfaces,
        }),
        encoding="utf-8",
    )
    (observer_dir / "observer.json").chmod(0o600)
    return observer_dir, surfaces


def _write_complete_observation_manifest(
    tmp_path: Path, surfaces: dict[str, dict[str, object]]
) -> Path:
    identifiers = [
        *harness_module.SEQUENCE,
        *sorted(harness_module.REQUIRED_INVENTORY),
        *harness_module.REQUIRED_FINAL_GATES,
    ]
    rows = []
    for identifier in identifiers:
        surface_id = harness_module._surface_for_identifier(identifier)
        target = surfaces[surface_id]["target"]
        assertions = list(harness_module._canonical_browser_assertions(identifier))
        if not assertions:
            assertions = [{
                "name": f"browser context: {identifier}",
                "path": ["nodes"],
                "operator": "ax_name_contains",
                "expected": identifier,
            }]
        row = {
            "id": identifier,
            "page_url": f"{target['base_url']}/acceptance/{identifier}",
            "assertions": assertions,
        }
        typed_conjuncts = harness_module._canonical_typed_conjuncts(identifier)
        if typed_conjuncts:
            row["typed_evidence"] = [
                {
                    "evidence": f"typed/{identifier}-{index}.json",
                    "run_id": "acceptance-run-1",
                    "action_id": identifier,
                    "entity": identifier,
                    "causal_index": index,
                }
                for index, _conjunct in enumerate(typed_conjuncts, start=1)
            ]
        rows.append(row)
    path = tmp_path / "observations.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "operator_topology": {
            **{
                kind: dict(value)
                for kind, value in harness_module.CURRENT_OPERATOR_TOPOLOGY.items()
            },
            "optional_opus_worker": {
                "enabled": False,
                "count": 0,
                "model": "opus",
                "tier_max": 2,
            },
        },
        "observations": rows,
    }), encoding="utf-8")
    return path


def _signed_listener_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    trusted_signer: bool,
) -> None:
    bundle = tmp_path / "AionUi.app"
    core = bundle / "Contents" / "Resources" / "bundled-aioncore" / "darwin-arm64" / "aioncore"
    core.parent.mkdir(parents=True)
    core.write_bytes(b"aioncore")
    core.chmod(0o700)
    info = {
        "CFBundleIdentifier": "com.aionui.app",
        "CFBundleShortVersionString": HOST_VERSION,
    }
    (bundle / "Contents" / "Info.plist").write_bytes(observer_module.plistlib.dumps(info))

    def run_command(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(arguments, 0, stdout="4242\n", stderr="")
        if arguments[0] == "/bin/ps":
            command = (
                f"{core} --host 127.0.0.1 --port 8765 --app-version {HOST_VERSION} "
                "--identity-mode webui"
            )
            return subprocess.CompletedProcess(arguments, 0, stdout=command, stderr="")
        if arguments[0] == "/usr/bin/codesign" and "--verify" in arguments:
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
        signer = "AionUi Inc. (52JQX2HUSC)" if trusted_signer else "Example (BADTEAM)"
        team = "52JQX2HUSC" if trusted_signer else "BADTEAM"
        signature = "\n".join(
            [
                "Identifier=com.aionui.app",
                "CDHash=cbd8ca92afa6ff19a38b95728b11c116734b68bd",
                f"Authority=Developer ID Application: {signer}",
                f"TeamIdentifier={team}",
                "Notarization Ticket=stapled",
            ]
        )
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr=signature)

    monkeypatch.setattr(observer_module.sys, "platform", "darwin")
    monkeypatch.setattr(observer_module.subprocess, "run", run_command)


def test_host_identity_binds_signed_live_aionui_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _signed_listener_fixture(monkeypatch, tmp_path, trusted_signer=True)
    assert observer_module.probe_host_identity("http://127.0.0.1:8765") == {
        "product": "AionUi",
        "version": HOST_VERSION,
        "build": "cbd8ca92afa6ff19a38b95728b11c116734b68bd",
        "source": "signed-aionui-webui-listener",
    }


def test_host_identity_rejects_untrusted_bundle_signer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _signed_listener_fixture(monkeypatch, tmp_path, trusted_signer=False)
    with pytest.raises(observer_module.ObserverError, match="signer identity is unverifiable"):
        observer_module.probe_host_identity("http://127.0.0.1:8765")


def test_forged_helper_host_cannot_bypass_signed_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unavailable(base_url: str) -> dict[str, str]:
        calls.append(base_url)
        raise observer_module.ObserverError(
            observer_module.EXIT_CAPABILITY_UNAVAILABLE,
            "signed listener unavailable",
        )

    monkeypatch.setattr(observer_module, "_probe_signed_bundle_listener", unavailable)
    observation = {
        "host_status": {
            "http_status": 200,
            "content_type": "application/json",
            "payload": json.loads(STATUS_PAYLOAD),
        },
        "candidate_status": {
            "http_status": 200,
            "content_type": "application/json",
            "payload": {"schema_version": 1, "candidate_commit": COMMIT},
        },
        "selected_board": BOARD,
    }
    with pytest.raises(
        observer_module.ObserverError,
        match="helper-authored host status cannot establish host identity",
    ):
        observer_module._observed_runtime_binding(observation, "http://127.0.0.1:8765")
    assert calls == ["http://127.0.0.1:8765"]


def test_healthy_capture_replays_through_the_installed_observer(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
        completed = _replay(observer_dir, _request_from(captured["receipt"]))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload) == {
        "attestation",
        "attestation_nonce",
        "observer_id", "observation_id", "target", "host_product", "host_version",
        "host_build", "host_identity_source", "candidate_commit", "captured_at", "page_url",
        "screenshot_base64", "snapshot", "surface_id",
    }
    assert payload["host_product"] == "AionUi"
    receipt = captured["receipt"]
    evidence_root = captured["evidence_root"]
    screenshot = (evidence_root / receipt["screenshot"]["path"]).read_bytes()
    assert base64.b64decode(payload["screenshot_base64"]) == screenshot


def test_requested_candidate_cannot_relabel_observed_runtime(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        exit_code, _ = _capture_exit(
            tmp_path, observer_dir, base_url, requested_commit="1" * 40
        )
    assert exit_code == runner_module.EXIT_FAILED
    assert list((observer_dir / "captures").glob("*.json")) == []


def test_requested_board_cannot_relabel_observed_home_ui(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        exit_code, _ = _capture_exit(
            tmp_path, observer_dir, base_url, requested_board="sandbox-other"
        )
    assert exit_code == runner_module.EXIT_FAILED
    assert list((observer_dir / "captures").glob("*.json")) == []


def test_ego_binding_reads_are_isolated_from_page_monkeypatches() -> None:
    """Page-owned fetch/querySelector overrides cannot supply runtime bindings."""
    script = observer_module.EGO_SCRIPT % (
        json.dumps(17), json.dumps("http://127.0.0.1:8766/")
    )
    assert "Page.createIsolatedWorld" in script
    assert "pursers-verifier-observer" in script
    assert script.index("Page.createIsolatedWorld") < script.index(
        "fetch('/pursers/status'"
    )
    evaluations = script.split("Runtime.evaluate")[1:]
    assert len(evaluations) == 4
    assert all("contextId: contextId" in evaluation for evaluation in evaluations)
    assert "fetch('/pursers/status'" in evaluations[0]
    assert "candidate.json" in evaluations[1]
    assert "document.querySelector" in evaluations[2]
    assert "crypto.subtle.digest" in evaluations[3]


def _transition_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "context": {
            "observation_id": "extension.join-progress",
            "run_id": "run-1",
            "action_id": "inspect-progress",
            "entity": "aionui-home",
            "surface": "aionui",
            "board_id": BOARD,
            "candidate_commit": COMMIT,
            "issued_at": observer_module._now().isoformat().replace("+00:00", "Z"),
            "causal_index": 4,
        },
        "surface_id": "aionui",
        "target": {"base_url": "http://127.0.0.1:18921", "board_id": BOARD},
        "candidate_commit": COMMIT,
        "page_url": "http://127.0.0.1:18921/home",
        "recipe": {
            "before": [
                {"path": "/current", "selector": ".stepper .current", "property": "text"}
            ],
            "actions": [{"kind": "observe", "path": "/performed"}],
            "after": [
                {"path": "/steps", "selector": ".stepper li", "property": "count"}
            ],
            "settle_milliseconds": 0,
        },
    }


def test_typed_browser_transition_is_closed_and_runtime_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _transition_spec()
    now = observer_module._now().isoformat().replace("+00:00", "Z")
    observed = {
        "page_url": spec["page_url"],
        "before": {"/current": "Connect"},
        "action": {"/performed": "observed"},
        "after": {"/steps": 3},
        "order": {"before_at": now, "action_at": now, "after_at": now},
        "host_status": {},
        "candidate_status": {},
        "selected_board": BOARD,
        "page_sha256": "0" * 64,
    }
    monkeypatch.setattr(observer_module, "_load_config", lambda: {})
    monkeypatch.setattr(
        observer_module, "_run_transition_backend",
        lambda _config, _page, _recipe: observed,
    )
    monkeypatch.setattr(
        observer_module, "_observed_surface_binding",
        lambda *_args: {
            "product": "AionUi", "version": HOST_VERSION, "build": HOST_BUILD,
            "source": "signed-aionui-webui-listener",
            "candidate_commit": COMMIT, "selected_board": BOARD,
        },
    )
    output = io.StringIO()
    assert observer_module.transition(io.StringIO(json.dumps(spec)), output) == 0
    result = json.loads(output.getvalue())
    assert result["before"]["selected"] == {"/current": "Connect"}
    assert result["action"]["selected"] == {"/performed": "observed"}
    assert result["after"]["selected"] == {"/steps": 3}
    assert result["context"] == spec["context"]

    transport = {
        "type": "stdio",
        "command": "pursers-wait-bridge",
        "args": [],
        "env": {},
    }
    spec["recipe"]["actions"] = [{
        "kind": "click_response_json",
        "selector": "#recover-seat",
        "method": "POST",
        "endpoint": "/pursers/onboarding/recover",
        "pointer": "/body/mcp_definition/transport",
        "path": "/mcp_transport",
    }]
    observed["action"] = {"/mcp_transport": transport}
    output = io.StringIO()
    assert observer_module.transition(io.StringIO(json.dumps(spec)), output) == 0
    assert json.loads(output.getvalue())["action"]["selected"] == {
        "/mcp_transport": transport
    }

    observed["after"] = {"/decoy": 3}
    with pytest.raises(observer_module.ObserverError, match="selectors differ"):
        observer_module.transition(io.StringIO(json.dumps(spec)), io.StringIO())


def test_typed_browser_transition_rejects_arbitrary_script_action() -> None:
    spec = _transition_spec()
    spec["recipe"]["actions"] = [
        {"kind": "javascript", "path": "/performed", "expression": "window.evil()"}
    ]
    with pytest.raises(observer_module.ObserverError, match="kind is unsupported"):
        observer_module._read_transition_spec(io.StringIO(json.dumps(spec)))


def test_typed_browser_transition_rejects_invalid_fetch_json_pointer() -> None:
    spec = _transition_spec()
    spec["recipe"]["actions"] = [{
        "kind": "click_response_json",
        "selector": "#recover-seat",
        "method": "GET",
        "endpoint": "/status",
        "pointer": "/body/~2invalid",
        "path": "/selected",
    }]
    with pytest.raises(observer_module.ObserverError, match="JSON pointer is invalid"):
        observer_module._read_transition_spec(io.StringIO(json.dumps(spec)))


def test_ego_transition_uses_isolated_world_and_closed_operations() -> None:
    script = observer_module.EGO_TRANSITION_SCRIPT % (
        json.dumps("acceptance"),
        json.dumps("http://127.0.0.1:18921/home"),
        json.dumps(_transition_spec()["recipe"]),
    )
    assert "Page.createIsolatedWorld" in script
    assert "pursers-verifier-transition" in script
    assert "eval(" not in script
    assert "spec.kind === 'fetch'" in script
    assert "spec.kind === 'fetch_json'" in script
    assert "selectJson(envelope, spec.pointer)" in script
    assert "spec.kind === 'click_response_json'" in script
    assert "response capture did not match exactly once" in script
    assert "window.fetch = state.original" in script
    assert "spec.kind === 'click'" in script


def test_harness_observer_binds_report_artifacts_to_the_capture(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "result_visible")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url, observation="result_visible")
        receipt = captured["receipt"]
        evidence_root = captured["evidence_root"]
        observer = VerifierBrowserObserver(
            (observer_dir / "browser_observer.py").resolve(), evidence_root
        )
        request = BrowserObservationRequest(
            observation_id=receipt["observation_id"],
            target=LiveTarget(**receipt["target"]),
            host_version=receipt["runtime"]["version"],
            host_build=receipt["runtime"]["build"],
            candidate_commit=receipt["candidate_commit"],
            captured_at=receipt["captured_at"],
            page_url=receipt["page_url"],
            assertions=tuple(receipt["assertions"]),
            surface_id=receipt["surface_id"],
            runtime_product=receipt["runtime"]["product"],
            runtime_identity_source=receipt["runtime"]["identity_source"],
        )
        capture = observer.capture(request)
    snapshot = json.loads(
        (evidence_root / receipt["accessibility_snapshot"]["path"]).read_text(encoding="utf-8")
    )
    evidence = harness_module._BrowserEvidence(
        request=request,
        screenshot=(evidence_root / receipt["screenshot"]["path"]).read_bytes(),
        snapshot=snapshot["snapshot"],
        references=(receipt["screenshot"]["path"], receipt["accessibility_snapshot"]["path"]),
        attestation=receipt["attestation"],
        attestation_nonce=receipt["attestation_nonce"],
    )
    _validate_trusted_browser_observations([evidence], observer)
    assert capture.screenshot == evidence.screenshot
    forged = harness_module._BrowserEvidence(
        request=request,
        screenshot=_png("forged"),
        snapshot=evidence.snapshot,
        references=evidence.references,
        attestation=evidence.attestation,
        attestation_nonce=evidence.attestation_nonce,
    )
    with pytest.raises(AcceptanceError, match="screenshot does not match"):
        _validate_trusted_browser_observations([forged], observer)


def test_missing_observer_is_explicit_non_pass() -> None:
    with pytest.raises(AcceptanceCapabilityUnavailable, match="observer is unavailable"):
        _validate_trusted_browser_observations([], None)


def test_prepare_expands_every_authoritative_observation_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer_dir, surfaces = _write_surface_observer(tmp_path)
    manifest = _write_complete_observation_manifest(tmp_path, surfaces)
    evidence = tmp_path / "evidence-plan"
    exit_code = runner_module.main([
        "runner.py", "prepare", "--observer", str(observer_dir),
        "--manifest", str(manifest), "--evidence", str(evidence),
    ])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    plan = json.loads((evidence / "capture-plan.json").read_text(encoding="utf-8"))
    expected = (
        len(harness_module.SEQUENCE)
        + len(harness_module.REQUIRED_INVENTORY)
        + len(harness_module.REQUIRED_FINAL_GATES)
    )
    assert result["observations"] == expected == 201
    assert len(plan["commands"]) == expected
    assert len({command[command.index("--observation") + 1] for command in plan["commands"]}) == expected
    assert set(plan["typed_evidence"]) == {
        identifier
        for identifier in (
            *harness_module.SEQUENCE,
            *harness_module.REQUIRED_INVENTORY,
            *harness_module.REQUIRED_FINAL_GATES,
        )
        if harness_module._canonical_typed_conjuncts(identifier)
    }


def test_prepare_refuses_missing_typed_evidence_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer_dir, surfaces = _write_surface_observer(tmp_path)
    manifest_path = _write_complete_observation_manifest(tmp_path, surfaces)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next(
        item for item in manifest["observations"]
        if item["id"] == "dashboard-ui.styles"
    )
    row.pop("typed_evidence")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    exit_code = runner_module.main([
        "runner.py", "prepare", "--observer", str(observer_dir),
        "--manifest", str(manifest_path), "--evidence", str(tmp_path / "evidence"),
    ])
    assert exit_code == runner_module.EXIT_USAGE
    assert "fields do not match schema" in capsys.readouterr().err


def test_personal_surface_refuses_served_page_digest_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "dashboard.html"
    artifact.write_bytes(b"verifier-pinned-personal-artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: (COMMIT, ""))
    with pytest.raises(observer_module.ObserverError, match="served Personal artifact"):
        observer_module._probe_pinned_artifact_without_listener(
            {"repository_root": str(tmp_path)},
            {
                "candidate_commit": COMMIT,
                "artifact": artifact.name,
                "artifact_sha256": digest,
                "product": "Pursers Personal",
                "version": "5.0.0a25",
            },
            "0" * 64,
        )


def _personal_runtime_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], str]:
    repository = tmp_path / "checkout"
    runtime_dir = tmp_path / "runtime"
    artifact = repository / "packages/personal/src/pursers_personal/resources/dashboard.html"
    server = repository / "packages/personal/src/pursers_personal/apps_server.py"
    artifact.parent.mkdir(parents=True)
    server.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"personal-dashboard")
    server.write_bytes(b"exact-personal-server")
    runtime_dir.mkdir(mode=0o700)
    pid_file = runtime_dir / "personal.pid"
    receipt_file = runtime_dir / "personal-receipt.json"
    pid_file.write_text("4242\n"); pid_file.chmod(0o600)
    source_digest = hashlib.sha256(server.read_bytes()).hexdigest()
    receipt = {
        "schema_version": 1,
        "product": "Pursers Personal",
        "server_name": "On Board Personal",
        "version": "5.0.0a25",
        "build": source_digest,
        "candidate_commit": COMMIT,
        "candidate_source": str(server),
        "board_id": BOARD,
        "pid": 4242,
        "transport": "stdio",
    }
    receipt_file.write_text(json.dumps(receipt)); receipt_file.chmod(0o600)
    challenge_key = runtime_dir / "acceptance-challenge.key"
    challenge_key.write_bytes(b"\x11" * 32); challenge_key.chmod(0o600)
    surface: dict[str, object] = {
        "adapter": "pinned-signed-aionui-personal-mcp",
        "target": {"base_url": "http://127.0.0.1:8765", "board_id": BOARD},
        "product": "Pursers Personal",
        "version": "5.0.0a25",
        "candidate_commit": COMMIT,
        "artifact": artifact.relative_to(repository).as_posix(),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "runtime": {
            "artifact": server.relative_to(repository).as_posix(),
            "artifact_sha256": source_digest,
            "pid_file": str(pid_file),
            "receipt": str(receipt_file),
            "challenge_key": str(challenge_key),
        },
    }
    return {"repository_root": str(repository)}, surface, hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_personal_runtime_binds_live_exact_apps_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, surface, page_digest = _personal_runtime_fixture(tmp_path)
    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: (COMMIT, ""))
    runtime = surface["runtime"]
    command = (
        f"/usr/bin/python3 -m pursers_personal.cli mcp --candidate-source "
        f"{tmp_path}/checkout/{runtime['artifact']} --candidate-commit {COMMIT} "
        f"--board-id {BOARD} --acceptance-runtime-receipt {runtime['receipt']} "
        f"--acceptance-challenge-key {runtime['challenge_key']}"
    )
    monkeypatch.setattr(
        observer_module,
        "_run_identity_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=command, stderr=""),
    )
    binding = observer_module._probe_personal_mcp_runtime(config, surface, page_digest)
    assert binding == {
        "product": "Pursers Personal",
        "version": "5.0.0a25",
        "build": runtime["artifact_sha256"],
        "candidate_commit": COMMIT,
        "attestation_pid": "4242",
        "attestation_build": runtime["artifact_sha256"],
        "attestation_source": f"{tmp_path}/checkout/{runtime['artifact']}",
        "attestation_key_path": runtime["challenge_key"],
    }


def test_personal_runtime_rejects_stale_or_unrelated_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, surface, page_digest = _personal_runtime_fixture(tmp_path)
    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: (COMMIT, ""))
    monkeypatch.setattr(
        observer_module,
        "_run_identity_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="/usr/bin/python3 unrelated.py", stderr=""),
    )
    with pytest.raises(observer_module.ObserverError, match="not the MCP server"):
        observer_module._probe_personal_mcp_runtime(config, surface, page_digest)

    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: ("d" * 40, ""))
    with pytest.raises(observer_module.ObserverError, match="candidate binding changed"):
        observer_module._probe_personal_mcp_runtime(config, surface, page_digest)

    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: (COMMIT, " M changed"))
    with pytest.raises(observer_module.ObserverError, match="candidate binding changed"):
        observer_module._probe_personal_mcp_runtime(config, surface, page_digest)


def test_python_execution_selector_stops_at_the_first_selector() -> None:
    selector = observer_module._python_execution_selector
    assert selector(["python3", "-m", "pursers_personal.cli", "mcp"]) == (
        "module",
        "pursers_personal.cli",
        3,
    )
    assert selector(["python3", "-mpursers_personal.cli", "mcp"]) == (
        "module",
        "pursers_personal.cli",
        2,
    )
    assert selector(["python3", "-S", "-u", "-m", "pursers_personal.cli", "mcp"]) == (
        "module",
        "pursers_personal.cli",
        5,
    )
    assert selector(["python3", "-Sm", "pursers_personal.cli", "mcp"]) == (
        "module",
        "pursers_personal.cli",
        3,
    )
    assert selector(["python3", "-W", "ignore", "-m", "pursers_personal.cli", "mcp"]) == (
        "module",
        "pursers_personal.cli",
        5,
    )
    # An earlier -c wins; the later -m tuple is inert argument text.
    assert selector(["python3", "-c", "pass", "-m", "pursers_personal.cli", "mcp"]) == (
        "command",
        "pass",
        3,
    )
    assert selector(["python3", "unrelated.py", "-m", "pursers_personal.cli", "mcp"]) == (
        "script",
        "unrelated.py",
        2,
    )
    assert selector(["python3", "-", "-m", "pursers_personal.cli", "mcp"]) == ("stdin", None, 2)
    assert selector(["python3"]) == ("repl", None, 1)


def test_personal_runtime_rejects_inert_module_tuple_after_an_earlier_selector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact bypass reported against 8d04a65.

    Python executes ``-c pass`` and never imports the module, yet the old
    predicate accepted the command because the required three-token tuple
    appeared somewhere in argv.
    """

    config, surface, page_digest = _personal_runtime_fixture(tmp_path)
    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: (COMMIT, ""))
    runtime = surface["runtime"]
    tail = (
        f"--candidate-source {tmp_path}/checkout/{runtime['artifact']} "
        f"--candidate-commit {COMMIT} --board-id {BOARD} "
        f"--acceptance-runtime-receipt {runtime['receipt']}"
    )
    for decoy in (
        f"/usr/bin/python3 -c pass -m pursers_personal.cli mcp {tail}",
        f"/usr/bin/python3 decoy.py -m pursers_personal.cli mcp {tail}",
        f"/usr/bin/python3 - -m pursers_personal.cli mcp {tail}",
        f"/usr/bin/python3 -m pursers_personal.other mcp {tail}",
        f"/usr/bin/python-decoy -m pursers_personal.cli mcp {tail}",
    ):
        monkeypatch.setattr(
            observer_module,
            "_run_identity_command",
            lambda *_args, _stdout=decoy, **_kwargs: subprocess.CompletedProcess(
                [], 0, stdout=_stdout, stderr=""
            ),
        )
        with pytest.raises(observer_module.ObserverError):
            observer_module._probe_personal_mcp_runtime(config, surface, page_digest)


def test_assemble_refuses_any_missing_authoritative_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer_dir, surfaces = _write_surface_observer(tmp_path)
    suites = tmp_path / "suites.json"
    suites.write_text(json.dumps({
        "schema_version": 1,
        "suites": {name: f"suites/{name}.json" for name in harness_module.REQUIRED_SUITES},
    }), encoding="utf-8")
    evidence = tmp_path / "empty-evidence"
    manifest = _write_complete_observation_manifest(tmp_path, surfaces)
    assert runner_module.main([
        "runner.py", "prepare", "--observer", str(observer_dir),
        "--manifest", str(manifest), "--evidence", str(evidence),
    ]) == 0
    capsys.readouterr()
    exit_code = runner_module.main([
        "runner.py", "assemble", "--observer", str(observer_dir),
        "--evidence", str(evidence), "--suite-manifest", str(suites),
        "--report", str(evidence / "report.json"),
    ])
    assert exit_code == runner_module.EXIT_BLOCKED
    assert "capture missing or invalid" in capsys.readouterr().err


def test_unknown_observation_is_refused(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
    request = _request_from(captured["receipt"])
    request["observation_id"] = "team_setup"
    completed = _replay(observer_dir, request)
    assert completed.returncode == observer_module.EXIT_UNKNOWN_OBSERVATION
    assert "holds no capture" in completed.stderr


def test_capture_missing_required_field_is_refused_even_with_extra_field(
    tmp_path: Path,
) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
    store = observer_dir / "captures" / "door_connect.json"
    record = json.loads(store.read_text(encoding="utf-8"))
    record.pop("screenshot_base64")
    record["unexpected"] = "does not replace the required field"
    store.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    completed = _replay(observer_dir, _request_from(captured["receipt"]))
    assert completed.returncode == observer_module.EXIT_CONFIG
    assert "capture schema" in completed.stderr


def test_install_records_explicit_ego_browser_task_space(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer_dir = tmp_path / "verifier"
    ego = tmp_path / "ego-browser"
    ego.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ego.chmod(0o700)
    exit_code = runner_module.main(
        [
            "runner.py",
            "install-observer",
            "--dir",
            str(observer_dir),
            "--ego-browser",
            str(ego),
            "--task-space",
            "17",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    config = json.loads((observer_dir / "observer.json").read_text(encoding="utf-8"))
    assert config["backend"]["task_space"] == "17"


def test_install_resolves_default_ego_browser_from_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ego = tmp_path / "ego-browser"
    ego.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ego.chmod(0o700)
    monkeypatch.setattr(runner_module.shutil, "which", lambda value: str(ego))
    observer_dir = tmp_path / "verifier"
    assert runner_module.main(
        ["runner.py", "install-observer", "--dir", str(observer_dir)]
    ) == 0
    capsys.readouterr()
    config = json.loads((observer_dir / "observer.json").read_text(encoding="utf-8"))
    assert config["backend"]["command"] == str(ego.resolve())


def test_install_surface_manifest_carries_private_challenge_into_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    challenge = tmp_path / "personal-runtime" / "acceptance-challenge.key"
    challenge.parent.mkdir()
    challenge.write_bytes(b"\x25" * 48)
    challenge.chmod(0o600)
    manifest = _write_surface_manifest(tmp_path, str(challenge))
    monkeypatch.setattr(runner_module, "_git", _clean_candidate_git)
    observer_dir = tmp_path / "verifier"

    assert runner_module.main([
        "runner.py", "install-observer",
        "--dir", str(observer_dir),
        "--backend-command", str(backend),
        "--surface-manifest", str(manifest),
    ]) == 0
    capsys.readouterr()

    config = json.loads(
        (observer_dir / "observer.json").read_text(encoding="utf-8")
    )
    personal = observer_module._surface_config(config, "personal")
    runtime = personal["runtime"]
    assert set(runtime) == {
        "artifact", "artifact_sha256", "challenge_key", "pid_file", "receipt"
    }
    assert runtime["challenge_key"] == str(challenge.resolve())
    runtime_source = (
        runner_module.REPOSITORY_ROOT / runtime["artifact"]
    ).resolve()
    assert runtime["artifact_sha256"] == hashlib.sha256(
        runtime_source.read_bytes()
    ).hexdigest()


def test_keyboard_transition_action_is_closed_and_shared_with_evaluator() -> None:
    action = {
        "kind": "press_key",
        "selector": "[role=tab][aria-selected=true]",
        "key": "ArrowRight",
        "path": "/pressed_key",
    }
    assert observer_module._validate_transition_actions([action]) == [action]
    assert typed_evidence._browser_actions([action]) == [action]
    for changed in (
        {**action, "key": "Tab"},
        {**action, "script": "arbitrary()"},
    ):
        with pytest.raises(observer_module.ObserverError):
            observer_module._validate_transition_actions([changed])
        with pytest.raises(typed_evidence.TypedEvidenceError):
            typed_evidence._browser_actions([changed])


@pytest.mark.parametrize(
    "unsafe_kind",
    ["missing", "relative", "inside-checkout", "group-readable", "short", "symlink"],
)
def test_install_surface_manifest_rejects_unsafe_challenge_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    runtime_dir = tmp_path / "personal-runtime"
    runtime_dir.mkdir()
    challenge = runtime_dir / "acceptance-challenge.key"
    challenge.write_bytes(b"\x26" * 48)
    challenge.chmod(0o600)
    challenge_value = str(challenge)
    if unsafe_kind == "missing":
        challenge_value = str(runtime_dir / "missing.key")
    elif unsafe_kind == "relative":
        challenge_value = "relative.key"
    elif unsafe_kind == "inside-checkout":
        challenge_value = str(
            runner_module.REPOSITORY_ROOT
            / "tools/aionui-extension/tests/home_acceptance/runner.py"
        )
    elif unsafe_kind == "group-readable":
        challenge.chmod(0o640)
    elif unsafe_kind == "short":
        challenge.write_bytes(b"short")
    elif unsafe_kind == "symlink":
        link = runtime_dir / "challenge-link.key"
        link.symlink_to(challenge)
        challenge_value = str(link)
    manifest = _write_surface_manifest(tmp_path, challenge_value)
    monkeypatch.setattr(runner_module, "_git", _clean_candidate_git)
    observer_dir = tmp_path / "verifier"

    assert runner_module.main([
        "runner.py", "install-observer",
        "--dir", str(observer_dir),
        "--backend-command", str(backend),
        "--surface-manifest", str(manifest),
    ]) == runner_module.EXIT_USAGE
    assert not (observer_dir / "observer.json").exists()


def test_reinstall_with_changed_challenge_rejects_old_runtime_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    runtime_dir = tmp_path / "personal-runtime"
    runtime_dir.mkdir()
    old_key = runtime_dir / "old.key"
    new_key = runtime_dir / "new.key"
    for key, byte in ((old_key, b"\x27"), (new_key, b"\x28")):
        key.write_bytes(byte * 48)
        key.chmod(0o600)
    monkeypatch.setattr(runner_module, "_git", _clean_candidate_git)
    observer_dir = tmp_path / "verifier"

    for key in (old_key, new_key):
        manifest = _write_surface_manifest(tmp_path, str(key))
        assert runner_module.main([
            "runner.py", "install-observer",
            "--dir", str(observer_dir),
            "--backend-command", str(backend),
            "--surface-manifest", str(manifest),
        ]) == 0
        capsys.readouterr()

    config = json.loads(
        (observer_dir / "observer.json").read_text(encoding="utf-8")
    )
    personal = observer_module._surface_config(config, "personal")
    runtime = personal["runtime"]
    runtime_source = (
        runner_module.REPOSITORY_ROOT / runtime["artifact"]
    ).resolve()
    Path(runtime["pid_file"]).write_text("12345", encoding="utf-8")
    Path(runtime["pid_file"]).chmod(0o600)
    Path(runtime["receipt"]).write_text("{}", encoding="utf-8")
    Path(runtime["receipt"]).chmod(0o600)
    command = (
        f"/usr/bin/python3 -m pursers_personal.cli mcp "
        f"--candidate-source {runtime_source} --candidate-commit {COMMIT} "
        f"--board-id {BOARD} --acceptance-runtime-receipt {runtime['receipt']} "
        f"--acceptance-challenge-key {old_key}"
    )
    monkeypatch.setattr(
        observer_module, "_git_identity", lambda _repository: (COMMIT, "")
    )
    monkeypatch.setattr(
        observer_module,
        "_run_identity_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=command, stderr=""
        ),
    )
    page_digest = hashlib.sha256(
        (runner_module.REPOSITORY_ROOT / personal["artifact"]).read_bytes()
    ).hexdigest()
    with pytest.raises(
        observer_module.ObserverError,
        match="acceptance-challenge-key changed",
    ):
        observer_module._probe_personal_mcp_runtime(config, personal, page_digest)


@pytest.mark.parametrize(
    "field, value",
    [
        ("candidate_commit", "1" * 40),
        ("captured_at", "2020-01-01T00:00:00+00:00"),
        ("host_build", "2026.09.08.2"),
        ("host_version", "2.2.2"),
    ],
)
def test_mismatched_binding_is_refused(tmp_path: Path, field: str, value: str) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
    request = _request_from(captured["receipt"])
    request[field] = value
    completed = _replay(observer_dir, request)
    assert completed.returncode == observer_module.EXIT_MISMATCH
    assert "does not match the replayed request" in completed.stderr


def test_mismatched_page_url_on_target_origin_is_refused(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
    request = _request_from(captured["receipt"])
    request["page_url"] = f"{base_url}/other-route"
    completed = _replay(observer_dir, request)
    assert completed.returncode == observer_module.EXIT_MISMATCH


def test_stale_capture_is_refused(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend, max_age_s=60)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
    store = observer_dir / "captures" / "door_connect.json"
    record = json.loads(store.read_text(encoding="utf-8"))
    record["captured_at"] = "2026-01-01T00:00:00Z"
    record["recorded_at"] = record["captured_at"]
    store.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    request = _request_from(captured["receipt"])
    request["captured_at"] = record["captured_at"]
    completed = _replay(observer_dir, request)
    assert completed.returncode == observer_module.EXIT_STALE
    assert "is stale by" in completed.stderr


def test_unreachable_host_blocks_capture_and_records_no_capture(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    closed = f"http://127.0.0.1:{_free_port()}"
    exit_code = runner_module.main(
        [
            "runner.py", "capture",
            "--observer", str(observer_dir),
            "--evidence", str(tmp_path / "evidence"),
            "--observation", "door_connect",
            "--target", closed,
            "--board", BOARD,
            "--commit", COMMIT,
            "--page", f"{closed}/",
            "--assertions", str(_assertions_file(tmp_path)),
        ]
    )
    assert exit_code == runner_module.EXIT_BLOCKED
    assert list((observer_dir / "captures").glob("*.json")) == []


def test_non_contract_authenticated_browser_status_blocks_capture(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect", observed_commit="not-a-sha")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        exit_code = runner_module.main(
            [
                "runner.py", "capture",
                "--observer", str(observer_dir),
                "--evidence", str(tmp_path / "evidence"),
                "--observation", "door_connect",
                "--target", base_url,
                "--board", BOARD,
                "--commit", COMMIT,
                "--page", f"{base_url}/",
                "--assertions", str(_assertions_file(tmp_path)),
            ]
        )
    assert exit_code == runner_module.EXIT_BLOCKED
    assert list((observer_dir / "captures").glob("*.json")) == []


def test_group_writable_observer_home_is_refused(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
    observer_dir.chmod(0o777)
    try:
        completed = _replay(observer_dir, _request_from(captured["receipt"]))
    finally:
        observer_dir.chmod(0o700)
    assert completed.returncode == observer_module.EXIT_CONFIG
    assert "group/world writable" in completed.stderr


def test_observer_inside_the_checkout_is_refused(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    inside = runner_module.REPOSITORY_ROOT / ".pursers-observer-boundary-probe"
    exit_code = runner_module.main(
        [
            "runner.py", "install-observer",
            "--dir", str(inside),
            "--backend-command", str(backend),
        ]
    )
    assert exit_code == runner_module.EXIT_USAGE
    assert not inside.exists()
    probe = runner_module.REPOSITORY_ROOT / ".pursers-observer-boundary-probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    probe.chmod(0o700)
    try:
        with pytest.raises(AcceptanceError, match="verifier-owned outside the checkout"):
            VerifierBrowserObserver(probe.resolve(), tmp_path)
    finally:
        probe.unlink()


def test_doctor_reports_blocked_host_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    capsys.readouterr()
    closed = f"http://127.0.0.1:{_free_port()}"
    exit_code = runner_module.main(
        ["runner.py", "doctor", "--observer", str(observer_dir), "--target", closed]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == runner_module.EXIT_BLOCKED
    assert report["acceptance_ready"] is False
    assert report["checks"]["observer_installed"]["state"] == "ok"
    assert report["checks"]["host_identity"]["state"] == "blocked"


def test_doctor_reports_healthy_host_runtime_and_browser_channel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    capsys.readouterr()
    with _status_host(STATUS_PAYLOAD) as base_url:
        exit_code = runner_module.main(
            [
                "runner.py",
                "doctor",
                "--observer",
                str(observer_dir),
                "--target",
                base_url,
                "--probe-browser",
                f"{base_url}/",
            ]
        )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["acceptance_ready"] is True
    assert report["checks"]["host_identity"] == {
        "state": "ok",
        "product": "AionUi",
        "version": HOST_VERSION,
        "build": HOST_BUILD,
        "source": "signed-aionui-webui-listener",
        "candidate_commit": COMMIT,
        "selected_board": BOARD,
    }
    assert report["checks"]["runtime_health"] == {
        "state": "ok",
        "product": "AionCore",
        "version": "0.2.1",
        "build": "1788252518",
    }
    assert report["checks"]["browser_channel"]["state"] == "ok"


# --- ground A: the live transport challenge carried by the capture ----------

NONCE = "a" * 64


def _attested_binding(surface: dict[str, object]) -> dict[str, str]:
    """The binding _probe_personal_mcp_runtime returns for the fixture."""
    runtime = surface["runtime"]
    return {
        "product": "Pursers Personal",
        "version": "5.0.0a25",
        "build": runtime["artifact_sha256"],
        "candidate_commit": COMMIT,
        "selected_board": BOARD,
        "attestation_pid": "4242",
        "attestation_build": runtime["artifact_sha256"],
        "attestation_source": "/does/not/matter/apps_server.py",
        "attestation_key_path": runtime["challenge_key"],
    }


def _signed_attestation(
    binding: dict[str, str], nonce: str, key: bytes, **overrides: object
) -> dict[str, object]:
    """Sign a claim the way apps_server.acceptance_attestation serializes it."""
    claim = {
        "schema_version": 1,
        "server_name": "On Board Personal",
        "version": binding["version"],
        "build": binding["attestation_build"],
        "candidate_commit": binding["candidate_commit"],
        "candidate_source": binding["attestation_source"],
        "board_id": binding["selected_board"],
        "pid": int(binding["attestation_pid"]),
        "transport": "stdio",
        "nonce": nonce,
    }
    claim.update(overrides)
    payload = json.dumps(claim, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**claim, "signature": hmac.new(key, payload, hashlib.sha256).hexdigest()}


def _snapshot_carrying(attestation: dict[str, object] | None) -> dict[str, object]:
    """An accessibility snapshot shaped like the ones the backend returns."""
    nodes: list[dict[str, object]] = [
        {"role": "heading", "name": "On Board Personal"},
        {"role": "button", "name": "Refresh"},
    ]
    if attestation is not None:
        nodes.append(
            {"role": "text", "name": f"acceptance_runtime_attest -> {json.dumps(attestation)}"}
        )
    return {"nodes": nodes}


def test_attestation_verifies_when_the_live_server_answers_the_nonce(
    tmp_path: Path,
) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    key = Path(binding["attestation_key_path"]).read_bytes()
    attestation = _signed_attestation(binding, NONCE, key)
    verified = observer_module._verify_acceptance_attestation(
        binding, {"attestation_nonce": NONCE}, _snapshot_carrying(attestation)
    )
    assert verified == attestation


def test_capture_without_any_attestation_is_refused(tmp_path: Path) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    with pytest.raises(observer_module.ObserverError, match="no valid acceptance_runtime_attest"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(None)
        )


def test_decoy_without_the_verifier_key_cannot_produce_the_signature(
    tmp_path: Path,
) -> None:
    """A decoy MCP with a correct command line still holds no challenge key."""
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    forged = _signed_attestation(binding, NONCE, b"\x99" * 32)
    with pytest.raises(observer_module.ObserverError, match="no valid acceptance_runtime_attest"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(forged)
        )


def test_stale_answer_after_key_rotation_is_refused(tmp_path: Path) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    key_path = Path(binding["attestation_key_path"])
    stale = _signed_attestation(binding, NONCE, key_path.read_bytes())
    key_path.write_bytes(b"\x22" * 32)
    with pytest.raises(observer_module.ObserverError, match="no valid acceptance_runtime_attest"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(stale)
        )


def test_replayed_nonce_from_an_earlier_capture_is_refused(tmp_path: Path) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    key = Path(binding["attestation_key_path"]).read_bytes()
    earlier = _signed_attestation(binding, "b" * 64, key)
    with pytest.raises(observer_module.ObserverError, match="no valid acceptance_runtime_attest"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(earlier)
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("pid", 9999),
        ("build", "0" * 64),
        ("candidate_commit", "1" * 40),
        ("board_id", "another-board"),
        ("transport", "http"),
        ("server_name", "On Board Central"),
    ],
)
def test_attestation_disagreeing_with_the_probed_runtime_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    key = Path(binding["attestation_key_path"]).read_bytes()
    signed = _signed_attestation(binding, NONCE, key, **{field: value})
    with pytest.raises(observer_module.ObserverError, match="no valid acceptance_runtime_attest"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(signed)
        )


def test_group_readable_challenge_key_is_refused(tmp_path: Path) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    key_path = Path(binding["attestation_key_path"])
    attestation = _signed_attestation(binding, NONCE, key_path.read_bytes())
    key_path.chmod(0o640)
    with pytest.raises(observer_module.ObserverError, match="challenge key must be private"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(attestation)
        )


def test_short_challenge_key_is_refused(tmp_path: Path) -> None:
    _config, surface, _digest = _personal_runtime_fixture(tmp_path)
    binding = _attested_binding(surface)
    key_path = Path(binding["attestation_key_path"])
    key_path.write_bytes(b"\x33" * 16)
    attestation = _signed_attestation(binding, NONCE, b"\x33" * 16)
    with pytest.raises(observer_module.ObserverError, match="challenge key is too short"):
        observer_module._verify_acceptance_attestation(
            binding, {"attestation_nonce": NONCE}, _snapshot_carrying(attestation)
        )


def test_personal_process_without_the_challenge_key_flag_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old command line, valid before ground A, no longer binds Personal."""
    config, surface, page_digest = _personal_runtime_fixture(tmp_path)
    monkeypatch.setattr(observer_module, "_git_identity", lambda _repository: (COMMIT, ""))
    runtime = surface["runtime"]
    command = (
        f"/usr/bin/python3 -m pursers_personal.cli mcp --candidate-source "
        f"{tmp_path}/checkout/{runtime['artifact']} --candidate-commit {COMMIT} "
        f"--board-id {BOARD} --acceptance-runtime-receipt {runtime['receipt']}"
    )
    monkeypatch.setattr(
        observer_module,
        "_run_identity_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=command, stderr=""),
    )
    with pytest.raises(
        observer_module.ObserverError, match="lacks --acceptance-challenge-key"
    ):
        observer_module._probe_personal_mcp_runtime(config, surface, page_digest)


def test_capture_spec_without_a_nonce_is_refused(tmp_path: Path) -> None:
    spec = {
        "schema_version": 1,
        "observation_id": "door-connect",
        "target": {"base_url": "http://127.0.0.1:8765", "board_id": BOARD},
        "candidate_commit": COMMIT,
        "page_url": "http://127.0.0.1:8765/index.html",
        "assertions": [{"path": "nodes.0.name", "equals": "On Board Personal"}],
        "surface_id": "personal",
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(observer_module.ObserverError, match="fields do not match schema"):
        observer_module._read_spec(spec_path)


@pytest.mark.parametrize("nonce", ["", "short", "A" * 64, "z" * 64, "a" * 200])
def test_capture_spec_nonce_must_be_lowercase_hex(tmp_path: Path, nonce: str) -> None:
    spec = {
        "schema_version": 1,
        "observation_id": "door-connect",
        "target": {"base_url": "http://127.0.0.1:8765", "board_id": BOARD},
        "candidate_commit": COMMIT,
        "page_url": "http://127.0.0.1:8765/index.html",
        "assertions": [{"path": "nodes.0.name", "equals": "On Board Personal"}],
        "surface_id": "personal",
        "attestation_nonce": nonce,
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(observer_module.ObserverError, match="attestation_nonce must be"):
        observer_module._read_spec(spec_path)
