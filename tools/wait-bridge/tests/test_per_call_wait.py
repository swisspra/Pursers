from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from onboard_client import BoardClientError, JoinedIdentity  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.agent_name = "env-default"
        self.identity = JoinedIdentity(
            wait_server.BOARD_ID,
            wait_server._derived_agent_id("PR-shared", "env-default"),
            "PR-shared",
            "env-default",
            "worker",
        )
        self.join_calls: list[str | None] = []
        self.catchup_calls: list[str | None] = []
        self.tickets: dict[str, dict[str, Any]] = {}
        self.catchup_error_once = False
        self.renewed: list[str] = []

    async def board_join(self, *, agent_name: str | None = None):
        self.join_calls.append(agent_name)
        selected = self.agent_name if agent_name is None else agent_name
        identity = JoinedIdentity(
            wait_server.BOARD_ID,
            wait_server._derived_agent_id("PR-shared", selected),
            "PR-shared",
            selected,
            "worker",
        )
        if agent_name is None:
            self.identity = identity
        return {
            "board_id": wait_server.BOARD_ID,
            "agent_id": identity.agent_id,
            "principal_id": identity.principal_id,
            "agent_name": identity.agent_name,
            "role": identity.role,
            "identity": identity,
        }

    async def board_catchup(self, **arguments: Any):
        name = arguments.get("agent_name")
        self.catchup_calls.append(name)
        if self.catchup_error_once:
            self.catchup_error_once = False
            raise BoardClientError(
                "agent handed off; call board_onboard or board_join before more work"
            )
        selected = self.agent_name if name is None else name
        ticket_id = f"TK-{selected}"
        agent_id = wait_server._derived_agent_id("PR-shared", selected)
        self.tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "status": "open",
            "created_by_agent_id": agent_id,
            "target_url": "pursers/tools/wait-bridge",
        }
        return {
            "events": [
                {
                    "kind": "ticket_created",
                    "ticket_id": ticket_id,
                    "status_to": "open",
                }
            ],
            "next_cursor": int(arguments["cursor"]) + 1,
            "has_more": False,
            "resync_required": False,
        }

    async def ticket_get(self, ticket_id: str):
        return {"ticket": self.tickets[ticket_id]}

    async def ticket_list(self, **_arguments: Any):
        return {"tickets": []}

    async def lease_renew(self, ticket_id: str):
        self.renewed.append(ticket_id)
        return {"lease_expires_at": "later"}


class PerCallWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_omitted_name_uses_default_without_extra_join(self) -> None:
        client = FakeClient()

        result = await wait_server._wait_for_work(
            client, since_seq=0, only_mine=True
        )

        self.assertEqual(client.join_calls, [])
        self.assertEqual(client.catchup_calls, [None])
        self.assertEqual(result["events"][0]["ticket_id"], "TK-env-default")

    async def test_explicit_name_drives_join_catchup_and_relevance(self) -> None:
        client = FakeClient()

        result = await wait_server._wait_for_work(
            client, since_seq=0, only_mine=True, agent_name="session-x"
        )

        self.assertEqual(client.join_calls, ["session-x"])
        self.assertEqual(client.catchup_calls, ["session-x"])
        self.assertEqual(result["events"][0]["ticket_id"], "TK-session-x")
        self.assertEqual(client.identity.agent_name, "env-default")

    async def test_concurrent_names_have_no_cross_talk(self) -> None:
        client = FakeClient()
        first_joined = asyncio.Event()
        second_joined = asyncio.Event()
        original_join = client.board_join

        async def interleaved_join(*, agent_name: str | None = None):
            if agent_name == "session-a":
                first_joined.set()
                await second_joined.wait()
            elif agent_name == "session-b":
                await first_joined.wait()
                second_joined.set()
            return await original_join(agent_name=agent_name)

        client.board_join = interleaved_join  # type: ignore[method-assign]

        first, second = await asyncio.gather(
            wait_server._wait_for_work(
                client, since_seq=0, only_mine=True, agent_name="session-a"
            ),
            wait_server._wait_for_work(
                client, since_seq=0, only_mine=True, agent_name="session-b"
            ),
        )

        self.assertEqual(first["events"][0]["ticket_id"], "TK-session-a")
        self.assertEqual(second["events"][0]["ticket_id"], "TK-session-b")
        self.assertCountEqual(client.catchup_calls, ["session-a", "session-b"])
        self.assertEqual(client.identity.agent_name, "env-default")

    async def test_handed_off_name_rejoins_and_retries_once(self) -> None:
        client = FakeClient()
        client.catchup_error_once = True

        result = await wait_server._wait_for_work(
            client, since_seq=0, only_mine=True, agent_name="session-x"
        )

        self.assertEqual(client.join_calls, ["session-x", "session-x"])
        self.assertEqual(client.catchup_calls, ["session-x", "session-x"])
        self.assertFalse(result["timed_out"])

    async def test_omitted_name_preserves_handoff_failure_behavior(self) -> None:
        client = FakeClient()
        client.catchup_error_once = True

        with self.assertRaises(BoardClientError):
            await wait_server._wait_for_work(
                client, since_seq=0, only_mine=True
            )

        self.assertEqual(client.join_calls, [])
        self.assertEqual(client.catchup_calls, [None])

    async def test_heartbeat_filters_substring_matches_by_exact_agent_id(self) -> None:
        client = FakeClient()
        mine = wait_server._derived_agent_id("PR-shared", "purser-codex")
        sibling = wait_server._derived_agent_id("PR-shared", "purser-codex-2")

        async def ticket_list(**_arguments: Any):
            return {
                "tickets": [
                    {
                        "ticket_id": "TK-mine",
                        "status": "claimed",
                        "claimed_by_agent_id": mine,
                    },
                    {
                        "ticket_id": "TK-sibling",
                        "status": "claimed",
                        "claimed_by_agent_id": sibling,
                    },
                ]
            }

        client.ticket_list = ticket_list  # type: ignore[method-assign]

        await wait_server._heartbeat(client, "purser-codex", mine)

        self.assertEqual(client.renewed, ["TK-mine"])


if __name__ == "__main__":
    unittest.main()
