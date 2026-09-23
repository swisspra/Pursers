from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "board_butler.py"
SPEC = importlib.util.spec_from_file_location("board_butler_model_tests", MODULE_PATH)
assert SPEC and SPEC.loader
butler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = butler
SPEC.loader.exec_module(butler)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
POLICY_DIGEST = "a" * 64


def validate_contract(value: Mapping[str, Any]) -> None:
    schema_path = (
        MODULE_PATH.parents[2]
        / "docs/design/schemas/autonomous-butler-model-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(value)


def registry() -> tuple[Any, str]:
    schemas = butler.TaskSchemaRegistry()
    digest = schemas.register(
        "answer:v1",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
        },
    )
    return schemas, digest


def model_request(schema_digest: str, **changes: Any) -> dict[str, Any]:
    task_input = {
        "instruction": "Answer only from cited evidence.",
        "observation_refs": ["observation:one"],
        "constraints": ["Return the exact task schema."],
    }
    value: dict[str, Any] = {
        "schema": "autonomous_butler_model_v1",
        "schema_version": 1,
        "message_type": "request",
        "request_id": "request:one",
        "board_id": "pursers",
        "subject_id": "question:one",
        "task_kind": "answer_question",
        "policy_digest_sha256": POLICY_DIGEST,
        "task_input": task_input,
        "task_input_digest_sha256": butler._sha256_json(task_input),
        "task_schema": {
            "schema_id": "answer:v1",
            "schema_sha256": schema_digest,
        },
        "evidence_refs": ["evidence:one"],
        "max_output_bytes": 4096,
        "usage_limit": {
            "max_input_tokens": 1000,
            "max_output_tokens": 200,
            "max_cost_microunits": 1000,
        },
        "deadline": (NOW + timedelta(minutes=1)).isoformat(),
        "cancellation_token": "cancel:one",
    }
    value.update(changes)
    return value


def backend_response() -> Any:
    return butler.ModelBackendResponse(
        proposal_json='{"answer":"bounded"}',
        citations=("evidence:one",),
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cost_microunits": 3,
            "measured": True,
        },
        provider_request_ref="provider:one",
    )


class FakeBackend:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response or backend_response()
        self.error = error
        self.calls = 0

    async def run(
        self, _request: Mapping[str, Any], *, timeout_s: float
    ) -> Any:
        assert timeout_s > 0
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def runner(backend: Any, schemas: Any, **changes: Any) -> Any:
    return butler.AutonomousModelRunner(
        backend,
        task_schemas=schemas,
        policy_digest=lambda _board: POLICY_DIGEST,
        now=lambda: NOW,
        **changes,
    )


def test_common_runner_validates_and_normalizes_success() -> None:
    schemas, digest = registry()
    result = asyncio.run(runner(FakeBackend(), schemas).run(model_request(digest)))

    assert result["outcome"] == "succeeded"
    assert json.loads(result["proposal_json"]) == {"answer": "bounded"}
    assert result["citations"] == ["evidence:one"]
    assert result["usage"]["total_tokens"] == 15
    assert result["provider_request_ref"] == "provider:one"
    validate_contract(result)


@pytest.mark.parametrize("request_value", [None, [], "not-an-object"])
def test_common_runner_normalizes_non_object_requests(request_value: Any) -> None:
    schemas, _digest = registry()

    result = asyncio.run(runner(FakeBackend(), schemas).run(request_value))

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "invalid_model_request"
    assert result["request_id"] == "invalid-request"
    validate_contract(result)


class FailingResultStore:
    def __init__(self, failure_point: str) -> None:
        self.failure_point = failure_point

    def get(self, _request_id: str) -> None:
        if self.failure_point == "get":
            raise OSError("private storage detail")
        return None

    def put(
        self, _request_id: str, _request_digest: str, _result: Mapping[str, Any]
    ) -> None:
        if self.failure_point == "put":
            raise OSError("private storage detail")


