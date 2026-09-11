from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_dashboard_evidence_test", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


class Cache:
    def labels(self) -> list[str]:
        return ["default"]


def _trace(
    tmp_path: Path, *, initial_output: bytes = b"", max_bytes: int = 65_536
) -> tuple[Any, Path]:
    output = tmp_path / "fleet-evidence.jsonl"
    if initial_output:
        output.write_bytes(initial_output)
        output.chmod(0o600)
    config = tmp_path / "trace.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "output_path": str(output),
                "max_bytes": max_bytes,
                "runtime_id": "fleet-runtime-test",
                "board_id": "sandbox-board",
                "surface": "fleet",
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return dashboard.EvidenceTrace.from_config(config, MODULE_PATH), output


def _headers(
    action: bytes | None = None,
    *,
    observation_id: str = "fleet.attention-state",
    run_id: str = "run-1",
    action_id: str = "save-attention",
    entity: str = "TK-123",
) -> dict[str, str]:
    result = {
        "X-Pursers-Observation-Id": observation_id,
        "X-Pursers-Run-Id": run_id,
        "X-Pursers-Action-Id": action_id,
        "X-Pursers-Entity-Id": entity,
    }
    if action is not None:
        result["X-Pursers-Action-SHA256"] = hashlib.sha256(action).hexdigest()
        result["Content-Type"] = "application/json"
    return result


def _call(
    base_url: str,
    method: str,
    *,
    path: str = "/api/attention",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], Any]:
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read()), response.headers


class ProjectBoard:
    def __init__(self, board_id: str, central: "ProjectCentral") -> None:
        self.board_id = board_id
        self.central = central
        self.memberships: dict[str, str] = {}
        self.dispatch_policy = {
            "offer_ttl_s": 120,
            "broadcast_reoffer_s": 600,
            "second_opinion": False,
            "fallback_broadcast": False,
        }
        self.review_policy = "workflow"

    async def __aenter__(self) -> "ProjectBoard":
        self.central.boards_present.add(self.board_id)
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def board_list(self) -> dict[str, Any]:
        return {
            "boards": [
                {"board_id": board_id, "membership_role": "admin"}
                for board_id in sorted(self.central.boards_present)
            ]
        }

    async def board_onboard(self, **_kwargs: object) -> dict[str, Any]:
        self.central.boards_present.add(self.board_id)
        return {"ok": True, "board_id": self.board_id}

    async def board_members(self) -> dict[str, Any]:
        return {
            "members": [
                {"principal_id": principal_id, "role": role}
                for principal_id, role in self.memberships.items()
            ]
        }

    async def board_member_add(
        self, principal_id: str, role: str = "member", **_kwargs: object
    ) -> dict[str, Any]:
        self.memberships[principal_id] = role
        return {"ok": True}

    async def board_status(self) -> dict[str, Any]:
        return {
            "dispatch_policy": self.dispatch_policy,
            "review_policy": self.review_policy,
        }

    async def board_dispatch_policy_set(self, **kwargs: object) -> dict[str, Any]:
        self.dispatch_policy.update(kwargs)
        return {"ok": True}

    async def board_review_policy_set(
        self, review_policy: str, **_kwargs: object
    ) -> dict[str, Any]:
        self.review_policy = review_policy
        return {"ok": True}

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        assert key == "project_registry"
        return {"state": {"value": json.dumps(self.central.registry)}}

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "board_state_update":
            self.central.registry = json.loads(arguments["value"])
            return {"ok": True}
        if name == "board_members":
            return await self.board_members()
        if name == "board_member_add":
            return await self.board_member_add(
                arguments["principal_id"], arguments.get("role", "member")
            )
        raise NotImplementedError(name)


class ProjectCentral:
    def __init__(self) -> None:
        self.registry: dict[str, Any] = {"schema_version": 1, "projects": {}}
        self.boards_present = {"pursers"}
        self.boards: dict[str, ProjectBoard] = {}

    def client_factory(
        self, _url: str, _token: str, board_id: str, **_kwargs: object
    ) -> ProjectBoard:
        return self.boards.setdefault(board_id, ProjectBoard(board_id, self))


