from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from . import typed_evidence
from .typed_evidence import TypedEvidenceError, evaluate_evidence, record_evidence


REPOSITORY = Path(__file__).resolve().parents[4]
CANDIDATE = subprocess.check_output(
    ["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], text=True
).strip()
BOARD = "sandbox-typed-evidence"
EVIDENCE_KEY = "11" * 32
SOURCE_KEY = "22" * 32


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sign(document: dict[str, Any], fields: list[str], key_hex: str, signature_field: str = "signature") -> None:
    del fields
    document[signature_field] = hmac.new(
        bytes.fromhex(key_hex), _json_bytes(document), hashlib.sha256
    ).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    state = "idle"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        for name in (
            "X-Pursers-Observation-Id", "X-Pursers-Run-Id",
            "X-Pursers-Action-Id", "X-Pursers-Entity-Id",
        ):
            self.send_header(name, self.headers.get(name, ""))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _common(self) -> dict[str, Any]:
        return {
            "board": BOARD, "candidate": CANDIDATE, "surface": "fleet",
            "runtime": "fleet-runtime-1",
            "entity": self.headers["X-Pursers-Entity-Id"],
            "run": self.headers["X-Pursers-Run-Id"],
            "action": self.headers["X-Pursers-Action-Id"],
        }

    def do_GET(self) -> None:
        payload = self._common()
        if self.path == "/status":
            payload.update({"ok": True, "count": 3})
            self._send(200, payload)
        elif self.path == "/state":
            payload.update({"state": type(self).state})
            self._send(200, payload)
        elif self.path == "/wrong-entity":
            payload.update({"entity": "decoy", "ok": True})
            self._send(200, payload)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/decoy")
            self.end_headers()
        else:
            self._send(404, payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        payload = self._common()
        if self.path == "/state/action" and body.get("next") in {"busy", "idle"}:
            type(self).state = body["next"]
            payload.update({"state": type(self).state, "accepted": True})
            self._send(202, payload)
        elif self.path == "/state/action":
            payload.update({"state": type(self).state, "accepted": False})
            self._send(409, payload)
        else:
            self._send(404, payload)


@pytest.fixture
def http_server() -> str:
    _Handler.state = "idle"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _context(**changes: Any) -> dict[str, Any]:
    value = {
        "observation_id": "fleet.ticket-row",
        "run_id": "run-1",
        "action_id": "read-status",
        "entity": "TK-123",
        "surface": "fleet",
        "board_id": BOARD,
        "candidate_commit": CANDIDATE,
        "issued_at": _now(),
        "causal_index": 3,
    }
    value.update(changes)
    return value


def _trust(tmp_path: Path, http_server: str, **changes: Any) -> dict[str, Any]:
    candidate_checkout = tmp_path / "candidate-checkout"
    if not candidate_checkout.exists():
        subprocess.run(
            [
                "git", "clone", "--quiet", "--shared", "--no-checkout",
                str(REPOSITORY), str(candidate_checkout),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(candidate_checkout), "checkout", "--quiet", CANDIDATE],
            check=True,
        )
    pid_file = tmp_path / "http-runtime.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    pid_file.chmod(0o600)
    command = subprocess.check_output(
        ["/bin/ps", "-p", str(os.getpid()), "-o", "command="], text=True
    ).strip()
    start_time = subprocess.check_output(
        ["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="], text=True
    ).strip()
    artifact = (
        candidate_checkout
        / "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py"
    )
    runtime = {
        "pid_file": str(pid_file),
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "start_time": start_time,
        "executable": str(Path(command.split()[0]).resolve()),
        "artifact_path": str(artifact),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "listener_port": int(http_server.rsplit(":", 1)[1]),
    }
    http_source = {
        "adapter": "trusted_http_v1",
        "provenance": "fleet-disposable-service",
        "runtime_id": "fleet-runtime-1",
        "base_url": http_server,
        "surface": "fleet",
        "board_id": BOARD,
        "candidate_commit": CANDIDATE,
        "methods": ["GET", "POST"],
        "headers": {},
        "timeout_seconds": 2,
        "select_allowlist": ["/ok", "/count", "/state", "/accepted"],
        "response_bindings": {
            "/board": "$board_id", "/candidate": "$candidate_commit",
            "/surface": "$surface", "/entity": "$entity",
            "/run": "$run_id", "/action": "$action_id",
            "/runtime": "fleet-runtime-1",
        },
        "runtime": runtime,
    }
    value = {
        "schema_version": 1,
        "verifier_id": "purser-reviewer-2",
        "trusted_module_path": str(Path(typed_evidence.__file__).resolve()),
        "module_sha256": typed_evidence._module_digest(),
        "candidate_checkout_root": str(candidate_checkout),
        "candidate_commit": CANDIDATE,
        "board_id": BOARD,
        "max_age_seconds": 300,
        "active_evidence_key": "test-key",
        "evidence_keys": {"test-key": EVIDENCE_KEY},
        "http_sources": {"fleet-api": http_source},
        "receipt_sources": {},
        "log_sources": {},
        "state_sources": {
            "fleet-state": {
                "adapter": "trusted_http_state_v1",
                "provenance": "fleet-disposable-state",
                "runtime_id": "fleet-runtime-1",
                "http_source_id": "fleet-api",
                "http_source_config_sha256": typed_evidence._digest(http_source),
            }
        },
        "replay_guard": {"path": str(tmp_path / "replay.log"), "consume": False},
    }
    value.update(changes)
    return value


def _request(kind: str, recorder: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": 1, "kind": kind, "context": context or _context(), "recorder": recorder}


def _expected(evidence: dict[str, Any], conjuncts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1, "kind": evidence["kind"],
        "context": dict(evidence["context"]), "all_of": conjuncts,
    }


def _resign_evidence(evidence: dict[str, Any]) -> None:
    evidence["payload_sha256"] = hashlib.sha256(
        _json_bytes(evidence["record"])
    ).hexdigest()
    unsigned = {key: value for key, value in evidence.items() if key != "auth"}
    evidence["auth"]["hmac_sha256"] = hmac.new(
        bytes.fromhex(EVIDENCE_KEY), _json_bytes(unsigned), hashlib.sha256
    ).hexdigest()


def _http_recorder(path: str = "/status") -> dict[str, Any]:
    return {
        "source_id": "fleet-api", "action_origin": "verifier_api",
        "request": {"method": "GET", "path": path, "body": None, "select": ["/ok", "/count"]},
    }


def test_http_response_real_roundtrip_and_all_of(tmp_path: Path, http_server: str) -> None:
    trust = _trust(tmp_path, http_server)
    evidence = record_evidence(_request("http_response", _http_recorder()), trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"target": "status", "path": "", "op": "eq", "value": 200},
        {"target": "field", "path": "/ok", "op": "eq", "value": True},
        {"target": "field", "path": "/count", "op": "gte", "value": 2},
        {"target": "action_origin", "path": "", "op": "eq", "value": "verifier_api"},
    ]), trust)
    assert result["passed"] is True
    assert result["correlation"]["entity"] == "TK-123"
    assert evidence["record"]["response"]["path"] == "/status"
    assert "headers" not in json.dumps(evidence).lower()


