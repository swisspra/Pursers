"""Trust-boundary regressions for the verifier-owned browser observer.

These tests never skip on missing capability: they drive the real observer
executable with a scripted capture backend, so healthy replay, forged
artifacts, mismatched bindings, stale captures, unreachable hosts and missing
host identity all assert a concrete outcome.
"""

from __future__ import annotations

import base64
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
        "    'selected_board': OBSERVED_BOARD,\n"
        "}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    return command


def _install(tmp_path: Path, backend: Path, *, max_age_s: int = 43_200) -> Path:
    observer_dir = tmp_path / "verifier"
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
    assert exit_code == 0
    return observer_dir


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
    host = receipt["host"]
    return {
        "observation_id": receipt["observation_id"],
        "target": receipt["target"],
        "host_version": host["version"],
        "host_build": host["build"],
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


def test_healthy_capture_replays_through_the_installed_observer(tmp_path: Path) -> None:
    backend = _write_backend(tmp_path, "door_connect")
    observer_dir = _install(tmp_path, backend)
    with _status_host(STATUS_PAYLOAD) as base_url:
        captured = _capture(tmp_path, observer_dir, base_url)
        completed = _replay(observer_dir, _request_from(captured["receipt"]))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload) == {
        "observer_id", "observation_id", "target", "host_product", "host_version",
        "host_build", "candidate_commit", "captured_at", "page_url",
        "screenshot_base64", "snapshot",
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
            host_version=receipt["host"]["version"],
            host_build=receipt["host"]["build"],
            candidate_commit=receipt["candidate_commit"],
            captured_at=receipt["captured_at"],
            page_url=receipt["page_url"],
            assertions=tuple(receipt["assertions"]),
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
    )
    _validate_trusted_browser_observations([evidence], observer)
    assert capture.screenshot == evidence.screenshot
    forged = harness_module._BrowserEvidence(
        request=request,
        screenshot=_png("forged"),
        snapshot=evidence.snapshot,
        references=evidence.references,
    )
    with pytest.raises(AcceptanceError, match="screenshot does not match"):
        _validate_trusted_browser_observations([forged], observer)


def test_missing_observer_is_explicit_non_pass() -> None:
    with pytest.raises(AcceptanceCapabilityUnavailable, match="observer is unavailable"):
        _validate_trusted_browser_observations([], None)


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
