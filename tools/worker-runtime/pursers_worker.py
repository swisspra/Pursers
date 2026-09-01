#!/usr/bin/env python3
"""Headless Pursers worker for OpenAI-compatible chat-completions APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
CLIENT_SRC = ROOT / "packages" / "client" / "src"
WAIT_ROOT = ROOT / "tools" / "wait-bridge"
for import_root in (CLIENT_SRC, WAIT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from pursers_client import BoardClient  # noqa: E402
import pursers_wait_server as wait_bridge  # noqa: E402


DIRECTIVE_PATH = WAIT_ROOT / "WORKER-DIRECTIVE.md"
MAX_TOOL_OUTPUT = 20_000
MAX_FILE_READ = 100_000
LEASE_INTERVAL_S = 20.0
SHELL_ENV_ALLOWLIST = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "USER",
)
AUTH_SCHEME_PARTS = ("Bea", "rer")
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
SECURITY_CLI = Path("/usr/bin/security")
KEYCHAIN_SERVICE = "pursers-worker"


@dataclass(frozen=True)
class Config:
    agent_name: str
    central_url: str
    token_file: Path
    boards: str | tuple[str, ...]
    base_url: str
    api_key_env: str | None
    api_key_file: Path | None
    api_key_keychain: str | None
    model: str
    max_tokens: int
    max_iterations: int
    command_timeout_s: int
    log_file: Path


def _private_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    details = resolved.stat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise PermissionError(f"{label} must have mode 0600")
    return resolved


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def load_config(path: str | Path) -> Config:
    config_path = _private_file(Path(path), "config")
    raw = config_path.read_bytes()
    document = (
        json.loads(raw)
        if config_path.suffix.lower() == ".json"
        else tomllib.loads(raw.decode("utf-8"))
    )
    if not isinstance(document, dict):
        raise ValueError("config must be an object")
    seat = document.get("seat")
    llm = document.get("llm")
    if not isinstance(seat, dict) or not isinstance(llm, dict):
        raise ValueError("config requires seat and llm objects")
    if "token" in seat or "api_key" in llm:
        raise ValueError("inline tokens and API keys are forbidden")
    boards_raw = document.get("boards", "registry")
    if boards_raw == "registry":
        boards: str | tuple[str, ...] = "registry"
    elif isinstance(boards_raw, list) and boards_raw and all(
        isinstance(item, str) and item.strip() for item in boards_raw
    ):
        boards = tuple(dict.fromkeys(item.strip() for item in boards_raw))
    else:
        raise ValueError("boards must be 'registry' or a non-empty string list")
    token_file = _private_file(
        Path(_text(seat.get("token_file"), "seat.token_file")), "token file"
    )
    base_url = _text(llm.get("base_url"), "llm.base_url").rstrip("/")
    key_env = llm.get("api_key_env")
    key_file_raw = llm.get("api_key_file")
    keychain_raw = llm.get("api_key_keychain")
    key_sources = sum(bool(value) for value in (key_env, key_file_raw, keychain_raw))
    hostname = (urlsplit(base_url).hostname or "").lower()
    keyless_loopback = hostname in {"127.0.0.1", "localhost", "::1"}
    if key_sources != 1 and not (key_sources == 0 and keyless_loopback):
        raise ValueError(
            "llm requires exactly one of api_key_env, api_key_file, or "
            "api_key_keychain (loopback providers may omit all three)"
        )
    key_file = (
        _private_file(Path(_text(key_file_raw, "llm.api_key_file")), "API key file")
        if key_file_raw
        else None
    )

    def positive(name: str, default: int) -> int:
        value = llm.get(name, default)
        if type(value) is not int or value < 1:
            raise ValueError(f"llm.{name} must be a positive integer")
        return value

    log_file_raw = document.get("log_file")
    log_file = (
        Path(log_file_raw).expanduser().resolve()
        if isinstance(log_file_raw, str) and log_file_raw
        else config_path.with_suffix(config_path.suffix + ".session.log")
    )
    return Config(
        agent_name=_text(seat.get("agent_name"), "seat.agent_name"),
        central_url=_text(seat.get("central_url"), "seat.central_url"),
        token_file=token_file,
        boards=boards,
        base_url=base_url,
        api_key_env=_text(key_env, "llm.api_key_env") if key_env else None,
        api_key_file=key_file,
        api_key_keychain=(
            _text(keychain_raw, "llm.api_key_keychain") if keychain_raw else None
        ),
        model=_text(llm.get("model"), "llm.model"),
        max_tokens=positive("max_tokens", 4_096),
        max_iterations=positive("max_iterations", 40),
        command_timeout_s=positive("command_timeout_s", 120),
        log_file=log_file,
    )


def read_keychain_secret(account: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("macOS Keychain API keys require macOS")
    try:
        result = subprocess.run(
            [
                str(SECURITY_CLI),
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("API key is unavailable in macOS Keychain") from exc
    value = result.stdout.strip()
    if not value:
        raise ValueError("API key is empty in macOS Keychain")
    return value


def read_secret(config: Config, kind: str) -> str:
    if kind == "token":
        value = config.token_file.read_text(encoding="utf-8").strip()
    elif config.api_key_env is not None:
        value = os.environ.get(config.api_key_env, "").strip()
    elif config.api_key_file is not None:
        value = config.api_key_file.read_text(encoding="utf-8").strip()
    elif config.api_key_keychain is not None:
        value = read_keychain_secret(config.api_key_keychain)
    else:  # pragma: no cover - Config prevents this.
        value = ""
    configured_api_source = any(
        source is not None
        for source in (
            config.api_key_env,
            config.api_key_file,
            config.api_key_keychain,
        )
    )
    if not value and (kind == "token" or configured_api_source):
        raise ValueError(f"{kind} is empty")
    return value


class SessionLog:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.path = path
        self.secrets = tuple(
            sorted((secret for secret in secrets if secret), key=len, reverse=True)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)

    def redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        return value

    def write(self, event: str, **fields: Any) -> None:
        safe = self.scrub({"event": event, **fields})
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n")


class BoardAPI(Protocol):
    async def wait(self, cursors: dict[str, int]) -> dict[str, Any]: ...
    async def claim(self, board_id: str, ticket_id: str) -> dict[str, Any]: ...
    async def ticket(self, board_id: str, ticket_id: str) -> dict[str, Any]: ...
    async def work_dir(self, board_id: str) -> Path: ...
    async def renew(self, board_id: str, ticket_id: str) -> None: ...
    async def submit(
        self, board_id: str, ticket_id: str, arguments: dict[str, Any]
    ) -> None: ...
    async def release(self, board_id: str, ticket_id: str, reason: str) -> None: ...


class PursersBoardAPI:
    def __init__(self, config: Config, token: str) -> None:
        self.config = config
        self.client = BoardClient(
            config.central_url,
            token,
            wait_bridge.BOARD_ID,
            agent_name=config.agent_name,
        )
        self.registry: dict[str, Any] | None = None
        self.views: dict[str, Any] = {}

    async def __aenter__(self) -> "PursersBoardAPI":
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.client.__aexit__(*args)

    async def _boards(self) -> list[str]:
        if self.config.boards == "registry":
            self.registry = await wait_bridge._read_project_registry(self.client)
            return wait_bridge._registry_boards(self.registry)
        return list(self.config.boards)

    async def _view(self, board_id: str) -> Any:
        view = self.views.get(board_id)
        if view is None:
            view = wait_bridge._BoardView(self.client, board_id)
            await view.board_join(agent_name=self.config.agent_name)
            self.views[board_id] = view
        return view

    async def wait(self, cursors: dict[str, int]) -> dict[str, Any]:
        return await wait_bridge._wait_for_work_many(
            self.client,
            boards=await self._boards(),
            since_seq=cursors,
            timeout_s=180,
            only_mine=False,
            agent_name=self.config.agent_name,
        )

    async def claim(self, board_id: str, ticket_id: str) -> dict[str, Any]:
        result = await (await self._view(board_id))._call(
            "ticket_claim",
            {"agent_name": self.config.agent_name, "ticket_id": ticket_id},
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result

    async def ticket(self, board_id: str, ticket_id: str) -> dict[str, Any]:
        result = await (await self._view(board_id)).ticket_get(ticket_id)
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result.get("ticket", result)

    async def work_dir(self, board_id: str) -> Path:
        self.registry = self.registry or await wait_bridge._read_project_registry(
            self.client
        )
        for project in self.registry["projects"].values():
            if project["board_id"] == board_id and project["status"] == "active":
                return Path(project["work_dir"]).resolve()
        raise ValueError(f"no active project registry entry for board {board_id}")

    async def renew(self, board_id: str, ticket_id: str) -> None:
        result = await (await self._view(board_id)).lease_renew(ticket_id)
        if result.get("error"):
            raise RuntimeError(str(result["error"]))

    async def submit(
        self, board_id: str, ticket_id: str, arguments: dict[str, Any]
    ) -> None:
        result = await (await self._view(board_id))._call(
            "ticket_submit",
            {
                "agent_name": self.config.agent_name,
                "ticket_id": ticket_id,
                "summary": arguments.get("summary"),
                "files_changed": arguments.get("files_changed", []),
                "notes": arguments.get("notes"),
                "stay_active": True,
            },
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))

    async def release(self, board_id: str, ticket_id: str, reason: str) -> None:
        result = await (await self._view(board_id))._call(
            "ticket_unclaim",
            {"agent_name": self.config.agent_name, "ticket_id": ticket_id},
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))


class OpenAICompatible:
    def __init__(self, config: Config, api_key: str) -> None:
        self.config = config
        self.api_key = api_key

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": self.config.max_tokens,
            },
            separators=(",", ":"),
        ).encode("utf-8")

        def request() -> dict[str, Any]:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = (
                    "".join(AUTH_SCHEME_PARTS) + " " + self.api_key
                )
            raw = urllib.request.Request(
                self.config.base_url + "/chat/completions",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(raw, timeout=self.config.command_timeout_s) as response:
                result = json.load(response)
            return result["choices"][0]["message"]

        return await asyncio.to_thread(request)


TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "run_shell", "description": "Run a timeboxed shell command in the assigned work directory.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a bounded UTF-8 file inside the assigned work directory.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write UTF-8 text inside the assigned work directory.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "submit_work", "description": "Submit completed ticket work for independent review.", "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "files_changed": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}}, "required": ["summary", "notes"]}}},
    {"type": "function", "function": {"name": "give_up", "description": "Release the claim with a local reason so another worker can retry.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
]


def _jailed(work_dir: Path, raw_path: str) -> Path:
    root = work_dir.resolve()
    candidate = (root / raw_path).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise PermissionError("path escapes assigned work directory")
    return candidate


class Worker:
    def __init__(
        self,
        config: Config,
        board: BoardAPI,
        llm: Any,
        log: SessionLog,
        *,
        directive: str | None = None,
    ) -> None:
        self.config = config
        self.board = board
        self.llm = llm
        self.log = log
        self.directive = directive or DIRECTIVE_PATH.read_text(encoding="utf-8")
        self.stop = asyncio.Event()

    def messages(
        self, board_id: str, ticket: dict[str, Any], work_dir: Path
    ) -> list[dict[str, Any]]:
        context = {
            "board_id": board_id,
            "work_dir": str(work_dir),
            "review": "independent reviewer required; never review or merge your own work",
        }
        return [
            {"role": "system", "content": self.directive},
            {"role": "system", "content": "BOARD CONTEXT\n" + json.dumps(context, sort_keys=True)},
            {"role": "user", "content": "DYNAMIC TICKET\n" + json.dumps(ticket, sort_keys=True)},
        ]

    def _shell_argv(self, command: str) -> list[str] | None:
        if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
            return None
        protected = [self.config.token_file]
        if self.config.api_key_file is not None:
            protected.append(self.config.api_key_file)
        denies = "\n".join(
            f"(deny file-read* (literal {json.dumps(str(path))}))"
            for path in protected
        )
        profile = f"(version 1)\n(allow default)\n{denies}"
        return [str(SANDBOX_EXEC), "-p", profile, "/bin/sh", "-lc", command]

    async def _tool(
        self,
        name: str,
        args: dict[str, Any],
        work_dir: Path,
        board_id: str,
        ticket_id: str,
    ) -> tuple[str, bool]:
        if name == "read_file":
            path = _jailed(work_dir, _text(args.get("path"), "path"))
            data = await asyncio.to_thread(path.read_bytes)
            return data[:MAX_FILE_READ].decode("utf-8", errors="replace"), False
        if name == "write_file":
            path = _jailed(work_dir, _text(args.get("path"), "path"))
            content = _text(args.get("content"), "content")
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
            self.log.write("write_file", path=str(path.relative_to(work_dir)))
            return "written", False
        if name == "run_shell":
            command = _text(args.get("command"), "command")
            self.log.write("run_shell", command=command)
            shell_env = {
                key: value
                for key in SHELL_ENV_ALLOWLIST
                if key != self.config.api_key_env
                and (value := os.environ.get(key)) is not None
            }
            shell_env["HOME"] = str(work_dir)
            shell_env["TMPDIR"] = str(work_dir)
            shell_argv = self._shell_argv(command)
            process_args = shell_argv or ["/bin/sh", "-lc", command]
            process = await asyncio.create_subprocess_exec(
                *process_args,
                cwd=work_dir,
                env=shell_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async def bounded_output() -> bytes:
                assert process.stdout is not None
                output = bytearray()
                while True:
                    chunk = await process.stdout.read(4_096)
                    if not chunk:
                        await process.wait()
                        return bytes(output)
                    remaining = MAX_TOOL_OUTPUT - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        process.kill()
                        await process.wait()
                        return bytes(output) + b"\n[output truncated]"

            try:
                output = await asyncio.wait_for(
                    bounded_output(), timeout=self.config.command_timeout_s
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return "command timed out", False
            return output.decode("utf-8", errors="replace"), False
        if name == "submit_work":
            safe_args = self.log.scrub(args)
            try:
                await self.board.submit(board_id, ticket_id, safe_args)
            except Exception as exc:
                self.log.write(
                    "submit_failed", ticket_id=ticket_id, error=type(exc).__name__
                )
                await self._release(board_id, ticket_id, "submission API failure")
                return "released", True
            self.log.write("submit_work", ticket_id=ticket_id)
            return "submitted", True
        if name == "give_up":
            reason = _text(args.get("reason"), "reason")
            self.log.write("give_up", ticket_id=ticket_id, reason=reason)
            await self.board.release(board_id, ticket_id, reason)
            return "released", True
        raise ValueError(f"unknown tool: {name}")

    async def _release(self, board_id: str, ticket_id: str, reason: str) -> None:
        try:
            await self.board.release(board_id, ticket_id, reason)
            self.log.write("release", ticket_id=ticket_id, reason=reason)
        except Exception as exc:
            self.log.write(
                "release_failed", ticket_id=ticket_id, error=type(exc).__name__
            )

    async def _renew(self, board_id: str, ticket_id: str) -> None:
        try:
            while not self.stop.is_set():
                await asyncio.sleep(LEASE_INTERVAL_S)
                try:
                    await self.board.renew(board_id, ticket_id)
                except Exception as exc:
                    self.log.write(
                        "lease_renew_failed",
                        ticket_id=ticket_id,
                        error=type(exc).__name__,
                    )
        except asyncio.CancelledError:
            raise

    async def run_ticket(
        self, board_id: str, ticket: dict[str, Any], work_dir: Path
    ) -> str:
        ticket_id = str(ticket["ticket_id"])
        messages = self.messages(board_id, ticket, work_dir)
        renewal = asyncio.create_task(self._renew(board_id, ticket_id))
        try:
            for _ in range(self.config.max_iterations):
                if self.stop.is_set():
                    await self._release(board_id, ticket_id, "graceful shutdown")
                    return "released"
                message = await self.llm.complete(messages, TOOLS)
                messages.append({"role": "assistant", **message})
                calls = message.get("tool_calls") or []
                if not calls:
                    continue
                for call in calls:
                    function = call.get("function", {})
                    arguments = json.loads(function.get("arguments") or "{}")
                    try:
                        output, done = await self._tool(
                            str(function.get("name")),
                            arguments,
                            work_dir,
                            board_id,
                            ticket_id,
                        )
                    except Exception as exc:
                        output, done = f"error: {type(exc).__name__}: {exc}", False
                    output = self.log.redact(output)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": output[:MAX_TOOL_OUTPUT],
                        }
                    )
                    if done:
                        return output
            await self._release(board_id, ticket_id, "max iterations reached")
            self.log.write("max_iterations", ticket_id=ticket_id)
            return "released"
        except Exception as exc:
            self.log.write("hard_failure", ticket_id=ticket_id, error=type(exc).__name__)
            await self._release(board_id, ticket_id, "LLM or runtime hard failure")
            return "released"
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def run(self) -> None:
        cursors: dict[str, int] = {}
        while not self.stop.is_set():
            waited = await self.board.wait(cursors)
            cursors = dict(waited.get("new_seq", cursors))
            for event in waited.get("events", []):
                ticket_id = event.get("ticket_id")
                board_id = event.get("board_id")
                if not ticket_id or not board_id:
                    continue
                try:
                    await self.board.claim(board_id, ticket_id)
                except Exception:
                    continue
                try:
                    ticket = await self.board.ticket(board_id, ticket_id)
                    work_dir = await self.board.work_dir(board_id)
                    await self.run_ticket(board_id, ticket, work_dir)
                except Exception as exc:
                    self.log.write(
                        "claimed_ticket_failure",
                        ticket_id=ticket_id,
                        error=type(exc).__name__,
                    )
                    await self._release(board_id, ticket_id, "board API failure")
                break


async def async_main(config_path: Path) -> None:
    config = load_config(config_path)
    token = read_secret(config, "token")
    api_key = read_secret(config, "API key")
    log = SessionLog(config.log_file, secrets=(token, api_key))
    async with PursersBoardAPI(config, token) as board:
        worker = Worker(config, board, OpenAICompatible(config, api_key), log)
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is not None:
                loop.add_signal_handler(signum, worker.stop.set)
        await worker.run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a headless Pursers worker")
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    asyncio.run(async_main(args.config))


if __name__ == "__main__":
    main()