def test_http_rejects_wrong_target_stale_and_replay(tmp_path: Path, http_server: str) -> None:
    trust = _trust(tmp_path, http_server)
    request = _request("http_response", _http_recorder("/wrong-entity"))
    with pytest.raises(TypedEvidenceError, match="binding mismatch"):
        record_evidence(request, trust)
    redirect = _request("http_response", _http_recorder("/redirect"))
    with pytest.raises(TypedEvidenceError, match="bounded JSON"):
        record_evidence(redirect, trust)

    stale = _request("http_response", _http_recorder(), _context(
        issued_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    ))
    with pytest.raises(TypedEvidenceError, match="stale"):
        record_evidence(stale, trust)

    evidence = record_evidence(_request("http_response", _http_recorder()), trust)
    expected = _expected(evidence, [{"target": "status", "path": "", "op": "eq", "value": 200}])
    consuming = {**trust, "replay_guard": {"path": str(tmp_path / "seen.log"), "consume": True}}
    assert evaluate_evidence(evidence, expected, consuming)["passed"]
    with pytest.raises(TypedEvidenceError, match="replay"):
        evaluate_evidence(evidence, expected, consuming)


def test_http_direct_api_cannot_spoof_browser_observation(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    recorder = _http_recorder()
    recorder["action_origin"] = "browser_observed"
    with pytest.raises(TypedEvidenceError, match="action_origin"):
        record_evidence(_request("http_response", recorder), trust)


def test_http_rejects_unrelated_echo_service_even_with_matching_body(
    tmp_path: Path, http_server: str,
) -> None:
    decoy = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=decoy.serve_forever, daemon=True)
    thread.start()
    try:
        trust = _trust(tmp_path, http_server)
        trust["http_sources"]["fleet-api"]["base_url"] = (
            f"http://127.0.0.1:{decoy.server_port}"
        )
        with pytest.raises(TypedEvidenceError, match="listener port"):
            record_evidence(_request("http_response", _http_recorder()), trust)
    finally:
        decoy.shutdown()
        thread.join(timeout=2)


