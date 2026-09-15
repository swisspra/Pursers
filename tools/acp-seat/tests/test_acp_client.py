from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).parents[1] / "acp_client.py"
SPEC = importlib.util.spec_from_file_location("acp_client", MODULE_PATH)
assert SPEC and SPEC.loader
acp_client = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acp_client
SPEC.loader.exec_module(acp_client)

FAKE_AGENT = Path(__file__).with_name("fake_acp_agent.py")


class ACPClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clients: list[Any] = []

    async def asyncTearDown(self) -> None:
        for client in reversed(self.clients):
            await client.close()
        self.temp.cleanup()

    def client(
        self,
        script: dict[str, Any],
        *,
        policy: Any = None,
        timeout: float = 2.0,
        load_flag: bool = False,
    ) -> Any:
        script_path = self.root / f"script-{len(self.clients)}.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")
        command = [sys.executable, str(FAKE_AGENT), "--script", str(script_path)]
        if load_flag:
            command.append("--load-session")
        client = acp_client.ACPClient(
            command, permission_policy=policy, request_timeout=timeout
        )
        self.clients.append(client)
        return client

    async def initialized(self, client: Any) -> dict[str, Any]:
        await client.start()
        return await client.initialize(
            client_capabilities={
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            client_info={"name": "acp-seat-test", "version": "1"},
        )

    async def test_happy_path_streams_updates_and_stops(self) -> None:
        updates = [
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "working"},
            },
            {
                "sessionUpdate": "plan",
                "entries": [
                    {"content": "finish", "priority": "high", "status": "pending"}
                ],
            },
        ]
        client = self.client(
            {
                "sessionId": "happy-session",
                "promptActions": [
                    {"type": "update", "update": update} for update in updates
                ],
                "stopReason": "end_turn",
            }
        )

        initialized = await self.initialized(client)
        self.assertEqual(initialized["protocolVersion"], 1)
        self.assertEqual(initialized["agentInfo"]["name"], "pursers-fake-acp-agent")
        session_id = await client.new_session(self.root)
        self.assertEqual(session_id, "happy-session")
        prompt = asyncio.create_task(client.prompt(session_id, "do the work"))
        stream = client.updates()
        received = [await anext(stream), await anext(stream)]
        await stream.aclose()

        self.assertEqual([row["update"] for row in received], updates)
        self.assertEqual(await prompt, {"stopReason": "end_turn"})

    async def test_update_drain_waits_for_consumer_acknowledgement(self) -> None:
        update = {"sessionUpdate": "plan", "entries": []}
        client = self.client(
            {
                "promptActions": [{"type": "update", "update": update}],
                "stopReason": "end_turn",
            }
        )

        await self.initialized(client)
        session_id = await client.new_session(self.root)
        prompt = asyncio.create_task(client.prompt(session_id, "do the work"))
        received = await client.next_update()
        await prompt
        drained = asyncio.create_task(client.wait_for_updates())
        await asyncio.sleep(0)
        self.assertFalse(drained.done())

        client.acknowledge_update()
        await drained
        self.assertEqual(received["update"], update)

    async def test_permission_policy_selects_allow_option(self) -> None:
        seen: list[dict[str, Any]] = []

        def policy(params: dict[str, Any]) -> str:
            seen.append(params)
            return "allow-once"

        client = self.client(
            {
                "promptActions": [
                    {
                        "type": "permission",
                        "toolCall": {
                            "toolCallId": "edit-1",
                            "title": "Edit file",
                            "kind": "edit",
                        },
                        "expectedOutcome": {
                            "outcome": "selected",
                            "optionId": "allow-once",
                        },
                    }
                ]
            },
            policy=policy,
        )

        await self.initialized(client)
        session_id = await client.new_session(self.root)
        result = await client.prompt(session_id, "edit")

        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual(seen[0]["toolCall"]["toolCallId"], "edit-1")

    async def test_permission_policy_can_deny(self) -> None:
        client = self.client(
            {
                "promptActions": [
                    {
                        "type": "permission",
                        "expectedOutcome": {
                            "outcome": "selected",
                            "optionId": "reject-once",
                        },
                    }
                ]
            },
            policy=lambda _params: {
                "outcome": "selected",
                "optionId": "reject-once",
            },
        )

        await self.initialized(client)
        session_id = await client.new_session(self.root)
        self.assertEqual(
            await client.prompt(session_id, "try it"), {"stopReason": "end_turn"}
        )

    async def test_missing_permission_policy_cancels_request(self) -> None:
        client = self.client(
            {
                "promptActions": [
                    {
                        "type": "permission",
                        "expectedOutcome": {"outcome": "cancelled"},
                    }
                ]
            }
        )

        await self.initialized(client)
        session_id = await client.new_session(self.root)
        self.assertEqual(
            await client.prompt(session_id, "no policy"),
            {"stopReason": "cancelled"},
        )

    async def test_cancel_resolves_pending_permission_as_cancelled(self) -> None:
        policy_started = asyncio.Event()

        async def policy(_params: dict[str, Any]) -> str:
            policy_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = self.client(
            {
                "promptActions": [
                    {
                        "type": "permission",
                        "expectedOutcome": {"outcome": "cancelled"},
                    },
                    {"type": "wait_for_cancel"},
                ]
            },
            policy=policy,
        )

        await self.initialized(client)
        session_id = await client.new_session(self.root)
        prompt = asyncio.create_task(client.prompt(session_id, "long operation"))
        await asyncio.wait_for(policy_started.wait(), 1.0)
        await client.cancel(session_id)

        self.assertEqual(await prompt, {"stopReason": "cancelled"})

    async def test_cancelling_prompt_task_notifies_agent(self) -> None:
        client = self.client(
            {"promptActions": [{"type": "wait_for_cancel"}]}
        )
        await self.initialized(client)
        session_id = await client.new_session(self.root)
        prompt = asyncio.create_task(client.prompt(session_id, "cancel me"))
        await asyncio.sleep(0.02)

        prompt.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await prompt
        await asyncio.sleep(0.05)
        self.assertIsNone(client.process.returncode)

    async def test_load_session_replays_updates_when_advertised(self) -> None:
        replay = {
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": "earlier prompt"},
        }
        client = self.client({"loadUpdates": [replay]}, load_flag=True)

        initialized = await self.initialized(client)
        self.assertIs(initialized["agentCapabilities"]["loadSession"], True)
        await client.load_session("existing-session", self.root)

        self.assertEqual((await client.next_update(timeout=1))["update"], replay)

    async def test_load_session_rejected_without_capability(self) -> None:
        client = self.client({})
        await self.initialized(client)

        with self.assertRaisesRegex(
            acp_client.ACPProtocolError, "did not advertise"
        ):
            await client.load_session("missing", self.root)

    async def test_new_session_rejects_relative_cwd(self) -> None:
        client = self.client({})
        await self.initialized(client)

        with self.assertRaisesRegex(ValueError, "must be absolute"):
            await client.new_session("relative/path")

    async def test_session_methods_require_initialize_and_known_session(self) -> None:
        client = self.client({})
        await client.start()

        with self.assertRaisesRegex(acp_client.ACPProtocolError, "not initialized"):
            await client.new_session(self.root)
        await client.initialize()
        with self.assertRaisesRegex(RuntimeError, "already initialized"):
            await client.initialize()
        with self.assertRaisesRegex(ValueError, "unknown ACP session"):
            await client.prompt("not-created", "prompt")
        with self.assertRaisesRegex(ValueError, "unknown ACP session"):
            await client.cancel("not-created")

    async def test_initialize_rejects_different_protocol_major(self) -> None:
        client = self.client({"protocolVersion": 2})
        await client.start()

        with self.assertRaisesRegex(acp_client.ACPProtocolError, "unsupported"):
            await client.initialize(protocol_version=1)

    async def test_prompt_timeout_ignores_late_response(self) -> None:
        client = self.client(
            {"promptActions": [{"type": "sleep", "seconds": 0.15}]}
        )
        await self.initialized(client)
        session_id = await client.new_session(self.root)

        with self.assertRaisesRegex(acp_client.ACPTimeoutError, "timed out"):
            await client.prompt(session_id, "slow", timeout=0.03)
        await asyncio.sleep(0.2)
        self.assertIsNone(client.process.returncode)

    async def test_agent_crash_fails_pending_prompt(self) -> None:
        client = self.client(
            {
                "promptActions": [
                    {"type": "crash", "code": 23, "stderr": "crash-marker"}
                ]
            }
        )
        await self.initialized(client)
        session_id = await client.new_session(self.root)

        with self.assertRaisesRegex(acp_client.ACPProcessError, "code 23"):
            await client.prompt(session_id, "crash")

    async def test_malformed_agent_output_is_protocol_error(self) -> None:
        client = self.client(
            {"promptActions": [{"type": "raw", "text": "{not-json"}]}
        )
        await self.initialized(client)
        session_id = await client.new_session(self.root)

        with self.assertRaisesRegex(acp_client.ACPProtocolError, "invalid JSON"):
            await client.prompt(session_id, "malformed")

    async def test_unoffered_permission_choice_becomes_remote_error(self) -> None:
        client = self.client(
            {"promptActions": [{"type": "permission"}]},
            policy=lambda _params: "not-offered",
        )
        await self.initialized(client)
        session_id = await client.new_session(self.root)

        with self.assertRaises(acp_client.ACPRemoteError):
            await client.prompt(session_id, "bad policy")

    async def test_next_update_timeout_has_specific_error(self) -> None:
        client = self.client({})
        await self.initialized(client)

        with self.assertRaises(acp_client.ACPTimeoutError):
            await client.next_update(timeout=0.01)


if __name__ == "__main__":
    unittest.main()
