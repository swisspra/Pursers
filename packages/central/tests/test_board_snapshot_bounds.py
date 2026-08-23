from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "onboard_central"))

import central  # noqa: E402


class BoardSnapshotBoundsTests(unittest.IsolatedAsyncioTestCase):
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
        self.principal = central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.call("board_join", agent_name="admin-agent")
        self.assertFalse(joined.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name,
            {"board_id": "pursers", **arguments},
        )

    def seed(self, ticket_count: int, description_size: int = 10) -> None:
        def mutate(document: dict[str, object]) -> None:
            tickets = document["tickets"]
            state = document["state"]
            config = document["config"]
            assert isinstance(tickets, dict)
            assert isinstance(state, dict)
            assert isinstance(config, dict)
            for index in range(ticket_count):
                ticket_id = f"TK-{index:04d}"
                tickets[ticket_id] = {
                    "ticket_id": ticket_id,
                    "title": f"ticket {index}",
                    "description": "x" * description_size,
                    "status": "open",
                }
                state[f"key-{index:04d}"] = f"value-{index}"
            config["scrub_allow_counts"] = {
                f"rule-{index:04d}": index + 1
                for index in range(ticket_count)
            }

        self.service.mutate("pursers", mutate)

    async def test_limit_caps_each_collection_and_reports_omissions(self) -> None:
        self.seed(3)

        result = await self.call("board_snapshot", limit=2, max_bytes=100_000)

        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["bounds"]["limit_per_collection"], 2)
        self.assertEqual(payload["returned_counts"]["tickets"], 2)
        self.assertEqual(payload["returned_counts"]["state"], 2)
        self.assertEqual(payload["returned_counts"]["scrub_allow_counts"], 2)
        self.assertEqual(payload["omitted_counts"]["tickets"], 1)
        self.assertEqual(payload["omitted_counts"]["state"], 1)
        self.assertEqual(payload["omitted_counts"]["scrub_allow_counts"], 1)

    async def test_max_bytes_trims_large_entries_with_explicit_counts(self) -> None:
        self.seed(3, description_size=5_000)

        result = await self.call("board_snapshot", max_bytes=4_096)

        self.assertFalse(result.is_error)
        payload = result.structured_content
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        self.assertLessEqual(len(serialized), 4_096)
        self.assertTrue(payload["truncated"])
        self.assertGreater(payload["omitted_counts"]["tickets"], 0)
        for name, total in payload["total_counts"].items():
            self.assertEqual(
                total,
                payload["returned_counts"][name] + payload["omitted_counts"][name],
            )

    async def test_default_snapshot_stays_below_serialized_ceiling(self) -> None:
        self.seed(100, description_size=5_000)

        result = await self.call("board_snapshot")

        self.assertFalse(result.is_error)
        payload = result.structured_content
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        self.assertLessEqual(len(serialized), central.DEFAULT_SNAPSHOT_MAX_BYTES)
        self.assertLess(len(serialized), 800_000)
        self.assertTrue(payload["truncated"])


if __name__ == "__main__":
    unittest.main()