def test_http_rejects_plaintext_non_loopback_origin(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    port = trust["http_sources"]["fleet-api"]["runtime"]["listener_port"]
    trust["http_sources"]["fleet-api"]["base_url"] = (
        f"http://example.invalid:{port}"
    )
    with pytest.raises(TypedEvidenceError, match="loopback or verified TLS"):
        record_evidence(_request("http_response", _http_recorder()), trust)


def _receipt_source(path: Path, process: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "adapter": "hmac_json_v1", "provenance": "personal-runtime-receipt",
        "runtime_id": "personal-runtime-1", "path": str(path),
        "hmac_key_hex": SOURCE_KEY, "signature_field": "signature",
        "document_keys": [
            "schema_version", "issuer", "runtime_id", "pid", "candidate_commit",
            "board_id", "surface", "entity", "run_id", "action_id",
            "transport", "role", "captured_at",
        ],
        "timestamp_pointer": "/captured_at", "max_age_seconds": 300,
        "required_bindings": {
            "/candidate_commit": "$candidate_commit", "/board_id": "$board_id",
            "/surface": "$surface", "/entity": "$entity", "/run_id": "$run_id",
            "/action_id": "$action_id",
        },
        "issuer_pointer": "/issuer", "issuer": "personal-verifier",
        "runtime_pointer": "/runtime_id", "transport_pointer": "/transport",
        "transport": "stdio",
        "process": process,
    }


def _personal_receipt_source(
    path: Path, candidate_source: Path, process: dict[str, Any],
) -> dict[str, Any]:
    return {
        "adapter": "personal_runtime_receipt_v1",
        "provenance": "personal-runtime-receipt",
        "runtime_id": "personal-runtime-1",
        "path": str(path),
        "max_age_seconds": 300,
        "required_bindings": {
            "/candidate_commit": "$candidate_commit",
            "/board_id": "$board_id",
        },
        "transport_pointer": "/transport",
        "transport": "stdio",
        "process": process,
        "document_keys": [
            "schema_version", "product", "server_name", "version", "build",
            "candidate_commit", "candidate_source", "board_id", "pid",
            "transport",
        ],
        "candidate_source": str(candidate_source),
        "candidate_source_sha256": hashlib.sha256(
            candidate_source.read_bytes()
        ).hexdigest(),
        "product": "Pursers Personal",
        "server_name": "On Board Personal",
    }


def _write_receipt(path: Path, **changes: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1, "issuer": "personal-verifier", "runtime_id": "personal-runtime-1",
        "pid": os.getpid(), "candidate_commit": CANDIDATE, "board_id": BOARD,
        "surface": "personal", "entity": "seat-3", "run_id": "run-1",
        "action_id": "read-role", "transport": "stdio", "role": "worker",
        "captured_at": _now(),
    }
    value.update(changes)
    fields: list[str] = []
    _sign(value, fields, SOURCE_KEY)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return value


def test_receipt_field_real_roundtrip_and_forgery(tmp_path: Path, http_server: str) -> None:
    path = tmp_path / "receipt.json"
    _write_receipt(path)
    context = _context(
        observation_id="personal.role", action_id="read-role", entity="seat-3", surface="personal"
    )
    trust = _trust(tmp_path, http_server)
    trust["receipt_sources"] = {"personal-receipt": _receipt_source(path)}
    request = _request("receipt_field", {"source_id": "personal-receipt", "fields": ["/role", "/transport", "/pid"]}, context)
    evidence = record_evidence(request, trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"path": "/role", "op": "eq", "value": "worker"},
        {"path": "/transport", "op": "eq", "value": "stdio"},
    ]), trust)
    assert result["passed"] is True
    assert evidence["record"]["authenticity"] == "canonical_hmac_sha256"

    receipt = json.loads(path.read_text())
    receipt["role"] = "reviewer"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(TypedEvidenceError, match="authenticity"):
        record_evidence(request, trust)

    _write_receipt(path, captured_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    with pytest.raises(TypedEvidenceError, match="stale"):
        record_evidence(request, trust)


def test_receipt_rejects_decoy_pid(tmp_path: Path, http_server: str) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "typed-evidence-marker"])
    try:
        pid_file = tmp_path / "runtime.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        pid_file.chmod(0o600)
        path = tmp_path / "receipt.json"
        _write_receipt(path, pid=process.pid + 1)
        context = _context(
            observation_id="personal.role", action_id="read-role", entity="seat-3", surface="personal"
        )
        trust = _trust(tmp_path, http_server)
        process_trust = {
            "pid_file": str(pid_file),
            "argv0_names": [Path(sys.executable).name, "Python"], "argv_prefix": ["-c"],
            "argv_contains": ["typed-evidence-marker"],
            "receipt_pid_pointer": "/pid",
        }
        trust["receipt_sources"] = {"personal-receipt": _receipt_source(path, process_trust)}
        request = _request("receipt_field", {"source_id": "personal-receipt", "fields": ["/role"]}, context)
        with pytest.raises(TypedEvidenceError, match="receipt PID"):
            record_evidence(request, trust)
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_receipt_rejects_unsigned_empty_container(
    tmp_path: Path, http_server: str,
) -> None:
    path = tmp_path / "receipt.json"
    _write_receipt(path)
    receipt = json.loads(path.read_text())
    receipt["unsigned"] = {}
    path.write_text(json.dumps(receipt), encoding="utf-8")
    path.chmod(0o600)
    trust = _trust(tmp_path, http_server)
    trust["receipt_sources"] = {"personal-receipt": _receipt_source(path)}
    context = _context(
        observation_id="personal.role", action_id="read-role",
        entity="seat-3", surface="personal",
    )
    with pytest.raises(TypedEvidenceError, match="document fields"):
        record_evidence(
            _request(
                "receipt_field",
                {"source_id": "personal-receipt", "fields": ["/role"]},
                context,
            ),
            trust,
        )


