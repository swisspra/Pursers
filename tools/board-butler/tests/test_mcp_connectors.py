from __future__ import annotations

import asyncio
import contextlib
import gzip
import importlib.util
import json
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "board_butler.py"
REPOSITORY_ROOT = MODULE_PATH.parents[2]
SPEC = importlib.util.spec_from_file_location("board_butler_connectors", MODULE_PATH)
assert SPEC and SPEC.loader
butler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = butler
SPEC.loader.exec_module(butler)

SHA = "a" * 64
SECRET = "fixture-connector-secret"
PRIVATE_PATH = "/Users/synthetic-user/private/project"


class Model:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        def dump(value: Any) -> Any:
            if isinstance(value, Model):
                return {key: dump(item) for key, item in value.__dict__.items()}
            if isinstance(value, SimpleNamespace):
                return {key: dump(item) for key, item in vars(value).items()}
            if isinstance(value, list):
                return [dump(item) for item in value]
            if isinstance(value, dict):
                return {key: dump(item) for key, item in value.items()}
            return value

        return dump(self)


class FakeClient:
    protocol_version = "2026-07-28"

    def __init__(
        self,
        *,
        call_results: list[Any] | None = None,
        delay: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.call_results = call_results or [
            Model(
                content=[{"type": "text", "text": "ok"}],
                structured_content={"ok": True},
                is_error=False,
                _meta={"authority": "admin"},
            )
        ]
        self.delay = delay

    async def list_tools(self, **_kwargs: Any) -> Model:
        return Model(
            tools=[
                SimpleNamespace(
                    name="lookup",
                    description="Allowed lookup",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query", "call_id"],
                        "properties": {
                            "query": {"type": "string"},
                            "call_id": {"type": "string"},
                            "context": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["value"],
                                "properties": {"value": {"type": "string"}},
                            },
                        },
                    },
                ),
                SimpleNamespace(
                    name="dangerous",
                    description="Not declared",
                    input_schema={"type": "object"},
                ),
                SimpleNamespace(
                    name="mutate",
                    description="Allowed mutation",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["value"],
                        "properties": {"value": {"type": "string"}},
                    },
                ),
            ],
            next_cursor=None,
        )

    async def list_resources(self, **_kwargs: Any) -> Model:
        return Model(
            resources=[
                SimpleNamespace(uri="cfg://allowed", name="Allowed", mime_type="application/json"),
                SimpleNamespace(uri="cfg://hidden", name="Hidden", mime_type="text/plain"),
            ],
            next_cursor=None,
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **_kwargs: Any
    ) -> Any:
        self.calls.append((name, dict(arguments)))
        if self.delay is not None:
            await self.delay.wait()
        result = self.call_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def read_resource(self, uri: str, **_kwargs: Any) -> Model:
        return Model(
            contents=[
                {
                    "uri": uri,
                    "text": f"secret={SECRET} path={PRIVATE_PATH}",
                    "_meta": {"authority": "operator"},
                }
            ]
        )


def declaration(
    *,
    transport: str = "stdio",
    output_bytes: int = 32_000,
    calls_per_minute: int = 60,
) -> butler.ConnectorDeclaration:
    return butler.ConnectorDeclaration.from_mapping(
        {
            "connector_id": "connector:test",
            "enabled": True,
            "transport": transport,
            "protocol_revision": "2026-07-28",
            "endpoint_ref": "endpoint:test",
            "secret_ref": "secret:test",
            "tools": [
                {
                    "name": "lookup",
                    "effect": "read_only",
                    "replay": "safe_with_stable_call_id",
                    "stable_call_id_field": "call_id",
                },
                {
                    "name": "mutate",
                    "effect": "mutating",
                    "replay": "never",
                    "stable_call_id_field": None,
                },
            ],
            "resources": ["cfg://allowed"],
            "risky_tools": ["mutate"],
            "limits": {
                "timeout_ms": 30_000,
                "max_input_bytes": 4_096,
                "max_output_bytes": output_bytes,
                "max_concurrency": 2,
                "calls_per_minute": calls_per_minute,
            },
        }
    )


