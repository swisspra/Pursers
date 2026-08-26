from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import registry_admin  # noqa: E402


INITIAL = {
    "schema_version": 1,
    "projects": {
        "alpha": {
            "board_id": "alpha-board",
            "work_dir": "/synthetic/alpha",
            "status": "active",
        }
    },
}


class FakeClient:
    def __init__(
        self,
        document: Any = INITIAL,
        *,
        mismatch_after_write: bool = False,
    ) -> None:
        self.value = json.dumps(document)
        self.mismatch_after_write = mismatch_after_write
        self.get_calls = 0
        self.writes: list[tuple[str, str]] = []

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        self.get_calls += 1
        value = self.value
        if self.mismatch_after_write and self.writes:
            value = json.dumps(INITIAL)
        return {"state": {"key": key, "value": value}}

    async def board_state_update(self, key: str, value: str) -> dict[str, Any]:
        self.writes.append((key, value))
        self.value = value
        return {"ok": True}

    def document(self) -> dict[str, Any]:
        return json.loads(self.value)


def parse(*arguments: str) -> argparse.Namespace:
    return registry_admin.build_parser().parse_args(list(arguments))


def invoke(client: FakeClient, *arguments: str) -> str:
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(registry_admin.execute(parse(*arguments), client))
    return output.getvalue()


class RegistryAdminTests(unittest.TestCase):
    def test_show_validates_and_prints_without_writing(self) -> None:
        client = FakeClient()

        output = invoke(client, "show")

        self.assertEqual(json.loads(output), INITIAL)
        self.assertEqual(client.writes, [])
        self.assertEqual(client.get_calls, 1)

    def test_add_writes_and_reads_back(self) -> None:
        client = FakeClient()

        output = invoke(
            client,
            "add",
            "beta",
            "--board-id",
            "beta-board",
            "--work-dir",
            "/synthetic/beta",
            "--status",
            "paused",
        )

        result = json.loads(output)
        self.assertEqual(
            result["projects"]["beta"],
            {
                "board_id": "beta-board",
                "work_dir": "/synthetic/beta",
                "status": "paused",
            },
        )
        self.assertEqual(client.get_calls, 2)
        self.assertEqual(client.writes[0][0], registry_admin.REGISTRY_KEY)

    def test_duplicate_add_requires_force_and_force_replaces(self) -> None:
        client = FakeClient()
        arguments = (
            "add",
            "alpha",
            "--board-id",
            "replacement-board",
            "--work-dir",
            "/synthetic/replacement",
        )

        with self.assertRaisesRegex(registry_admin.RegistryError, "--force"):
            invoke(client, *arguments)
        self.assertEqual(client.writes, [])

        invoke(client, *arguments, "--force")
        self.assertEqual(
            client.document()["projects"]["alpha"]["board_id"],
            "replacement-board",
        )

    def test_pause_subcommand(self) -> None:
        client = FakeClient()

        invoke(client, "pause", "alpha")

        self.assertEqual(client.document()["projects"]["alpha"]["status"], "paused")

    def test_activate_subcommand(self) -> None:
        paused = json.loads(json.dumps(INITIAL))
        paused["projects"]["alpha"]["status"] = "paused"
        client = FakeClient(paused)

        invoke(client, "activate", "alpha")

        self.assertEqual(client.document()["projects"]["alpha"]["status"], "active")

    def test_remove_prints_restorable_entry(self) -> None:
        client = FakeClient()

        output = invoke(client, "remove", "alpha")

        self.assertNotIn("alpha", client.document()["projects"])
        self.assertIn("Removed entry", output)
        self.assertIn('"alpha"', output)
        self.assertIn('"board_id": "alpha-board"', output)

    def test_unknown_mutations_abort_without_write(self) -> None:
        for command in ("pause", "activate", "remove"):
            with self.subTest(command=command):
                client = FakeClient()
                with self.assertRaisesRegex(registry_admin.RegistryError, "unknown"):
                    invoke(client, command, "missing")
                self.assertEqual(client.writes, [])

    def test_invalid_add_inputs_abort_without_write(self) -> None:
        cases = (
            ("", "/synthetic/beta", "board_id"),
            ("beta-board", "relative/path", "absolute"),
        )
        for board_id, work_dir, message in cases:
            with self.subTest(board_id=board_id, work_dir=work_dir):
                client = FakeClient()
                with self.assertRaisesRegex(registry_admin.RegistryError, message):
                    invoke(
                        client,
                        "add",
                        "beta",
                        "--board-id",
                        board_id,
                        "--work-dir",
                        work_dir,
                    )
                self.assertEqual(client.writes, [])

    def test_malformed_current_document_aborts_before_write(self) -> None:
        client = FakeClient({"schema_version": 1, "projects": []})

        with self.assertRaisesRegex(registry_admin.RegistryError, "projects"):
            invoke(client, "pause", "alpha")

        self.assertEqual(client.writes, [])

    def test_read_back_mismatch_fails_with_diff(self) -> None:
        client = FakeClient(mismatch_after_write=True)

        with self.assertRaisesRegex(
            registry_admin.RegistryError,
            "read-back mismatch",
        ) as caught:
            invoke(
                client,
                "add",
                "beta",
                "--board-id",
                "beta-board",
                "--work-dir",
                "/synthetic/beta",
            )

        self.assertIn("--- expected", str(caught.exception))
        self.assertIn("+++ read-back", str(caught.exception))

    def test_validation_rejects_extra_fields(self) -> None:
        malformed = json.loads(json.dumps(INITIAL))
        malformed["projects"]["alpha"]["unexpected"] = True
        client = FakeClient(malformed)

        with self.assertRaisesRegex(registry_admin.RegistryError, "exactly"):
            invoke(client, "show")

        self.assertEqual(client.writes, [])

    def test_safe_error_redacts_token(self) -> None:
        self.assertEqual(
            registry_admin._safe_error(
                RuntimeError("request SECRET failed"),
                "SECRET",
            ),
            "request [REDACTED] failed",
        )


if __name__ == "__main__":
    unittest.main()
