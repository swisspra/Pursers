"""Security regression for environment-proxy isolation in BoardClient."""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from pursers_client import DEFAULT_EVENT_KINDS, GENERATION_META_KEY, BoardClient
from pursers_client import client as client_module


def test_generation_constants_remain_public() -> None:
    assert DEFAULT_EVENT_KINDS == client_module.DEFAULT_EVENT_KINDS
    assert GENERATION_META_KEY == client_module.GENERATION_META_KEY


def test_board_client_disables_environment_proxy_inheritance(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def recording_async_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(client_module.httpx2, "AsyncClient", recording_async_client)

    result = BoardClient(
        "http://127.0.0.1:8766/mcp",
        "synthetic-local-bearer",
        "board-proxy-negative",
    )._http()

    assert type(result) is object
    assert captured["trust_env"] is False
    assert captured["headers"] == {
        "Authorization": "Bearer synthetic-local-bearer"
    }


def test_proxy_environment_never_receives_local_bearer(monkeypatch) -> None:
    seen: dict[str, list[tuple[str, str | None]]] = {
        "target": [],
        "proxy": [],
    }

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["target"].append(
                (self.path, self.headers.get("Authorization"))
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["proxy"].append(
                (self.path, self.headers.get("Authorization"))
            )
            self.send_response(502)
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    def start_server(
        handler: type[BaseHTTPRequestHandler],
    ) -> tuple[ThreadingHTTPServer, threading.Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    target, target_thread = start_server(TargetHandler)
    proxy, proxy_thread = start_server(ProxyHandler)
    try:
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.setenv(name, proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        bearer = "proxy-must-never-see-this-bearer"
        client = BoardClient(
            f"http://127.0.0.1:{target.server_port}/mcp",
            bearer,
            "board-proxy-e2e",
        )

        async def request() -> int:
            async with client._http() as http:
                response = await http.get(
                    f"http://127.0.0.1:{target.server_port}/probe"
                )
                return response.status_code

        assert asyncio.run(request()) == 204
        assert seen["target"] == [("/probe", f"Bearer {bearer}")]
        assert seen["proxy"] == []
    finally:
        target.shutdown()
        proxy.shutdown()
        target.server_close()
        proxy.server_close()
        target_thread.join(timeout=2)
        proxy_thread.join(timeout=2)


class HangingStack:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def aclose(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


@pytest.mark.anyio
async def test_exit_cancels_hanging_transport_close_within_bound(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "TRANSPORT_CLOSE_TIMEOUT_S", 0.01)
    client = BoardClient(
        "http://127.0.0.1:8766/mcp",
        "synthetic-local-bearer",
        "board-close-bound",
    )
    stack = HangingStack()
    client._stack = stack
    client._client = object()

    await asyncio.wait_for(client.__aexit__(None, None, None), timeout=0.2)
    await asyncio.wait_for(stack.cancelled.wait(), timeout=0.2)

    assert stack.started.is_set()
    assert client._stack is None
    assert client._client is None


@pytest.mark.anyio
async def test_enter_failure_preserves_error_when_transport_close_hangs(
    monkeypatch,
) -> None:
    class EnterFailure(RuntimeError):
        pass

    class FailingEnterStack(HangingStack):
        def __init__(self) -> None:
            super().__init__()
            self.entries = 0

        async def enter_async_context(self, _context: object) -> object:
            self.entries += 1
            if self.entries == 1:
                return object()
            raise EnterFailure("synthetic transport setup failure")

    stack = FailingEnterStack()
    monkeypatch.setattr(client_module, "TRANSPORT_CLOSE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(client_module, "AsyncExitStack", lambda: stack)
    monkeypatch.setattr(BoardClient, "_http", lambda _self: object())
    monkeypatch.setattr(
        client_module,
        "streamable_http_client",
        lambda _url, *, http_client: object(),
    )
    monkeypatch.setattr(client_module, "Client", lambda *_args, **_kwargs: object())
    client = BoardClient(
        "http://127.0.0.1:8766/mcp",
        "synthetic-local-bearer",
        "board-enter-failure",
    )

    with pytest.raises(EnterFailure, match="synthetic transport setup failure"):
        await asyncio.wait_for(client.__aenter__(), timeout=0.2)
    await asyncio.wait_for(stack.cancelled.wait(), timeout=0.2)

    assert stack.started.is_set()
    assert client._stack is None
    assert client._client is None