@pytest.mark.parametrize("failure_point", ["get", "put"])
def test_common_runner_normalizes_result_store_errors(failure_point: str) -> None:
    schemas, digest = registry()
    backend = FakeBackend()

    result = asyncio.run(
        runner(
            backend,
            schemas,
            result_store=FailingResultStore(failure_point),
        ).run(model_request(digest))
    )

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "result_store_error"
    assert result["error"] == {
        "category": "provider",
        "code": "result_store_error",
        "retryable": True,
    }
    assert "private storage detail" not in json.dumps(result)
    assert backend.calls == (0 if failure_point == "get" else 1)
    validate_contract(result)


def test_common_runner_fails_closed_for_digest_schema_citation_and_usage() -> None:
    schemas, digest = registry()
    bad_digest = model_request(digest, task_input_digest_sha256="0" * 64)
    assert asyncio.run(runner(FakeBackend(), schemas).run(bad_digest))["reason_code"] == (
        "task_input_digest_mismatch"
    )

    invalid_schema = FakeBackend(
        butler.ModelBackendResponse(
            proposal_json='{"wrong":true}',
            citations=("evidence:one",),
            usage=backend_response().usage,
            provider_request_ref="provider:one",
        )
    )
    assert asyncio.run(runner(invalid_schema, schemas).run(model_request(digest)))[
        "reason_code"
    ] == "task_schema_rejected"

    bad_citation = FakeBackend(
        butler.ModelBackendResponse(
            proposal_json='{"answer":"bounded"}',
            citations=("evidence:other",),
            usage=backend_response().usage,
            provider_request_ref="provider:one",
        )
    )
    assert asyncio.run(runner(bad_citation, schemas).run(model_request(digest)))[
        "reason_code"
    ] == "unknown_citation"

    usage = dict(backend_response().usage)
    usage["measured"] = False
    unmeasured = FakeBackend(
        butler.ModelBackendResponse(
            proposal_json='{"answer":"bounded"}',
            citations=("evidence:one",),
            usage=usage,
            provider_request_ref="provider:one",
        )
    )
    assert asyncio.run(runner(unmeasured, schemas).run(model_request(digest)))[
        "reason_code"
    ] == "unmeasured_usage"


def test_common_runner_enforces_task_schema_formats() -> None:
    schemas = butler.TaskSchemaRegistry()
    digest = schemas.register(
        "formatted-answer:v1",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["address"],
            "properties": {"address": {"type": "string", "format": "ipv4"}},
        },
    )
    backend = FakeBackend(
        butler.ModelBackendResponse(
            proposal_json='{"address":"999.999.999.999"}',
            citations=("evidence:one",),
            usage=backend_response().usage,
            provider_request_ref="provider:one",
        )
    )

    result = asyncio.run(
        runner(backend, schemas).run(
            model_request(
                digest,
                task_schema={
                    "schema_id": "formatted-answer:v1",
                    "schema_sha256": digest,
                },
            )
        )
    )

    assert result["reason_code"] == "task_schema_rejected"


def test_crash_result_is_stored_before_reply_and_replayed(tmp_path: Path) -> None:
    schemas, digest = registry()
    backend = FakeBackend(error=RuntimeError("secret provider detail"))
    store = butler.FileModelResultStore(tmp_path / "results")
    first = asyncio.run(
        runner(backend, schemas, result_store=store).run(model_request(digest))
    )
    second = asyncio.run(
        runner(backend, schemas, result_store=store).run(model_request(digest))
    )

    assert first == second
    assert first["reason_code"] == "provider_crash"
    assert "secret provider detail" not in json.dumps(first)
    assert backend.calls == 1
    record = next((tmp_path / "results").iterdir())
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_file_result_store_rejects_non_private_or_symlink_directory(
    tmp_path: Path,
) -> None:
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(butler.ModelRunnerFailure, match="unsafe_replay_directory"):
        butler.FileModelResultStore(public)

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(butler.ModelRunnerFailure, match="unsafe_replay_directory"):
        butler.FileModelResultStore(link)


def test_replay_rejects_changed_request_id_payload() -> None:
    schemas, digest = registry()
    backend = FakeBackend()
    instance = runner(backend, schemas)
    request = model_request(digest)
    assert asyncio.run(instance.run(request))["outcome"] == "succeeded"
    changed = model_request(digest)
    changed["task_input"]["instruction"] = "Changed"
    changed["task_input_digest_sha256"] = butler._sha256_json(changed["task_input"])

    result = asyncio.run(instance.run(changed))
    assert result["reason_code"] == "replay_digest_mismatch"
    assert backend.calls == 1


class SlowBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, _request: Mapping[str, Any], *, timeout_s: float) -> Any:
        self.started.set()
        try:
            await asyncio.sleep(timeout_s + 1)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return backend_response()


async def cancel_during_dispatch(schema_digest: str, schemas: Any) -> tuple[Any, Any]:
    backend = SlowBackend()
    instance = runner(backend, schemas)
    pending = asyncio.create_task(instance.run(model_request(schema_digest)))
    await backend.started.wait()
    instance.cancel("cancel:one")
    return await pending, backend


def test_cancel_discards_late_output_and_cancels_backend() -> None:
    schemas, digest = registry()
    result, backend = asyncio.run(cancel_during_dispatch(digest, schemas))

    assert result["outcome"] == "cancelled"
    assert result["reason_code"] == "cancelled_during_dispatch"
    assert result["proposal_json"] is None
    assert backend.cancelled is True
    validate_contract(result)


def test_timeout_is_typed_and_fail_closed() -> None:
    schemas, digest = registry()
    request = model_request(
        digest, deadline=(NOW + timedelta(milliseconds=20)).isoformat()
    )
    result = asyncio.run(runner(SlowBackend(), schemas).run(request))

    assert result["outcome"] == "failed"
    assert result["error"] == {
        "category": "timeout",
        "code": "provider_timeout",
        "retryable": True,
    }


def test_policy_digest_drift_discards_provider_output() -> None:
    schemas, digest = registry()
    current = {"digest": POLICY_DIGEST}

    class DriftBackend(FakeBackend):
        async def run(
            self, request: Mapping[str, Any], *, timeout_s: float
        ) -> Any:
            response = await super().run(request, timeout_s=timeout_s)
            current["digest"] = "b" * 64
            return response

    instance = butler.AutonomousModelRunner(
        DriftBackend(),
        task_schemas=schemas,
        policy_digest=lambda _board: current["digest"],
        now=lambda: NOW,
    )

    result = asyncio.run(instance.run(model_request(digest)))

    assert result["reason_code"] == "policy_digest_drift"
    assert result["proposal_json"] is None