def test_personal_receipt_real_producer_capture_adapter(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    candidate_source = (
        Path(trust["candidate_checkout_root"])
        / "packages/personal/src/pursers_personal/apps_server.py"
    )
    receipt = tmp_path / "personal-runtime.json"
    producer = tmp_path / "personal_receipt_producer.py"
    producer.write_text(
        "import pathlib,sys,time\n"
        "from types import SimpleNamespace\n"
        "from pursers_personal.apps_server import _write_acceptance_runtime_receipt\n"
        "board=sys.argv[4]; state=SimpleNamespace(config=SimpleNamespace(board_id=board))\n"
        "_write_acceptance_runtime_receipt(pathlib.Path(sys.argv[1]),state=state,"
        "candidate_source=pathlib.Path(sys.argv[2]),candidate_commit=sys.argv[3],"
        "board_id=board)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    personal_src = Path(trust["candidate_checkout_root"]) / "packages/personal/src"
    process = subprocess.Popen(
        [
            sys.executable, str(producer), str(receipt), str(candidate_source),
            CANDIDATE, BOARD,
        ],
        env={**os.environ, "PYTHONPATH": str(personal_src)},
    )
    try:
        for _ in range(100):
            if receipt.exists():
                break
            threading.Event().wait(0.01)
        assert receipt.exists()
        pid_file = tmp_path / "personal-runtime.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        pid_file.chmod(0o600)
        process_trust = {
            "pid_file": str(pid_file),
            "argv0_names": [Path(sys.executable).name, "Python"],
            "argv_prefix": [str(producer)],
            "argv_contains": [BOARD],
            "receipt_pid_pointer": "/pid",
        }
        trust["receipt_sources"] = {
            "personal-runtime": _personal_receipt_source(
                receipt, candidate_source, process_trust
            )
        }
        context = _context(
            observation_id="personal.runtime", action_id="capture-runtime",
            entity="personal-mcp", surface="personal",
        )
        evidence = record_evidence(
            _request(
                "receipt_field",
                {
                    "source_id": "personal-runtime",
                    "fields": ["/product", "/build", "/transport"],
                },
                context,
            ),
            trust,
        )
        result = evaluate_evidence(
            evidence,
            _expected(
                evidence,
                [
                    {"path": "/product", "op": "eq", "value": "Pursers Personal"},
                    {"path": "/transport", "op": "eq", "value": "stdio"},
                ],
            ),
            trust,
        )
        assert result["passed"] is True
        assert (
            evidence["record"]["authenticity"]
            == "verifier_bound_personal_runtime"
        )
    finally:
        process.terminate()
        process.wait(timeout=3)


def _log_source(
    path: Path, process: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "adapter": "hmac_jsonl_v1", "provenance": "central-authenticated-stderr",
        "runtime_id": "central-runtime-1", "path": str(path), "hmac_key_hex": SOURCE_KEY,
        "signature_field": "signature",
        "document_keys": [
            "emitter", "timestamp", "candidate_commit", "board_id", "surface",
            "entity", "run_id", "action_id", "event", "outcome", "runtime_id",
        ],
        "timestamp_pointer": "/timestamp", "max_age_seconds": 300,
        "required_bindings": {
            "/candidate_commit": "$candidate_commit", "/board_id": "$board_id",
            "/surface": "$surface", "/entity": "$entity", "/run_id": "$run_id",
            "/action_id": "$action_id",
        },
        "emitter": "central-runtime", "runtime_pointer": "/runtime_id",
        "max_bytes": 65_536, "process": process,
    }


def _log_entry(**changes: Any) -> dict[str, Any]:
    value = {
        "emitter": "central-runtime", "timestamp": _now(), "candidate_commit": CANDIDATE,
        "board_id": BOARD, "surface": "fleet", "entity": "TK-123", "run_id": "run-1",
        "action_id": "submit-ticket", "event": "ticket_submitted", "outcome": "accepted",
        "runtime_id": "central-runtime-1",
    }
    value.update(changes)
    fields: list[str] = []
    _sign(value, fields, SOURCE_KEY)
    return value


def test_log_assertion_real_roundtrip_and_substitution(tmp_path: Path, http_server: str) -> None:
    path = tmp_path / "central.jsonl"
    authentic = _log_entry()
    fake = {**authentic, "outcome": "fake-success"}
    path.write_text(json.dumps(fake) + "\n" + json.dumps(authentic) + "\n", encoding="utf-8")
    path.chmod(0o600)
    trust = _trust(tmp_path, http_server)
    trust["log_sources"] = {"central-log": _log_source(path)}
    context = _context(action_id="submit-ticket")
    request = _request("log_assertion", {
        "source_id": "central-log", "field_equals": {"/event": "ticket_submitted"},
    }, context)
    evidence = record_evidence(request, trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"path": "/event", "op": "eq", "value": "ticket_submitted"},
        {"path": "/outcome", "op": "eq", "value": "accepted"},
    ]), trust)
    assert result["passed"]
    assert "signature" not in evidence["record"]["entry"]

    substituted = _log_entry(emitter="decoy")
    path.write_text(json.dumps(substituted) + "\n", encoding="utf-8")
    with pytest.raises(TypedEvidenceError, match="exactly one"):
        record_evidence(request, trust)

    stale = _log_entry(timestamp=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    with pytest.raises(TypedEvidenceError, match="exactly one"):
        record_evidence(request, trust)


def test_log_rejects_unsigned_empty_container(
    tmp_path: Path, http_server: str,
) -> None:
    path = tmp_path / "central.jsonl"
    entry = _log_entry()
    entry["unsigned"] = []
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    path.chmod(0o600)
    trust = _trust(tmp_path, http_server)
    trust["log_sources"] = {"central-log": _log_source(path)}
    request = _request(
        "log_assertion",
        {"source_id": "central-log", "field_equals": {"/event": "ticket_submitted"}},
        _context(action_id="submit-ticket"),
    )
    with pytest.raises(TypedEvidenceError, match="exactly one"):
        record_evidence(request, trust)


def test_verified_emitter_producer_to_log_recorder(
    tmp_path: Path, http_server: str,
) -> None:
    path = tmp_path / "emitter.jsonl"
    key_file = tmp_path / "emitter.key"
    key_file.write_text(SOURCE_KEY, encoding="utf-8")
    key_file.chmod(0o600)
    producer = tmp_path / "verified_emitter.py"
    producer.write_text(
        "import hashlib,hmac,json,os,pathlib,sys,time\n"
        "out=pathlib.Path(sys.argv[1]); key=bytes.fromhex(pathlib.Path(sys.argv[2]).read_text())\n"
        "value={'emitter':'central-runtime','timestamp':"
        "time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"
        "'candidate_commit':sys.argv[3],'board_id':sys.argv[4],"
        "'surface':'fleet','entity':'TK-123','run_id':'run-1',"
        "'action_id':'submit-ticket','event':'ticket_submitted',"
        "'outcome':'accepted','runtime_id':'central-runtime-1'}\n"
        "raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode(); "
        "value['signature']=hmac.new(key,raw,hashlib.sha256).hexdigest()\n"
        "out.write_text(json.dumps(value)+'\\n'); out.chmod(0o600); time.sleep(30)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable, str(producer), str(path), str(key_file),
            CANDIDATE, BOARD,
        ]
    )
    try:
        for _ in range(100):
            if path.exists():
                break
            threading.Event().wait(0.01)
        assert path.exists()
        pid_file = tmp_path / "emitter.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        pid_file.chmod(0o600)
        process_trust = {
            "pid_file": str(pid_file),
            "argv0_names": [Path(sys.executable).name, "Python"],
            "argv_prefix": [str(producer)],
            "argv_contains": [BOARD],
            "receipt_pid_pointer": "/pid",
        }
        trust = _trust(tmp_path, http_server)
        trust["log_sources"] = {
            "central-log": _log_source(path, process_trust)
        }
        evidence = record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "central-log",
                    "field_equals": {"/event": "ticket_submitted"},
                },
                _context(action_id="submit-ticket"),
            ),
            trust,
        )
        result = evaluate_evidence(
            evidence,
            _expected(
                evidence,
                [
                    {"path": "/event", "op": "eq", "value": "ticket_submitted"},
                    {"path": "/outcome", "op": "eq", "value": "accepted"},
                ],
            ),
            trust,
        )
        assert result["passed"] is True
        assert evidence["record"]["process"]["pid"] == process.pid
    finally:
        process.terminate()
        process.wait(timeout=3)

