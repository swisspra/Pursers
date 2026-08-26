from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import pursers_wait_server as wait_server  # noqa: E402


REGISTRY = {
    "schema_version": 1,
    "projects": {
        "home-alias": {
            "board_id": wait_server.BOARD_ID,
            "work_dir": "/workspace/home",
            "status": "active",
        },
        "alpha": {
            "board_id": "alpha",
            "work_dir": "/workspace/alpha",
            "status": "active",
        },
        "alpha-alias": {
            "board_id": "alpha",
            "work_dir": "/workspace/alpha-alias",
            "status": "active",
        },
        "paused": {
            "board_id": "paused-board",
            "work_dir": "/workspace/paused",
            "status": "paused",
        },
    },
}


class FakeRegistryClient:
    def __init__(self, value: str) -> None:
        self.value = value
        self.get_calls: list[str | None] = []

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        self.get_calls.append(key)
        return {
            "ok": True,
            "state": {
                "value": self.value,
                "scope": "project",
            },
        }


def context_for(client: FakeRegistryClient) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"client": client})
    )


class ProjectRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_parse_active_filter_dedupe_and_home_inclusion(self) -> None:
        client = FakeRegistryClient(json.dumps(REGISTRY))

        parsed = await wait_server._read_project_registry(client)

        self.assertEqual(parsed, REGISTRY)
        self.assertEqual(
            wait_server._registry_boards(parsed),
            [wait_server.BOARD_ID, "alpha"],
        )
        self.assertEqual(client.get_calls, ["project_registry"])

    async def test_registry_sentinel_list_and_omitted_paths_do_not_cross(self) -> None:
        client = FakeRegistryClient(json.dumps(REGISTRY))
        context = context_for(client)
        many_result = {"path": "many"}
        single_result = {"path": "single"}

        with (
            patch.object(
                wait_server,
                "_wait_for_work_many",
                AsyncMock(return_value=many_result),
            ) as many,
            patch.object(
                wait_server,
                "_wait_for_work",
                AsyncMock(return_value=single_result),
            ) as single,
        ):
            sentinel = await wait_server.a2a_wait(
                context,
                boards="registry",
                since_seq={wait_server.BOARD_ID: 3, "alpha": 4},
            )
            updated_registry = {
                "schema_version": 1,
                "projects": {
                    "beta": {
                        "board_id": "beta",
                        "work_dir": "/workspace/beta",
                        "status": "active",
                    }
                },
            }
            client.value = json.dumps(updated_registry)
            sentinel_again = await wait_server.a2a_wait(
                context,
                boards="registry",
                since_seq={wait_server.BOARD_ID: 5, "beta": 6},
            )
            explicit = await wait_server.a2a_wait(
                context,
                boards=["explicit"],
                since_seq={"explicit": 8},
            )
            omitted = await wait_server.a2a_wait(context, since_seq=11)

        self.assertEqual(sentinel, many_result)
        self.assertEqual(sentinel_again, many_result)
        self.assertEqual(explicit, many_result)
        self.assertEqual(omitted, single_result)
        self.assertEqual(
            client.get_calls,
            ["project_registry", "project_registry"],
        )
        self.assertEqual(
            many.await_args_list[0].kwargs["boards"],
            [wait_server.BOARD_ID, "alpha"],
        )
        self.assertEqual(
            many.await_args_list[1].kwargs["boards"],
            [wait_server.BOARD_ID, "beta"],
        )
        self.assertEqual(many.await_args_list[2].kwargs["boards"], ["explicit"])
        self.assertEqual(single.await_args.kwargs["since_seq"], 11)

    async def test_malformed_registry_falls_back_with_warning_and_home_cursor(self) -> None:
        client = FakeRegistryClient("{not-json")
        context = context_for(client)
        fallback = {
            "new_seq": 12,
            "events": [],
            "waited_s": 0.0,
            "timed_out": True,
            "resynced": False,
        }

        with (
            patch.object(
                wait_server,
                "_wait_for_work",
                AsyncMock(return_value=fallback),
            ) as single,
            patch.object(
                wait_server,
                "_wait_for_work_many",
                AsyncMock(),
            ) as many,
        ):
            result = await wait_server.a2a_wait(
                context,
                boards="registry",
                since_seq={wait_server.BOARD_ID: 12, "alpha": 9},
            )

        self.assertEqual(result["new_seq"], 12)
        self.assertIn("not valid JSON", result["registry_warning"])
        self.assertEqual(single.await_args.kwargs["since_seq"], 12)
        many.assert_not_awaited()

    async def test_project_registry_get_returns_parsed_registry_shape(self) -> None:
        client = FakeRegistryClient(json.dumps(REGISTRY))

        result = await wait_server.project_registry_get(context_for(client))

        self.assertEqual(result, REGISTRY)
        self.assertEqual(list(result), ["schema_version", "projects"])


if __name__ == "__main__":
    unittest.main()
