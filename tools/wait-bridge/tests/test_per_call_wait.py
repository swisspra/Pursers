from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from pursers_client import (  # noqa: E402
    REVIEW_LEASE_EXPIRED,
    REVIEW_LEASE_RELEASED,
    BoardClientError,
    JoinedIdentity,
)
import pursers_wait_server as wait_server  # noqa: E402


class FakeClient:
    def __init__(self, role: str = "worker") -> None:
        self.agent_name = "env-default"
        self.role = role
        self.identity = JoinedIdentity(
            wait_server.BOARD_ID,
            wait_server._derived_agent_id("PR-shared", "env-default"),
            "PR-shared",
            "env-default",
            role,
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
            self.role,
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
        return {"tickets": list(self.tickets.values())}

    async def lease_renew(self, ticket_id: str):
        self.renewed.append(ticket_id)
        return {"lease_expires_at": "later"}


class PerCallWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        wait_server._BACKLOG_SEEN.clear()

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

    async def test_lease_snapshot_filters_substring_matches_by_exact_agent_id(self) -> None:
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

        held: dict[str, float] = {}
        await wait_server._scan_open_backlog(
            client,
            mine,
            True,
            None,
            held,
        )
        await wait_server._renew_due_leases(
            client,
            held,
            {ticket_id: 0.0 for ticket_id in held},
            1.0,
        )

        self.assertEqual(client.renewed, ["TK-mine"])

    async def test_auto_reviewer_ignores_open_backlog_and_times_out(self) -> None:
        client = FakeClient(role="reviewer")

        async def empty_catchup(**arguments: Any):
            return {
                "events": [],
                "next_cursor": int(arguments["cursor"]),
                "has_more": False,
                "resync_required": False,
            }

        async def open_backlog(**_arguments: Any):
            return {"tickets": [{"ticket_id": "TK-open", "status": "open"}]}

        client.board_catchup = empty_catchup  # type: ignore[method-assign]
        client.ticket_list = open_backlog  # type: ignore[method-assign]
        with (
            patch.object(wait_server, "WAIT_MODE", "poll"),
            patch.object(wait_server, "clamp_timeout", return_value=0.03),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
        ):
            result = await wait_server._wait_for_work(
                client, timeout_s=1, only_mine=False
            )

        self.assertTrue(result["timed_out"])
        self.assertEqual(result["reason"], "timeout")

    async def test_worker_backlog_is_surfaced_once_until_journal_change(self) -> None:
        client = FakeClient()
        ticket = {
            "ticket_id": "TK-once",
            "status": "open",
            "updated_at": "2026-09-04T12:00:00Z",
        }

        async def ticket_list(**_arguments: Any):
            return {"tickets": [ticket]}

        client.ticket_list = ticket_list  # type: ignore[method-assign]
        first = await wait_server._scan_open_backlog(
            client, client.identity.agent_id, False, None
        )
        second = await wait_server._scan_open_backlog(
            client, client.identity.agent_id, False, None
        )
        wait_server._forget_backlog_for_events(
            wait_server.BOARD_ID,
            [{"kind": "ticket_status_changed", "ticket_id": "TK-once"}],
        )
        third = await wait_server._scan_open_backlog(
            client, client.identity.agent_id, False, None
        )

        self.assertEqual([event["ticket_id"] for event in first], ["TK-once"])
        self.assertEqual(second, [])
        self.assertEqual([event["ticket_id"] for event in third], ["TK-once"])

    async def test_submitted_backlog_wakes_reviewer(self) -> None:
        client = FakeClient(role="reviewer")
        list_calls: list[dict[str, Any]] = []

        async def empty_catchup(**arguments: Any):
            return {
                "events": [],
                "next_cursor": int(arguments["cursor"]),
                "has_more": False,
                "resync_required": False,
            }

        async def submitted_backlog(**arguments: Any):
            list_calls.append(arguments)
            return {
                "tickets": [
                    {
                        "ticket_id": "TK-submitted",
                        "status": "submitted",
                        "review_state": "unclaimed",
                    }
                ]
            }

        client.board_catchup = empty_catchup  # type: ignore[method-assign]
        client.ticket_list = submitted_backlog  # type: ignore[method-assign]
        result = await wait_server._wait_for_work(
            client, timeout_s=1, only_mine=True
        )

        self.assertFalse(result["timed_out"])
        self.assertEqual(result["reason"], "backlog")
        self.assertEqual(result["events"][0]["ticket_id"], "TK-submitted")
        self.assertEqual(
            list_calls,
            [{"include_closed": False, "limit": 500, "status": "submitted"}],
        )

    async def test_release_and_expiry_events_wake_waiting_reviewer(self) -> None:
        for index, kind in enumerate(
            (REVIEW_LEASE_RELEASED, REVIEW_LEASE_EXPIRED), start=1
        ):
            with self.subTest(kind=kind):
                client = FakeClient(role="reviewer")

                async def lease_event_catchup(
                    **arguments: Any,
                ) -> dict[str, Any]:
                    return {
                        "events": [{"kind": kind, "ticket_id": "TK-review"}],
                        "next_cursor": int(arguments["cursor"]) + index,
                        "has_more": False,
                        "resync_required": False,
                    }

                async def no_backlog(**_arguments: Any) -> dict[str, Any]:
                    return {"tickets": []}

                client.board_catchup = lease_event_catchup  # type: ignore[method-assign]
                client.ticket_list = no_backlog  # type: ignore[method-assign]
                result = await wait_server._wait_for_work(
                    client, timeout_s=1, only_mine=False
                )

                self.assertFalse(result["timed_out"])
                self.assertEqual(result["reason"], "journal")
                self.assertEqual(result["events"][0]["kind"], kind)

    async def test_wait_for_override_requires_reviewer_authorization(self) -> None:
        worker = FakeClient()
        with self.assertRaisesRegex(wait_server.ToolError, "board:review"):
            await wait_server._wait_for_work(worker, wait_for="submitted")

        reviewer = FakeClient(role="reviewer")
        result = await wait_server._wait_for_work(
            reviewer, wait_for="claimable", only_mine=True
        )
        self.assertEqual(result["events"][0]["status_to"], "open")

    async def test_dispatch_offer_sets_offer_reason_and_payload(self) -> None:
        client = FakeClient()
        mine = client.identity.agent_id
        client.tickets["TK-offer"] = {
            "ticket_id": "TK-offer",
            "status": "open",
            "target_url": "pursers/tools/wait-bridge",
            "dispatch_state": {"state": "offered"},
            "work_offer": {"agent_id": mine, "expires_at": "later"},
            "tier": 1,
            "skills_required": ["python"],
        }

        async def offered(**arguments: Any) -> dict[str, Any]:
            return {
                "events": [
                    {
                        "kind": "ticket_created",
                        "ticket_id": "TK-offer",
                        "status_to": "open",
                    },
                    {
                        "kind": "ticket_offered",
                        "ticket_id": "TK-offer",
                        "offered_agent_id": mine,
                    },
                ],
                "next_cursor": int(arguments["cursor"]) + 2,
                "has_more": False,
                "resync_required": False,
            }

        client.board_catchup = offered  # type: ignore[method-assign]
        result = await wait_server._wait_for_work(
            client, timeout_s=1, only_mine=False
        )

        self.assertEqual(result["reason"], "offer")
        self.assertEqual(
            result["events"][0]["offer"],
            {
                "ticket_id": "TK-offer",
                "board_id": "pursers",
                "expires_at": "later",
                "tier": 1,
                "skills_required": ["python"],
            },
        )

    async def test_other_seat_offer_read_failure_times_out_for_worker_and_reviewer(
        self,
    ) -> None:
        for role, kind, wait_for in (
            ("worker", "ticket_offered", "claimable"),
            ("reviewer", "review_offered", "submitted"),
        ):
            with self.subTest(role=role):
                client = FakeClient(role=role)
                returned = False

                async def catchup(**arguments: Any) -> dict[str, Any]:
                    nonlocal returned
                    events = []
                    if not returned:
                        returned = True
                        events = [{
                            "kind": kind,
                            "ticket_id": f"TK-other-{role}",
                            "offered_agent_id": "AI-other-seat",
                            "recipient_identities": ["AI-other-seat"],
                        }]
                    return {
                        "events": events,
                        "next_cursor": int(arguments["cursor"]) + len(events),
                        "has_more": False,
                        "resync_required": False,
                    }

                async def failed_get(_ticket_id: str) -> dict[str, Any]:
                    raise BoardClientError("transient read failure")

                client.board_catchup = catchup  # type: ignore[method-assign]
                client.ticket_get = failed_get  # type: ignore[method-assign]
                with (
                    patch.object(wait_server, "WAIT_MODE", "poll"),
                    patch.object(wait_server, "clamp_timeout", return_value=0.03),
                    patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
                ):
                    result = await wait_server._wait_for_work(
                        client,
                        timeout_s=1,
                        only_mine=False,
                        wait_for=wait_for,
                    )

                self.assertTrue(result["timed_out"])
                self.assertEqual(result["events"], [])

    def test_capability_environment_is_explicit_and_legacy_is_absent(self) -> None:
        names = (
            "PURSERS_TIER_MAX", "PURSERS_SKILLS", "PURSERS_CAN_REVIEW",
            "PURSERS_CAN_WORK", "PURSERS_MODEL", "PURSERS_PROVIDER",
        )
        with patch.dict(os.environ, {name: "" for name in names}, clear=False):
            self.assertIsNone(wait_server._seat_capabilities())
        with patch.dict(
            os.environ,
            {
                "PURSERS_TIER_MAX": "1",
                "PURSERS_SKILLS": "docs,python,docs",
                "PURSERS_CAN_REVIEW": "false",
                "PURSERS_CAN_WORK": "true",
                "PURSERS_MODEL": "gpt-test",
                "PURSERS_PROVIDER": "openai",
                "PURSERS_HOST": "codex",
            },
            clear=False,
        ):
            self.assertEqual(
                wait_server._seat_capabilities(),
                {
                    "host": "codex",
                    "max_parallel": 1,
                    "tier_max": 1,
                    "skills": ["docs", "python"],
                    "can_review": False,
                    "can_work": True,
                    "model": "gpt-test",
                    "provider": "openai",
                },
            )

    def test_auto_wait_rejects_non_queue_roles(self) -> None:
        for role in ("coordinator", "orchestrator"):
            with self.subTest(role=role), self.assertRaisesRegex(
                wait_server.ToolError, "only to worker or reviewer"
            ):
                wait_server._resolve_wait_for("auto", role)


if __name__ == "__main__":
    unittest.main()
