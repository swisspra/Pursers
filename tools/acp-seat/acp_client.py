#!/usr/bin/env python3
"""Small dependency-free asyncio client for ACP v1 subprocesses."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

JSON = dict[str, Any]
PermissionPolicy = Callable[[JSON], JSON | str | None | Awaitable[JSON | str | None]]


class ACPError(Exception):
    """Base class for ACP client errors."""


class ACPProtocolError(ACPError):
    """The peer sent an invalid or unsupported protocol message."""


class ACPRemoteError(ACPError):
    """The peer returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ACPProcessError(ACPError):
    """The ACP subprocess stopped before completing pending requests."""


class ACPTimeoutError(ACPError):
    """An ACP request exceeded its configured timeout."""


class ACPClient:
    """JSON-RPC 2.0 ACP v1 client over newline-delimited subprocess stdio."""

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        process_cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        permission_policy: PermissionPolicy | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.command = tuple(os.fspath(part) for part in command)
        self.process_cwd = os.fspath(process_cwd) if process_cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.permission_policy = permission_policy
        self.request_timeout = request_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.agent_capabilities: JSON = {}
        self.agent_info: JSON = {}
        self._initialized = False
        self._sessions: set[str] = set()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._expired_ids: set[int] = set()
        self._updates: asyncio.Queue[JSON] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._active_prompts: set[str] = set()
        self._stderr = bytearray()
        self._fatal_error: ACPError | None = None
        self._closing = False

    async def __aenter__(self) -> ACPClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Spawn the configured ACP agent and start the JSON-RPC reader."""
        if self.process is not None:
            raise RuntimeError("ACP client already started")
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.process_cwd,
            env=self.env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def initialize(
        self,
        *,
        protocol_version: int = 1,
        client_capabilities: JSON | None = None,
        client_info: JSON | None = None,
        timeout: float | None = None,
    ) -> JSON:
        """Negotiate ACP version and capabilities."""
        if self._initialized:
            raise RuntimeError("ACP connection already initialized")
        params: JSON = {
            "protocolVersion": protocol_version,
            "clientCapabilities": client_capabilities or {},
            "clientInfo": client_info
            or {"name": "pursers-acp-seat", "version": "0"},
        }
        result = await self._request("initialize", params, timeout)
        if not isinstance(result, dict):
            raise ACPProtocolError("initialize result must be an object")
        selected = result.get("protocolVersion")
        if selected != protocol_version:
            raise ACPProtocolError(
                f"unsupported ACP protocol version {selected!r}; "
                f"requested {protocol_version}"
            )
        capabilities = result.get("agentCapabilities", {})
        info = result.get("agentInfo", {})
        if not isinstance(capabilities, dict) or not isinstance(info, dict):
            raise ACPProtocolError("initialize capabilities/info must be objects")
        self.agent_capabilities = capabilities
        self.agent_info = info
        self._initialized = True
        return result

    async def new_session(
        self,
        cwd: str | os.PathLike[str],
        *,
        mcp_servers: Sequence[JSON] = (),
        timeout: float | None = None,
    ) -> str:
        """Create a session rooted at an absolute working directory."""
        self._ensure_initialized()
        session_cwd = os.fspath(cwd)
        if not Path(session_cwd).is_absolute():
            raise ValueError("ACP session cwd must be absolute")
        result = await self._request(
            "session/new",
            {"cwd": session_cwd, "mcpServers": list(mcp_servers)},
            timeout,
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("sessionId"), str
        ):
            raise ACPProtocolError("session/new result must contain a sessionId")
        session_id = result["sessionId"]
        self._sessions.add(session_id)
        return session_id

    async def load_session(
        self,
        session_id: str,
        cwd: str | os.PathLike[str],
        *,
        mcp_servers: Sequence[JSON] = (),
        timeout: float | None = None,
    ) -> None:
        """Load a session after checking the negotiated capability."""
        self._ensure_initialized()
        if self.agent_capabilities.get("loadSession") is not True:
            raise ACPProtocolError("agent did not advertise session/load")
        session_cwd = os.fspath(cwd)
        if not Path(session_cwd).is_absolute():
            raise ValueError("ACP session cwd must be absolute")
        result = await self._request(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": session_cwd,
                "mcpServers": list(mcp_servers),
            },
            timeout,
        )
        if result not in (None, {}):
            raise ACPProtocolError("session/load result must be null or empty")
        self._sessions.add(session_id)

    async def prompt(
        self,
        session_id: str,
        prompt: str | Sequence[JSON],
        *,
        timeout: float | None = None,
    ) -> JSON:
        """Run one prompt turn while updates remain available via updates()."""
        self._ensure_initialized()
        if session_id not in self._sessions:
            raise ValueError(f"unknown ACP session {session_id}")
        if session_id in self._active_prompts:
            raise RuntimeError(f"prompt already active for session {session_id}")
        blocks = (
            [{"type": "text", "text": prompt}]
            if isinstance(prompt, str)
            else list(prompt)
        )
        event = asyncio.Event()
        self._cancel_events[session_id] = event
        self._active_prompts.add(session_id)
        try:
            try:
                result = await self._request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": blocks},
                    timeout,
                )
            except ACPTimeoutError:
                try:
                    await self.cancel(session_id)
                except ACPError:
                    pass
                raise
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(self.cancel(session_id))
                except ACPError:
                    pass
                raise
            if not isinstance(result, dict) or not isinstance(
                result.get("stopReason"), str
            ):
                raise ACPProtocolError(
                    "session/prompt result must contain a stopReason"
                )
            return result
        finally:
            self._active_prompts.discard(session_id)
            if self._cancel_events.get(session_id) is event:
                del self._cancel_events[session_id]

    async def cancel(self, session_id: str) -> None:
        """Cancel an active turn and cancel any pending permission decision."""
        if session_id not in self._sessions:
            raise ValueError(f"unknown ACP session {session_id}")
        event = self._cancel_events.get(session_id)
        if event is not None:
            event.set()
        await self._notify("session/cancel", {"sessionId": session_id})

    async def next_update(self, *, timeout: float | None = None) -> JSON:
        """Return the next session/update notification."""
        if timeout is None:
            return await self._updates.get()
        try:
            return await asyncio.wait_for(self._updates.get(), timeout)
        except TimeoutError as exc:
            raise ACPTimeoutError("timed out waiting for session/update") from exc

    async def updates(self) -> AsyncIterator[JSON]:
        """Yield session/update params until the consumer stops iteration."""
        while True:
            yield await self.next_update()

    async def close(self) -> None:
        """Terminate the subprocess and release all local tasks."""
        if self.process is None:
            return
        self._closing = True
        self._fail_pending(ACPProcessError("ACP client closed"))
        for task in tuple(self._inbound_tasks):
            task.cancel()
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 1.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, ACPError):
                    pass
        self.process = None

    @property
    def stderr_text(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    async def _request(
        self, method: str, params: JSON, timeout: float | None
    ) -> Any:
        self._ensure_running()
        deadline = self.request_timeout if timeout is None else timeout
        if deadline <= 0:
            raise ValueError("timeout must be positive")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        except Exception:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        try:
            return await asyncio.wait_for(asyncio.shield(future), deadline)
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            self._expired_ids.add(request_id)
            future.cancel()
            raise
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            self._expired_ids.add(request_id)
            future.cancel()
            raise ACPTimeoutError(f"{method} timed out after {deadline}s") from exc

    async def _notify(self, method: str, params: JSON) -> None:
        self._ensure_running()
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _ensure_running(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error
        if self.process is None or self.process.returncode is not None:
            raise ACPProcessError("ACP subprocess is not running")

    def _ensure_initialized(self) -> None:
        self._ensure_running()
        if not self._initialized:
            raise ACPProtocolError("ACP connection is not initialized")

    async def _send(self, payload: JSON) -> None:
        assert self.process is not None and self.process.stdin is not None
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self.process.stdin.write(encoded)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ACPProcessError("ACP subprocess closed stdin") from exc

    async def _reader_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ACPProtocolError("agent emitted invalid JSON") from exc
                await self._handle_message(payload)
            if not self._closing:
                returncode = await self.process.wait()
                raise ACPProcessError(
                    f"ACP subprocess exited with code {returncode}: "
                    f"{self.stderr_text[-500:]}"
                )
        except asyncio.CancelledError:
            raise
        except ACPError as exc:
            self._fatal_error = exc
            self._fail_pending(exc)
            if self.process.returncode is None:
                self.process.terminate()
        except Exception as exc:
            error = ACPProtocolError(f"failed to process agent message: {exc}")
            self._fatal_error = error
            self._fail_pending(error)
            if self.process.returncode is None:
                self.process.terminate()

    async def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while chunk := await self.process.stderr.read(4096):
                self._stderr.extend(chunk)
                if len(self._stderr) > 64 * 1024:
                    del self._stderr[: len(self._stderr) - 64 * 1024]
        except asyncio.CancelledError:
            raise

    async def _handle_message(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise ACPProtocolError("agent message is not JSON-RPC 2.0")
        method = payload.get("method")
        if isinstance(method, str):
            params = payload.get("params", {})
            if not isinstance(params, dict):
                raise ACPProtocolError("agent method params must be an object")
            if "id" in payload:
                task = asyncio.create_task(
                    self._handle_inbound_request(payload["id"], method, params)
                )
                self._inbound_tasks.add(task)
                task.add_done_callback(self._inbound_tasks.discard)
            elif method == "session/update":
                await self._updates.put(params)
            return
        if "id" not in payload:
            raise ACPProtocolError("agent response has no id")
        request_id = payload["id"]
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            raise ACPProtocolError("agent response id must be an integer")
        future = self._pending.pop(request_id, None)
        if future is None:
            if request_id in self._expired_ids:
                self._expired_ids.discard(request_id)
                return
            raise ACPProtocolError(f"unexpected response id {request_id}")
        if "error" in payload:
            error = payload["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), int)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("message"), str)
            ):
                future.set_exception(ACPProtocolError("invalid JSON-RPC error"))
                return
            future.set_exception(
                ACPRemoteError(
                    error["code"],
                    error["message"],
                    error.get("data"),
                )
            )
        elif "result" in payload:
            future.set_result(payload["result"])
        else:
            future.set_exception(ACPProtocolError("response has no result or error"))

    async def _handle_inbound_request(
        self, request_id: Any, method: str, params: JSON
    ) -> None:
        if method != "session/request_permission":
            await self._send_error(request_id, -32601, f"unsupported method {method}")
            return
        try:
            outcome = await self._permission_outcome(params)
            await self._send_result(request_id, {"outcome": outcome})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._send_error(request_id, -32603, str(exc))

    async def _permission_outcome(self, params: JSON) -> JSON:
        session_id = params.get("sessionId")
        options = params.get("options")
        if not isinstance(session_id, str) or not isinstance(options, list):
            raise ACPProtocolError("invalid permission request")
        if session_id not in self._active_prompts:
            return {"outcome": "cancelled"}
        event = self._cancel_events.setdefault(session_id, asyncio.Event())
        if event.is_set():
            return {"outcome": "cancelled"}
        if self.permission_policy is None:
            return {"outcome": "cancelled"}
        decision = self.permission_policy(params)
        if inspect.isawaitable(decision):
            policy_task = asyncio.ensure_future(decision)
            cancel_task = asyncio.create_task(event.wait())
            done, _pending = await asyncio.wait(
                {policy_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                policy_task.cancel()
                try:
                    await policy_task
                except asyncio.CancelledError:
                    pass
                return {"outcome": "cancelled"}
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
            decision = policy_task.result()
        return self._normalize_permission(decision, options)

    @staticmethod
    def _normalize_permission(decision: JSON | str | None, options: list[Any]) -> JSON:
        if decision is None:
            return {"outcome": "cancelled"}
        if isinstance(decision, str):
            outcome: JSON = {"outcome": "selected", "optionId": decision}
        elif isinstance(decision, dict):
            outcome = dict(decision)
        else:
            raise ACPProtocolError("permission policy returned an invalid decision")
        kind = outcome.get("outcome")
        if kind == "cancelled":
            return {"outcome": "cancelled"}
        if kind != "selected" or not isinstance(outcome.get("optionId"), str):
            raise ACPProtocolError("permission outcome must be selected or cancelled")
        offered = {
            option.get("optionId")
            for option in options
            if isinstance(option, dict) and isinstance(option.get("optionId"), str)
        }
        if outcome["optionId"] not in offered:
            raise ACPProtocolError("permission policy selected an unoffered option")
        return {"outcome": "selected", "optionId": outcome["optionId"]}

    async def _send_result(self, request_id: Any, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _send_error(self, request_id: Any, code: int, message: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _fail_pending(self, error: ACPError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)