def _state_recorder(next_state: str) -> dict[str, Any]:
    return {
        "source_id": "fleet-state",
        "before": {"method": "GET", "path": "/state", "body": None, "select": ["/state"]},
        "action": {"method": "POST", "path": "/state/action", "body": {"next": next_state}, "select": ["/state", "/accepted"]},
        "after": {"method": "GET", "path": "/state", "body": None, "select": ["/state"]},
    }


def test_state_transition_real_roundtrip_and_negative_action(tmp_path: Path, http_server: str) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(action_id="set-busy", causal_index=9)
    evidence = record_evidence(_request("state_transition", _state_recorder("busy"), context), trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"phase": "before", "path": "/state", "op": "eq", "value": "idle"},
        {"phase": "action", "path": "/status", "op": "eq", "value": 202},
        {"phase": "after", "path": "/state", "op": "eq", "value": "busy"},
    ]), trust)
    assert result["passed"]
    assert result["correlation"]["causal_index"] == 9

    negative_context = _context(action_id="reject-invalid", causal_index=10)
    negative = record_evidence(_request("state_transition", _state_recorder("invalid"), negative_context), trust)
    rejected = evaluate_evidence(negative, _expected(negative, [
        {"phase": "action", "path": "/status", "op": "eq", "value": 409},
        {"phase": "action", "path": "/accepted", "op": "eq", "value": False},
        {"phase": "after", "path": "/state", "op": "eq", "value": "busy"},
    ]), trust)
    assert rejected["passed"]