class ProjectSeats:
    def __init__(self, clone_dir: Path) -> None:
        self.clone_dir = clone_dir

    def _clone_state(self, _path: Path, _ref: str = "main") -> dict[str, Any]:
        return {"status": "ready", "dirty": False}

    def prepare_fleet_clone(
        self, registry_payload: dict[str, Any], project_name: str
    ) -> dict[str, Any]:
        self.clone_dir.mkdir(parents=True, exist_ok=True)
        registry = copy.deepcopy(registry_payload["registry"])
        registry["projects"][project_name]["fleet_clone_dir"] = str(self.clone_dir)
        return {
            "project": project_name,
            "clone": {"path": str(self.clone_dir), "status": "ready"},
            "registry": registry,
            "expected_sha256": registry_payload["expected_sha256"],
        }


class ReadEvidenceSeats:
    def __init__(self) -> None:
        self.fail_release = False
        self.jobs = {
            "a" * 32: {
                "job_id": "a" * 32,
                "action": "doctor",
                "status": "succeeded",
            }
        }

    def seats(self) -> dict[str, Any]:
        return {"seats": []}

    def release_status(self) -> dict[str, Any]:
        if self.fail_release:
            raise RuntimeError("release status unavailable")
        return {"state": "ready", "version": "5.0.0b1"}

    def job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]


def test_real_attention_handler_emits_actual_sanitized_evidence(tmp_path: Path) -> None:
    trace, output = _trace(tmp_path)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    secret = "must-not-appear-in-evidence"
    action = json.dumps(
        {"TK-123": {"state": "ack", "note": secret}}, separators=(",", ":")
    ).encode()
    headers = _headers(action)
    try:
        before_status, before, before_headers = _call(
            base_url, "GET", headers=_headers()
        )
        status, result, response_headers = _call(
            base_url, "POST", body=action, headers=headers
        )
        after_status, after, after_headers = _call(
            base_url, "GET", headers=_headers()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert before_status == status == after_status == 200
    assert before["items"] == {}
    assert result["items"] == {"TK-123": {"state": "ack", "note": secret}}
    assert after["items"] == result["items"]
    assert json.loads((state_dir / "attention-state.json").read_text()) == result["items"]
    assert result["_evidence"]["outcome"] == "succeeded"
    assert result["_evidence"]["effect"] == "attention_state_changed"
    assert result["_evidence"]["changed"] is True
    assert result["_evidence"]["result_sha256"] == hashlib.sha256(
        dashboard._json_bytes({"items": result["items"]})
    ).hexdigest()
    assert before["_evidence"]["effect"] == "attention_state_unchanged"
    assert after["_evidence"]["effect"] == "attention_state_unchanged"
    for returned in (before_headers, response_headers, after_headers):
        assert returned["X-Pursers-Observation-Id"] == "fleet.attention-state"
        assert returned["X-Pursers-Run-Id"] == "run-1"
        assert returned["X-Pursers-Action-Id"] == "save-attention"
        assert returned["X-Pursers-Entity-Id"] == "TK-123"

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        key: value
        for key, value in result["_evidence"].items()
        if key != "log_emitted"
    }
    assert record["action_sha256"] == hashlib.sha256(action).hexdigest()
    assert record["pid"] == os.getpid()
    assert len(record["candidate_commit"]) == 40
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert secret not in lines[0]


