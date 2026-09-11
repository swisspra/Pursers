from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import socket
import shutil
import subprocess
import sys
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

if __package__:
    from . import typed_evidence
    from .typed_evidence import TypedEvidenceError, evaluate_evidence, record_evidence
else:  # pragma: no cover - disposable candidate subprocess entrypoints
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from home_acceptance import typed_evidence
    from home_acceptance.typed_evidence import (
        TypedEvidenceError, evaluate_evidence, record_evidence,
    )


REPOSITORY = Path(__file__).resolve().parents[4]
CANDIDATE = subprocess.check_output(
    ["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], text=True
).strip()
BOARD = "sandbox-typed-evidence"
EVIDENCE_KEY = "11" * 32
SOURCE_KEY = "22" * 32
RELEASE_VERSIONS = tomllib.loads(
    (REPOSITORY / "tools/release_versions.toml").read_text(encoding="utf-8")
)
PERSONAL_VERSION = RELEASE_VERSIONS["packages"]["personal"]


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
            payload.update({
                "state": type(self).state,
                "accepted": True,
                "action_sha256": self.headers.get("X-Pursers-Action-SHA256"),
            })
            self._send(202, payload)
        elif self.path == "/state/action":
            payload.update({
                "state": type(self).state,
                "accepted": False,
                "action_sha256": self.headers.get("X-Pursers-Action-SHA256"),
            })
            self._send(409, payload)
        elif self.path == "/api/attention":
            action_sha256 = self.headers.get("X-Pursers-Action-SHA256")
            payload["items"] = body
            payload["_evidence"] = {
                "schema_version": 1,
                "emitter": "fleet-dashboard-runtime",
                "timestamp": _now(),
                "runtime_id": "fleet-runtime-1",
                "pid": os.getpid(),
                "candidate_commit": CANDIDATE,
                "entrypoint_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "board_id": BOARD,
                "surface": "fleet",
                "observation_id": self.headers["X-Pursers-Observation-Id"],
                "run_id": self.headers["X-Pursers-Run-Id"],
                "action_id": self.headers["X-Pursers-Action-Id"],
                "entity": self.headers["X-Pursers-Entity-Id"],
                "method": "POST",
                "path": "/api/attention",
                "status": 200,
                "outcome": "succeeded",
                "effect": "attention_state_changed",
                "changed": True,
                "before_sha256": typed_evidence._digest({}),
                "after_sha256": typed_evidence._digest(body),
                "result_sha256": hashlib.sha256(
                    _json_bytes({"items": body})
                ).hexdigest(),
                "action_sha256": action_sha256,
                "log_emitted": True,
            }
            self._send(200, payload)
        else:
            self._send(404, payload)


_HTTP_RUNTIMES: dict[str, subprocess.Popen[str]] = {}


def _candidate_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "candidate-checkout"
    if not checkout.exists():
        subprocess.run(
            [
                "git", "clone", "--quiet", "--shared", "--no-checkout",
                str(REPOSITORY), str(checkout),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", CANDIDATE],
            check=True,
        )
    return checkout


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _wait_for_port(process: subprocess.Popen[str], port: int) -> None:
    for _ in range(200):
        if process.poll() is not None:
            _stdout, stderr = process.communicate()
            raise AssertionError(f"candidate service exited: {stderr[-1000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                return
        except OSError:
            threading.Event().wait(0.01)
    raise AssertionError("candidate service did not listen")


@pytest.fixture
def http_server(tmp_path: Path) -> str:
    checkout = _candidate_checkout(tmp_path)
    artifact = checkout / "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py"
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(artifact), "--typed-evidence-http-server", str(port)],
        cwd=checkout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_port(process, port)
    url = f"http://127.0.0.1:{port}"
    _HTTP_RUNTIMES[url] = process
    try:
        yield url
    finally:
        _HTTP_RUNTIMES.pop(url, None)
        process.terminate()
        process.wait(timeout=3)


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
    candidate_checkout = _candidate_checkout(tmp_path)
    process = _HTTP_RUNTIMES[http_server]
    pid_file = tmp_path / "http-runtime.pid"
    pid_file.write_text(str(process.pid), encoding="utf-8")
    pid_file.chmod(0o600)
    command = subprocess.check_output(
        ["/bin/ps", "-p", str(process.pid), "-o", "command="], text=True
    ).strip()
    start_time = subprocess.check_output(
        ["/bin/ps", "-p", str(process.pid), "-o", "lstart="], text=True
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
        "cwd": str(candidate_checkout),
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
        "select_allowlist": [
            "/ok", "/count", "/state", "/accepted", "/action_sha256",
            *sorted(typed_evidence.FLEET_ACTION_RESPONSE_POINTERS["/api/attention"]),
        ],
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
        "mcp_sources": {},
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


def _mcp_trust(tmp_path: Path, http_server: str) -> tuple[dict[str, Any], dict[str, Any]]:
    trust = _trust(tmp_path, http_server)
    checkout = Path(trust["candidate_checkout_root"])
    candidate_source = (
        checkout / "packages/personal/src/pursers_personal/apps_server.py"
    )
    challenge = tmp_path / "mcp-challenge.key"
    challenge.write_bytes(b"\x33" * 32)
    challenge.chmod(0o600)
    executable = Path(sys.executable).resolve()
    source = {
        "adapter": "trusted_mcp_stdio_v1",
        "provenance": "personal-live-stdio",
        "runtime_id": "personal-mcp-runtime-1",
        "surface": "personal",
        "board_id": BOARD,
        "candidate_commit": CANDIDATE,
        "command": str(executable),
        "command_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "args": ["-I", "-m", "pursers_personal.cli", "mcp"],
        "env": {"PATH": os.defpath},
        "cwd": str(checkout),
        "candidate_source": str(candidate_source),
        "candidate_source_sha256": hashlib.sha256(candidate_source.read_bytes()).hexdigest(),
        "challenge_key": str(challenge),
        "tool": "board_snapshot",
        "arguments": {},
        "select_allowlist": ["/connected", "/tickets"],
        "timeout_seconds": 2,
    }
    trust["mcp_sources"]["personal-board-snapshot"] = source
    context = _context(
        observation_id="personal-mcp.state.board-empty",
        action_id="board-snapshot",
        entity="personal-board",
        surface="personal",
    )
    return trust, context


async def _fake_mcp_result(
    source: dict[str, Any], context: dict[str, Any], selected: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    del selected
    pid = os.getpid()
    nonce = "44" * 32
    claim = {
        "schema_version": 1,
        "server_name": "On Board Personal",
        "version": PERSONAL_VERSION,
        "build": source["candidate_source_sha256"],
        "candidate_commit": context["candidate_commit"],
        "candidate_source": str(Path(source["candidate_source"]).resolve()),
        "board_id": context["board_id"],
        "pid": pid,
        "transport": "stdio",
        "nonce": nonce,
    }
    signature = hmac.new(
        Path(source["challenge_key"]).read_bytes(),
        _json_bytes(claim), hashlib.sha256,
    ).hexdigest()
    public = {
        key: value for key, value in {**claim, "signature": signature}.items()
        if key != "candidate_source"
    }
    public["candidate_source_sha256"] = source["candidate_source_sha256"]
    process = {
        "pid": pid,
        "argv_sha256": "55" * 32,
        "executable_sha256": source["command_sha256"],
        "candidate_source_sha256": source["candidate_source_sha256"],
    }
    result = {
        "tool": source["tool"],
        "arguments_sha256": typed_evidence._digest(source["arguments"]),
        "selected": {"/connected": True, "/tickets": []},
        "result_sha256": "66" * 32,
        "correlation": {
            key: context[key]
            for key in ("observation_id", "run_id", "action_id", "entity")
        },
    }
    return process, public, result


def test_mcp_stdio_response_is_source_bound_and_field_exact(
    tmp_path: Path, http_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust, context = _mcp_trust(tmp_path, http_server)
    monkeypatch.setattr(typed_evidence, "_execute_mcp_tool", _fake_mcp_result)
    recorder = {
        "source_id": "personal-board-snapshot",
        "tool": "board_snapshot",
        "arguments": {},
        "select": ["/connected", "/tickets"],
    }
    evidence = record_evidence(_request("mcp_tool_response", recorder, context), trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"path": "/connected", "op": "eq", "value": True},
        {"path": "/tickets", "op": "eq", "value": []},
    ]), trust)
    assert result["passed"] is True
    assert trust["mcp_sources"]["personal-board-snapshot"]["candidate_source"] not in json.dumps(evidence)

    wrong = _expected(evidence, [
        {"path": "/tickets", "op": "eq", "value": ["decoy"]},
    ])
    assert evaluate_evidence(evidence, wrong, trust)["passed"] is False
    evidence_path = tmp_path / "mcp-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    parent_request = {
        "observation_id": context["observation_id"],
        "run_id": context["run_id"],
        "action_id": context["action_id"],
        "entity": context["entity"],
        "causal_index": context["causal_index"],
        "surface_id": context["surface"],
        "board_id": context["board_id"],
        "candidate_commit": context["candidate_commit"],
        "conjunct": {
            "kind": "mcp_tool_response",
            "source_id": "personal-board-snapshot",
            "assertions": [
                {"path": "/connected", "op": "eq", "value": True},
                {"path": "/tickets", "op": "eq", "value": []},
            ],
        },
        "evidence_path": str(evidence_path),
    }
    assert typed_evidence.evaluate_parent_request(parent_request, trust)["passed"] is True


def test_mcp_stdio_rejects_inert_module_tuple(
    tmp_path: Path, http_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust, context = _mcp_trust(tmp_path, http_server)
    trust["mcp_sources"]["personal-board-snapshot"]["args"] = [
        "-c", "pass", "-m", "pursers_personal.cli", "mcp",
    ]
    monkeypatch.setattr(typed_evidence, "_execute_mcp_tool", _fake_mcp_result)
    recorder = {
        "source_id": "personal-board-snapshot",
        "tool": "board_snapshot", "arguments": {}, "select": ["/connected"],
    }
    with pytest.raises(TypedEvidenceError, match="does not execute"):
        record_evidence(_request("mcp_tool_response", recorder, context), trust)


def _browser_state_trust(
    tmp_path: Path, http_server: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    trust = _trust(tmp_path, http_server)
    verifier = tmp_path / "browser-verifier"
    verifier.mkdir(mode=0o700)
    config = verifier / "observer.json"
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o600)
    command = verifier / "browser_observer.py"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import datetime,hashlib,json,sys\n"
        "request=json.load(sys.stdin)\n"
        "context=request['context']\n"
        "corr={k:context[k] for k in ('observation_id','run_id','action_id','entity')}\n"
        "def phase(name,method,selected):\n"
        " return {'method':method,'path':'/browser/state/'+name,'status':200,'selected':selected,'response_sha256':hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest(),'correlation':corr}\n"
        "now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')\n"
        "print(json.dumps({'schema_version':1,'context':context,'surface_id':request['surface_id'],'target':request['target'],'candidate_commit':request['candidate_commit'],'page_url':request['page_url'],'runtime':{'product':'AionUi','version':'2.2.1','build':'build-1','source':'signed-aionui-webui-listener'},'before':phase('before','GET',{'/state':'idle'}),'action':phase('action','POST',{'/performed':'clicked'}),'after':phase('after','GET',{'/state':'ready'}),'order':{'before_at':now,'action_at':now,'after_at':now}}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    recipe = {
        "before": [{"path": "/state", "selector": "#status", "property": "text"}],
        "actions": [{"kind": "click", "selector": "#start", "path": "/performed"}],
        "after": [{"path": "/state", "selector": "#status", "property": "text"}],
        "settle_milliseconds": 0,
    }
    source = {
        "adapter": "trusted_browser_state_v1",
        "provenance": "verifier-browser-transition",
        "runtime_id": "aionui-browser-1",
        "surface": "aionui",
        "board_id": BOARD,
        "candidate_commit": CANDIDATE,
        "command": str(command),
        "command_sha256": hashlib.sha256(command.read_bytes()).hexdigest(),
        "config_path": str(config),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "base_url": "http://127.0.0.1:18921",
        "page_url": "http://127.0.0.1:18921/home",
        "recipe": recipe,
        "env": {"PATH": os.defpath},
        "timeout_seconds": 10,
        "select_allowlist": ["/state", "/performed"],
    }
    trust["state_sources"]["aionui-start"] = source
    context = _context(
        observation_id="extension.join-progress", action_id="start",
        entity="aionui-home", surface="aionui",
    )
    recorder = {
        "source_id": "aionui-start",
        "before": recipe["before"],
        "action": recipe["actions"],
        "after": recipe["after"],
    }
    return trust, context, recorder


def test_browser_state_transition_is_recipe_and_source_bound(
    tmp_path: Path, http_server: str,
) -> None:
    trust, context, recorder = _browser_state_trust(tmp_path, http_server)
    evidence = record_evidence(
        _request("state_transition", recorder, context), trust
    )
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"phase": "before", "path": "/state", "op": "eq", "value": "idle"},
        {"phase": "action", "path": "/performed", "op": "eq", "value": "clicked"},
        {"phase": "after", "path": "/state", "op": "eq", "value": "ready"},
    ]), trust)
    assert result["passed"] is True

    changed = copy.deepcopy(recorder)
    changed["action"][0]["selector"] = "#decoy"
    with pytest.raises(TypedEvidenceError, match="verifier-owned recipe"):
        record_evidence(_request("state_transition", changed, context), trust)


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
    trust = _trust(tmp_path, http_server)
    checkout = Path(trust["candidate_checkout_root"])
    artifact = checkout / "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py"
    port = _free_port()
    decoy = subprocess.Popen(
        [sys.executable, str(artifact), "--typed-evidence-http-server", str(port)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    _wait_for_port(decoy, port)
    try:
        command = subprocess.check_output(
            ["/bin/ps", "-p", str(decoy.pid), "-o", "command="], text=True
        ).strip()
        started = subprocess.check_output(
            ["/bin/ps", "-p", str(decoy.pid), "-o", "lstart="], text=True
        ).strip()
        runtime = trust["http_sources"]["fleet-api"]["runtime"]
        runtime["pid_file"] = str(tmp_path / "decoy.pid")
        Path(runtime["pid_file"]).write_text(str(decoy.pid), encoding="utf-8")
        Path(runtime["pid_file"]).chmod(0o600)
        runtime.update({
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "start_time": started,
            "executable": str(Path(command.split()[0]).resolve()),
            "listener_port": port,
        })
        trust["http_sources"]["fleet-api"]["base_url"] = f"http://127.0.0.1:{port}"
        with pytest.raises(TypedEvidenceError, match="working directory"):
            record_evidence(_request("http_response", _http_recorder()), trust)
    finally:
        decoy.terminate()
        decoy.wait(timeout=3)


def test_http_rejects_dirty_checkout_and_unbound_artifact(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    checkout = Path(trust["candidate_checkout_root"])
    unbound = checkout / "tools/aionui-extension/tests/home_acceptance/typed_evidence.py"
    runtime = trust["http_sources"]["fleet-api"]["runtime"]
    runtime.update({
        "artifact_path": str(unbound),
        "artifact_sha256": hashlib.sha256(unbound.read_bytes()).hexdigest(),
    })
    with pytest.raises(TypedEvidenceError, match="executed script"):
        record_evidence(_request("http_response", _http_recorder()), trust)

    trust = _trust(tmp_path, http_server)
    dirty = checkout / "untracked-runtime-input"
    dirty.write_text("decoy", encoding="utf-8")
    try:
        with pytest.raises(TypedEvidenceError, match="not clean"):
            record_evidence(_request("http_response", _http_recorder()), trust)
    finally:
        dirty.unlink()


def test_http_rejects_same_cwd_listener_with_unused_candidate_argument(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    checkout = Path(trust["candidate_checkout_root"])
    artifact = checkout / "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py"
    port = _free_port()
    decoy_code = """
import json,sys
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  body=json.dumps({'board':'sandbox-typed-evidence','candidate':'%s','surface':'fleet','runtime':'fleet-runtime-1','entity':self.headers['X-Pursers-Entity-Id'],'run':self.headers['X-Pursers-Run-Id'],'action':self.headers['X-Pursers-Action-Id'],'ok':True,'count':3}).encode()
  self.send_response(200)
  for n in ('X-Pursers-Observation-Id','X-Pursers-Run-Id','X-Pursers-Action-Id','X-Pursers-Entity-Id'): self.send_header(n,self.headers.get(n,''))
  self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[2])),H).serve_forever()
""" % CANDIDATE
    decoy = subprocess.Popen(
        [sys.executable, "-c", decoy_code, str(artifact), str(port)],
        cwd=checkout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    _wait_for_port(decoy, port)
    try:
        command = subprocess.check_output(
            ["/bin/ps", "-p", str(decoy.pid), "-o", "command="], text=True
        ).strip()
        started = subprocess.check_output(
            ["/bin/ps", "-p", str(decoy.pid), "-o", "lstart="], text=True
        ).strip()
        runtime = trust["http_sources"]["fleet-api"]["runtime"]
        runtime["pid_file"] = str(tmp_path / "same-cwd-decoy.pid")
        Path(runtime["pid_file"]).write_text(str(decoy.pid), encoding="utf-8")
        Path(runtime["pid_file"]).chmod(0o600)
        runtime.update({
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "start_time": started,
            "executable": str(Path(command.split()[0]).resolve()),
            "listener_port": port,
        })
        trust["http_sources"]["fleet-api"]["base_url"] = f"http://127.0.0.1:{port}"
        with pytest.raises(TypedEvidenceError, match="executed script"):
            record_evidence(_request("http_response", _http_recorder()), trust)
    finally:
        decoy.terminate()
        decoy.wait(timeout=3)


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
        "version": PERSONAL_VERSION,
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


def test_parent_cli_replays_authenticated_canonical_conjunct(
    tmp_path: Path, http_server: str,
) -> None:
    receipt_path = tmp_path / "receipt.json"
    _write_receipt(receipt_path)
    context = _context(
        observation_id="personal.role", action_id="read-role",
        entity="seat-3", surface="personal",
    )
    trust = _trust(tmp_path, http_server)
    trust["receipt_sources"] = {
        "personal-receipt": _receipt_source(receipt_path)
    }
    evidence = record_evidence(
        _request(
            "receipt_field",
            {
                "source_id": "personal-receipt",
                "fields": ["/role", "/transport", "/pid"],
            },
            context,
        ),
        trust,
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    trust_path.chmod(0o600)
    conjunct = {
        "kind": "receipt_field",
        "receipt": "personal-receipt",
        "field": "role",
        "expected": "worker",
    }
    completed = subprocess.run(
        [
            str(Path(typed_evidence.__file__).resolve()),
            "evaluate-parent", "--trust", str(trust_path),
        ],
        input=json.dumps({
            "observation_id": context["observation_id"],
            "run_id": context["run_id"],
            "action_id": context["action_id"],
            "entity": context["entity"],
            "causal_index": context["causal_index"],
            "surface_id": context["surface"],
            "board_id": context["board_id"],
            "candidate_commit": context["candidate_commit"],
            "conjunct": conjunct,
            "evidence_path": str(evidence_path),
        }),
        text=True, capture_output=True, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["passed"] is True
    assert result["predicate_sha256"] == typed_evidence._digest(conjunct)
    assert result["evidence_sha256"] == hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()


def test_parent_fielded_state_conjunct_is_exact_and_source_bound(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(action_id="set-busy", causal_index=9)
    recorder = {
        "source_id": "fleet-state",
        "before": {"method": "GET", "path": "/state", "body": None, "select": ["/state"]},
        "action": {
            "method": "POST", "path": "/state/action", "body": {"next": "busy"},
            "select": ["/state", "/accepted", "/action_sha256"],
        },
        "after": {"method": "GET", "path": "/state", "body": None, "select": ["/state"]},
    }
    evidence = record_evidence(
        _request("state_transition", recorder, context), trust
    )
    evidence_path = tmp_path / "fielded-state.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    conjunct = {
        "kind": "state_transition",
        "source_id": "fleet-state",
        "assertions": [
            {"phase": "before", "path": "/state", "op": "eq", "value": "idle"},
            {"phase": "action", "path": "/status", "op": "eq", "value": 202},
            {"phase": "after", "path": "/state", "op": "eq", "value": "busy"},
        ],
    }
    request = {
        "observation_id": context["observation_id"],
        "run_id": context["run_id"],
        "action_id": context["action_id"],
        "entity": context["entity"],
        "causal_index": context["causal_index"],
        "surface_id": context["surface"],
        "board_id": context["board_id"],
        "candidate_commit": context["candidate_commit"],
        "conjunct": conjunct,
        "evidence_path": str(evidence_path),
    }
    result = typed_evidence.evaluate_parent_request(request, trust)
    assert result["passed"] is True
    assert result["predicate_sha256"] == typed_evidence._digest(conjunct)

    wrong_value = json.loads(json.dumps(request))
    wrong_value["conjunct"]["assertions"][2]["value"] = "idle"
    assert typed_evidence.evaluate_parent_request(wrong_value, trust)["passed"] is False

    wrong_source = json.loads(json.dumps(request))
    wrong_source["conjunct"]["source_id"] = "decoy-state"
    with pytest.raises(TypedEvidenceError, match="source does not match"):
        typed_evidence.evaluate_parent_request(wrong_source, trust)

    open_schema = json.loads(json.dumps(request))
    open_schema["conjunct"]["assertions"][0]["unexpected"] = True
    with pytest.raises(TypedEvidenceError, match="fields do not match schema"):
        typed_evidence.evaluate_parent_request(open_schema, trust)


def test_receipt_rejects_decoy_pid(tmp_path: Path, http_server: str) -> None:
    marker = tmp_path / "receipt_process.py"
    marker.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(marker)], cwd=tmp_path)
    try:
        threading.Event().wait(0.05)
        command = subprocess.check_output(
            ["/bin/ps", "-p", str(process.pid), "-o", "command="], text=True
        ).strip()
        live_executable = Path(command.split()[0]).resolve()
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
            "argv0_names": [Path(sys.executable).name, "Python"],
            "executable": str(live_executable),
            "executable_sha256": hashlib.sha256(
                live_executable.read_bytes()
            ).hexdigest(),
            "argv_prefix": [str(marker)], "argv_contains": [],
            "required_arguments": {}, "cwd": str(tmp_path),
            "artifact_path": str(marker),
            "artifact_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            "entrypoint": {
                "kind": "script", "module": "", "path": str(marker),
                "sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                "resolver": "", "resolver_sha256": "",
            },
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
    candidate_checkout = Path(trust["candidate_checkout_root"])
    candidate_source = (
        candidate_checkout
        / "packages/personal/src/pursers_personal/apps_server.py"
    )
    receipt = tmp_path / "personal-runtime.json"
    build_sources = tmp_path / "build-sources"
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    for project_name in ("client", "central", "personal"):
        shutil.copytree(
            candidate_checkout / "packages" / project_name,
            build_sources / project_name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "*.egg-info"),
        )
    build_runtime = tmp_path / "build-venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(build_runtime)],
        check=True, capture_output=True, text=True,
    )
    build_python = build_runtime / "bin/python"
    build_requirements = [
        f"{name}=={version}"
        for name, version in RELEASE_VERSIONS["build_toolchain"].items()
    ]
    subprocess.run(
        [
            "uv", "pip", "install", "--offline", "--python",
            str(build_python), *build_requirements,
        ],
        check=True, capture_output=True, text=True,
    )
    build_environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": RELEASE_VERSIONS["source_date_epoch"],
    }
    for project_name in ("client", "central", "personal"):
        subprocess.run(
            [
                "uv", "build", "--offline", "--wheel", "--no-build-isolation",
                "--python", str(build_python), "--out-dir", str(wheel_dir),
                str(build_sources / project_name),
            ],
            check=True, capture_output=True, text=True, env=build_environment,
        )
    runtime = tmp_path / "personal-venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(runtime)],
        check=True, capture_output=True, text=True,
    )
    runtime_python = runtime / "bin/python"
    subprocess.run(
        [
            "uv", "pip", "install", "--offline", "--python",
            str(runtime_python),
            *[str(path) for path in sorted(wheel_dir.glob("*.whl"))],
        ],
        check=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    site_packages = Path(subprocess.check_output(
        [
            str(runtime_python), "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        text=True,
    ).strip())
    personal_package = site_packages / "pursers_personal"
    shutil.rmtree(personal_package)
    personal_package.symlink_to(
        candidate_checkout / "packages/personal/src/pursers_personal",
        target_is_directory=True,
    )
    project = tmp_path / "sandbox-personal"
    project.mkdir()
    profiles = tmp_path / "personal-profiles"
    profile_path = subprocess.check_output(
        [
            str(runtime_python), "-c",
            (
                "import pathlib,sys; from pursers_client import ensure_personal_profile; "
                "p=ensure_personal_profile(pathlib.Path(sys.argv[1]), "
                "profiles_root=pathlib.Path(sys.argv[2]), port=18767); "
                "print(p.profile_path)"
            ),
            str(project), str(profiles),
        ],
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    ).strip()
    profile_board = json.loads(Path(profile_path).read_text(encoding="utf-8"))[
        "board_id"
    ]
    challenge_key = tmp_path / "acceptance-challenge.key"
    challenge_key.write_bytes(b"\x11" * 32)
    challenge_key.chmod(0o600)
    command = [
        str(runtime_python), "-I", "-m", "pursers_personal.cli", "mcp",
        "--profile", profile_path, "--host-id", "pytest",
        "--session", "typed-evidence", "--acceptance-runtime-receipt",
        str(receipt), "--acceptance-challenge-key", str(challenge_key),
        "--candidate-source", str(candidate_source),
        "--candidate-commit", CANDIDATE, "--board-id", profile_board,
    ]
    process = subprocess.Popen(
        command, cwd=candidate_checkout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(500):
            if receipt.exists():
                break
            threading.Event().wait(0.01)
        if not receipt.exists():
            process.terminate()
            _stdout, stderr = process.communicate(timeout=3)
            raise AssertionError(f"Personal MCP did not write receipt: {stderr[-1000:]}")
        live_command = subprocess.check_output(
            ["/bin/ps", "-p", str(process.pid), "-o", "command="], text=True
        ).strip()
        live_executable = Path(live_command.split()[0]).resolve()
        pid_file = tmp_path / "personal-runtime.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        pid_file.chmod(0o600)
        process_trust = {
            "pid_file": str(pid_file),
            "argv0_names": [
                Path(sys.executable).name, runtime_python.name,
                runtime_python.resolve().name, "Python",
            ],
            "executable": str(live_executable),
            "executable_sha256": hashlib.sha256(
                live_executable.read_bytes()
            ).hexdigest(),
            "argv_prefix": ["-I", "-m", "pursers_personal.cli", "mcp"],
            "argv_contains": [],
            "required_arguments": {
                "--profile": profile_path,
                "--host-id": "pytest",
                "--session": "typed-evidence",
                "--acceptance-runtime-receipt": str(receipt),
                "--acceptance-challenge-key": str(challenge_key),
                "--candidate-source": str(candidate_source),
                "--candidate-commit": CANDIDATE,
                "--board-id": profile_board,
            },
            "cwd": str(candidate_checkout),
            "artifact_path": str(candidate_source),
            "artifact_sha256": hashlib.sha256(candidate_source.read_bytes()).hexdigest(),
            "entrypoint": {
                "kind": "isolated_module", "module": "pursers_personal.cli",
                "path": str(candidate_source.with_name("cli.py")),
                "sha256": hashlib.sha256(
                    candidate_source.with_name("cli.py").read_bytes()
                ).hexdigest(),
                "resolver": str(runtime_python),
                "resolver_sha256": hashlib.sha256(
                    runtime_python.resolve().read_bytes()
                ).hexdigest(),
            },
            "receipt_pid_pointer": "/pid",
        }
        trust["board_id"] = profile_board
        trust["receipt_sources"] = {
            "personal-runtime": _personal_receipt_source(
                receipt, candidate_source, process_trust
            )
        }
        context = _context(
            observation_id="personal.runtime", action_id="capture-runtime",
            entity="personal-mcp", surface="personal", board_id=profile_board,
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

        mcp_receipt = tmp_path / "personal-tool-runtime.json"
        mcp_args = list(command[1:])
        receipt_index = mcp_args.index("--acceptance-runtime-receipt") + 1
        mcp_args[receipt_index] = str(mcp_receipt)
        executable = runtime_python.resolve()
        mcp_source = {
            "adapter": "trusted_mcp_stdio_v1",
            "provenance": "personal-live-stdio",
            "runtime_id": "personal-mcp-tool-runtime-1",
            "surface": "personal",
            "board_id": profile_board,
            "candidate_commit": CANDIDATE,
            "command": str(runtime_python),
            "command_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "args": mcp_args,
            "env": {
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "cwd": str(candidate_checkout),
            "candidate_source": str(candidate_source),
            "candidate_source_sha256": hashlib.sha256(
                candidate_source.read_bytes()
            ).hexdigest(),
            "challenge_key": str(challenge_key),
            "tool": "board_snapshot",
            "arguments": {},
            "select_allowlist": ["/connected", "/tickets"],
            "timeout_seconds": 8,
        }
        trust["mcp_sources"] = {"personal-board-snapshot": mcp_source}
        mcp_context = _context(
            observation_id="personal-mcp.state.board-empty",
            action_id="board-snapshot",
            entity="personal-board",
            surface="personal",
            board_id=profile_board,
        )
        mcp_recorder = {
            "source_id": "personal-board-snapshot",
            "tool": "board_snapshot",
            "arguments": {},
            "select": ["/connected", "/tickets"],
        }
        mcp_evidence = record_evidence(
            _request("mcp_tool_response", mcp_recorder, mcp_context), trust
        )
        selected = mcp_evidence["record"]["result"]["selected"]
        assert set(selected) == {"/connected", "/tickets"}
        assert isinstance(selected["/connected"], bool)
        assert isinstance(selected["/tickets"], list)
        assert mcp_evidence["record"]["transport"] == "stdio"
        assert evaluate_evidence(
            mcp_evidence,
            _expected(mcp_evidence, [
                {"path": "/connected", "op": "eq", "value": selected["/connected"]},
                {"path": "/tickets", "op": "eq", "value": selected["/tickets"]},
            ]),
            trust,
        )["passed"] is True

        wrong_target = {**mcp_context, "board_id": "sandbox-decoy"}
        with pytest.raises(TypedEvidenceError, match="board does not match"):
            record_evidence(
                _request("mcp_tool_response", mcp_recorder, wrong_target), trust
            )
        wrong_tool = {**mcp_recorder, "tool": "fleet_snapshot"}
        with pytest.raises(TypedEvidenceError, match="tool recipe"):
            record_evidence(
                _request("mcp_tool_response", wrong_tool, mcp_context), trust
            )
        wrong_process = copy.deepcopy(mcp_evidence)
        wrong_process["record"]["process"]["pid"] += 1
        _resign_evidence(wrong_process)
        with pytest.raises(TypedEvidenceError, match="result binding changed"):
            evaluate_evidence(
                wrong_process,
                _expected(wrong_process, [
                    {"path": "/connected", "op": "eq", "value": selected["/connected"]},
                ]),
                trust,
            )

        original = json.loads(receipt.read_text())
        for field, value in (("schema_version", 999), ("version", "forged-version")):
            forged = {**original, field: value}
            receipt.write_text(json.dumps(forged), encoding="utf-8")
            receipt.chmod(0o600)
            with pytest.raises(TypedEvidenceError, match="binding mismatch"):
                record_evidence(
                    _request(
                        "receipt_field",
                        {"source_id": "personal-runtime", "fields": ["/product"]},
                        context,
                    ),
                    trust,
                )
        receipt.write_text(json.dumps(original), encoding="utf-8")
        receipt.chmod(0o600)
        personal_package.unlink()
        personal_package.mkdir()
        (personal_package / "__init__.py").write_text("", encoding="utf-8")
        (personal_package / "cli.py").write_text(
            "import time; time.sleep(30)\n", encoding="utf-8"
        )
        shadow = subprocess.Popen(
            command, cwd=candidate_checkout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            shadow_receipt = {**original, "pid": shadow.pid}
            receipt.write_text(json.dumps(shadow_receipt), encoding="utf-8")
            receipt.chmod(0o600)
            pid_file.write_text(str(shadow.pid), encoding="utf-8")
            with pytest.raises(TypedEvidenceError, match="module resolution"):
                record_evidence(
                    _request(
                        "receipt_field",
                        {"source_id": "personal-runtime", "fields": ["/product"]},
                        context,
                    ),
                    trust,
                )
        finally:
            shadow.terminate()
            shadow.wait(timeout=3)
        shutil.rmtree(personal_package)
        personal_package.symlink_to(
            candidate_checkout / "packages/personal/src/pursers_personal",
            target_is_directory=True,
        )
        receipt.write_text(json.dumps(original), encoding="utf-8")
        receipt.chmod(0o600)
        arbitrary = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path,
        )
        try:
            forged = {**original, "pid": arbitrary.pid}
            receipt.write_text(json.dumps(forged), encoding="utf-8")
            receipt.chmod(0o600)
            pid_file.write_text(str(arbitrary.pid), encoding="utf-8")
            with pytest.raises(TypedEvidenceError, match="process identity"):
                record_evidence(
                    _request(
                        "receipt_field",
                        {"source_id": "personal-runtime", "fields": ["/product"]},
                        context,
                    ),
                    trust,
                )
        finally:
            arbitrary.terminate()
            arbitrary.wait(timeout=3)
    finally:
        process.terminate()
        process.wait(timeout=3)


@pytest.fixture
def log_emitter(tmp_path: Path, request: pytest.FixtureRequest) -> Any:
    processes: list[subprocess.Popen[str]] = []

    def start(trust: dict[str, Any], **changes: Any) -> tuple[Path, dict[str, Any]]:
        index = len(processes)
        action = {
            "emitter": "central-runtime", "candidate_commit": CANDIDATE,
            "board_id": BOARD, "surface": "fleet", "entity": "TK-123",
            "run_id": "run-1", "action_id": "fixture-signal",
            "event": "fixture_process_emitted", "outcome": "observed",
            "runtime_id": "central-runtime-1",
        }
        action.update(changes)
        action_path = tmp_path / f"action-{index}.json"
        action_path.write_text(json.dumps(action), encoding="utf-8")
        action_path.chmod(0o600)
        log_path = tmp_path / f"emitter-{index}.jsonl"
        checkout = Path(trust["candidate_checkout_root"])
        artifact = checkout / "tools/aionui-extension/tests/home_acceptance/test_typed_evidence.py"
        process = subprocess.Popen(
            [
                sys.executable, str(artifact), "--typed-evidence-log-emitter",
                "--action-input", str(action_path), "--output", str(log_path),
            ],
            cwd=checkout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        processes.append(process)
        for _ in range(200):
            if log_path.exists():
                break
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                raise AssertionError(f"emitter exited: {stderr[-1000:]}")
            threading.Event().wait(0.01)
        assert log_path.exists()
        live_command = subprocess.check_output(
            ["/bin/ps", "-p", str(process.pid), "-o", "command="], text=True
        ).strip()
        live_executable = Path(live_command.split()[0]).resolve()
        pid_file = tmp_path / f"emitter-{index}.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        pid_file.chmod(0o600)
        process_trust = {
            "pid_file": str(pid_file),
            "argv0_names": [Path(sys.executable).name, "Python"],
            "executable": str(live_executable),
            "executable_sha256": hashlib.sha256(
                live_executable.read_bytes()
            ).hexdigest(),
            "argv_prefix": [str(artifact), "--typed-evidence-log-emitter"],
            "argv_contains": [],
            "required_arguments": {
                "--action-input": str(action_path), "--output": str(log_path),
            },
            "cwd": str(checkout), "artifact_path": str(artifact),
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "entrypoint": {
                "kind": "script", "module": "", "path": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "resolver": "", "resolver_sha256": "",
            },
            "receipt_pid_pointer": "/pid",
        }
        source = {
            "adapter": "process_captured_jsonl_v1",
            "provenance": "verifier-captured-pinned-emitter",
            "runtime_id": "central-runtime-1", "path": str(log_path),
            "document_keys": [
                "emitter", "timestamp", "candidate_commit", "board_id",
                "surface", "entity", "run_id", "action_id", "event",
                "outcome", "runtime_id", "action_sha256",
            ],
            "timestamp_pointer": "/timestamp", "max_age_seconds": 300,
            "required_bindings": {
                "/candidate_commit": "$candidate_commit",
                "/board_id": "$board_id", "/surface": "$surface",
                "/entity": "$entity", "/run_id": "$run_id",
                "/action_id": "$action_id",
            },
            "emitter": "central-runtime", "runtime_pointer": "/runtime_id",
            "max_bytes": 65_536, "process": process_trust,
            "action_input_path": str(action_path),
            "action_input_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
            "action_digest_pointer": "/action_sha256",
        }
        return log_path, source

    def cleanup() -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)

    request.addfinalizer(cleanup)
    return start


def _fleet_trace_source(
    tmp_path: Path,
    trust: dict[str, Any],
    context: dict[str, Any],
    *,
    action: dict[str, Any] | None = None,
    action_route: str = "/api/attention",
    entry_changes: dict[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    action_path = tmp_path / "fleet-action.json"
    action_path.write_bytes(
        _json_bytes(action if action is not None else {"attention": []})
    )
    action_path.chmod(0o600)
    action_sha256 = hashlib.sha256(action_path.read_bytes()).hexdigest()
    action_document = json.loads(action_path.read_bytes())
    runtime = trust["http_sources"]["fleet-api"]["runtime"]
    entry = {
        "schema_version": 1,
        "emitter": "fleet-dashboard-runtime",
        "timestamp": _now(),
        "runtime_id": "fleet-runtime-1",
        "candidate_commit": context["candidate_commit"],
        "board_id": context["board_id"],
        "surface": context["surface"],
        "observation_id": context["observation_id"],
        "run_id": context["run_id"],
        "action_id": context["action_id"],
        "entity": context["entity"],
        "method": "POST",
        "path": action_route,
        "status": 200,
        "outcome": "succeeded",
        "effect": (
            "attention_state_changed"
            if action_route == "/api/attention"
            else "project_state_changed"
        ),
        "changed": True,
        "before_sha256": typed_evidence._digest({}),
        "after_sha256": typed_evidence._digest(action_document),
        "result_sha256": hashlib.sha256(
            _json_bytes({"items": action_document})
        ).hexdigest(),
        "action_sha256": action_sha256,
        "pid": _HTTP_RUNTIMES[trust["http_sources"]["fleet-api"]["base_url"]].pid,
        "entrypoint_sha256": runtime["artifact_sha256"],
    }
    entry.update(entry_changes or {})
    log_path = tmp_path / "fleet-evidence.jsonl"
    log_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    log_path.chmod(0o600)
    source = {
        "adapter": "fleet_evidence_trace_v1",
        "provenance": "fleet-runtime-evidence-trace",
        "runtime_id": "fleet-runtime-1",
        "path": str(log_path),
        "document_keys": list(entry),
        "timestamp_pointer": "/timestamp",
        "max_age_seconds": 300,
        "required_bindings": {
            "/candidate_commit": "$candidate_commit",
            "/board_id": "$board_id",
            "/surface": "$surface",
            "/observation_id": "$observation_id",
            "/entity": "$entity",
            "/run_id": "$run_id",
            "/action_id": "$action_id",
        },
        "emitter": "fleet-dashboard-runtime",
        "runtime_pointer": "/runtime_id",
        "max_bytes": 65_536,
        "action_input_path": str(action_path),
        "action_input_sha256": action_sha256,
        "action_digest_pointer": "/action_sha256",
        "action_path": action_route,
        "http_source_id": "fleet-api",
        "http_source_config_sha256": typed_evidence._digest(
            trust["http_sources"]["fleet-api"]
        ),
        "schema_version_pointer": "/schema_version",
        "pid_pointer": "/pid",
        "entrypoint_digest_pointer": "/entrypoint_sha256",
        "status_pointer": "/status",
        "changed_pointer": "/changed",
        "outcome_pointer": "/outcome",
        "effect_pointer": "/effect",
        "sha256_pointers": [
            "/before_sha256", "/after_sha256", "/result_sha256",
            "/action_sha256", "/entrypoint_sha256",
        ],
    }
    return action_path, log_path, source


def test_fleet_trace_rejects_caller_owned_result_fields(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(
        observation_id="fleet.attention",
        action_id="save-attention",
        entity="fleet-attention",
    )
    _action_path, _log_path, source = _fleet_trace_source(
        tmp_path,
        trust,
        context,
        action={"attention": [], "outcome": "accepted"},
    )
    trust["log_sources"] = {"fleet-trace": source}
    with pytest.raises(TypedEvidenceError, match="producer-owned"):
        record_evidence(
            _request(
                "log_assertion",
                {"source_id": "fleet-trace", "field_equals": {"/outcome": "succeeded"}},
                context,
            ),
            trust,
        )


def test_fleet_trace_rejects_noncanonical_action_bytes(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(
        observation_id="fleet.attention", action_id="save-attention",
        entity="fleet-attention",
    )
    action_path, _log_path, source = _fleet_trace_source(
        tmp_path, trust, context
    )
    action_path.write_text('{ "attention": [] }\n', encoding="utf-8")
    source["action_input_sha256"] = hashlib.sha256(
        action_path.read_bytes()
    ).hexdigest()
    trust["log_sources"] = {"fleet-trace": source}
    with pytest.raises(TypedEvidenceError, match="non-canonical"):
        record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "fleet-trace",
                    "field_equals": {"/outcome": "succeeded"},
                },
                context,
            ),
            trust,
        )


@pytest.mark.parametrize(
    "entry_changes",
    [
        {"pid": 999_999},
        {"entrypoint_sha256": "66" * 32},
        {"observation_id": "fleet.decoy"},
        {"status": True},
        {"changed": 1},
        {"outcome": "failed"},
        {"effect": "attention_state_unchanged"},
        {"after_sha256": "33" * 32},
        {"result_sha256": "55" * 32},
        {"method": "GET"},
        {"path": "/api/decoy"},
    ],
)
def test_fleet_trace_rejects_forged_runtime_correlation_and_types(
    tmp_path: Path, http_server: str, entry_changes: dict[str, Any],
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(
        observation_id="fleet.attention",
        action_id="save-attention",
        entity="fleet-attention",
    )
    _action_path, _log_path, source = _fleet_trace_source(
        tmp_path, trust, context, entry_changes=entry_changes
    )
    trust["log_sources"] = {"fleet-trace": source}
    with pytest.raises(TypedEvidenceError, match="append"):
        record_evidence(
            _request(
                "log_assertion",
                {"source_id": "fleet-trace", "field_equals": {"/outcome": "succeeded"}},
                context,
            ),
            trust,
        )


def test_fleet_trace_rejects_changed_http_runtime_source(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(
        observation_id="fleet.attention",
        action_id="save-attention",
        entity="fleet-attention",
    )
    _action_path, _log_path, source = _fleet_trace_source(
        tmp_path, trust, context
    )
    source["http_source_config_sha256"] = "77" * 32
    trust["log_sources"] = {"fleet-trace": source}
    with pytest.raises(TypedEvidenceError, match="trusted HTTP runtime"):
        record_evidence(
            _request(
                "log_assertion",
                {"source_id": "fleet-trace", "field_equals": {"/outcome": "succeeded"}},
                context,
            ),
            trust,
        )


def test_fleet_trace_rejects_weakened_schema_and_pointer_contract(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(
        observation_id="fleet.attention",
        action_id="save-attention",
        entity="fleet-attention",
    )
    _action_path, _log_path, source = _fleet_trace_source(
        tmp_path, trust, context
    )
    source["document_keys"].remove("effect")
    trust["log_sources"] = {"fleet-trace": source}
    with pytest.raises(TypedEvidenceError, match="document schema"):
        record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "fleet-trace",
                    "field_equals": {"/outcome": "succeeded"},
                },
                context,
            ),
            trust,
        )

    _action_path, _log_path, source = _fleet_trace_source(
        tmp_path, trust, context
    )
    source["status_pointer"] = "/changed"
    trust["log_sources"] = {"fleet-trace": source}
    with pytest.raises(TypedEvidenceError, match="pointer contract"):
        record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "fleet-trace",
                    "field_equals": {"/outcome": "succeeded"},
                },
                context,
            ),
            trust,
        )


def test_fleet_project_add_route_has_closed_effect_and_result_contract(
    tmp_path: Path, http_server: str,
) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(
        observation_id="fleet.project-add",
        action_id="add-project",
        entity="project:new-board",
    )
    project_selectors = typed_evidence.FLEET_ACTION_RESPONSE_POINTERS[
        "/api/projects/add"
    ]
    assert "/steps" in project_selectors
    assert "/items" not in project_selectors
    assert "/doors" not in project_selectors
    trust["http_sources"]["fleet-api"]["select_allowlist"] = sorted(
        project_selectors
    )
    _action_path, _log_path, source = _fleet_trace_source(
        tmp_path,
        trust,
        context,
        action={
            "name": "new-svc",
            "board_id": "new-board",
            "work_dir": "/PATH/TO/work",
            "integration_ref": "main",
        },
        action_route="/api/projects/add",
    )
    entry = json.loads(_log_path.read_text(encoding="utf-8"))
    runtime = typed_evidence._runtime_check(
        trust["http_sources"]["fleet-api"]["runtime"],
        trust,
        trust["http_sources"]["fleet-api"]["base_url"],
    )
    assert typed_evidence._fleet_source_contract(source)
    typed_evidence._validate_fleet_entry(entry, source, runtime, context)

    wrong_effect = {**entry, "effect": "attention_state_changed"}
    with pytest.raises(TypedEvidenceError, match="outcome is inconsistent"):
        typed_evidence._validate_fleet_entry(
            wrong_effect, source, runtime, context
        )

    decoy_source = {**source, "action_path": "/api/projects/decoy"}
    with pytest.raises(TypedEvidenceError, match="action path is not allowlisted"):
        typed_evidence._fleet_source_contract(decoy_source)


def test_fleet_trace_real_product_roundtrip(tmp_path: Path) -> None:
    checkout_value = os.environ.get("PURSERS_FLEET_EVIDENCE_CHECKOUT")
    if not checkout_value:
        pytest.skip("set PURSERS_FLEET_EVIDENCE_CHECKOUT to a reviewed producer checkout")
    checkout = Path(checkout_value).resolve()
    candidate = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    assert not subprocess.check_output(
        ["git", "-C", str(checkout), "status", "--porcelain"], text=True
    )
    artifact = checkout / "tools/fleet-dashboard/fleet_dashboard.py"
    assert artifact.is_file()

    token_path = tmp_path / "central.token"
    token_path.write_text("test-token", encoding="utf-8")
    token_path.chmod(0o600)
    trace_path = tmp_path / "fleet-evidence.jsonl"
    trace_config_path = tmp_path / "trace.json"
    trace_config_path.write_text(
        json.dumps({
            "schema_version": 1,
            "output_path": str(trace_path),
            "max_bytes": 65_536,
            "runtime_id": "fleet-runtime-1",
            "board_id": BOARD,
            "surface": "fleet",
        }),
        encoding="utf-8",
    )
    trace_config_path.chmod(0o600)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable, str(artifact), "--port", str(port),
            "--url", "http://127.0.0.1:1", "--token-file", str(token_path),
            "--seat-state-dir", str(state_dir),
            "--evidence-trace-config", str(trace_config_path),
        ],
        cwd=checkout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(process, port)
        base_url = f"http://127.0.0.1:{port}"
        pid_path = tmp_path / "fleet.pid"
        pid_path.write_text(str(process.pid), encoding="utf-8")
        pid_path.chmod(0o600)
        command = subprocess.check_output(
            ["/bin/ps", "-p", str(process.pid), "-o", "command="], text=True
        ).strip()
        start_time = subprocess.check_output(
            ["/bin/ps", "-p", str(process.pid), "-o", "lstart="], text=True
        ).strip()
        runtime = {
            "pid_file": str(pid_path),
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "start_time": start_time,
            "executable": str(Path(command.split()[0]).resolve()),
            "cwd": str(checkout),
            "artifact_path": str(artifact),
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "listener_port": port,
        }
        response_bindings = {
            "/_evidence/candidate_commit": "$candidate_commit",
            "/_evidence/board_id": "$board_id",
            "/_evidence/surface": "$surface",
            "/_evidence/entity": "$entity",
            "/_evidence/run_id": "$run_id",
            "/_evidence/action_id": "$action_id",
            "/_evidence/runtime_id": "fleet-runtime-1",
        }
        http_source = {
            "adapter": "trusted_http_v1",
            "provenance": "reviewed-fleet-disposable-service",
            "runtime_id": "fleet-runtime-1",
            "base_url": base_url,
            "surface": "fleet",
            "board_id": BOARD,
            "candidate_commit": candidate,
            "methods": ["POST"],
            "headers": {},
            "timeout_seconds": 2,
            "select_allowlist": sorted(
                typed_evidence.FLEET_ACTION_RESPONSE_POINTERS["/api/attention"]
            ),
            "response_bindings": response_bindings,
            "runtime": runtime,
        }
        action_path = tmp_path / "action.json"
        action = {"TK-123": {"state": "ack", "note": "real-product-roundtrip"}}
        action_path.write_bytes(_json_bytes(action))
        action_path.chmod(0o600)
        log_source = {
            "adapter": "fleet_evidence_trace_v1",
            "provenance": "fleet-runtime-evidence-trace",
            "runtime_id": "fleet-runtime-1",
            "path": str(trace_path),
            "document_keys": sorted(typed_evidence.FLEET_TRACE_KEYS),
            "timestamp_pointer": "/timestamp",
            "max_age_seconds": 300,
            "required_bindings": {
                "/candidate_commit": "$candidate_commit",
                "/board_id": "$board_id",
                "/surface": "$surface",
                "/observation_id": "$observation_id",
                "/entity": "$entity",
                "/run_id": "$run_id",
                "/action_id": "$action_id",
            },
            "emitter": "fleet-dashboard-runtime",
            "runtime_pointer": "/runtime_id",
            "max_bytes": 65_536,
            "action_input_path": str(action_path),
            "action_input_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
            "action_digest_pointer": "/action_sha256",
            "action_path": "/api/attention",
            "http_source_id": "fleet-api",
            "http_source_config_sha256": typed_evidence._digest(http_source),
            "schema_version_pointer": "/schema_version",
            "pid_pointer": "/pid",
            "entrypoint_digest_pointer": "/entrypoint_sha256",
            "status_pointer": "/status",
            "changed_pointer": "/changed",
            "outcome_pointer": "/outcome",
            "effect_pointer": "/effect",
            "sha256_pointers": [
                "/before_sha256", "/after_sha256", "/result_sha256",
                "/action_sha256", "/entrypoint_sha256",
            ],
        }
        trust = {
            "schema_version": 1,
            "verifier_id": "purser-reviewer-2",
            "trusted_module_path": str(Path(typed_evidence.__file__).resolve()),
            "module_sha256": typed_evidence._module_digest(),
            "candidate_checkout_root": str(checkout),
            "candidate_commit": candidate,
            "board_id": BOARD,
            "max_age_seconds": 300,
            "active_evidence_key": "test-key",
            "evidence_keys": {"test-key": EVIDENCE_KEY},
            "http_sources": {"fleet-api": http_source},
            "mcp_sources": {},
            "receipt_sources": {},
            "log_sources": {"fleet-trace": log_source},
            "state_sources": {},
            "replay_guard": {"path": str(tmp_path / "replay.log"), "consume": False},
        }
        context = _context(
            observation_id="fleet.attention-state", action_id="save-attention",
            entity="TK-123", candidate_commit=candidate,
        )
        rejected_request = Request(
            base_url + "/api/attention",
            data=_json_bytes(action),
            headers={
                "Content-Type": "application/json",
                "Host": "evil.example",
                "X-Pursers-Observation-Id": context["observation_id"],
                "X-Pursers-Run-Id": context["run_id"],
                "X-Pursers-Action-Id": "rejected-before-operation",
                "X-Pursers-Entity-Id": context["entity"],
                "X-Pursers-Action-SHA256": "0" * 64,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as rejected:
            build_opener(ProxyHandler({})).open(rejected_request, timeout=2)
        assert rejected.value.code == 403
        assert "_evidence" not in json.loads(rejected.value.read())
        assert rejected.value.headers.get("X-Pursers-Run-Id") is None
        assert not trace_path.exists()
        assert not (state_dir / "attention-state.json").exists()

        evidence = record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "fleet-trace",
                    "field_equals": {
                        "/outcome": "succeeded",
                        "/effect": "attention_state_changed",
                    },
                },
                context,
            ),
            trust,
        )
        result = evaluate_evidence(
            evidence,
            _expected(evidence, [
                {"path": "/outcome", "op": "eq", "value": "succeeded"},
                {
                    "path": "/effect", "op": "eq",
                    "value": "attention_state_changed",
                },
            ]),
            trust,
        )
        assert result["passed"]
        assert evidence["record"]["runtime"]["pid"] == process.pid
        assert evidence["record"]["action_response"]["status"] == 200
        assert json.loads(
            (state_dir / "attention-state.json").read_text(encoding="utf-8")
        ) == action
        forged = copy.deepcopy(evidence)
        forged["record"]["entry"]["outcome"] = "failed"
        forged["record"]["action_response"]["selected"][
            "/_evidence/outcome"
        ] = "failed"
        _resign_evidence(forged)
        with pytest.raises(TypedEvidenceError, match="outcome is inconsistent"):
            evaluate_evidence(
                forged,
                _expected(forged, [
                    {"path": "/outcome", "op": "eq", "value": "failed"},
                ]),
                trust,
            )
        with pytest.raises(TypedEvidenceError, match="JSON pointer is absent"):
            record_evidence(
                _request(
                    "log_assertion",
                    {
                        "source_id": "fleet-trace",
                        "field_equals": {"/outcome": "succeeded"},
                    },
                    context,
                ),
                trust,
            )
        assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_log_process_binding_and_substitution_unit(
    tmp_path: Path, http_server: str, log_emitter: Any,
) -> None:
    trust = _trust(tmp_path, http_server)
    path, source = log_emitter(trust)
    trust["log_sources"] = {"central-log": source}
    context = _context(action_id="fixture-signal")
    request = _request("log_assertion", {
        "source_id": "central-log",
        "field_equals": {"/event": "fixture_process_emitted"},
    }, context)
    evidence = record_evidence(request, trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"path": "/event", "op": "eq", "value": "fixture_process_emitted"},
        {"path": "/outcome", "op": "eq", "value": "observed"},
    ]), trust)
    assert result["passed"]
    assert evidence["record"]["authenticity"] == "verifier_captured_process_bound"

    substituted = {**evidence["record"]["entry"], "emitter": "decoy"}
    path.write_text(json.dumps(substituted) + "\n", encoding="utf-8")
    with pytest.raises(TypedEvidenceError, match="exactly one"):
        record_evidence(request, trust)

    stale = {
        **evidence["record"]["entry"],
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    with pytest.raises(TypedEvidenceError, match="exactly one"):
        record_evidence(request, trust)


def test_log_rejects_unsigned_empty_container(
    tmp_path: Path, http_server: str, log_emitter: Any,
) -> None:
    trust = _trust(tmp_path, http_server)
    path, source = log_emitter(trust)
    entry = json.loads(path.read_text())
    entry["unsigned"] = []
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    path.chmod(0o600)
    trust["log_sources"] = {"central-log": source}
    request = _request(
        "log_assertion",
        {
            "source_id": "central-log",
            "field_equals": {"/event": "fixture_process_emitted"},
        },
        _context(action_id="fixture-signal"),
    )
    with pytest.raises(TypedEvidenceError, match="exactly one"):
        record_evidence(request, trust)


def test_hardcoded_signed_success_adapter_is_rejected(
    tmp_path: Path, http_server: str, log_emitter: Any,
) -> None:
    trust = _trust(tmp_path, http_server)
    _path, source = log_emitter(trust)
    source["adapter"] = "hmac_jsonl_v1"
    trust["log_sources"] = {"central-log": source}
    with pytest.raises(TypedEvidenceError, match="adapter"):
        record_evidence(
            _request(
                "log_assertion",
                {
                    "source_id": "central-log",
                    "field_equals": {"/outcome": "observed"},
                },
                _context(action_id="fixture-signal"),
            ),
            trust,
        )

def _state_recorder(next_state: str) -> dict[str, Any]:
    return {
        "source_id": "fleet-state",
        "before": {"method": "GET", "path": "/state", "body": None, "select": ["/state"]},
        "action": {
            "method": "POST", "path": "/state/action",
            "body": {"next": next_state},
            "select": ["/state", "/accepted", "/action_sha256"],
        },
        "after": {"method": "GET", "path": "/state", "body": None, "select": ["/state"]},
    }


def test_state_transition_real_roundtrip_and_negative_action(tmp_path: Path, http_server: str) -> None:
    trust = _trust(tmp_path, http_server)
    context = _context(action_id="set-busy", causal_index=9)
    evidence = record_evidence(_request("state_transition", _state_recorder("busy"), context), trust)
    result = evaluate_evidence(evidence, _expected(evidence, [
        {"phase": "before", "path": "/state", "op": "eq", "value": "idle"},
        {"phase": "action", "path": "/status", "op": "eq", "value": 202},
        {
            "phase": "action", "path": "/action_sha256", "op": "eq",
            "value": hashlib.sha256(_json_bytes({"next": "busy"})).hexdigest(),
        },
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
    tmp_path: Path, http_server: str, log_emitter: Any,
) -> None:
    trust = _trust(tmp_path, http_server)
    receipt_path = tmp_path / "receipt.json"
    _write_receipt(receipt_path)
    trust["receipt_sources"] = {
        "personal-receipt": _receipt_source(receipt_path)
    }
    _log_path, log_source = log_emitter(trust)
    trust["log_sources"] = {"central-log": log_source}
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
                    "field_equals": {"/event": "fixture_process_emitted"},
                },
                _context(action_id="fixture-signal"),
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
                    else [{
                        "path": "/event", "op": "eq",
                        "value": "fixture_process_emitted",
                    }]
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


def _run_disposable_entrypoint() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--typed-evidence-http-server":
        _Handler.state = "idle"
        ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), _Handler).serve_forever()
        return
    if len(sys.argv) == 6 and sys.argv[1] == "--typed-evidence-log-emitter":
        if sys.argv[2] != "--action-input" or sys.argv[4] != "--output":
            raise SystemExit(2)
        action_path = Path(sys.argv[3]).resolve(strict=True)
        output_path = Path(sys.argv[5]).resolve()
        action_raw = action_path.read_bytes()
        action = json.loads(action_raw)
        required = {
            "emitter", "candidate_commit", "board_id", "surface", "entity",
            "run_id", "action_id", "event", "outcome", "runtime_id",
        }
        if not isinstance(action, dict) or set(action) != required:
            raise SystemExit(2)
        entry = {
            **action,
            "timestamp": _now(),
            "action_sha256": hashlib.sha256(action_raw).hexdigest(),
        }
        output_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        output_path.chmod(0o600)
        threading.Event().wait(60)
        return
    raise SystemExit(2)


if __name__ == "__main__":  # pragma: no cover - exercised as bound subprocesses
    _run_disposable_entrypoint()