def test_state_transition_rejects_authenticated_out_of_order_record(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    evidence = record_evidence(_request("state_transition", _state_recorder("busy"), _context(action_id="set-busy")), trust)
    order = evidence["record"]["order"]
    order["before_at"], order["after_at"] = order["after_at"], order["before_at"]
    evidence["payload_sha256"] = hashlib.sha256(_json_bytes(evidence["record"])).hexdigest()
    unsigned = {key: value for key, value in evidence.items() if key != "auth"}
    evidence["auth"]["hmac_sha256"] = hmac.new(
        bytes.fromhex(EVIDENCE_KEY), _json_bytes(unsigned), hashlib.sha256
    ).hexdigest()
    expected = _expected(evidence, [{"phase": "action", "path": "/status", "op": "eq", "value": 202}])
    with pytest.raises(TypedEvidenceError, match="causal order"):
        evaluate_evidence(evidence, expected, trust)


def test_state_transition_rejects_runtime_and_source_digest_mismatch(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    trust["state_sources"]["fleet-state"]["runtime_id"] = "decoy-runtime"
    with pytest.raises(TypedEvidenceError, match="trusted HTTP runtime"):
        record_evidence(
            _request("state_transition", _state_recorder("busy")), trust
        )
    trust = _trust(tmp_path, http_server)
    trust["state_sources"]["fleet-state"]["http_source_config_sha256"] = "0" * 64
    with pytest.raises(TypedEvidenceError, match="trusted HTTP runtime"):
        record_evidence(
            _request("state_transition", _state_recorder("busy")), trust
        )


def test_all_kinds_reject_resigned_unknown_missing_and_wrong_nested_fields(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    receipt_path = tmp_path / "receipt.json"
    _write_receipt(receipt_path)
    trust["receipt_sources"] = {
        "personal-receipt": _receipt_source(receipt_path)
    }
    log_path = tmp_path / "central.jsonl"
    log_path.write_text(json.dumps(_log_entry()) + "\n", encoding="utf-8")
    log_path.chmod(0o600)
    trust["log_sources"] = {"central-log": _log_source(log_path)}
    receipt_context = _context(
        observation_id="personal.role", action_id="read-role",
        entity="seat-3", surface="personal",
    )
    evidence = {
        "http_response": record_evidence(
            _request("http_response", _http_recorder()), trust
        ),
        "receipt_field": record_evidence(
            _request(
                "receipt_field",
                {"source_id": "personal-receipt", "fields": ["/role"]},
                receipt_context,
            ),
            trust,
        ),
        "log_assertion": record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "central-log",
                    "field_equals": {"/event": "ticket_submitted"},
                },
                _context(action_id="submit-ticket"),
            ),
            trust,
        ),
        "state_transition": record_evidence(
            _request(
                "state_transition", _state_recorder("busy"),
                _context(action_id="set-busy"),
            ),
            trust,
        ),
    }
    mutations = {
        "http_response": (
            lambda record: record["response"].update({"unknown": {}}),
            lambda record: record["response"].pop("path"),
            lambda record: record["response"].update({"status": True}),
        ),
        "receipt_field": (
            lambda record: record.update({"unknown": {}}),
            lambda record: record.pop("receipt_sha256"),
            lambda record: record.update({"receipt_sha256": True}),
        ),
        "log_assertion": (
            lambda record: record.update({"unknown": []}),
            lambda record: record.pop("entry_sha256"),
            lambda record: record.update({"entry_sha256": False}),
        ),
        "state_transition": (
            lambda record: record["before"].update({"unknown": {}}),
            lambda record: record["before"].pop("path"),
            lambda record: record["before"].update({"status": True}),
        ),
    }
    for kind, originals in evidence.items():
        for mutation in mutations[kind]:
            changed = copy.deepcopy(originals)
            mutation(changed["record"])
            _resign_evidence(changed)
            expected = _expected(
                changed,
                (
                    [{"target": "status", "path": "", "op": "eq", "value": 200}]
                    if kind == "http_response"
                    else [{"path": "/role", "op": "eq", "value": "worker"}]
                    if kind == "receipt_field"
                    else [{"path": "/event", "op": "eq", "value": "ticket_submitted"}]
                    if kind == "log_assertion"
                    else [{"phase": "action", "path": "/status", "op": "eq", "value": 202}]
                ),
            )
            with pytest.raises(TypedEvidenceError):
                evaluate_evidence(changed, expected, trust)