class DirectHandler(BaseHTTPRequestHandler):
    body: dict[str, Any] | None = None
    authorization: str | None = None
    response_payload: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).body = json.loads(self.rfile.read(length))
        type(self).authorization = self.headers.get("Authorization")
        encoded = json.dumps(type(self).response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_direct_api_backend_uses_approved_protocol_and_exact_model() -> None:
    schemas, digest = registry()
    secret = "provider-secret-value"
    payload = {
        "proposal": {"answer": "direct"},
        "citations": ["evidence:one"],
    }
    DirectHandler.response_payload = {
        "id": "provider:direct-one",
        "model": "exact-model",
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 3,
            "total_tokens": 12,
            "cost_microunits": 4,
            "measured": True,
        },
        "choices": [{"message": {"content": json.dumps(payload)}}],
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = butler.ProviderRuntime(
            endpoint=f"http://127.0.0.1:{server.server_port}",
            model="exact-model",
            credential=secret,
            draft_path="v1/chat/completions",
            draft_protocol="openai_chat_completions_v1",
        )
        result = asyncio.run(
            runner(butler.DirectAPIModelBackend(runtime), schemas).run(
                model_request(digest)
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["outcome"] == "succeeded"
    assert json.loads(result["proposal_json"]) == {"answer": "direct"}
    assert DirectHandler.body["model"] == "exact-model"
    assert DirectHandler.body["response_format"] == {"type": "json_object"}
    assert secret not in json.dumps(DirectHandler.body)
    assert DirectHandler.authorization == f"Bearer {secret}"


def test_direct_api_backend_rejects_credential_echo_without_persisting_it() -> None:
    schemas, digest = registry()
    secret = "provider-secret-value"
    DirectHandler.response_payload = {
        "id": "provider:direct-two",
        "model": "exact-model",
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 3,
            "total_tokens": 12,
            "cost_microunits": 4,
            "measured": True,
        },
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "proposal": {"answer": secret},
                            "citations": ["evidence:one"],
                        }
                    )
                }
            }
        ],
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = butler.ProviderRuntime(
            endpoint=f"http://127.0.0.1:{server.server_port}",
            model="exact-model",
            credential=secret,
            draft_path="v1/chat/completions",
            draft_protocol="openai_chat_completions_v1",
        )
        result = asyncio.run(
            runner(butler.DirectAPIModelBackend(runtime), schemas).run(
                model_request(digest)
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["reason_code"] == "provider_credential_echo"
    assert secret not in json.dumps(result)


def test_direct_api_backend_rejects_credential_echo_in_request_reference() -> None:
    schemas, digest = registry()
    secret = "provider-secret-value"
    DirectHandler.response_payload = {
        "id": f"request:{secret}",
        "model": "exact-model",
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 3,
            "total_tokens": 12,
            "cost_microunits": 4,
            "measured": True,
        },
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "proposal": {"answer": "bounded"},
                            "citations": ["evidence:one"],
                        }
                    )
                }
            }
        ],
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = butler.ProviderRuntime(
            endpoint=f"http://127.0.0.1:{server.server_port}",
            model="exact-model",
            credential=secret,
            draft_path="v1/chat/completions",
            draft_protocol="openai_chat_completions_v1",
        )
        result = asyncio.run(
            runner(butler.DirectAPIModelBackend(runtime), schemas).run(
                model_request(digest)
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["reason_code"] == "provider_credential_echo"
    assert secret not in json.dumps(result)


def test_direct_api_backend_normalizes_non_object_content_as_malformed() -> None:
    schemas, digest = registry()
    DirectHandler.response_payload = {
        "id": "provider:direct-three",
        "model": "exact-model",
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 3,
            "total_tokens": 12,
            "cost_microunits": 4,
            "measured": True,
        },
        "choices": [
            {"message": {"content": json.dumps(["proposal", "citations"])}}
        ],
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = butler.ProviderRuntime(
            endpoint=f"http://127.0.0.1:{server.server_port}",
            model="exact-model",
            credential="provider-secret-value",
            draft_path="v1/chat/completions",
            draft_protocol="openai_chat_completions_v1",
        )
        result = asyncio.run(
            runner(butler.DirectAPIModelBackend(runtime), schemas).run(
                model_request(digest)
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["reason_code"] == "invalid_backend_payload"
    assert result["error"]["category"] == "malformed"


def test_acp_backend_uses_no_mcp_servers_and_normalizes_result(tmp_path: Path) -> None:
    schemas, digest = registry()
    payload = {
        "model": "exact-acp-model",
        "proposal": {"answer": "acp"},
        "citations": ["evidence:one"],
    }
    script = tmp_path / "agent.json"
    script.write_text(
        json.dumps(
            {
                "promptMustContain": [
                    '"model":"exact-acp-model"',
                    '"protocol":"autonomous_butler_model_v1"',
                ],
                "promptActions": [
                    {
                        "type": "update",
                        "update": {
                            "sessionUpdate": "pursers_model_usage",
                            "model": "exact-acp-model",
                            "providerRequestRef": "provider:acp-one",
                            "usage": {
                                "input_tokens": 8,
                                "output_tokens": 4,
                                "total_tokens": 12,
                                "cost_microunits": 2,
                                "measured": True,
                            },
                        },
                    },
                    {
                        "type": "update",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": json.dumps(payload)},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake = MODULE_PATH.parents[1] / "acp-seat/tests/fake_acp_agent.py"
    backend = butler.ACPModelBackend(
        [sys.executable, str(fake), "--script", str(script)],
        session_root=tmp_path,
        model="exact-acp-model",
    )

    result = asyncio.run(runner(backend, schemas).run(model_request(digest)))

    assert result["outcome"] == "succeeded"
    assert json.loads(result["proposal_json"]) == {"answer": "acp"}
    assert result["provider_request_ref"] == "provider:acp-one"


def test_acp_backend_drains_all_updates_before_normalizing(tmp_path: Path) -> None:
    schemas, digest = registry()
    encoded = json.dumps(
        {
            "model": "exact-acp-model",
            "proposal": {"answer": "burst"},
            "citations": ["evidence:one"],
        }
    )
    actions = [
        {
            "type": "update",
            "update": {
                "sessionUpdate": "pursers_model_usage",
                "model": "exact-acp-model",
                "providerRequestRef": "provider:acp-burst",
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "total_tokens": 12,
                    "cost_microunits": 2,
                    "measured": True,
                },
            },
        },
        *[
            {
                "type": "update",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": character},
                },
            }
            for character in encoded
        ],
    ]
    backend = acp_backend(tmp_path, {"promptActions": actions})

    result = asyncio.run(runner(backend, schemas).run(model_request(digest)))

    assert result["outcome"] == "succeeded"
    assert json.loads(result["proposal_json"]) == {"answer": "burst"}
    assert result["provider_request_ref"] == "provider:acp-burst"


def test_acp_permission_request_is_denied_and_normalized(tmp_path: Path) -> None:
    schemas, digest = registry()
    script = tmp_path / "agent.json"
    script.write_text(
        json.dumps(
            {
                "promptActions": [
                    {
                        "type": "permission",
                        "expectedOutcome": {"outcome": "cancelled"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    fake = MODULE_PATH.parents[1] / "acp-seat/tests/fake_acp_agent.py"
    backend = butler.ACPModelBackend(
        [sys.executable, str(fake), "--script", str(script)],
        session_root=tmp_path,
        model="exact-acp-model",
        process_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )

    result = asyncio.run(runner(backend, schemas).run(model_request(digest)))

    assert result["outcome"] == "cancelled"
    assert result["reason_code"] == "acp_cancelled"


def acp_backend(tmp_path: Path, script_value: Mapping[str, Any]) -> Any:
    script = tmp_path / "agent-runtime.json"
    script.write_text(json.dumps(script_value), encoding="utf-8")
    fake = MODULE_PATH.parents[1] / "acp-seat/tests/fake_acp_agent.py"
    return butler.ACPModelBackend(
        [sys.executable, str(fake), "--script", str(script)],
        session_root=tmp_path,
        model="exact-acp-model",
    )


def test_acp_crash_is_typed_without_stderr_disclosure(tmp_path: Path) -> None:
    schemas, digest = registry()
    backend = acp_backend(
        tmp_path,
        {
            "promptActions": [
                {"type": "crash", "code": 23, "stderr": "private-host-detail"}
            ]
        },
    )

    result = asyncio.run(runner(backend, schemas).run(model_request(digest)))

    assert result["reason_code"] == "acp_process_error"
    assert result["error"]["retryable"] is True
    assert "private-host-detail" not in json.dumps(result)


def test_acp_timeout_is_typed(tmp_path: Path) -> None:
    schemas, digest = registry()
    backend = acp_backend(
        tmp_path, {"promptActions": [{"type": "sleep", "seconds": 1}]}
    )
    request = model_request(
        digest, deadline=(NOW + timedelta(milliseconds=100)).isoformat()
    )

    result = asyncio.run(runner(backend, schemas).run(request))

    assert result["error"]["category"] == "timeout"
    assert result["reason_code"] in {"acp_timeout", "provider_timeout"}


async def cancel_acp(tmp_path: Path, schemas: Any, digest: str) -> dict[str, Any]:
    backend = acp_backend(
        tmp_path, {"promptActions": [{"type": "wait_for_cancel"}]}
    )
    instance = runner(backend, schemas)
    pending = asyncio.create_task(instance.run(model_request(digest)))
    await asyncio.sleep(0.05)
    instance.cancel("cancel:one")
    return await pending


def test_acp_cancellation_returns_no_partial_result(tmp_path: Path) -> None:
    schemas, digest = registry()
    result = asyncio.run(cancel_acp(tmp_path, schemas, digest))

    assert result["outcome"] == "cancelled"
    assert result["reason_code"] == "cancelled_during_dispatch"
    assert result["proposal_json"] is None
