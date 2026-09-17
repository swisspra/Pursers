from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class TicketUnclaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
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
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.admin = central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.member = central.Principal(
            "PR-member",
            "member-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.other_member = central.Principal(
            "PR-other-member",
            "other-member-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        self.access_token = "central-test-access-token"
        self.access_patch = patch.object(
            central,
            "get_access_token",
            return_value=SimpleNamespace(token=self.access_token),
        )
        self.access_patch.start()
        await self.call("board_join", agent_name="admin-agent")
        for principal in (self.member, self.other_member):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        self.principal = self.member
        await self.call("board_join", agent_name="member-agent")
        self.principal = self.other_member
        await self.call("board_join", agent_name="other-agent")

    async def asyncTearDown(self) -> None:
        self.access_patch.stop()
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        result = await self.mcp.call_tool(
            name,
            {"board_id": "pursers", **arguments},
        )
        return result

    async def create_and_claim(
        self, *, required_fields: list[str] | None = None
    ) -> str:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="unclaim test ticket",
            description="exercise explicit claim release",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=required_fields or ["test_output"],
        )
        self.assertFalse(created.is_error)
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.member
        claimed = await self.call(
            "ticket_claim", agent_name="member-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)
        return ticket_id

    def submission_preflight(
        self, ticket_id: str, branch: str, sha: str
    ) -> dict[str, str]:
        preflight = {
            "kind": "git-ls-remote-exact-tip-v1",
            "branch": branch,
            "commit": sha,
            "remote_ref": f"origin/{branch}",
            "remote_tip": sha,
        }
        payload = json.dumps(
            {
                "board_id": "pursers",
                "ticket_id": ticket_id,
                "agent_name": "member-agent",
                **preflight,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        preflight["proof"] = hmac.new(
            self.access_token.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return preflight

    async def test_ticket_transport_allows_bearer_prose_and_placeholders(self) -> None:
        self.principal = self.admin
        for index, description in enumerate(
            (
                "The literal Bearer scheme word is harmless prose.",
                "Authorization example: Bearer <placeholder>",
                "Authorization example: Bearer [REDACTED]",
                "Authorization example: Bearer placeholder_value",
            )
        ):
            created = await self.call(
                "ticket_create",
                agent_name="admin-agent",
                title=f"scrub documentation case {index}",
                description=description,
                target_url="pursers/packages/central",
                scope="interactive-no-send",
                required_fields=["tests"],
            )
            self.assertFalse(created.is_error)
            self.assertTrue(created.structured_content["ok"])

    async def test_ticket_transport_still_blocks_real_bearer_value(self) -> None:
        self.principal = self.admin
        with self.assertRaisesRegex(ToolError, "bearer_token"):
            await self.call(
                "ticket_create",
                agent_name="admin-agent",
                title="unsafe bearer value",
                description="Authorization: Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                target_url="pursers/packages/central",
                scope="interactive-no-send",
                required_fields=["tests"],
            )

    async def test_claimer_unclaims_to_open_and_emits_journal_event(self) -> None:
        ticket_id = await self.create_and_claim()
        before_seq = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]

        result = await self.call(
            "ticket_unclaim", agent_name="member-agent", ticket_id=ticket_id
        )

        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertEqual(payload["ticket"]["status"], "open")
        self.assertEqual(payload["permission"], "current claiming agent")
        for key in (
            "claimed_by_agent_id",
            "claimed_by_principal_id",
            "claimed_by",
            "claimed_at",
            "lease_expires_at_epoch",
            "lease_expires_at",
            "lease_renewed_at",
            "ttl_s",
        ):
            self.assertNotIn(key, payload["ticket"])
        self.assertEqual(payload["event"]["kind"], "ticket_status_changed")
        self.assertEqual(payload["event"]["status_from"], "claimed")
        self.assertEqual(payload["event"]["status_to"], "open")
        journal = self.service.journal.read_after("pursers", before_seq, 10)
        self.assertEqual(len(journal["events"]), 1)
        self.assertEqual(journal["events"][0]["ticket_id"], ticket_id)
        self.assertEqual(journal["events"][0]["status_to"], "open")

    async def test_non_claimer_non_admin_is_rejected(self) -> None:
        ticket_id = await self.create_and_claim()
        self.principal = self.other_member

        with self.assertRaisesRegex(
            ToolError, "current claiming agent or board admin"
        ):
            await self.call(
                "ticket_unclaim", agent_name="other-agent", ticket_id=ticket_id
            )
        ticket = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(ticket["status"], "claimed")
        self.assertEqual(ticket["claimed_by"], "member-agent")

    async def test_admin_can_unclaim_another_agents_ticket(self) -> None:
        ticket_id = await self.create_and_claim()
        self.principal = self.admin

        result = await self.call(
            "ticket_unclaim", agent_name="admin-agent", ticket_id=ticket_id
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["permission"], "board admin")
        self.assertEqual(result.structured_content["ticket"]["status"], "open")

    async def test_submitted_and_closed_tickets_are_rejected(self) -> None:
        ticket_id = await self.create_and_claim()
        submitted = await self.call(
            "ticket_submit",
            agent_name="member-agent",
            ticket_id=ticket_id,
            summary="done",
        )
        self.assertFalse(submitted.is_error)

        with self.assertRaisesRegex(ToolError, "ticket is submitted"):
            await self.call(
                "ticket_unclaim", agent_name="member-agent", ticket_id=ticket_id
            )

        self.principal = self.admin
        reviewed = await self.call(
            "ticket_review",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            verdict="approve",
            review_notes="approved",
        )
        self.assertFalse(reviewed.is_error)
        with self.assertRaisesRegex(ToolError, "ticket is closed"):
            await self.call(
                "ticket_unclaim", agent_name="admin-agent", ticket_id=ticket_id
            )

    async def test_ticket_submit_notes_accepts_5000_and_rejects_5001(self) -> None:
        ticket_id = await self.create_and_claim()
        accepted = await self.call(
            "ticket_submit",
            agent_name="member-agent",
            ticket_id=ticket_id,
            summary="within notes limit",
            notes="n" * 5_000,
        )
        self.assertFalse(accepted.is_error)
        self.assertEqual(len(accepted.structured_content["ticket"]["notes"]), 5_000)

        ticket_id = await self.create_and_claim()
        with self.assertRaisesRegex(ToolError, "notes must be at most 5000 characters"):
            await self.call(
                "ticket_submit",
                agent_name="member-agent",
                ticket_id=ticket_id,
                summary="over notes limit",
                notes="n" * 5_001,
            )

    async def test_ticket_submit_rejects_malformed_branch_and_commit_without_network(
        self,
    ) -> None:
        for notes in (
            "test_output: pass",
            "branch_and_commit: codex/TK-shape @ d428fcd",
            "branch_and_commit: missing-slash @ " + "a" * 40,
            (
                "branch_and_commit: codex/TK-one @ "
                + "a" * 40
                + "\nbranch_and_commit: codex/TK-two @ "
                + "b" * 40
            ),
        ):
            ticket_id = await self.create_and_claim(
                required_fields=["branch_and_commit", "test_output"]
            )
            with self.assertRaisesRegex(
                ToolError, "branch_and_commit must appear exactly once"
            ):
                await self.call(
                    "ticket_submit",
                    agent_name="member-agent",
                    ticket_id=ticket_id,
                    summary="malformed submission",
                    notes=notes,
                )
            ticket = self.service.load("pursers")["tickets"][ticket_id]
            self.assertEqual(ticket["status"], "claimed")

    async def test_ticket_submit_rejects_raw_code_submission_without_preflight(
        self,
    ) -> None:
        ticket_id = await self.create_and_claim(
            required_fields=["branch_and_commit", "test_output"]
        )
        with self.assertRaisesRegex(ToolError, "raw ticket_submit is unavailable"):
            await self.call(
                "ticket_submit",
                agent_name="member-agent",
                ticket_id=ticket_id,
                summary="shape valid but unverified",
                notes=(
                    "branch_and_commit: codex/TK-shape @ "
                    + "a" * 40
                    + "\ntest_output: pass"
                ),
            )
        self.assertEqual(
            self.service.load("pursers")["tickets"][ticket_id]["status"], "claimed"
        )

    async def test_ticket_submit_accepts_matching_clone_preflight(self) -> None:
        ticket_id = await self.create_and_claim(
            required_fields=["branch_and_commit", "test_output"]
        )
        sha = "a" * 40
        result = await self.call(
            "ticket_submit",
            agent_name="member-agent",
            ticket_id=ticket_id,
            summary="shape and clone preflight valid",
            notes=f"branch_and_commit: codex/TK-shape @ {sha}\ntest_output: pass",
            submission_preflight=self.submission_preflight(
                ticket_id, "codex/TK-shape", sha
            ),
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content["ticket"]["submission_preflight"]["remote_tip"],
            sha,
        )

    async def test_ticket_submit_rejects_fabricated_matching_preflight(self) -> None:
        ticket_id = await self.create_and_claim(
            required_fields=["branch_and_commit", "test_output"]
        )
        sha = "a" * 40
        preflight = self.submission_preflight(ticket_id, "codex/TK-shape", sha)
        preflight["proof"] = "0" * 64
        with self.assertRaisesRegex(ToolError, "remote-tip proof is invalid"):
            await self.call(
                "ticket_submit",
                agent_name="member-agent",
                ticket_id=ticket_id,
                summary="fabricated",
                notes=(
                    f"branch_and_commit: codex/TK-shape @ {sha}"
                    "\ntest_output: pass"
                ),
                submission_preflight=preflight,
            )
        self.assertEqual(
            self.service.load("pursers")["tickets"][ticket_id]["status"], "claimed"
        )

    async def test_ticket_submit_rejects_mismatched_clone_preflight(self) -> None:
        ticket_id = await self.create_and_claim(
            required_fields=["branch_and_commit", "test_output"]
        )
        submitted_sha = "a" * 40
        remote_sha = "b" * 40
        with self.assertRaisesRegex(
            ToolError, f"declared SHA {submitted_sha}.*actual remote SHA {remote_sha}"
        ):
            await self.call(
                "ticket_submit",
                agent_name="member-agent",
                ticket_id=ticket_id,
                summary="mismatch",
                notes=(
                    f"branch_and_commit: codex/TK-shape @ {submitted_sha}"
                    "\ntest_output: pass"
                ),
                submission_preflight={
                    "kind": "git-ls-remote-exact-tip-v1",
                    "branch": "codex/TK-shape",
                    "commit": submitted_sha,
                    "remote_ref": "origin/codex/TK-shape",
                    "remote_tip": remote_sha,
                    "proof": "0" * 64,
                },
            )
        self.assertEqual(
            self.service.load("pursers")["tickets"][ticket_id]["status"], "claimed"
        )


if __name__ == "__main__":
    unittest.main()