def factory_for(clients: list[Any]):
    calls = {"count": 0}

    @contextlib.asynccontextmanager
    async def factory(
        _declaration: butler.ConnectorDeclaration,
        _endpoint: butler.ConnectorEndpoint,
        _secret: str,
    ) -> AsyncIterator[Any]:
        calls["count"] += 1
        current = clients.pop(0)
        if isinstance(current, BaseException):
            raise current
        yield current

    return factory, calls


def runtime(
    clients: list[Any],
    *,
    declared: butler.ConnectorDeclaration | None = None,
    policy_gate=None,
    board_id: str = "board-one",
) -> tuple[butler.ConnectorRuntime, butler.InMemoryConnectorPersistence, dict[str, int]]:
    factory, calls = factory_for(clients)
    persistence = butler.InMemoryConnectorPersistence()
    connector = butler.ConnectorRuntime(
        board_id=board_id,
        project_id="project-one",
        actor_id="butler-one",
        policy_digest_sha256=SHA,
        declaration=declared or declaration(),
        approved_connector_ids=["connector:test"],
        endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint("fake-server"),
        secret_resolver=lambda _ref: SECRET,
        persistence=persistence,
        policy_gate=policy_gate,
        client_factory=factory,
    )
    return connector, persistence, calls


def test_declaration_rejects_unknown_transport_protocol_and_replay_shape() -> None:
    value = {
        "connector_id": "connector:test",
        "enabled": True,
        "transport": "websocket",
        "protocol_revision": "2025-11-25",
        "endpoint_ref": "endpoint:test",
        "secret_ref": None,
        "tools": [],
        "resources": [],
        "limits": {
            "timeout_ms": 100,
            "max_input_bytes": 1,
            "max_output_bytes": 1,
            "max_concurrency": 1,
            "calls_per_minute": 1,
        },
    }
    with pytest.raises(butler.ConnectorConfigError, match="transport"):
        butler.ConnectorDeclaration.from_mapping(value)
    value["transport"] = "stdio"
    with pytest.raises(butler.ConnectorConfigError, match="protocol_revision"):
        butler.ConnectorDeclaration.from_mapping(value)


def test_http_endpoint_rejects_cleartext_non_loopback_and_embedded_credentials() -> None:
    with pytest.raises(butler.ConnectorConfigError, match="requires TLS"):
        butler.HttpConnectorEndpoint("http://example.invalid/mcp")
    with pytest.raises(butler.ConnectorConfigError, match="invalid"):
        butler.HttpConnectorEndpoint("https://user:pass@example.invalid/mcp")
    butler.HttpConnectorEndpoint("http://127.0.0.1:8123/mcp")


def test_runtime_rejects_connector_outside_immutable_envelope() -> None:
    with pytest.raises(butler.ConnectorDenied, match="immutable envelope"):
        butler.ConnectorRuntime(
            board_id="board-one",
            project_id="project-one",
            actor_id="butler-one",
            policy_digest_sha256=SHA,
            declaration=declaration(),
            approved_connector_ids=[],
            endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint("server"),
            secret_resolver=lambda _ref: SECRET,
            persistence=butler.InMemoryConnectorPersistence(),
        )


def test_discovery_filters_before_exposure_and_reconnects() -> None:
    async def scenario() -> None:
        connector, persistence, calls = runtime([OSError("offline"), FakeClient()])
        found = await connector.discover("operation-discover")
        assert [tool["name"] for tool in found.tools] == ["lookup", "mutate"]
        assert [resource["uri"] for resource in found.resources] == ["cfg://allowed"]
        assert calls["count"] == 2
        assert connector.health.status == "healthy"
        assert persistence.audit_records[-1]["outcome"] == "succeeded"

    asyncio.run(scenario())