def test_release_and_job_reads_emit_correlated_evidence_without_a_log(
    tmp_path: Path,
) -> None:
    trace, output = _trace(tmp_path)
    seats = ReadEvidenceSeats()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats,
            evidence_trace=trace,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    headers = _headers(
        observation_id="fleet.release-status",
        action_id="read-release",
        entity="release",
    )
    try:
        release_status, release, release_headers = _call(
            base_url, "GET", path="/api/config/release", headers=headers
        )
        job_status, job, job_headers = _call(
            base_url, "GET", path=f"/api/config/jobs/{'a' * 32}", headers=headers
        )
        missing_status, missing, missing_headers = _call(
            base_url, "GET", path=f"/api/config/jobs/{'b' * 32}", headers=headers
        )
        seats_status, seats_result, seats_headers = _call(
            base_url, "GET", path="/api/config/seats", headers=headers
        )
        seats.fail_release = True
        failed_status, failed, failed_headers = _call(
            base_url, "GET", path="/api/config/release", headers=headers
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert release_status == job_status == seats_status == 200
    assert missing_status == 404
    assert failed_status == 503
    assert release["state"] == "ready"
    assert job["status"] == "succeeded"
    assert missing["error"] == "job not found"
    assert failed["error"] == "RuntimeError"
    assert seats_result == {"seats": []}
    assert "_evidence" not in seats_result
    assert seats_headers.get("X-Pursers-Run-Id") is None
    for document, effect, outcome in (
        (release, "release_state_unchanged", "succeeded"),
        (job, "job_state_unchanged", "succeeded"),
        (missing, "job_state_unchanged", "failed"),
        (failed, "release_state_unchanged", "failed"),
    ):
        evidence = document["_evidence"]
        assert evidence["effect"] == effect
        assert evidence["outcome"] == outcome
        assert evidence["changed"] is False
        assert evidence["action_sha256"] is None
        assert evidence["log_emitted"] is False
    for returned in (
        release_headers, job_headers, missing_headers, failed_headers,
    ):
        assert returned["X-Pursers-Observation-Id"] == "fleet.release-status"
        assert returned["X-Pursers-Run-Id"] == "run-1"
        assert returned["X-Pursers-Action-Id"] == "read-release"
        assert returned["X-Pursers-Entity-Id"] == "release"
    assert not output.exists()


def test_real_add_project_handler_emits_steps_and_actual_registry_transition(
    tmp_path: Path,
) -> None:
    trace, output = _trace(tmp_path)
    central = ProjectCentral()
    config = dashboard.Config(
        url="http://127.0.0.1:1/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="fleet-evidence-test",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=tmp_path / "keys",
        jwks_path=tmp_path / "jwks.json",
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=central.client_factory)
    cache = dashboard.DashboardCache([fetcher], 60)
    seats = ProjectSeats(tmp_path / "fleet-clone")
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(cache, seat_manager=seats, evidence_trace=trace),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    action = json.dumps(
        {
            "name": "demo",
            "board_id": "sandbox-board",
            "work_dir": str(tmp_path / "work"),
        },
        separators=(",", ":"),
    ).encode()
    (tmp_path / "work").mkdir()
    trace_headers = _headers(
        action,
        observation_id="fleet.add-project-clone-steps",
        action_id="add-project",
        entity="demo",
    )
    try:
        status, result, response_headers = _call(
            base_url,
            "POST",
            path="/api/projects/add",
            body=action,
            headers={**trace_headers, "Origin": base_url},
        )

        failed_action = json.dumps(
            {
                "name": "invalid-work-dir",
                "board_id": "sandbox-board",
                "work_dir": "relative",
            },
            separators=(",", ":"),
        ).encode()
        failed_status, failed, _failed_headers = _call(
            base_url,
            "POST",
            path="/api/projects/add",
            body=failed_action,
            headers={
                **_headers(
                    failed_action,
                    observation_id="fleet.add-project-negative",
                    action_id="invalid-work-dir",
                    entity="invalid-work-dir",
                ),
                "Origin": base_url,
            },
        )

        mismatched = json.dumps(
            {
                "name": "mismatched",
                "board_id": "sandbox-board",
                "work_dir": str(tmp_path / "mismatched-work"),
            },
            separators=(",", ":"),
        ).encode()
        (tmp_path / "mismatched-work").mkdir()
        mismatch_status, mismatch_result, mismatch_headers = _call(
            base_url,
            "POST",
            path="/api/projects/add",
            body=mismatched,
            headers={
                **_headers(
                    mismatched,
                    observation_id="fleet.add-project-mismatch",
                    action_id="wrong-entity",
                    entity="different-project",
                ),
                "Origin": base_url,
            },
        )

        wrong_board = json.dumps(
            {
                "name": "wrong-board",
                "board_id": "sandbox-other",
                "work_dir": str(tmp_path / "wrong-board-work"),
            },
            separators=(",", ":"),
        ).encode()
        (tmp_path / "wrong-board-work").mkdir()
        wrong_board_status, wrong_board_result, wrong_board_headers = _call(
            base_url,
            "POST",
            path="/api/projects/add",
            body=wrong_board,
            headers={
                **_headers(
                    wrong_board,
                    observation_id="fleet.add-project-wrong-board",
                    action_id="wrong-board",
                    entity="wrong-board",
                ),
                "Origin": base_url,
            },
        )

        wrong_origin = json.dumps(
            {
                "name": "wrong-origin",
                "board_id": "sandbox-board",
                "work_dir": str(tmp_path / "wrong-origin-work"),
            },
            separators=(",", ":"),
        ).encode()
        wrong_origin_status, wrong_origin_result, wrong_origin_headers = _call(
            base_url,
            "POST",
            path="/api/projects/add",
            body=wrong_origin,
            headers={
                **_headers(
                    wrong_origin,
                    observation_id="fleet.add-project-wrong-origin",
                    action_id="wrong-origin",
                    entity="wrong-origin",
                ),
                "Origin": "https://attacker.invalid",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        fetcher.close()

    assert status == 200
    fleet_clone = next(step for step in result["steps"] if step["step"] == "fleet_clone")
    assert fleet_clone["status"] == "prepared"
    assert central.registry["projects"]["demo"]["board_id"] == "sandbox-board"
    evidence = result["_evidence"]
    assert evidence["path"] == "/api/projects/add"
    assert evidence["entity"] == "demo"
    assert evidence["outcome"] == "succeeded"
    assert evidence["effect"] == "project_state_changed"
    assert evidence["before_sha256"] == hashlib.sha256(
        dashboard._json_bytes({"project": None})
    ).hexdigest()
    assert evidence["after_sha256"] == hashlib.sha256(
        dashboard._json_bytes({"project": central.registry["projects"]["demo"]})
    ).hexdigest()
    original_result = {key: value for key, value in result.items() if key != "_evidence"}
    assert evidence["result_sha256"] == hashlib.sha256(
        dashboard._json_bytes(original_result)
    ).hexdigest()
    context = trace.context(trace_headers, "POST", "/api/projects/add")
    assert context is not None
    for key, header in dashboard.CORRELATION_HEADERS.items():
        assert response_headers[header] == getattr(context, key)

    assert failed_status == 400
    assert failed["error"] == "work_dir must be an absolute path"
    assert failed["_evidence"]["outcome"] == "failed"
    assert failed["_evidence"]["effect"] == "project_state_unchanged"

    assert mismatch_status == 200
    assert "_evidence" not in mismatch_result
    assert mismatch_headers.get("X-Pursers-Run-Id") is None
    assert wrong_board_status == 200
    assert "_evidence" not in wrong_board_result
    assert wrong_board_headers.get("X-Pursers-Run-Id") is None
    assert wrong_origin_status == 403
    assert wrong_origin_result == {"error": "same-origin request required"}
    assert wrong_origin_headers.get("X-Pursers-Run-Id") is None

    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [record["action_id"] for record in records] == [
        "add-project",
        "invalid-work-dir",
    ]
    serialized = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert "prs1." not in serialized


def test_trace_route_allowlist_is_closed_for_project_evidence(tmp_path: Path) -> None:
    trace, _output = _trace(tmp_path)
    headers = _headers(
        b"{}",
        observation_id="fleet.add-project-clone-steps",
        action_id="add-project",
        entity="demo",
    )
    assert trace.context(headers, "POST", "/api/projects/add") is not None
    assert trace.context(headers, "GET", "/api/projects/add") is None
    assert trace.context(headers, "POST", "/api/projects/add/steps") is None
    assert trace.context(headers, "POST", "/api/config/registry/clone") is None
    assert trace.context(headers, "GET", "/api/config/release") is not None
    assert trace.context(headers, "POST", "/api/config/release") is None
    assert trace.context(headers, "GET", f"/api/config/jobs/{'a' * 32}") is not None
    assert trace.context(headers, "GET", f"/api/config/jobs/{'A' * 32}") is None
    assert trace.context(headers, "POST", f"/api/config/jobs/{'a' * 32}") is None
    context = trace.context(headers, "POST", "/api/projects/add")
    assert context is not None
    assert trace.observe(
        context=context,
        method="POST",
        route="/api/projects/add/steps",
        status=200,
        before={},
        after={"changed": True},
        result_body=b'{}',
    ) == (None, False)


def test_concurrent_untraced_save_cannot_change_traced_action_effect(
    tmp_path: Path,
) -> None:
    trace, output = _trace(tmp_path)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    action_inside_lock = threading.Event()
    changing_action_entered = threading.Event()
    release_action = threading.Event()
    original_save = seats._save_attention_state_unlocked

    def controlled_save(value: Any) -> dict[str, Any]:
        if value == {}:
            action_inside_lock.set()
            assert release_action.wait(2)
        else:
            changing_action_entered.set()
        return original_save(value)

    seats._save_attention_state_unlocked = controlled_save
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats,
            evidence_trace=trace,
        ),
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    no_op = b"{}"
    changing = b'{"B":{"state":"ack"}}'
    results: dict[str, tuple[int, dict[str, Any], Any]] = {}
    failures: list[BaseException] = []

    def call_a() -> None:
        try:
            results["a"] = _call(
                base_url, "POST", body=no_op, headers=_headers(no_op)
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in test thread.
            failures.append(exc)

    def call_b() -> None:
        try:
            results["b"] = _call(
                base_url, "POST", body=changing,
                headers={"Content-Type": "application/json"},
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in test thread.
            failures.append(exc)

    request_a = threading.Thread(target=call_a)
    request_b = threading.Thread(target=call_b)
    try:
        request_a.start()
        assert action_inside_lock.wait(2)
        request_b.start()
        assert not changing_action_entered.wait(0.1)
        release_action.set()
        request_a.join(2)
        request_b.join(2)
        assert not request_a.is_alive()
        assert not request_b.is_alive()
    finally:
        release_action.set()
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert not failures
    status_a, result_a, headers_a = results["a"]
    status_b, result_b, headers_b = results["b"]
    assert status_a == status_b == 200
    assert result_a["items"] == {}
    assert result_b == {"items": {"B": {"state": "ack"}}}
    assert headers_a["X-Pursers-Action-Id"] == "save-attention"
    assert headers_b.get("X-Pursers-Action-Id") is None
    record = json.loads(output.read_text(encoding="utf-8"))
    expected = hashlib.sha256(dashboard._json_bytes({"items": {}})).hexdigest()
    assert record["before_sha256"] == expected
    assert record["after_sha256"] == expected
    assert record["result_sha256"] == expected
    assert record["changed"] is False
    assert record["effect"] == "attention_state_unchanged"
    assert seats.attention_state() == {"items": {"B": {"state": "ack"}}}


def test_trace_rejects_bad_correlation_digest_replay_and_forged_success(
    tmp_path: Path,
) -> None:
    trace, output = _trace(tmp_path)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    good = b'{"TK-123":{"state":"ack"}}'
    try:
        missing = _headers(good)
        missing.pop("X-Pursers-Entity-Id")
        assert "_evidence" not in _call(base_url, "POST", body=good, headers=missing)[1]

        malformed = _headers(good)
        malformed["X-Pursers-Run-Id"] = "bad id with spaces"
        assert "_evidence" not in _call(base_url, "POST", body=good, headers=malformed)[1]

        wrong_digest = _headers(good)
        wrong_digest["X-Pursers-Action-SHA256"] = "0" * 64
        assert "_evidence" not in _call(
            base_url, "POST", body=good, headers=wrong_digest
        )[1]

        first = _call(base_url, "POST", body=good, headers=_headers(good))[1]
        replay = _call(base_url, "POST", body=good, headers=_headers(good))[1]
        assert first["_evidence"]["log_emitted"] is True
        assert "_evidence" not in replay

        forged = {f"item-{index}": index for index in range(500)}
        forged["outcome"] = "succeeded"
        forged_body = json.dumps(forged, separators=(",", ":")).encode()
        forged_headers = _headers(forged_body)
        forged_headers["X-Pursers-Action-Id"] = "forged-success"
        failed_status, failed, _ = _call(
            base_url, "POST", body=forged_body, headers=forged_headers
        )

        unrelated = urllib.request.Request(
            base_url + "/api/centrals", headers=_headers(), method="GET"
        )
        with urllib.request.urlopen(unrelated) as response:
            unrelated_body = json.loads(response.read())
            assert response.headers.get("X-Pursers-Run-Id") is None
        assert "_evidence" not in unrelated_body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert failed_status == 400
    assert failed["_evidence"]["outcome"] == "failed"
    assert failed["_evidence"]["effect"] == "attention_state_unchanged"
    assert failed["_evidence"]["changed"] is False
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [record["action_id"] for record in records] == [
        "save-attention",
        "forged-success",
    ]


@pytest.mark.parametrize(
    ("guard_header", "guard_value", "expected_status"),
    [
        ("Host", "evil.example", 403),
        ("Origin", "http://evil.example", 403),
        ("Content-Type", "text/plain", 415),
    ],
)
def test_post_guard_rejections_never_emit_trusted_evidence(
    tmp_path: Path,
    guard_header: str,
    guard_value: str,
    expected_status: int,
) -> None:
    trace, output = _trace(tmp_path)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    action = b'{"TK-guard":{"state":"ack"}}'
    headers = _headers(action)
    headers["X-Pursers-Action-SHA256"] = "0" * 64
    headers[guard_header] = guard_value
    try:
        status, result, response_headers = _call(
            f"http://127.0.0.1:{server.server_port}",
            "POST",
            body=action,
            headers=headers,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert status == expected_status
    assert "_evidence" not in result
    for header in dashboard.CORRELATION_HEADERS.values():
        assert response_headers.get(header) is None
    assert not output.exists()
    assert seats.attention_state()["items"] == {}
    assert not (state_dir / "attention-state.json").exists()


def test_trace_requires_private_config_and_real_git_entrypoint(tmp_path: Path) -> None:
    config = tmp_path / "trace.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "output_path": str(tmp_path / "trace.jsonl"),
                "max_bytes": 65_536,
                "runtime_id": "fleet-runtime-test",
                "board_id": "sandbox-board",
                "surface": "fleet",
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o644)
    with pytest.raises(dashboard.EvidenceTraceConfigError, match="0600"):
        dashboard.EvidenceTrace.from_config(config, MODULE_PATH)

    config.chmod(0o600)
    unrelated = tmp_path / "not-fleet.py"
    unrelated.write_text("print('not Fleet')\n", encoding="utf-8")
    with pytest.raises(
        dashboard.EvidenceTraceConfigError, match="checkout identity"
    ):
        dashboard.EvidenceTrace.from_config(config, unrelated)


def test_full_trace_file_is_fail_passive_for_real_action(tmp_path: Path) -> None:
    initial = b"{}\n" * 1_300
    trace, output = _trace(tmp_path, initial_output=initial, max_bytes=4_096)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    action = b'{"TK-123":{"state":"ack"}}'
    try:
        status, result, headers = _call(
            f"http://127.0.0.1:{server.server_port}",
            "POST",
            body=action,
            headers=_headers(action),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert status == 200
    assert result == {"items": {"TK-123": {"state": "ack"}}}
    assert headers.get("X-Pursers-Run-Id") is None
    assert output.read_bytes() == initial
    assert json.loads((state_dir / "attention-state.json").read_text()) == result["items"]
