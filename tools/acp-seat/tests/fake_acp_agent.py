#!/usr/bin/env python3
"""Scriptable ACP v1 agent used by the acp-seat conformance tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

JSON = dict[str, Any]


class FakeAgent:
    def __init__(self, script: JSON, *, load_session: bool = False) -> None:
        self.script = script
        self.load_session = load_session or bool(script.get("loadSession"))
        self.write_lock = asyncio.Lock()
        self.sessions: set[str] = set()
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.next_request_id = 1000

    async def run(self) -> None:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self.handle(message)
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def handle(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return
        method = message.get("method")
        if not isinstance(method, str):
            self.resolve_response(message)
            return
        params = message.get("params", {})
        request_id = message.get("id")
        if method == "session/cancel":
            session_id = params.get("sessionId")
            if isinstance(session_id, str):
                self.cancel_events.setdefault(session_id, asyncio.Event()).set()
            return
        if request_id is None:
            return
        if method == "session/prompt":
            self.spawn(self.prompt(request_id, params))
            return
        self.spawn(self.request(request_id, method, params))

    def resolve_response(self, message: JSON) -> None:
        request_id = message.get("id")
        future = self.pending.pop(request_id, None)
        if future is None:
            return
        if "error" in message:
            error = message["error"]
            future.set_exception(
                RuntimeError(
                    error.get("message", "client error")
                    if isinstance(error, dict)
                    else "client error"
                )
            )
        elif "result" in message:
            future.set_result(message["result"])
        else:
            future.set_exception(RuntimeError("client response missing result"))

    def spawn(self, awaitable: Any) -> None:
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def request(self, request_id: Any, method: str, params: JSON) -> None:
        try:
            if method == "initialize":
                selected = self.script.get(
                    "protocolVersion", params.get("protocolVersion", 1)
                )
                await self.result(
                    request_id,
                    {
                        "protocolVersion": selected,
                        "agentCapabilities": {
                            "loadSession": self.load_session,
                            "promptCapabilities": {
                                "image": False,
                                "audio": False,
                                "embeddedContext": False,
                            },
                        },
                        "agentInfo": {
                            "name": "pursers-fake-acp-agent",
                            "version": "1",
                        },
                        "authMethods": [],
                    },
                )
            elif method == "session/new":
                cwd = params.get("cwd")
                if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                    await self.error(request_id, -32602, "cwd must be absolute")
                    return
                fallback_id = f"fake-session-{len(self.sessions) + 1}"
                session_id = str(self.script.get("sessionId", fallback_id))
                self.sessions.add(session_id)
                self.cancel_events[session_id] = asyncio.Event()
                await self.result(request_id, {"sessionId": session_id})
            elif method == "session/load":
                if not self.load_session:
                    await self.error(request_id, -32601, "session/load unsupported")
                    return
                session_id = params.get("sessionId")
                if not isinstance(session_id, str):
                    await self.error(request_id, -32602, "sessionId required")
                    return
                self.sessions.add(session_id)
                self.cancel_events[session_id] = asyncio.Event()
                for update in self.script.get("loadUpdates", []):
                    await self.update(session_id, update)
                await self.result(request_id, None)
            elif method == "test/error":
                await self.error(request_id, -32042, "scripted remote error")
            else:
                await self.error(request_id, -32601, f"unknown method {method}")
        except Exception as exc:
            await self.error(request_id, -32603, str(exc))

    async def prompt(self, request_id: Any, params: JSON) -> None:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or session_id not in self.sessions:
            await self.error(request_id, -32602, "unknown session")
            return
        event = self.cancel_events.setdefault(session_id, asyncio.Event())
        event.clear()
        try:
            for action in self.script.get("promptActions", []):
                kind = action.get("type")
                if kind == "update":
                    await self.update(session_id, action["update"])
                elif kind == "permission":
                    outcome = await self.permission(session_id, action)
                    expected = action.get("expectedOutcome")
                    if expected is not None and outcome != expected:
                        await self.error(
                            request_id,
                            -32001,
                            f"permission mismatch: {outcome!r} != {expected!r}",
                        )
                        return
                    if outcome.get("outcome") == "cancelled":
                        event.set()
                elif kind == "sleep":
                    if await self.sleep_or_cancel(event, float(action["seconds"])):
                        break
                elif kind == "wait_for_cancel":
                    await event.wait()
                    break
                elif kind == "crash":
                    sys.stdout.flush()
                    sys.stderr.write(str(action.get("stderr", "scripted crash")))
                    sys.stderr.flush()
                    os._exit(int(action.get("code", 17)))
                elif kind == "raw":
                    async with self.write_lock:
                        sys.stdout.write(str(action["text"]) + "\n")
                        sys.stdout.flush()
                else:
                    await self.error(request_id, -32602, f"unknown action {kind}")
                    return
                if event.is_set():
                    break
            stop_reason = "cancelled" if event.is_set() else self.script.get(
                "stopReason", "end_turn"
            )
            await self.result(request_id, {"stopReason": stop_reason})
        except Exception as exc:
            await self.error(request_id, -32603, str(exc))

    async def permission(self, session_id: str, action: JSON) -> JSON:
        self.next_request_id += 1
        request_id = self.next_request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": action.get(
                        "toolCall", {"toolCallId": f"tool-{request_id}"}
                    ),
                    "options": action.get(
                        "options",
                        [
                            {
                                "optionId": "allow-once",
                                "name": "Allow once",
                                "kind": "allow_once",
                            },
                            {
                                "optionId": "reject-once",
                                "name": "Reject",
                                "kind": "reject_once",
                            },
                        ],
                    ),
                },
            }
        )
        response = await future
        if not isinstance(response, dict) or not isinstance(
            response.get("outcome"), dict
        ):
            raise RuntimeError("invalid permission response")
        return response["outcome"]

    @staticmethod
    async def sleep_or_cancel(event: asyncio.Event, seconds: float) -> bool:
        sleeper = asyncio.create_task(asyncio.sleep(seconds))
        cancelled = asyncio.create_task(event.wait())
        done, pending = await asyncio.wait(
            {sleeper, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return cancelled in done

    async def update(self, session_id: str, update: Any) -> None:
        await self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    async def result(self, request_id: Any, result: Any) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def error(self, request_id: Any, code: int, message: str) -> None:
        await self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    async def send(self, message: JSON) -> None:
        encoded = json.dumps(message, separators=(",", ":"))
        async with self.write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--load-session", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script = json.loads(args.script.read_text(encoding="utf-8"))
    if not isinstance(script, dict):
        raise SystemExit("script must be a JSON object")
    asyncio.run(FakeAgent(script, load_session=args.load_session).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