def test_json_comparisons_keep_boolean_distinct_from_number(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    evidence = record_evidence(
        _request("http_response", _http_recorder()), trust
    )
    result = evaluate_evidence(
        evidence,
        _expected(
            evidence,
            [
                {"target": "field", "path": "/ok", "op": "eq", "value": 1},
                {"target": "field", "path": "/ok", "op": "in", "value": [1]},
            ],
        ),
        trust,
    )
    assert result["passed"] is False


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda request: request.pop("context"), "record request fields"),
        (lambda request: request["context"].update({"causal_index": "3"}), "causal_index"),
        (lambda request: request["recorder"].update({"extra": True}), "recorder fields"),
    ],
)
def test_missing_keys_and_wrong_types_fail_closed(
    tmp_path: Path, http_server: str, mutation: Any, match: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    request = _request("http_response", _http_recorder())
    mutation(request)
    with pytest.raises(TypedEvidenceError, match=match):
        record_evidence(request, trust)


def test_evidence_tamper_wrong_board_candidate_and_cross_entity_fail(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    evidence = record_evidence(_request("http_response", _http_recorder()), trust)
    evidence["record"]["response"]["status"] = 201
    with pytest.raises(TypedEvidenceError, match="authentication"):
        evaluate_evidence(evidence, _expected(evidence, [
            {"target": "status", "path": "", "op": "eq", "value": 201}
        ]), trust)
    for change, match in [({"board_id": "sandbox-other"}, "board"), ({"candidate_commit": "b" * 40}, "candidate")]:
        request = _request("http_response", _http_recorder(), _context(**change))
        with pytest.raises(TypedEvidenceError, match=match):
            record_evidence(request, trust)
    wrong_entity = _request("state_transition", _state_recorder("busy"), _context(entity="TK-decoy"))
    # The service echoes the requested entity; expected context cannot be relabelled later.
    cross = record_evidence(wrong_entity, trust)
    expected = _expected(cross, [{"phase": "action", "path": "/status", "op": "eq", "value": 202}])
    expected["context"]["entity"] = "TK-123"
    with pytest.raises(TypedEvidenceError, match="context"):
        evaluate_evidence(cross, expected, trust)


def test_installed_standalone_cli_roundtrip(tmp_path: Path, http_server: str) -> None:
    install_dir = tmp_path / "verifier"
    installed = typed_evidence.install_module(install_dir)
    module = Path(installed["module"])
    trust = _trust(tmp_path, http_server)
    trust["trusted_module_path"] = str(module)
    trust["module_sha256"] = installed["module_sha256"]
    request = _request("http_response", _http_recorder())
    request_path = tmp_path / "request.json"
    trust_path = tmp_path / "trust.json"
    evidence_path = tmp_path / "evidence.json"
    expected_path = tmp_path / "expected.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    completed = subprocess.run(
        [str(module), "record", "--request", str(request_path), "--trust", str(trust_path), "--output", str(evidence_path)],
        text=True, capture_output=True, check=False, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(evidence_path.read_text())
    expected_path.write_text(json.dumps(_expected(evidence, [
        {"target": "status", "path": "", "op": "eq", "value": 200}
    ])), encoding="utf-8")
    checked = subprocess.run(
        [str(module), "evaluate", "--evidence", str(evidence_path), "--expected", str(expected_path), "--trust", str(trust_path)],
        text=True, capture_output=True, check=False, timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["passed"] is True
