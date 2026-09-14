from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
WAIT_BRIDGE = REPOSITORY_ROOT / "tools" / "wait-bridge"
sys.path.insert(0, str(WAIT_BRIDGE))

import central  # noqa: E402
import door_admin  # noqa: E402
from jwt_verifier import JWTTokenVerifier, JWTVerifierConfig  # noqa: E402


class BoardBoundAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temporary.name)
        self.jwks = self.root / "jwks.json"
        self.keys = self.root / "keys"
        self.central_url = "http://localhost:8765/mcp"
        self.issuer = "http://localhost:8765"
        self.now = int(time.time())
        self.worker = self.issue("project-a", "worker")
        self.reviewer = self.issue("project-a", "reviewer")
        legacy_token = self.issue_legacy_unbound_token()
        self.verifier = JWTTokenVerifier(
            JWTVerifierConfig(
                issuer=self.issuer,
                audience=self.central_url,
                jwks_path=self.jwks,
            )
        )
        self.worker_access = await self.verified(self.worker.token)
        self.reviewer_access = await self.verified(self.reviewer.token)
        self.legacy_access = await self.verified(legacy_token)
        self.access = self.legacy_access
        self.access_patch = patch.object(
            central, "get_access_token", side_effect=lambda: self.access
        )
        self.access_patch.start()
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": self.issuer,
                "CENTRAL_JWT_AUDIENCE": self.central_url,
                "CENTRAL_JWKS_PATH": str(self.jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )

        worker_principal = self.principal(self.worker_access)
        reviewer_principal = self.principal(self.reviewer_access)
        for board in ("project-a", "project-b"):
            await self.call(board, "board_join", agent_name="legacy-admin")
        for board, target, role in (
            ("project-a", worker_principal, "member"),
            ("project-a", reviewer_principal, "reviewer"),
            ("project-b", worker_principal, "member"),
        ):
            await self.call(
                board,
                "board_member_add",
                agent_name="legacy-admin",
                principal_id=target.principal_id,
                role=role,
            )

    async def asyncTearDown(self) -> None:
        task = getattr(self.service, "recurring_reaper_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.environment.stop()
        self.access_patch.stop()
        self.temporary.cleanup()

    def issue(self, board: str, role: str, *, rotate: bool = False):
        return door_admin.issue_credential(
            board=board,
            role=role,
            central_url=self.central_url,
            issuer=self.issuer,
            jwks_path=self.jwks,
            keys_dir=self.keys,
            rotate=rotate,
            now=self.now + int(rotate),
        )

    def issue_legacy_unbound_token(self) -> str:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        kid = "legacy-unbound-operator-v1"
        document = json.loads(self.jwks.read_text(encoding="utf-8"))
        public = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        public.update({"kid": kid, "alg": "RS256", "use": "sig"})
        document["keys"].append(public)
        self.jwks.write_text(json.dumps(document), encoding="utf-8")
        return jwt.encode(
            {
                "iss": self.issuer,
                "sub": "legacy-unbound-operator",
                "client_id": "legacy-unbound-operator",
                "aud": self.central_url,
                "resource": self.central_url,
                "scope": "board:read board:write board:review",
                "iat": self.now,
                "nbf": self.now - 60,
                "exp": self.now + 3600,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": kid, "typ": "JWT"},
        )

    async def verified(self, token: str):
        access = await self.verifier.verify_token(token)
        self.assertIsNotNone(access)
        return access

    def principal(self, access):
        previous = self.access
        self.access = access
        try:
            return central.current_principal()
        finally:
            self.access = previous

    async def call(self, board: str, tool: str, **arguments: object):
        return await self.mcp.call_tool(tool, {"board_id": board, **arguments})

    def persisted_boards(self) -> set[str]:
        return {
            str(value)
            for _, value in self.service.store.document_values("boards", "board_id")
        }

    async def test_same_worker_jwt_is_confined_before_join_read_and_write(self) -> None:
        exact_jwt = self.worker.token
        self.access = await self.verified(exact_jwt)
        joined = await self.call(
            "project-a", "board_join", agent_name="bound-worker", role="worker"
        )
        self.assertFalse(joined.is_error)

        for tool in ("board_join", "board_onboard"):
            for wrong_board in ("project-b", "project-never-created"):
                with self.assertRaisesRegex(ToolError, "token is not authorized"):
                    await self.call(
                        wrong_board,
                        tool,
                        agent_name="bound-worker",
                        role="worker",
                    )
        for tool in ("board_status", "ticket_create"):
            request = SimpleNamespace(
                method="tools/call",
                params={"name": tool, "arguments": {"board_id": "project-b"}},
                meta={},
            )
            reached_tool = False

            async def should_not_run(_ctx: object) -> object:
                nonlocal reached_tool
                reached_tool = True
                return {"accepted": True}

            denied = await central.SubscriptionAuthorization(self.service)(
                request, should_not_run
            )
            self.assertTrue(denied["isError"])
            self.assertIn("token is not authorized", denied["content"][0]["text"])
            self.assertFalse(reached_tool)
        self.assertNotIn("project-never-created", self.persisted_boards())
        self.assertNotIn(
            "project-never-created", self.service.board_ids_by_token.values()
        )

        listed = await self.mcp.call_tool("board_list", {})
        self.assertEqual(
            [item["board_id"] for item in listed.structured_content["boards"]],
            ["project-a"],
        )

    async def test_bound_roles_and_subscription_paths_remain_confined(self) -> None:
        self.access = self.worker_access
        worker = await self.call(
            "project-a", "board_join", agent_name="bound-worker", role="worker"
        )
        self.assertFalse(worker.is_error)
        with self.assertRaisesRegex(ToolError, "board:review"):
            await self.call(
                "project-a", "board_join", agent_name="worker-as-reviewer", role="reviewer"
            )

        self.access = self.reviewer_access
        reviewer = await self.call(
            "project-a", "board_join", agent_name="bound-reviewer", role="reviewer"
        )
        self.assertFalse(reviewer.is_error)
        with self.assertRaisesRegex(ToolError, "board:write"):
            await self.call(
                "project-a", "board_join", agent_name="reviewer-as-worker", role="worker"
            )

        self.access = self.worker_access
        request = SimpleNamespace(
            method="subscriptions/listen",
            params={"notifications": {"resourceSubscriptions": ["board://project-b/journal"]}},
            meta={},
        )
        with self.assertRaisesRegex(MCPError, "subscription denied"):
            await central.SubscriptionAuthorization(self.service)(
                request, lambda _ctx: None
            )

    async def test_bound_worker_cannot_bootstrap_fresh_board_as_admin(self) -> None:
        fresh = self.issue("fresh-project", "worker")
        self.access = await self.verified(fresh.token)
        with self.assertRaisesRegex(
            ToolError, "board-bound credentials cannot create boards"
        ):
            await self.call(
                "fresh-project", "board_join", agent_name="fresh-worker", role="worker"
            )
        self.assertNotIn("fresh-project", self.persisted_boards())

    async def test_rotation_revokes_old_and_reconnects_new_with_same_boundary(self) -> None:
        self.access = self.worker_access
        joined = await self.call(
            "project-a", "board_join", agent_name="rotating-worker", role="worker"
        )
        self.assertFalse(joined.is_error)

        old_jwt = self.worker.token
        rotated = self.issue("project-a", "worker", rotate=True)
        self.assertIsNone(await self.verifier.verify_token(old_jwt))
        self.access = await self.verified(rotated.token)
        rejoined = await self.call(
            "project-a",
            "board_join",
            agent_name="rotating-worker-successor",
            role="worker",
        )
        self.assertFalse(rejoined.is_error)
        with self.assertRaisesRegex(ToolError, "token is not authorized"):
            await self.call(
                "project-b",
                "board_join",
                agent_name="rotating-worker-successor",
                role="worker",
            )

    async def test_explicit_legacy_unbound_operator_compatibility(self) -> None:
        self.access = self.legacy_access
        listed = await self.mcp.call_tool("board_list", {})
        self.assertEqual(
            [item["board_id"] for item in listed.structured_content["boards"]],
            ["project-a", "project-b"],
        )


if __name__ == "__main__":
    unittest.main()