def test_allowed_call_validates_schema_reserves_stable_id_and_redacts_result() -> None:
    async def scenario() -> None:
        result = Model(
            content=[
                {
                    "type": "text",
                    "text": f"{SECRET} {PRIVATE_PATH}",
                }
            ],
            structured_content={
                "token": SECRET,
                "value": "safe",
                "_meta": {"authority": "admin"},
            },
            is_error=False,
        )
        client = FakeClient(call_results=[result])
        connector, persistence, _calls = runtime([client])
        called = await connector.call_tool(
            "operation-call", "lookup", {"query": "status"}
        )
        sent = client.calls[0][1]
        assert sent["call_id"] == called.call_id
        assert persistence.reservations == {called.call_id: next(iter(persistence.reservations.values()))}
        encoded = json.dumps(called.payload)
        assert SECRET not in encoded
        assert PRIVATE_PATH not in encoded
        assert "authority" not in encoded
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (
                REPOSITORY_ROOT
                / "docs/design/schemas/autonomous-butler-audit-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(persistence.audit_records[-1])

    asyncio.run(scenario())


def test_denied_name_and_invalid_arguments_never_dispatch() -> None:
    async def scenario() -> None:
        client = FakeClient()
        connector, _persistence, calls = runtime([client])
        with pytest.raises(butler.ConnectorDenied, match="allowlisted"):
            await connector.call_tool("operation-one", "dangerous", {})
        assert calls["count"] == 0

        connector, _persistence, _calls = runtime([client])
        with pytest.raises(butler.ConnectorDenied, match="discovered schema"):
            await connector.call_tool("operation-two", "lookup", {"query": 3})
        assert client.calls == []

    asyncio.run(scenario())


def test_risky_call_requires_typed_policy_gate_and_never_retries() -> None:
    async def scenario() -> None:
        connector, _persistence, calls = runtime([FakeClient()])
        with pytest.raises(butler.ConnectorDenied, match="no policy gate"):
            await connector.call_tool("operation-risky", "mutate", {"value": "x"})
        assert calls["count"] == 0

        async def deny(_request: butler.ConnectorPolicyRequest):
            return butler.ConnectorPolicyDecision(False, "decision-denied", "denied")

        connector, _persistence, calls = runtime([FakeClient()], policy_gate=deny)
        with pytest.raises(butler.ConnectorDenied, match="denied by policy"):
            await connector.call_tool("operation-denied", "mutate", {"value": "x"})
        assert calls["count"] == 0

        requests: list[butler.ConnectorPolicyRequest] = []

        async def allow(request: butler.ConnectorPolicyRequest):
            requests.append(request)
            return butler.ConnectorPolicyDecision(True, "decision-one", "approved")

        connector, _persistence, calls = runtime(
            [FakeClient(call_results=[OSError("ambiguous")])], policy_gate=allow
        )
        with pytest.raises(butler.ConnectorProtocolError):
            await connector.call_tool("operation-risky", "mutate", {"value": "x"})
        assert calls["count"] == 1
        assert requests[0].arguments_sha256 == butler.hashlib.sha256(
            butler._canonical_json({"value": "x"})
        ).hexdigest()

    asyncio.run(scenario())


@pytest.mark.parametrize("malformed_allowed", ["not-a-bool", 1])
def test_policy_decision_rejects_truthy_non_booleans_before_dispatch(
    malformed_allowed: Any,
) -> None:
    with pytest.raises(butler.ConnectorConfigError, match="allowed must be boolean"):
        butler.ConnectorPolicyDecision(
            malformed_allowed, "decision-malformed", "approved"
        )

    async def scenario() -> None:
        malformed = object.__new__(butler.ConnectorPolicyDecision)
        object.__setattr__(malformed, "allowed", malformed_allowed)
        object.__setattr__(malformed, "decision_id", "decision-malformed")
        object.__setattr__(malformed, "reason_code", "approved")

        async def malformed_gate(_request: butler.ConnectorPolicyRequest):
            return malformed

        client = FakeClient()
        connector, _persistence, calls = runtime(
            [client], policy_gate=malformed_gate
        )
        with pytest.raises(butler.ConnectorDenied, match="denied by policy"):
            await connector.call_tool("operation-malformed", "mutate", {"value": "x"})
        assert calls["count"] == 0
        assert client.calls == []

    asyncio.run(scenario())


def test_safe_replay_reuses_stable_call_id_after_ambiguous_disconnect() -> None:
    async def scenario() -> None:
        first = FakeClient(call_results=[OSError("disconnect")])
        second = FakeClient()
        connector, persistence, calls = runtime([first, second])
        result = await connector.call_tool(
            "operation-replay", "lookup", {"query": "same"}
        )
        assert calls["count"] == 2
        assert first.calls[0][1]["call_id"] == second.calls[0][1]["call_id"]
        assert first.calls[0][1]["call_id"] == result.call_id
        assert len(persistence.reservations) == 1

    asyncio.run(scenario())


def test_nested_caller_mutation_cannot_change_reserved_or_dispatched_payload() -> None:
    class BarrierPersistence(butler.InMemoryConnectorPersistence):
        def __init__(self) -> None:
            super().__init__()
            self.reserved = asyncio.Event()
            self.release = asyncio.Event()

        async def reserve_call(self, call_id: str, payload_sha256: str) -> None:
            await super().reserve_call(call_id, payload_sha256)
            self.reserved.set()
            await self.release.wait()

    async def scenario() -> None:
        client = FakeClient()
        factory, _calls = factory_for([client])
        persistence = BarrierPersistence()
        connector = butler.ConnectorRuntime(
            board_id="board-one",
            project_id="project-one",
            actor_id="butler-one",
            policy_digest_sha256=SHA,
            declaration=declaration(),
            approved_connector_ids=["connector:test"],
            endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint("fake-server"),
            secret_resolver=lambda _ref: SECRET,
            persistence=persistence,
            client_factory=factory,
        )
        arguments = {"query": "status", "context": {"value": "reserved"}}
        task = asyncio.create_task(
            connector.call_tool("operation-owned-snapshot", "lookup", arguments)
        )
        await persistence.reserved.wait()
        arguments["context"]["value"] = "changed-after-reservation"
        persistence.release.set()
        result = await task

        sent = client.calls[0][1]
        assert sent["context"]["value"] == "reserved"
        assert persistence.reservations[result.call_id] == butler.hashlib.sha256(
            butler._canonical_json(sent)
        ).hexdigest()

    asyncio.run(scenario())


def test_oversize_malformed_and_rate_limited_results_fail_closed() -> None:
    async def scenario() -> None:
        oversized = Model(content=[{"text": "x" * 2_000}], is_error=False)
        connector, _persistence, _calls = runtime(
            [FakeClient(call_results=[oversized])],
            declared=declaration(output_bytes=500),
        )
        with pytest.raises(butler.ConnectorResultError, match="output byte"):
            await connector.call_tool("operation-large", "lookup", {"query": "x"})

        connector, _persistence, _calls = runtime(
            [FakeClient()], declared=declaration(calls_per_minute=1)
        )
        await connector.discover("operation-rate-one")
        with pytest.raises(butler.ConnectorDenied, match="rate limit"):
            await connector.discover("operation-rate-two")

    asyncio.run(scenario())


def test_cancellation_is_local_and_audited() -> None:
    async def scenario() -> None:
        delay = asyncio.Event()
        connector, persistence, _calls = runtime([FakeClient(delay=delay)])
        task = asyncio.create_task(
            connector.call_tool("operation-cancel", "lookup", {"query": "x"})
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connector.health.reason_code == "cancelled"
        assert persistence.audit_records[-1]["outcome"] == "cancelled"

    asyncio.run(scenario())


def test_resource_redaction_and_concurrent_board_isolation() -> None:
    async def scenario() -> None:
        one, store_one, _ = runtime([FakeClient()], board_id="board-one")
        two, store_two, _ = runtime([FakeClient()], board_id="board-two")
        first, second = await asyncio.gather(
            one.read_resource("operation-one", "cfg://allowed"),
            two.read_resource("operation-two", "cfg://allowed"),
        )
        assert SECRET not in json.dumps(first.payload)
        assert PRIVATE_PATH not in json.dumps(second.payload)
        assert first.call_id != second.call_id
        assert store_one.audit_records[-1]["board_id"] == "board-one"
        assert store_two.audit_records[-1]["board_id"] == "board-two"

    asyncio.run(scenario())


def test_secret_resolution_failure_does_not_disclose_value_or_path() -> None:
    async def scenario() -> None:
        persistence = butler.InMemoryConnectorPersistence()

        def fail(_ref: str) -> str:
            raise RuntimeError(f"{SECRET} {PRIVATE_PATH}/credential")

        connector = butler.ConnectorRuntime(
            board_id="board-secret",
            project_id="project-one",
            actor_id="butler-one",
            policy_digest_sha256=SHA,
            declaration=declaration(),
            approved_connector_ids=["connector:test"],
            endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint(
                f"{PRIVATE_PATH}/server", (SECRET,)
            ),
            secret_resolver=fail,
            persistence=persistence,
        )
        with pytest.raises(butler.ConnectorConfigError) as caught:
            await connector.discover("operation-secret")
        assert SECRET not in str(caught.value)
        assert PRIVATE_PATH not in str(caught.value)
        assert SECRET not in repr(connector.endpoint_resolver("endpoint:test"))
        assert PRIVATE_PATH not in repr(connector.endpoint_resolver("endpoint:test"))
        assert SECRET not in json.dumps(persistence.audit_records)
        assert PRIVATE_PATH not in json.dumps(persistence.audit_records)

    asyncio.run(scenario())


def _server_script(path: Path, *, transport: str, port: int | None = None) -> None:
    run = "mcp.run()"
    if transport == "streamable_http":
        run = (
            "mcp.run(transport='streamable-http', host='127.0.0.1', "
            f"port={port}, json_response=True, stateless_http=True)"
        )
    path.write_text(
        textwrap.dedent(
            f"""
            from mcp.server import MCPServer

            mcp = MCPServer("fake-connector")

            @mcp.tool()
            def lookup(query: str, call_id: str) -> dict[str, str]:
                return {{"query": query, "call_id": call_id}}

            @mcp.resource("cfg://allowed")
            def allowed() -> dict[str, bool]:
                return {{"ready": True}}

            if __name__ == "__main__":
                {run}
            """
        ),
        encoding="utf-8",
    )


def _transport_declaration(transport: str) -> butler.ConnectorDeclaration:
    value = declaration(transport=transport)
    return butler.ConnectorDeclaration(
        value.connector_id,
        value.enabled,
        value.transport,
        value.protocol_revision,
        value.endpoint_ref,
        None,
        (value.tools[0],),
        value.resources,
        frozenset(),
        value.limits,
    )


def test_real_sdk_stdio_transport(tmp_path: Path) -> None:
    script = tmp_path / "stdio_server.py"
    _server_script(script, transport="stdio")

    async def scenario() -> None:
        declared = _transport_declaration("stdio")
        connector = butler.ConnectorRuntime(
            board_id="board-stdio",
            project_id="project-one",
            actor_id="butler-one",
            policy_digest_sha256=SHA,
            declaration=declared,
            approved_connector_ids=["connector:test"],
            endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint(
                sys.executable, (str(script),)
            ),
            secret_resolver=lambda _ref: "",
            persistence=butler.InMemoryConnectorPersistence(),
        )
        found = await connector.discover("stdio-discover")
        assert [item["name"] for item in found.tools] == ["lookup"]
        result = await connector.call_tool(
            "stdio-call", "lookup", {"query": "stdio"}
        )
        assert result.payload["structuredContent"]["query"] == "stdio"

    asyncio.run(scenario())


def test_real_sdk_streamable_http_transport(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = tmp_path / "http_server.py"
    _server_script(script, transport="streamable_http", port=port)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("fake streamable HTTP server did not start")

        async def scenario() -> None:
            declared = _transport_declaration("streamable_http")
            connector = butler.ConnectorRuntime(
                board_id="board-http",
                project_id="project-one",
                actor_id="butler-one",
                policy_digest_sha256=SHA,
                declaration=declared,
                approved_connector_ids=["connector:test"],
                endpoint_resolver=lambda _ref: butler.HttpConnectorEndpoint(
                    f"http://127.0.0.1:{port}/mcp"
                ),
                secret_resolver=lambda _ref: "",
                persistence=butler.InMemoryConnectorPersistence(),
            )
            found = await connector.discover("http-discover")
            assert [item["name"] for item in found.tools] == ["lookup"]
            result = await connector.call_tool(
                "http-call", "lookup", {"query": "http"}
            )
            assert result.payload["structuredContent"]["query"] == "http"

        asyncio.run(scenario())
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_http_dns_pin_rejects_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = iter(
        [
            ("93.184.216.34",),
            ("93.184.216.35",),
        ]
    )

    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return next(addresses)

    monkeypatch.setattr(butler, "_resolve_connector_addresses", resolve)
    endpoint = butler.HttpConnectorEndpoint("https://connector.example/mcp")
    connector = butler.ConnectorRuntime(
        board_id="board-http-pin",
        project_id="project-one",
        actor_id="butler-one",
        policy_digest_sha256=SHA,
        declaration=_transport_declaration("streamable_http"),
        approved_connector_ids=["connector:test"],
        endpoint_resolver=lambda _ref: endpoint,
        secret_resolver=lambda _ref: "",
        persistence=butler.InMemoryConnectorPersistence(),
    )

    async def scenario() -> None:
        assert await connector._pin_http_endpoint(endpoint) == "93.184.216.34"
        with pytest.raises(butler.ConnectorConfigError, match="address changed"):
            await connector._pin_http_endpoint(endpoint)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("host", "address", "message"),
    [
        ("connector.example", "127.0.0.1", "not public"),
        ("localhost", "93.184.216.34", "escaped loopback"),
    ],
)
def test_http_address_policy_rejects_private_or_loopback_escape(
    host: str,
    address: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        butler.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ],
    )
    with pytest.raises(butler.ConnectorConfigError, match=message):
        asyncio.run(butler._resolve_connector_addresses(host, 443))


@pytest.mark.parametrize(
    ("content_type", "wire_body"),
    [
        ("application/json", b'{"result":"' + (b"x" * 200) + b'"}'),
        ("text/event-stream", b"event: message\ndata: " + (b"x" * 200) + b"\n\n"),
    ],
)
def test_http_transport_pins_address_and_bounds_frames_before_parsing(
    content_type: str,
    wire_body: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2
    import mcp.client.streamable_http as sdk_http

    parser_calls = 0
    original_parser = sdk_http.jsonrpc_message_adapter.validate_json

    def counted_parser(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        return original_parser(*args, **kwargs)

    monkeypatch.setattr(
        sdk_http.jsonrpc_message_adapter,
        "validate_json",
        counted_parser,
    )

    class Stream(httpx2.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self.body = body

        async def __aiter__(self):
            yield self.body

        async def aclose(self) -> None:
            return None

    class InnerTransport:
        request = None

        async def handle_async_request(self, request):
            self.request = request
            return httpx2.Response(
                200,
                headers={"content-type": content_type},
                stream=Stream(wire_body),
            )

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = butler._pinned_http_transport(
            "https://connector.example/mcp",
            "93.184.216.34",
            64,
        )
        inner = InnerTransport()
        transport._transport = inner
        request = httpx2.Request(
            "POST",
            "https://connector.example/mcp",
            headers={"host": "connector.example", "authorization": "Bearer private"},
            stream=Stream(b"{}"),
        )
        response = await transport.handle_async_request(request)
        with pytest.raises(butler.ConnectorResultError, match="frame exceeded"):
            body = await response.aread()
            sdk_http.jsonrpc_message_adapter.validate_json(body, by_name=False)
        assert parser_calls == 0
        assert str(inner.request.url).startswith("https://93.184.216.34/")
        assert inner.request.headers["host"] == "connector.example"
        assert inner.request.extensions["sni_hostname"] == "connector.example"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("content_type", "decoded_body"),
    [
        ("application/json", b'{"result":"' + (b"x" * 10_000) + b'"}'),
        (
            "text/event-stream",
            b"event: message\ndata: " + (b"x" * 10_000) + b"\n\n",
        ),
    ],
)
def test_http_transport_rejects_gzip_before_parsing(
    content_type: str,
    decoded_body: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2
    import mcp.client.streamable_http as sdk_http

    parser_calls = 0
    original_parser = sdk_http.jsonrpc_message_adapter.validate_json

    def counted_parser(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        return original_parser(*args, **kwargs)

    monkeypatch.setattr(
        sdk_http.jsonrpc_message_adapter,
        "validate_json",
        counted_parser,
    )

    class Stream(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield gzip.compress(decoded_body)

        async def aclose(self) -> None:
            self.closed = True

    class InnerTransport:
        request = None
        response_stream = Stream()

        async def handle_async_request(self, request):
            self.request = request
            return httpx2.Response(
                200,
                headers={
                    "content-type": content_type,
                    "content-encoding": "gzip",
                },
                stream=self.response_stream,
            )

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = butler._pinned_http_transport(
            "https://connector.example/mcp",
            "93.184.216.34",
            128,
        )
        inner = InnerTransport()
        transport._transport = inner
        request = httpx2.Request(
            "POST",
            "https://connector.example/mcp",
            headers={
                "host": "connector.example",
                "authorization": "Bearer private",
                "accept-encoding": "gzip, deflate, br",
            },
            stream=Stream(),
        )
        with pytest.raises(
            butler.ConnectorResultError,
            match="content encoding is not allowed",
        ):
            await transport.handle_async_request(request)
        assert parser_calls == 0
        assert inner.request.headers["accept-encoding"] == "identity"
        assert inner.response_stream.closed is True
        await transport.aclose()

    asyncio.run(scenario())


def test_http_transport_rejects_cross_origin_before_forwarding_credentials() -> None:
    import httpx2

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"{}"

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        transport = butler._pinned_http_transport(
            "https://connector.example/mcp",
            "93.184.216.34",
            64,
        )
        request = httpx2.Request(
            "POST",
            "https://redirected.example/mcp",
            headers={"authorization": "Bearer private"},
            stream=Stream(),
        )
        with pytest.raises(butler.ConnectorDenied, match="origin changed"):
            await transport.handle_async_request(request)
        await transport.aclose()

    asyncio.run(scenario())


def test_stdio_oversize_frame_is_stopped_before_sdk_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "oversize_stdio_server.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"data\":\"' "
        "+ 'x' * 4096 + '\"}}\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    import mcp.client.stdio as sdk_stdio

    parser_calls = 0
    original_parser = sdk_stdio._parse_line

    def counted_parser(line: str):
        nonlocal parser_calls
        parser_calls += 1
        return original_parser(line)

    monkeypatch.setattr(sdk_stdio, "_parse_line", counted_parser)
    value = declaration(transport="stdio", output_bytes=128)
    declared = butler.ConnectorDeclaration(
        value.connector_id,
        value.enabled,
        value.transport,
        value.protocol_revision,
        value.endpoint_ref,
        None,
        (value.tools[0],),
        (),
        frozenset(),
        value.limits,
    )

    async def scenario() -> None:
        connector = butler.ConnectorRuntime(
            board_id="board-stdio-oversize",
            project_id="project-one",
            actor_id="butler-one",
            policy_digest_sha256=SHA,
            declaration=declared,
            approved_connector_ids=["connector:test"],
            endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint(
                sys.executable,
                (str(script),),
            ),
            secret_resolver=lambda _ref: "",
            persistence=butler.InMemoryConnectorPersistence(),
        )
        with pytest.raises(
            (butler.ConnectorResultError, butler.ConnectorProtocolError)
        ):
            await connector.discover("stdio-oversize")

    asyncio.run(scenario())
    assert parser_calls == 0
