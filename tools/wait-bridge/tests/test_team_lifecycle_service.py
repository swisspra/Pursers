from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pursers_client import BoardClientError
from team_lifecycle import CAPABILITIES, STATE_KEY, TeamLifecycleService, create_sidecar_client


class FakeClient:
    agent_name = "home-groups"

    def __init__(self) -> None:
        self.raw = None
        self.updates = []
        self.agents = [
            {"agent_id": "AI-worker-a", "agent_name": "worker-a", "role": "worker", "lifecycle_status": "active", "capabilities": {"can_work": True, "can_review": False}},
            {"agent_id": "AI-worker-b", "agent_name": "worker-b", "role": "worker", "lifecycle_status": "retired", "capabilities": {"can_work": True, "can_review": False}},
            {"agent_id": "AI-sidecar", "agent_name": "home-sidecar", "role": "worker", "lifecycle_status": "active", "capabilities": {"can_work": False, "can_review": False}},
        ]

    async def board_state_get(self, key):
        assert key == STATE_KEY
        if self.raw is None:
            raise BoardClientError("state key not found")
        return {"state": {"value": self.raw}}

    async def board_state_update(self, key, value, *, expected_sha256=None):
        assert key == STATE_KEY
        if expected_sha256 is not None:
            assert expected_sha256 == hashlib.sha256(self.raw.encode()).hexdigest()
        self.raw = value
        self.updates.append(expected_sha256)
        return {"ok": True}

    async def board_snapshot(self, **kwargs):
        assert kwargs == {"limit": 1_000, "max_bytes": 300_000, "include_retired": True}
        return {"agents": list(self.agents)}


class TeamLifecycleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_view_update_remove_and_recovery(self) -> None:
        client = FakeClient()
        service = TeamLifecycleService(client, "board-a")
        status = await service.dispatch("status", {"board": "board-a"})
        self.assertTrue(status["ok"])
        self.assertEqual(client.updates, [None])

        created = await service.dispatch("create", {
            "board": "board-a", "expected_revision": 0, "name": "Delivery",
            "member_agent_ids": ["AI-worker-a", "AI-worker-b"],
        })
        self.assertTrue(created["ok"] and created["revision"] == 1)
        group = created["groups"][0]
        self.assertEqual([member["lifecycle_status"] for member in group["members"]], ["active", "retired"])
        self.assertNotIn("AI-sidecar", {agent["agent_id"] for agent in created["agents"]})
        self.assertIsNotNone(client.updates[1])

        restarted = TeamLifecycleService(client, "board-a")
        viewed = await restarted.dispatch("list", {"board": "board-a"})
        self.assertEqual(viewed["groups"][0]["group_id"], group["group_id"])
        client.agents.pop(1)
        viewed = await restarted.dispatch("list", {"board": "board-a"})
        self.assertFalse(viewed["groups"][0]["members"][1]["present"])

        conflict = await restarted.dispatch("update", {
            "board": "board-a", "group_id": group["group_id"], "expected_revision": 0,
            "name": "Delivery 2", "member_agent_ids": ["AI-worker-a"],
        })
        self.assertEqual(conflict["error"]["code"], "conflict")
        updated = await restarted.dispatch("update", {
            "board": "board-a", "group_id": group["group_id"], "expected_revision": 1,
            "name": "Delivery 2", "member_agent_ids": ["AI-worker-a"],
        })
        self.assertEqual((updated["revision"], updated["groups"][0]["name"]), (2, "Delivery 2"))
        removed = await restarted.dispatch("remove", {
            "board": "board-a", "group_id": group["group_id"], "expected_revision": 2,
        })
        self.assertEqual((removed["revision"], removed["groups"]), (3, []))
        self.assertEqual(json.loads(client.raw), {"groups": [], "revision": 3, "schema_version": 1})

    async def test_board_member_and_name_isolation(self) -> None:
        service = TeamLifecycleService(FakeClient(), "board-a")
        mismatch = await service.dispatch("list", {"board": "board-b"})
        self.assertEqual(mismatch["error"]["code"], "board_mismatch")
        missing = await service.dispatch("create", {
            "board": "board-a", "expected_revision": 0, "name": "Delivery",
            "member_agent_ids": ["AI-other-board"],
        })
        self.assertEqual(missing["error"]["code"], "member_not_found")
        first = await service.dispatch("create", {
            "board": "board-a", "expected_revision": 0, "name": "Delivery",
            "member_agent_ids": ["AI-worker-a"],
        })
        duplicate = await service.dispatch("create", {
            "board": "board-a", "expected_revision": 1, "name": " delivery ",
            "member_agent_ids": ["AI-worker-a"],
        })
        self.assertTrue(first["ok"])
        self.assertEqual(duplicate["error"]["code"], "invalid_input")


def test_sidecar_uses_only_supported_inert_capabilities() -> None:
    calls = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    create_sidecar_client({"u": "http://central.test/mcp", "t": "token", "r": "worker"}, "board-a", factory)
    assert calls[0][1]["capabilities"] == CAPABILITIES == {"can_work": False, "can_review": False}
    assert calls[0][1]["allow_takeover"] is True
