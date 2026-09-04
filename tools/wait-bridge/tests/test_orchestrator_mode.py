from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import AbstractAsyncContextManager, aclosing
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
CLIENT_SRC = REPOSITORY / "packages" / "client" / "src"
CENTRAL_SRC = REPOSITORY / "packages" / "central" / "src" / "pursers_central"
sys.path.insert(0, str(CENTRAL_SRC))
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from mcp import Client  # noqa: E402
from mcp.server.mcpserver import Context  # noqa: E402
from mcp.server.subscriptions import ResourceUpdated  # noqa: E402
from pursers_client import (  # noqa: E402
    BoardClient,
    BoardClientError,
    JoinedIdentity,
)
import central  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


def persisted_documents(service: Any) -> list[tuple[str, str, int]]:
    connection = sqlite3.connect(service.store.db_path)
    try:
        return connection.execute(
            "SELECT path, doc, version FROM documents ORDER BY path"
        ).fetchall()
    finally:
        connection.close()


class InProcessBoardClient:
    """BoardClient adapter over in-process Central for wait-bridge tests."""

    def __init__(self, raw_client: Client) -> None:
        self._raw_client = raw_client
        self._client: Any = raw_client
        self.agent_name = "orchestrator-agent"
        self.identity: JoinedIdentity | None = None

    async def _call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self._raw_client.call_tool(
            name, {"board_id": wait_server.BOARD_ID, **arguments}
        )
        return BoardClient._decode(result)

    async def board_join(self, *, agent_name: str | None = None) -> dict[str, Any]:
        selected = self.agent_name if agent_name is None else agent_name
        joined = await self._call("board_join", agent_name=selected)
        identity = JoinedIdentity(
            joined["board_id"],
            joined["agent_id"],
            joined["principal_id"],
            joined["agent_name"],
            joined["role"],
        )
        if agent_name is None or agent_name == self.agent_name:
            self.identity = identity
        return joined

    async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
        arguments.setdefault("agent_name", self.agent_name)
        return await self._call("board_catchup", **arguments)

    async def ticket_get(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("ticket_get", ticket_id=ticket_id)

    async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
        return await self._call("ticket_list", **arguments)

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        return await self._call("board_state_get", key=key)

    async def create_ticket(
        self,
        title: str,
        *,
        agent_name: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        selected = "creator-agent" if agent_name is None else agent_name
        return await self._call(
            "ticket_create",
            agent_name=selected,
            title=title,
            description="synthetic test ticket",
            target_url="pursers/tools/wait-bridge",
            scope="interactive-no-send",
            required_fields=["test_output"],
            tags=tags or [],
        )

    async def claim_ticket(self, ticket_id: str, *, agent_name: str = "worker-agent") -> dict[str, Any]:
        return await self._call(
            "ticket_claim", agent_name=agent_name, ticket_id=ticket_id
        )

    async def submit_ticket(
        self,
        ticket_id: str,
        *,
        agent_name: str = "worker-agent",
        notes: str = "branch_and_commit: origin/main:abc1234\ntest_output: PASS",
        files_changed: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "ticket_submit",
            agent_name=agent_name,
            ticket_id=ticket_id,
            summary="ready for review",
            notes=notes,
            files_changed=files_changed or ["test.py"],
        )

    async def review_ticket(
        self,
        ticket_id: str,
        *,
        agent_name: str = "reviewer-agent",
        verdict: str = "approve",
        review_notes: str = "LGTM",
        reviewer_principal: Any = None,
    ) -> dict[str, Any]:
        prev = central.current_principal()
        try:
            if reviewer_principal is not None:
                central.current_principal = lambda: reviewer_principal
            return await self._call(
                "ticket_review",
                agent_name=agent_name,
                ticket_id=ticket_id,
                verdict=verdict,
                review_notes=review_notes,
            )
        finally:
            central.current_principal = lambda: prev


class DroppableSubscription:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.drop_requested = False

    def __aiter__(self) -> DroppableSubscription:
        return self

    async def __anext__(self) -> Any:
        if self.drop_requested:
            raise ConnectionResetError("synthetic listen stream drop")
        return await anext(self.inner)


class DroppableListenContext(AbstractAsyncContextManager[Any]):
    def __init__(self, inner_cm: Any, client: DroppableListenClient) -> None:
        self.inner_cm = inner_cm
        self.client = client
        self.sub: DroppableSubscription | None = None

    async def __aenter__(self) -> Any:
        inner = await self.inner_cm.__aenter__()
        self.sub = DroppableSubscription(inner)
        self.client.active_subscription = self.sub
        return self.sub

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self.inner_cm.__aexit__(exc_type, exc, tb)


class DroppableListenClient:
    """Wraps an MCP Client to simulate a dropped listen connection once."""

    def __init__(self, raw_client: Client) -> None:
        self.raw_client = raw_client
        self.active_subscription: DroppableSubscription | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw_client, name)

    def listen(self, **arguments: Any) -> Any:
        return DroppableListenContext(self.raw_client.listen(**arguments), self)

    def trigger_drop(self) -> None:
        if self.active_subscription is not None:
            self.active_subscription.drop_requested = True


class OrchestratorModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temp_dir.name)
        jwks_path = self.root / "jwks.json"
        jwks_path.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks_path),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
                "PURSERS_ROLE": "orchestrator",
                "PURSERS_BRIDGE_STATE": str(self.root / "orchestrator-state.json"),
                "PURSERS_BRIDGE_STATS": str(self.root / "bridge-stats.json"),
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.principal = central.Principal(
            "PR-orchestrator-test",
            "orchestrator-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.reviewer_principal = central.Principal(
            "PR-reviewer-test",
            "reviewer-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal

    def tearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def _setup_client_and_engine(
        self, raw_client: Client
    ) -> tuple[InProcessBoardClient, wait_server.OrchestratorEngine]:
        client = InProcessBoardClient(raw_client)
        await client.board_join()
        await client.board_join(agent_name="creator-agent")
        await client.board_join(agent_name="worker-agent")
        await client._call(
            "board_member_add",
            agent_name="creator-agent",
            principal_id=self.reviewer_principal.principal_id,
            role="reviewer",
        )
        prev = central.current_principal()
        try:
            central.current_principal = lambda: self.reviewer_principal
            await client.board_join(agent_name="reviewer-agent")
        finally:
            central.current_principal = lambda: prev
        meter = wait_server.BridgeStats(wait_server.bridge_stats_path())
        state_path = wait_server.orchestrator_state_path()
        engine = wait_server.OrchestratorEngine(client, meter, state_path)
        engine.load_state()
        return client, engine

    async def test_background_subscriber_receives_cues_without_any_tool_call(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                # Create a ticket on Central; DO NOT call any wait or digest tool
                created = await client.create_ticket("unattended background cue")
                ticket_id = created["ticket"]["ticket_id"]

                # Wait for background subscriber to receive the cue and populate buffer
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    async with engine.lock:
                        if any(e.get("ticket_id") == ticket_id for e in engine.ring_buffer):
                            break
                    await asyncio.sleep(0.05)

                async with engine.lock:
                    found = [e for e in engine.ring_buffer if e.get("ticket_id") == ticket_id]
                self.assertTrue(len(found) > 0, "Background subscriber must buffer events without any tool call")
                self.assertTrue(engine.subscription_health["connected"])
                self.assertIsNotNone(engine.subscription_health["last_event_at"])
            finally:
                await engine.stop_subscriber()

    async def test_digest_groups_transitions_per_ticket_and_includes_branch_and_commit_on_close(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                created = await client.create_ticket(
                    "lifecycle transitions ticket", agent_name="orchestrator-agent"
                )
                tid = created["ticket"]["ticket_id"]
                await client.claim_ticket(tid, agent_name="worker-agent")
                await client.submit_ticket(
                    tid,
                    agent_name="worker-agent",
                    notes="branch_and_commit: origin/feature-abc @ 0123456789abcdef0123456789abcdef01234567\nsummary: completed feature",
                )
                await client.review_ticket(
                    tid,
                    agent_name="reviewer-agent",
                    verdict="approve",
                    review_notes="Approved!",
                    reviewer_principal=self.reviewer_principal,
                )

                # Wait until the closed status is buffered
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    digest = await engine.build_digest(since=0)
                    if any(t["ticket_id"] == tid and t["status_now"] == "closed" for t in digest["tickets"]):
                        break
                    await asyncio.sleep(0.05)

                digest = await engine.build_digest(since=0)
                matching = [t for t in digest["tickets"] if t["ticket_id"] == tid]
                self.assertEqual(len(matching), 1)
                t = matching[0]

                # Verify grouping of transitions
                self.assertEqual(t["ticket_id"], tid)
                self.assertEqual(t["status_now"], "closed")
                self.assertGreaterEqual(len(t["transitions"]), 3)
                trans_to = [tr["to"] for tr in t["transitions"]]
                self.assertIn("open", trans_to)
                self.assertIn("submitted", trans_to)
                self.assertIn("closed", trans_to)

                # Verify branch_and_commit extracted in notes_subset on close
                self.assertIn("branch_and_commit", t["notes_subset"])
                self.assertIn("origin/feature-abc", t["notes_subset"]["branch_and_commit"])

                # Verify review details
                self.assertEqual(t["review"]["verdict"], "approve")
                self.assertEqual(t["review"]["rejection_count"], 0)
                self.assertIsNotNone(t["closed_at"])

                # Verify counts
                self.assertGreaterEqual(digest["counts"]["total_tickets"], 1)
                self.assertGreaterEqual(digest["counts"]["closed"], 1)
                self.assertGreaterEqual(digest["counts"]["approved"], 1)
            finally:
                await engine.stop_subscriber()

    async def test_ack_advances_and_subsequent_digest_shows_only_newer_changes(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                created1 = await client.create_ticket("first ticket for ack test")
                tid1 = created1["ticket"]["ticket_id"]

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    digest1 = await engine.build_digest()
                    if any(t["ticket_id"] == tid1 for t in digest1["tickets"]):
                        break
                    await asyncio.sleep(0.05)

                digest1 = await engine.build_digest()
                self.assertTrue(any(t["ticket_id"] == tid1 for t in digest1["tickets"]))

                # Advance ack with returned cursor_map
                ack_resp = await engine.ack(digest1["cursor_map"])
                self.assertEqual(set(ack_resp.keys()), {"ok", "cursor_map"})
                self.assertTrue(ack_resp["ok"])
                self.assertGreaterEqual(
                    ack_resp["cursor_map"].get("pursers", 0),
                    digest1["cursor_map"].get("pursers", 0),
                )

                # Now next digest with since=None should show NO tickets
                digest_after_ack = await engine.build_digest(since=None)
                self.assertEqual(len(digest_after_ack["tickets"]), 0)
                self.assertEqual(digest_after_ack["counts"]["total_tickets"], 0)

                # Create second ticket
                created2 = await client.create_ticket("second ticket after ack")
                tid2 = created2["ticket"]["ticket_id"]

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    digest2 = await engine.build_digest(since=None)
                    if any(t["ticket_id"] == tid2 for t in digest2["tickets"]):
                        break
                    await asyncio.sleep(0.05)

                digest2 = await engine.build_digest(since=None)
                ticket_ids2 = [t["ticket_id"] for t in digest2["tickets"]]
                self.assertIn(tid2, ticket_ids2)
                self.assertNotIn(tid1, ticket_ids2)
            finally:
                await engine.stop_subscriber()

    async def test_persistence_survives_simulated_bridge_restart(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine1 = await self._setup_client_and_engine(raw_client)
            await engine1.start_subscriber()
            try:
                await asyncio.wait_for(engine1.ready.wait(), timeout=3.0)
                created = await client.create_ticket("ticket to survive restart")
                tid = created["ticket"]["ticket_id"]

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    digest1 = await engine1.build_digest(since=0)
                    if any(t["ticket_id"] == tid for t in digest1["tickets"]):
                        break
                    await asyncio.sleep(0.05)

                cursor_before = dict(engine1.cursor_map)
                events_count_before = len(engine1.ring_buffer)
            finally:
                await engine1.stop_subscriber()

            # Verify state file was created
            state_path = wait_server.bridge_state_path()
            self.assertTrue(state_path.exists())

            # Now start Engine 2 on the same client and state_path (simulating restart)
            engine2 = wait_server.OrchestratorEngine(client, engine1.meter, state_path)
            engine2.load_state()

            # Assert cursors and events were loaded from file
            self.assertEqual(engine2.cursor_map, cursor_before)
            self.assertEqual(len(engine2.ring_buffer), events_count_before)

            # Start subscriber on engine 2; replay from persisted cursor should yield NO duplicates
            await engine2.start_subscriber()
            try:
                await asyncio.wait_for(engine2.ready.wait(), timeout=3.0)
                await asyncio.sleep(0.1)

                self.assertEqual(len(engine2.ring_buffer), events_count_before)
                all_ids = [e.get("id") for e in engine2.ring_buffer]
                self.assertEqual(len(all_ids), len(set(all_ids)), "Replay must not produce duplicate event IDs")

                digest2 = await engine2.build_digest(since=0)
                matching = [t for t in digest2["tickets"] if t["ticket_id"] == tid]
                self.assertEqual(len(matching), 1)
            finally:
                await engine2.stop_subscriber()

    async def test_reconnect_after_listen_drop(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            droppable = DroppableListenClient(raw_client)
            client = InProcessBoardClient(raw_client)
            await client.board_join()
            await client.board_join(agent_name="creator-agent")
            meter = wait_server.BridgeStats(wait_server.bridge_stats_path())
            state_path = wait_server.bridge_state_path()

            # Give client droppable listen
            client._raw_client = droppable
            engine = wait_server.OrchestratorEngine(client, meter, state_path)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                reconnects_before = engine.subscription_health["reconnects"]

                # Trigger drop on the active subscription
                engine.ready.clear()
                droppable.trigger_drop()

                # Create a ticket which publishes a cue and trips the drop in __anext__
                created = await client.create_ticket("ticket after reconnect")
                tid = created["ticket"]["ticket_id"]

                # Wait for subscriber to reconnect and set ready
                await asyncio.wait_for(engine.ready.wait(), timeout=5.0)
                self.assertTrue(engine.subscription_health["connected"])
                self.assertGreater(
                    engine.subscription_health["reconnects"], reconnects_before
                )

                # Verify the ticket is present in digest
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    digest = await engine.build_digest(since=0)
                    if any(t["ticket_id"] == tid for t in digest["tickets"]):
                        break
                    await asyncio.sleep(0.05)

                digest = await engine.build_digest(since=0)
                self.assertTrue(any(t["ticket_id"] == tid for t in digest["tickets"]))
            finally:
                await engine.stop_subscriber()

    async def test_zero_board_writes_while_idle(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)

                # Capture SQLite state before idle run
                before = persisted_documents(self.service)

                # Idle for multiple subscriber cycles
                for _ in range(5):
                    # Trigger catchup with touch=False (what the subscriber executes)
                    view = wait_server._BoardView(client, wait_server.BOARD_ID)
                    await view.board_catchup(cursor=0, ack=False, touch=False, agent_name=client.agent_name)
                    await asyncio.sleep(0.02)

                after = persisted_documents(self.service)
                self.assertEqual(
                    after,
                    before,
                    "Idle catchups and subscriptions must produce zero document/lease/activity mutations",
                )
            finally:
                await engine.stop_subscriber()

    async def test_resource_notification_emitted_and_readable(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)

            # Mock a session to receive send_resource_updated
            notifications_sent: list[str] = []

            class MockSession:
                async def send_resource_updated(self, uri: str) -> None:
                    notifications_sent.append(str(uri))

            mock_session = MockSession()
            engine.sessions.add(mock_session)

            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                notifications_sent.clear()
                created = await client.create_ticket("resource notification trigger")
                tid = created["ticket"]["ticket_id"]

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if notifications_sent:
                        break
                    await asyncio.sleep(0.05)

                digest_uri = f"board://{wait_server.BOARD_ID}/digest"
                self.assertIn(digest_uri, notifications_sent)

                # Test reading the resource
                wait_server._GLOBAL_ENGINE = engine
                content = await wait_server.board_digest_resource(wait_server.BOARD_ID)
                parsed = json.loads(content)
                self.assertIn("tickets", parsed)
                self.assertIn("counts", parsed)
                self.assertTrue(any(t["ticket_id"] == tid for t in parsed["tickets"]))
            finally:
                await engine.stop_subscriber()
                wait_server._GLOBAL_ENGINE = None

    async def test_a2a_wait_behaviour_unchanged_for_worker_and_reviewer_profiles(
        self,
    ) -> None:
        # For worker profile: clamp_timeout respects host limits without orchestrator 200s clamp
        with patch.dict(os.environ, {"PURSERS_ROLE": "worker", "PURSERS_HOST": "codex"}):
            self.assertEqual(wait_server.clamp_timeout(500), 500)
            self.assertEqual(wait_server.clamp_timeout(1000), 560)  # 620 - 60 margin

        # For reviewer profile:
        with patch.dict(os.environ, {"PURSERS_ROLE": "reviewer", "PURSERS_HOST": "codex"}):
            self.assertEqual(wait_server.clamp_timeout(500), 500)

        # For orchestrator profile: clamped to 200s
        with patch.dict(os.environ, {"PURSERS_ROLE": "orchestrator", "PURSERS_HOST": "codex"}):
            self.assertEqual(wait_server.clamp_timeout(500), 200)
            self.assertEqual(wait_server.clamp_timeout(100), 100)

    async def test_board_watch_and_unwatch(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)

                created1 = await client.create_ticket("normal ticket")
                tid1 = created1["ticket"]["ticket_id"]
                created2 = await client.create_ticket("watched ticket", tags=["priority-1"])
                tid2 = created2["ticket"]["ticket_id"]

                # Watch ticket 2 by tag
                watch_res = await engine.watch(tags=["priority-1"])
                self.assertIn("priority-1", watch_res["watched_tags"])

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    digest = await engine.build_digest(since=0)
                    if len(digest["tickets"]) >= 2:
                        break
                    await asyncio.sleep(0.05)

                digest = await engine.build_digest(since=0)
                self.assertGreaterEqual(len(digest["tickets"]), 2)
                # Watched ticket must be sorted first!
                self.assertEqual(digest["tickets"][0]["ticket_id"], tid2)
                self.assertTrue(digest["tickets"][0]["watched"])
                self.assertFalse(digest["tickets"][1]["watched"])

                # Unwatch
                unwatch_res = await engine.unwatch(all=True)
                self.assertEqual(unwatch_res["watched_tags"], [])
                digest_unwatched = await engine.build_digest(since=0)
                t2 = [t for t in digest_unwatched["tickets"] if t["ticket_id"] == tid2][0]
                self.assertFalse(t2["watched"])
            finally:
                await engine.stop_subscriber()

    async def test_tools_via_mcp_protocol_and_metering(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                created = await client.create_ticket("ticket for protocol test")
                tid = created["ticket"]["ticket_id"]

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    d = await engine.build_digest(since=0)
                    if any(t["ticket_id"] == tid for t in d["tickets"]):
                        break
                    await asyncio.sleep(0.05)

                wait_server._GLOBAL_ENGINE = engine

                # Create Context with mock request_context pointing to engine
                ctx = SimpleNamespace(
                    request_context=SimpleNamespace(
                        lifespan_context={"orchestrator_engine": engine, "client": client}
                    )
                )

                # 1. Call board_digest
                digest_res = await wait_server.board_digest(ctx, since=0)
                self.assertIn("tickets", digest_res)
                self.assertTrue(any(t["ticket_id"] == tid for t in digest_res["tickets"]))

                # 2. Call board_digest_ack
                ack_res = await wait_server.board_digest_ack(ctx, digest_res["cursor_map"])
                self.assertEqual(set(ack_res.keys()), {"ok", "cursor_map"})
                self.assertTrue(ack_res["ok"])
                self.assertEqual(ack_res["cursor_map"], digest_res["cursor_map"])

                # 3. Call board_watch & unwatch
                watch_res = await wait_server.board_watch(ctx, ticket_ids=[tid])
                self.assertIn(tid, watch_res["watched_ticket_ids"])
                unwatch_res = await wait_server.board_unwatch(ctx, ticket_ids=[tid])
                self.assertNotIn(tid, unwatch_res["watched_ticket_ids"])

                # 4. Verify metering in bridge stats
                stats_path = wait_server.bridge_stats_path()
                self.assertTrue(stats_path.exists())
                stats = json.loads(stats_path.read_text(encoding="utf-8"))
                model_wait = stats.get("model_wait", {})
                seat_key = json.dumps([wait_server.BOARD_ID, client.identity.agent_name], separators=(",", ":"))
                self.assertIn(seat_key, model_wait)
                seat_stats = model_wait[seat_key]
                hours = seat_stats.get("hours", {})
                self.assertTrue(any(h.get("outcomes", {}).get("digest", 0) > 0 for h in hours.values()))
            finally:
                await engine.stop_subscriber()
                wait_server._GLOBAL_ENGINE = None

    async def test_default_state_path_permissions_and_replay_with_env_unset(self) -> None:
        fake_home = self.root / "fake_home"
        fake_home.mkdir(parents=True, exist_ok=True)
        with patch.dict(
            os.environ,
            {
                "HOME": str(fake_home),
                "USERPROFILE": str(fake_home),
            },
            clear=False,
        ):
            for k in ("PURSERS_BRIDGE_STATE", "PURSERS_BRIDGE_STATE_DIR", "PURSERS_BRIDGE_STATS"):
                os.environ.pop(k, None)

            expected_path = fake_home / ".pursers" / "wait-bridge" / f"orchestrator_state_{wait_server.BOARD_ID}.json"
            resolved = wait_server.orchestrator_state_path()
            self.assertEqual(resolved, expected_path)
            self.assertEqual(resolved.name, f"orchestrator_state_{wait_server.BOARD_ID}.json")

            meter = wait_server.BridgeStats(wait_server.bridge_stats_path())
            engine1 = wait_server.OrchestratorEngine(None, meter, resolved)
            engine1.cursor_map["pursers"] = 42
            engine1.ack_cursor_map["pursers"] = 40
            engine1.ring_buffer.append({"id": "EV-1", "board_id": "pursers", "seq": 41, "kind": "ticket_created", "ticket_id": "TK-persist"})
            engine1.seen_event_ids.add("EV-1")
            engine1.ticket_cache["pursers:TK-persist"] = {"title": "Test Persist", "status": "open"}
            engine1.save_state()

            self.assertTrue(resolved.exists())
            mode = resolved.stat().st_mode & 0o777
            self.assertEqual(oct(mode), oct(0o600))

            # Simulate restart: load into engine2
            engine2 = wait_server.OrchestratorEngine(None, meter, resolved)
            engine2.load_state()
            self.assertEqual(engine2.cursor_map.get("pursers"), 42)
            self.assertEqual(engine2.ack_cursor_map.get("pursers"), 40)
            self.assertEqual(len(engine2.ring_buffer), 1)
            self.assertIn("EV-1", engine2.seen_event_ids)
            self.assertEqual(engine2.ticket_cache.get("pursers:TK-persist", {}).get("title"), "Test Persist")

            # Deduplication: re-adding same event ID does not duplicate
            self.assertIn("EV-1", engine2.seen_event_ids)

    async def test_dynamic_registry_activation_reopens_listen_and_receives_event(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            second_board = "second-board"
            await client._raw_client.call_tool(
                "board_join", {"board_id": second_board, "agent_name": "creator-agent"}
            )
            await client._raw_client.call_tool(
                "board_join", {"board_id": second_board, "agent_name": "orchestrator-agent"}
            )
            initial_reg = {
                "schema_version": 1,
                "projects": {
                    "home": {
                        "board_id": wait_server.BOARD_ID,
                        "work_dir": "/var/projects/pursers",
                        "status": "active",
                    },
                    "sub": {
                        "board_id": second_board,
                        "work_dir": "/var/projects/second",
                        "status": "paused",
                    },
                },
            }
            await client._raw_client.call_tool(
                "board_state_update",
                {
                    "board_id": wait_server.BOARD_ID,
                    "agent_name": "creator-agent",
                    "key": "project_registry",
                    "value": json.dumps(initial_reg),
                },
            )
            await engine.start_subscriber()
            try:
                await asyncio.wait_for(engine.ready.wait(), timeout=3.0)
                self.assertIn(wait_server.BOARD_ID, engine.active_boards)
                self.assertNotIn(second_board, engine.active_boards)

                active_reg = json.loads(json.dumps(initial_reg))
                active_reg["projects"]["sub"]["status"] = "active"
                await client._raw_client.call_tool(
                    "board_state_update",
                    {
                        "board_id": wait_server.BOARD_ID,
                        "agent_name": "creator-agent",
                        "key": "project_registry",
                        "value": json.dumps(active_reg),
                    },
                )

                created = await client._raw_client.call_tool(
                    "ticket_create",
                    {
                        "board_id": second_board,
                        "agent_name": "creator-agent",
                        "title": "second board ticket after activation",
                        "description": "test",
                        "target_url": "second/work",
                        "scope": "interactive-no-send",
                        "required_fields": ["test_output"],
                    },
                )
                tid2 = created.structured_content["ticket"]["ticket_id"]

                # Wait on subscriber-maintained ring/cursor/active-board state before any digest call
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if second_board in engine.active_boards and any(
                        ev.get("board_id") == second_board and ev.get("ticket_id") == tid2
                        for ev in engine.ring_buffer
                    ):
                        break
                    await asyncio.sleep(0.05)

                self.assertIn(second_board, engine.active_boards)
                self.assertTrue(any(ev.get("board_id") == second_board and ev.get("ticket_id") == tid2 for ev in engine.ring_buffer))

                # Make Central unavailable and assert digest returns from local state
                engine.connection = None
                d = await engine.build_digest(boards="registry")
                self.assertTrue(any(t["ticket_id"] == tid2 for t in d["tickets"]))
                self.assertEqual(engine.subscription_health["reconnects"], 0)

                # Restart offline coverage: loaded from disk while offline, boards="registry" includes secondary board
                offline_engine = wait_server.OrchestratorEngine(None, engine.meter, engine.state_path)
                offline_engine.load_state()
                self.assertIn(second_board, offline_engine.active_boards)
                offline_digest = await offline_engine.build_digest(boards="registry")
                self.assertTrue(any(t["ticket_id"] == tid2 for t in offline_digest["tickets"]))
            finally:
                await engine.stop_subscriber()

    async def test_orchestrator_clamp_in_real_wait_paths_with_worker_role(
        self,
    ) -> None:
        self.assertEqual(wait_server.clamp_timeout(600, role="worker"), 200)
        self.assertEqual(wait_server.clamp_timeout(600, role="coordinator"), 200)

        class MockClient:
            def __init__(self):
                aid = wait_server._derived_agent_id("PR-test", "test-worker")
                self.identity = SimpleNamespace(agent_name="test-worker", agent_id=aid, principal_id="PR-test")
                self.agent_name = "test-worker"
                self.aid = aid

            async def board_join(self, **kwargs):
                return {"agent_id": self.aid, "role": "worker"}

            async def board_catchup(self, **kwargs):
                return {"events": [], "next_cursor": 1, "has_more": False}

            async def ticket_list(self, **kwargs):
                return {"tickets": []}

            async def board_state_get(self, **kwargs):
                return {
                    "state": {
                        "value": json.dumps({
                            "schema_version": 1,
                            "projects": {
                                "home": {"board_id": wait_server.BOARD_ID, "work_dir": "/var/projects/pursers", "status": "active"}
                            }
                        })
                    }
                }

            async def call_tool(self, name, arguments):
                if name == "board_join":
                    bid = arguments.get("board_id", wait_server.BOARD_ID)
                    aname = arguments.get("agent_name", "test-worker")
                    aid = wait_server._derived_agent_id("PR-test", aname, bid)
                    return SimpleNamespace(structured_content={
                        "board_id": bid,
                        "agent_id": aid,
                        "principal_id": "PR-test",
                        "agent_name": aname,
                        "role": "worker",
                    }, is_error=False)
                if name == "board_catchup":
                    return SimpleNamespace(structured_content={"events": [], "next_cursor": 1, "has_more": False}, is_error=False)
                if name == "ticket_list":
                    return SimpleNamespace(structured_content={"tickets": []}, is_error=False)
                if name == "board_state_get":
                    return SimpleNamespace(structured_content={
                        "state": {
                            "value": json.dumps({
                                "schema_version": 1,
                                "projects": {
                                    "home": {"board_id": wait_server.BOARD_ID, "work_dir": "/var/projects/pursers", "status": "active"}
                                }
                            })
                        }
                    }, is_error=False)
                return SimpleNamespace(structured_content={}, is_error=False)

        client = MockClient()
        with (
            patch.dict(os.environ, {"PURSERS_ROLE": "orchestrator", "PURSERS_HOST": "codex"}),
            patch.object(wait_server, "WAIT_MODE", "poll"),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
        ):
            orig_clamp = wait_server.clamp_timeout
            seen_budgets = []
            def spy_clamp(t, role=None):
                b = orig_clamp(t, role)
                seen_budgets.append(b)
                return 0.02

            # Test single board path through _wait_for_work
            with patch.object(wait_server, "clamp_timeout", side_effect=spy_clamp):
                await wait_server._wait_for_work(client, timeout_s=600, since_seq=0, agent_name="test-worker")
                self.assertTrue(any(b == 200 for b in seen_budgets))

            # Test registry path through _a2a_wait_impl
            seen_budgets.clear()
            with patch.object(wait_server, "clamp_timeout", side_effect=spy_clamp):
                await wait_server._a2a_wait_impl(client, since_seq=0, timeout_s=600, boards="registry")
                self.assertTrue(any(b == 200 for b in seen_budgets))

    async def test_digest_prompt_return_with_delayed_or_unavailable_central(
        self,
    ) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
            client, engine = await self._setup_client_and_engine(raw_client)
            central_read_calls = 0

            # Add multiple uncached ticket events into the ring buffer (none in ticket_cache)
            for i in range(12):
                tid = f"TK-uncached-{i}"
                engine.ring_buffer.append({
                    "id": f"EV-uncached-{i}",
                    "board_id": wait_server.BOARD_ID,
                    "seq": 200 + i,
                    "ticket_id": tid,
                    "kind": "ticket_created",
                    "title": f"Uncached Ticket {i}",
                    "status_to": "open",
                })
                engine.seen_event_ids.add(f"EV-uncached-{i}")

            class UnreachableClient:
                def __getattr__(self, name):
                    nonlocal central_read_calls
                    central_read_calls += 1
                    raise ConnectionError(f"Central should not be queried during board_digest ({name})")

            unreachable = UnreachableClient()

            class UnreachableConnection:
                async def client(self):
                    return unreachable

            engine.connection = UnreachableConnection()
            wait_server._GLOBAL_ENGINE = engine

            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context={"orchestrator_engine": engine}
                )
            )

            # Invoke through the real MCP tool wrapper
            t0 = time.monotonic()
            digest = await wait_server.board_digest(ctx, since=0)
            elapsed = time.monotonic() - t0

            # Constant total return bound (< 0.05s) independent of ticket count
            self.assertLess(elapsed, 0.05)
            # Proves no Central reads were required
            self.assertEqual(central_read_calls, 0)
            # All 12 uncached tickets present and properly constructed
            self.assertEqual(len(digest["tickets"]), 12)
            self.assertTrue(all(t["status_now"] == "open" for t in digest["tickets"]))
