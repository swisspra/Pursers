#!/usr/bin/env python3
"""Small dependency-free asyncio client for ACP v1 subprocesses."""

from __future__ import annotations

import asyncio
import copy
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


class ACPConfigMismatch(ACPError):
    """A saved session selection is incompatible with the agent's current state."""

    def __init__(
        self,
        config_id: str,
        value: Any,
        reason: str,
    ) -> None:
        super().__init__(
            f"saved session option {config_id!r}={value!r} is incompatible: {reason}"
        )
        self.config_id = config_id
        self.value = value
        self.reason = reason


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
        self._session_config_options: dict[str, list[JSON] | None] = {}
        self._session_modes: dict[str, JSON | None] = {}
        self._boolean_config_options = False
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
        capabilities = copy.deepcopy(client_capabilities or {})
        if not isinstance(capabilities, dict):
            raise TypeError("client_capabilities must be an object")
        session_capabilities = capabilities.setdefault("session", {})
        if not isinstance(session_capabilities, dict):
            raise TypeError("clientCapabilities.session must be an object")
        config_capabilities = session_capabilities.setdefault("configOptions", {})
        if not isinstance(config_capabilities, dict):
            raise TypeError(
                "clientCapabilities.session.configOptions must be an object"
            )
        config_capabilities.setdefault("boolean", {})
        boolean_capabilities = config_capabilities.get("boolean")
        if boolean_capabilities is not None and not isinstance(
            boolean_capabilities, dict
        ):
            raise TypeError(
                "clientCapabilities.session.configOptions.boolean must be an object"
            )
        self._boolean_config_options = isinstance(boolean_capabilities, dict)
        params: JSON = {
            "protocolVersion": protocol_version,
            "clientCapabilities": capabilities,
            "clientInfo": client_info or {"name": "pursers-acp-seat", "version": "0"},
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
        if not isinstance(result, dict) or not isinstance(result.get("sessionId"), str):
            raise ACPProtocolError("session/new result must contain a sessionId")
        session_id = result["sessionId"]
        self._sessions.add(session_id)
        self._session_config_options.setdefault(session_id, None)
        self._session_modes.setdefault(session_id, None)
        self._capture_session_configuration(session_id, result)
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
        self._session_config_options.setdefault(session_id, None)
        self._session_modes.setdefault(session_id, None)
        result = await self._request(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": session_cwd,
                "mcpServers": list(mcp_servers),
            },
            timeout,
        )
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise ACPProtocolError("session/load result must be an object")
        self._sessions.add(session_id)
        self._capture_session_configuration(session_id, result)

    def discover_session_config(self, session_id: str) -> JSON:
        """Return the current preferred configuration surface without agent calls."""
        self._ensure_known_session(session_id)
        config_options = self._session_config_options.get(session_id)
        if config_options is not None:
            return {
                "source": "configOptions",
                "configOptions": copy.deepcopy(config_options),
            }
        modes = self._session_modes.get(session_id)
        if modes is not None:
            return {
                "source": "modes",
                "configOptions": [self._legacy_mode_option(modes)],
            }
        return {"source": "none", "configOptions": []}

    async def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str | bool,
        *,
        timeout: float | None = None,
    ) -> list[JSON]:
        """Validate and set one advertised option, replacing all cached state."""
        self._ensure_known_session(session_id)
        options = self._session_config_options.get(session_id)
        if options is None:
            raise ACPConfigMismatch(
                config_id, value, "agent advertised no configOptions"
            )
        option = self._find_config_option(options, config_id, value)
        option_type = option.get("type")
        params: JSON = {
            "sessionId": session_id,
            "configId": config_id,
            "value": value,
        }
        if option_type == "select":
            if not isinstance(value, str):
                raise ACPConfigMismatch(
                    config_id, value, "select value must be a string"
                )
            available = self._select_values(option)
            if value not in available:
                choices = ", ".join(repr(item) for item in available)
                raise ACPConfigMismatch(
                    config_id,
                    value,
                    f"value is not currently advertised; available values: {choices}",
                )
        elif option_type == "boolean":
            if not self._boolean_config_options:
                raise ACPConfigMismatch(
                    config_id, value, "boolean configOptions were not negotiated"
                )
            if not isinstance(value, bool):
                raise ACPConfigMismatch(config_id, value, "boolean value required")
            params["type"] = "boolean"
        else:
            raise ACPConfigMismatch(
                config_id,
                value,
                f"unsupported option type {option_type!r}; refresh or choose in the agent",
            )
        result = await self._request("session/set_config_option", params, timeout)
        if not isinstance(result, dict) or "configOptions" not in result:
            raise ACPProtocolError(
                "session/set_config_option result must contain complete configOptions"
            )
        replacement = self._validated_config_options(result["configOptions"])
        self._session_config_options[session_id] = replacement
        applied = next(
            (item for item in replacement if item.get("id") == config_id), None
        )
        if applied is None or applied.get("currentValue") != value:
            raise ACPConfigMismatch(
                config_id,
                value,
                "agent response did not report the requested value as current",
            )
        return copy.deepcopy(replacement)

    async def apply_config_preset(
        self,
        session_id: str,
        selections: Mapping[str, str | bool],
        *,
        timeout: float | None = None,
    ) -> JSON:
        """Apply saved selections in advertised order before the first prompt."""
        self._ensure_known_session(session_id)
        if not isinstance(selections, Mapping):
            raise TypeError("selections must be a mapping")
        remaining = dict(selections)
        for config_id, value in remaining.items():
            if not isinstance(config_id, str) or not isinstance(value, (str, bool)):
                raise TypeError(
                    "selection IDs must be strings and values strings/booleans"
                )
        state = self.discover_session_config(session_id)
        if state["source"] == "configOptions":
            advertised = {
                option["id"] for option in state["configOptions"] if "id" in option
            }
            for config_id, value in remaining.items():
                if config_id not in advertised:
                    raise ACPConfigMismatch(
                        config_id,
                        value,
                        "option ID is not advertised by this agent/version",
                    )
            while remaining:
                current = self._session_config_options.get(session_id) or []
                next_id = next(
                    (
                        option["id"]
                        for option in current
                        if option.get("id") in remaining
                    ),
                    None,
                )
                if next_id is None:
                    config_id, value = next(iter(remaining.items()))
                    raise ACPConfigMismatch(
                        config_id,
                        value,
                        "option disappeared after a dependent selection changed",
                    )
                await self.set_config_option(
                    session_id, next_id, remaining.pop(next_id), timeout=timeout
                )
            return self.discover_session_config(session_id)
        if state["source"] == "modes":
            if not remaining:
                return state
            if set(remaining) != {"mode"}:
                config_id, value = next(iter(remaining.items()))
                raise ACPConfigMismatch(
                    config_id,
                    value,
                    "legacy agent only advertises the saved option ID 'mode'",
                )
            value = remaining["mode"]
            option = state["configOptions"][0]
            if not isinstance(value, str) or value not in self._select_values(option):
                raise ACPConfigMismatch(
                    "mode", value, "legacy mode is not currently advertised"
                )
            result = await self._request(
                "session/set_mode",
                {"sessionId": session_id, "modeId": value},
                timeout,
            )
            if result not in (None, {}) and not isinstance(result, dict):
                raise ACPProtocolError("session/set_mode result must be an object")
            modes = copy.deepcopy(self._session_modes[session_id])
            assert modes is not None
            modes["currentModeId"] = value
            self._session_modes[session_id] = modes
            return self.discover_session_config(session_id)
        if remaining:
            config_id, value = next(iter(remaining.items()))
            raise ACPConfigMismatch(
                config_id, value, "agent advertised no session configuration"
            )
        return state

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

    def acknowledge_update(self) -> None:
        """Mark one update returned by :meth:`next_update` as processed."""
        self._updates.task_done()

    async def wait_for_updates(self) -> None:
        """Wait until every received update has been processed by a consumer."""
        await self._updates.join()

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

    async def _request(self, method: str, params: JSON, timeout: float | None) -> Any:
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

    def _ensure_known_session(self, session_id: str) -> None:
        self._ensure_initialized()
        if session_id not in self._sessions:
            raise ValueError(f"unknown ACP session {session_id}")

    def _capture_session_configuration(self, session_id: str, result: JSON) -> None:
        if "configOptions" in result:
            raw = result["configOptions"]
            self._session_config_options[session_id] = (
                None if raw is None else self._validated_config_options(raw)
            )
        if "modes" in result:
            raw_modes = result["modes"]
            self._session_modes[session_id] = (
                None if raw_modes is None else self._validated_modes(raw_modes)
            )

    def _validated_config_options(self, raw: Any) -> list[JSON]:
        if not isinstance(raw, list):
            raise ACPProtocolError("configOptions must be an array")
        result: list[JSON] = []
        seen: set[str] = set()
        for raw_option in raw:
            if not isinstance(raw_option, dict):
                raise ACPProtocolError("configOptions entries must be objects")
            option = copy.deepcopy(raw_option)
            config_id = option.get("id")
            if not isinstance(config_id, str) or not config_id:
                raise ACPProtocolError("config option id must be a non-empty string")
            if config_id in seen:
                raise ACPProtocolError(f"duplicate config option id {config_id!r}")
            seen.add(config_id)
            if not isinstance(option.get("name"), str):
                raise ACPProtocolError(f"config option {config_id!r} needs a name")
            category = option.get("category")
            if category is not None and not isinstance(category, str):
                raise ACPProtocolError(
                    f"config option {config_id!r} category must be a string"
                )
            option_type = option.get("type")
            if not isinstance(option_type, str):
                raise ACPProtocolError(f"config option {config_id!r} needs a type")
            if option_type == "select":
                if not isinstance(option.get("currentValue"), str):
                    raise ACPProtocolError(
                        f"select config option {config_id!r} needs a string currentValue"
                    )
                values = self._select_values(option)
                if option["currentValue"] not in values:
                    raise ACPProtocolError(
                        f"select config option {config_id!r} currentValue is not advertised"
                    )
            elif option_type == "boolean":
                if not self._boolean_config_options:
                    raise ACPProtocolError(
                        "agent sent a boolean config option without negotiation"
                    )
                if not isinstance(option.get("currentValue"), bool):
                    raise ACPProtocolError(
                        f"boolean config option {config_id!r} needs a boolean currentValue"
                    )
            result.append(option)
        return result

    @staticmethod
    def _select_values(option: JSON) -> list[str]:
        raw_options = option.get("options")
        config_id = option.get("id")
        if not isinstance(raw_options, list):
            raise ACPProtocolError(
                f"select config option {config_id!r} needs an options array"
            )
        if not raw_options:
            return []
        grouped = all(
            isinstance(item, dict) and "group" in item for item in raw_options
        )
        flat = all(isinstance(item, dict) and "value" in item for item in raw_options)
        if grouped == flat:
            raise ACPProtocolError(
                f"select config option {config_id!r} must use grouped or flat options"
            )
        candidates: list[Any] = []
        if grouped:
            seen_groups: set[str] = set()
            for group in raw_options:
                group_id = group.get("group")
                if (
                    not isinstance(group_id, str)
                    or not group_id
                    or group_id in seen_groups
                    or not isinstance(group.get("name"), str)
                    or not isinstance(group.get("options"), list)
                ):
                    raise ACPProtocolError(
                        f"select config option {config_id!r} has an invalid group"
                    )
                seen_groups.add(group_id)
                candidates.extend(group["options"])
        else:
            candidates = raw_options
        values: list[str] = []
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("value"), str)
                or not isinstance(candidate.get("name"), str)
                or candidate["value"] in values
            ):
                raise ACPProtocolError(
                    f"select config option {config_id!r} has an invalid value"
                )
            values.append(candidate["value"])
        return values

    @staticmethod
    def _find_config_option(options: list[JSON], config_id: str, value: Any) -> JSON:
        for option in options:
            if option.get("id") == config_id:
                return option
        raise ACPConfigMismatch(
            config_id, value, "option ID is not currently advertised"
        )

    @staticmethod
    def _validated_modes(raw: Any) -> JSON:
        if not isinstance(raw, dict):
            raise ACPProtocolError("modes must be an object")
        current = raw.get("currentModeId")
        available = raw.get("availableModes")
        if not isinstance(current, str) or not isinstance(available, list):
            raise ACPProtocolError(
                "modes must include currentModeId and availableModes"
            )
        seen: set[str] = set()
        for mode in available:
            if (
                not isinstance(mode, dict)
                or not isinstance(mode.get("id"), str)
                or not isinstance(mode.get("name"), str)
                or mode["id"] in seen
            ):
                raise ACPProtocolError("modes contains an invalid entry")
            seen.add(mode["id"])
        if current not in seen:
            raise ACPProtocolError("currentModeId is not in availableModes")
        return copy.deepcopy(raw)

    @staticmethod
    def _legacy_mode_option(modes: JSON) -> JSON:
        values = []
        for mode in modes["availableModes"]:
            value = {"value": mode["id"], "name": mode["name"]}
            for key in ("description", "_meta"):
                if key in mode:
                    value[key] = copy.deepcopy(mode[key])
            values.append(value)
        return {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": modes["currentModeId"],
            "options": values,
        }

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
                update = params.get("update")
                if (
                    isinstance(update, dict)
                    and update.get("sessionUpdate") == "config_option_update"
                ):
                    session_id = params.get("sessionId")
                    if (
                        not isinstance(session_id, str)
                        or session_id not in self._session_config_options
                    ):
                        raise ACPProtocolError(
                            "config_option_update referenced an unknown session"
                        )
                    if "configOptions" not in update:
                        raise ACPProtocolError(
                            "config_option_update must contain complete configOptions"
                        )
                    self._session_config_options[session_id] = (
                        self._validated_config_options(update["configOptions"])
                    )
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
