from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

def _bridge_version() -> str:
    import re as _re
    text = (ROOT / "pyproject.toml").read_text()
    return _re.search(r'version = "([^"]+)"', text).group(1)


CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import seat_admin

REGISTRY = {
    "schema_version": 1,
    "projects": {
        "one": {"board_id": "board-one", "work_dir": "/one", "status": "active"},
        "two": {"board_id": "board-two", "work_dir": "/two", "status": "active"},
        "paused": {
            "board_id": "board-paused",
            "work_dir": "/paused",
            "status": "paused",
        },
    },
}


class StrictFakeBackend:
    """Stateful fake matching Central duplicate-add and auto-join behavior."""

    home_board = "home"
    admin_principal = "PR-admin"

    def __init__(
        self,
        *,
        mismatch: bool = False,
        omitted_agents: int = 0,
        omitted_tickets: int = 0,
    ) -> None:
        self.mismatch = mismatch
        self.remove_failures: set[str] = set()
        self.remove_mismatches: set[str] = set()
        self.omitted_agents = omitted_agents
        self.omitted_tickets = omitted_tickets
        self.calls: list[tuple[str, str, str]] = []
        self.roles: dict[str, dict[str, str]] = {
            board: {
                self.admin_principal: "admin",
                "PR-worker": "member",
                "PR-reviewer": "reviewer",
            }
            for board in ("home", "board-one", "board-two")
        }
        self.roles["board-two"] = {self.admin_principal: "admin"}
        self.agents = {
            "home": [
                (self.admin_principal, "seat-admin", "active"),
                ("PR-worker", "worker-a", "active"),
                ("PR-reviewer", "reviewer-a", "working"),
            ],
            "board-one": [
                (self.admin_principal, "seat-admin", "active"),
                ("PR-worker", "worker-a", "active"),
                ("PR-reviewer", "reviewer-a", "active"),
            ],
            "board-two": [(self.admin_principal, "seat-admin", "active")],
        }
        old = "2020-01-01T00:00:00+00:00"
        self.last_activity = {
            (board, principal, name): old
            for board, agents in self.agents.items()
            for principal, name, _status in agents
        }
        self.tickets: dict[str, list[dict[str, Any]]] = {
            board: [] for board in ("home", "board-one", "board-two")
        }
        self.seats = seat_admin._empty_seat_registry()

    def _auto_join_admin(self, board_id: str) -> None:
        self.roles.setdefault(board_id, {}).setdefault(self.admin_principal, "admin")
        agents = self.agents.setdefault(board_id, [])
        if not any(principal == self.admin_principal for principal, _, _ in agents):
            agents.append((self.admin_principal, "seat-admin", "active"))
            self.last_activity[
                (board_id, self.admin_principal, "seat-admin")
            ] = "2020-01-01T00:00:00+00:00"
        self.tickets.setdefault(board_id, [])

    async def registry(self) -> dict[str, Any]:
        return json.loads(json.dumps(REGISTRY))

    async def seat_registry(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.seats))

    async def write_seat_registry(self, document: dict[str, Any]) -> None:
        validated = seat_admin._validate_seat_registry(document)
        self.calls.append(
            ("state_write", self.home_board, seat_admin.SEAT_REGISTRY_KEY)
        )
        self.seats = json.loads(json.dumps(validated))

    async def members(self, board_id: str) -> dict[str, Any]:
        self._auto_join_admin(board_id)
        rows = []
        for principal, role in self.roles[board_id].items():
            names = [
                name
                for candidate, name, _status in self.agents[board_id]
                if candidate == principal
            ]
            rows.append({"principal_id": principal, "role": role, "agent_names": names})
        return {"members": rows}

    async def snapshot(self, board_id: str) -> dict[str, Any]:
        self._auto_join_admin(board_id)
        return {
            "agents": [
                {
                    "agent_id": f"AI-{board_id}-{principal}-{name}",
                    "principal_id": principal,
                    "agent_name": name,
                    "status": status,
                    "lifecycle_status": status,
                    "last_activity_at": self.last_activity[
                        (board_id, principal, name)
                    ],
                }
                for principal, name, status in self.agents[board_id]
            ],
            "tickets": json.loads(json.dumps(self.tickets[board_id])),
            "omitted_counts": {
                "agents": self.omitted_agents,
                "tickets": self.omitted_tickets,
            },
        }

    async def member_add(self, board_id: str, principal_id: str) -> None:
        self._auto_join_admin(board_id)
        self.calls.append(("add", board_id, principal_id))
        if principal_id in self.roles[board_id]:
            raise ValueError("principal is already a board member")
        if not self.mismatch:
            self.roles[board_id][principal_id] = "member"

    async def member_set_role(
        self, board_id: str, principal_id: str, role: str
    ) -> None:
        self._auto_join_admin(board_id)
        self.calls.append(("set_role", board_id, principal_id))
        if principal_id not in self.roles[board_id]:
            raise ValueError("principal is not a board member")
        self.roles[board_id][principal_id] = role

    async def member_remove(self, board_id: str, principal_id: str) -> None:
        self._auto_join_admin(board_id)
        self.calls.append(("remove", board_id, principal_id))
        if principal_id not in self.roles[board_id]:
            raise ValueError("principal is not a board member")
        if board_id in self.remove_failures:
            raise RuntimeError("sensitive backend failure")
        if self.mismatch or board_id in self.remove_mismatches:
            return
        del self.roles[board_id][principal_id]
        self.agents[board_id] = [
            item for item in self.agents[board_id] if item[0] != principal_id
        ]


def parse(*arguments: str) -> argparse.Namespace:
    return seat_admin.build_parser().parse_args(list(arguments))


def invoke(backend: StrictFakeBackend, *arguments: str) -> str:
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(seat_admin.execute(parse(*arguments), backend))
    return output.getvalue()


class SeatAdminTests(unittest.TestCase):
    def test_packaged_client_adapter_forwards_admin_calls(self) -> None:
        board = seat_admin.SeatBoardClient(
            "https://central.example/mcp",
            "TOKEN_PLACEHOLDER",
            "board-one",
            agent_name="seat-admin",
        )
        calls: list[tuple[str, dict[str, Any]]] = []

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append((name, arguments))
            return {"ok": True}

        board._call = call  # type: ignore[method-assign]

        async def scenario() -> None:
            await board.board_members()
            await board.board_member_add("PR-new")
            await board.board_member_set_role("PR-new", "reviewer")
            await board.board_member_remove("PR-new")

        asyncio.run(scenario())

        self.assertEqual(
            calls,
            [
                ("board_members", {}),
                (
                    "board_member_add",
                    {
                        "agent_name": "seat-admin",
                        "principal_id": "PR-new",
                        "role": "member",
                    },
                ),
                (
                    "board_member_set_role",
                    {
                        "agent_name": "seat-admin",
                        "principal_id": "PR-new",
                        "role": "reviewer",
                    },
                ),
                (
                    "board_member_remove",
                    {
                        "agent_name": "seat-admin",
                        "principal_id": "PR-new",
                    },
                ),
            ],
        )

    def test_built_wheel_imports_and_runs_help_without_source_tree(self) -> None:
        uv = shutil.which("uv")
        self.assertIsNotNone(uv)
        assert uv is not None
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            dist = temp / "dist"
            environment = temp / "venv"
            subprocess.run(
                [uv, "build", "--wheel", "--out-dir", str(dist), str(ROOT)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    uv,
                    "build",
                    "--wheel",
                    "--out-dir",
                    str(dist),
                    str(ROOT.parents[1] / "packages" / "client"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(dist.glob("pursers_wait_bridge-*.whl"))
            subprocess.run(
                [uv, "venv", "--python", "3.12", str(environment)],
                check=True,
                capture_output=True,
                text=True,
            )
            python = environment / "bin" / "python"
            subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--find-links",
                    str(dist),
                    str(wheel),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            imported = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    (
                        "from importlib.metadata import version; "
                        "import registry_admin, registry_doctor, seat_admin; "
                        f"assert version('pursers-wait-bridge') == {_bridge_version()!r}; "
                        "assert version('pursers-client') == '0.1.0a15'; "
                        "assert version('mcp') == '2.1.1'; "
                        "assert hasattr(registry_doctor.LiveBackend, 'board_snapshot'); "
                        "assert hasattr(seat_admin.SeatBoardClient, 'board_member_add'); "
                        "assert hasattr(seat_admin.SeatBoardClient, 'board_member_remove'); "
                        "print(seat_admin.__file__)"
                    ),
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=temp,
            )
            helped = subprocess.run(
                [str(python), "-I", "-m", "seat_admin", "--help"],
                check=True,
                capture_output=True,
                text=True,
                cwd=temp,
            )
            self.assertIn(str(environment), imported.stdout)
            self.assertIn("Provision and inspect", helped.stdout)
            self.assertIn("retire", helped.stdout)
            self.assertIn("prune-stale", helped.stdout)
            doctor_help = subprocess.run(
                [str(python), "-I", "-m", "registry_doctor", "--help"],
                check=True,
                capture_output=True,
                text=True,
                cwd=temp,
            )
            self.assertIn("read-only health check", doctor_help.stdout)

    def test_invalid_identifiers_cause_zero_writes(self) -> None:
        cases = (
            ("invalid name", "PR-new", "board-one", "home"),
            ("valid-name", "invalid principal", "board-one", "home"),
            ("valid-name", "PR-new", "invalid board", "home"),
            ("valid-name", "PR-new", "board-one", "invalid home"),
        )
        for name, principal, board, home in cases:
            with self.subTest(name=name, principal=principal, board=board, home=home):
                backend = StrictFakeBackend()
                backend.home_board = home
                with self.assertRaisesRegex(seat_admin.RegistryError, "must match"):
                    invoke(
                        backend,
                        "add",
                        "--name",
                        name,
                        "--role",
                        "worker",
                        "--boards",
                        board,
                        "--principal",
                        principal,
                        "--token-path",
                        "/tokens/new.jwt",
                    )
                self.assertEqual(backend.calls, [])

    def test_duplicate_name_refuses_before_write(self) -> None:
        backend = StrictFakeBackend()
        with self.assertRaisesRegex(seat_admin.RegistryError, "already in use"):
            invoke(
                backend,
                "add",
                "--name",
                "worker-a",
                "--role",
                "worker",
                "--principal",
                "PR-new",
                "--token-path",
                "/tokens/new.jwt",
            )
        self.assertEqual(backend.calls, [])

    def test_incomplete_pool_scan_fails_closed(self) -> None:
        backend = StrictFakeBackend(omitted_agents=1)
        with self.assertRaisesRegex(seat_admin.RegistryError, "incomplete"):
            invoke(backend, "check", "--name", "worker-a")
        self.assertEqual(backend.calls, [])

    def test_reviewer_add_orders_writes_and_persists_pending_seat(self) -> None:
        backend = StrictFakeBackend()
        output = invoke(
            backend,
            "add",
            "--name",
            "reviewer-b",
            "--role",
            "reviewer",
            "--boards",
            "board-one",
            "--principal",
            "PR-new-reviewer",
            "--token-path",
            "/tokens/reviewer-b.jwt",
        )
        self.assertEqual(
            backend.calls,
            [
                ("add", "board-one", "PR-new-reviewer"),
                ("set_role", "board-one", "PR-new-reviewer"),
                ("state_write", "home", "seat_registry"),
            ],
        )
        self.assertEqual(
            backend.seats["seats"]["reviewer-b"],
            {
                "principal_id": "PR-new-reviewer",
                "role": "reviewer",
                "board_mode": ["board-one"],
            },
        )
        self.assertNotIn("TOKEN_PLACEHOLDER", output)
        checked = json.loads(invoke(backend, "check", "--name", "reviewer-b"))
        self.assertEqual(checked["boards"][0]["status"], "pending")
        self.assertEqual(checked["boards"][0]["role"], "reviewer")

    def test_existing_principal_add_is_idempotent(self) -> None:
        backend = StrictFakeBackend()
        invoke(
            backend,
            "add",
            "--name",
            "worker-b",
            "--role",
            "worker",
            "--boards",
            "board-one",
            "--principal",
            "PR-worker",
            "--token-path",
            "/tokens/worker-b.jwt",
        )
        self.assertEqual(backend.calls, [("state_write", "home", "seat_registry")])
        self.assertEqual(backend.roles["board-one"]["PR-worker"], "member")

    def test_role_conflict_fails_before_write(self) -> None:
        backend = StrictFakeBackend()
        with self.assertRaisesRegex(seat_admin.RegistryError, "incompatible"):
            invoke(
                backend,
                "add",
                "--name",
                "worker-b",
                "--role",
                "worker",
                "--boards",
                "board-one",
                "--principal",
                "PR-reviewer",
                "--token-path",
                "/tokens/worker-b.jwt",
            )
        self.assertEqual(backend.calls, [])

    def test_all_target_roles_are_preflighted_before_first_write(self) -> None:
        backend = StrictFakeBackend()

        with self.assertRaisesRegex(seat_admin.RegistryError, "incompatible"):
            invoke(
                backend,
                "add",
                "--name",
                "worker-b",
                "--role",
                "worker",
                "--boards",
                "fresh-board,board-one",
                "--principal",
                "PR-reviewer",
                "--token-path",
                "/tokens/worker-b.jwt",
            )

        self.assertEqual(backend.calls, [])

    def test_read_back_mismatch_stops_before_role_and_state_write(self) -> None:
        backend = StrictFakeBackend(mismatch=True)
        with self.assertRaisesRegex(seat_admin.RegistryError, "read-back failed"):
            invoke(
                backend,
                "add",
                "--name",
                "reviewer-b",
                "--role",
                "reviewer",
                "--boards",
                "board-one",
                "--principal",
                "PR-missing",
                "--token-path",
                "/tokens/missing.jwt",
            )
        self.assertEqual(backend.calls, [("add", "board-one", "PR-missing")])

    def test_new_board_preserves_auto_joined_admin_and_pending_seat(self) -> None:
        backend = StrictFakeBackend()
        backend.seats["seats"]["pending-reviewer"] = {
            "principal_id": "PR-pending",
            "role": "reviewer",
            "board_mode": "registry",
        }
        output = invoke(backend, "new-board", "--board", "fresh-board")
        self.assertNotIn(("add", "fresh-board", backend.admin_principal), backend.calls)
        self.assertEqual(backend.roles["fresh-board"][backend.admin_principal], "admin")
        self.assertEqual(backend.roles["fresh-board"]["PR-pending"], "reviewer")
        self.assertIn(("add", "fresh-board", "PR-pending"), backend.calls)
        self.assertIn(("set_role", "fresh-board", "PR-pending"), backend.calls)
        self.assertEqual(json.loads(output)["principals_provisioned"], 4)

    def test_check_reports_joined_and_pending_without_write(self) -> None:
        backend = StrictFakeBackend()
        backend.seats["seats"]["pending-worker"] = {
            "principal_id": "PR-pending-worker",
            "role": "worker",
            "board_mode": ["board-one"],
        }
        pending = json.loads(invoke(backend, "check", "--name", "pending-worker"))
        joined = json.loads(invoke(backend, "check", "--name", "reviewer-a"))
        self.assertEqual(pending["boards"][0]["status"], "pending")
        self.assertIsNone(pending["boards"][0]["role"])
        self.assertEqual(
            [(row["board_id"], row["role"], row["status"]) for row in joined["boards"]],
            [("home", "reviewer", "working"), ("board-one", "reviewer", "active")],
        )
        self.assertEqual(backend.calls, [])

    def test_retire_refuses_active_claim_before_any_write(self) -> None:
        backend = StrictFakeBackend()
        backend.tickets["board-one"] = [
            {
                "ticket_id": "TK-active",
                "status": "claimed",
                "claimed_by": "worker-a",
                "claimed_by_agent_id": "AI-board-one-PR-worker-worker-a",
            }
        ]

        with self.assertRaisesRegex(
            seat_admin.RegistryError, "board-one/TK-active"
        ):
            invoke(
                backend,
                "retire",
                "--name",
                "worker-a",
                "--boards",
                "board-one",
                "--force",
            )

        self.assertEqual(backend.calls, [])
        self.assertIn("PR-worker", backend.roles["board-one"])

    def test_retire_requires_force_for_recent_seat_and_verifies_read_back(
        self,
    ) -> None:
        backend = StrictFakeBackend()
        backend.last_activity[("board-one", "PR-worker", "worker-a")] = (
            datetime.now(timezone.utc).isoformat()
        )

        with self.assertRaisesRegex(seat_admin.RegistryError, "pass --force"):
            invoke(
                backend,
                "retire",
                "--name",
                "worker-a",
                "--boards",
                "board-one",
            )
        self.assertEqual(backend.calls, [])

        result = json.loads(
            invoke(
                backend,
                "retire",
                "--name",
                "worker-a",
                "--boards",
                "board-one",
                "--force",
            )
        )
        self.assertEqual(backend.calls, [("remove", "board-one", "PR-worker")])
        self.assertEqual(result["boards_removed"], ["board-one"])
        self.assertEqual(
            result["verified_read_back"],
            [
                {
                    "board_id": "board-one",
                    "principal_id": "PR-worker",
                    "membership_present": False,
                    "agents_present": 0,
                    "verified": True,
                }
            ],
        )

    def test_retire_read_back_mismatch_is_fatal(self) -> None:
        backend = StrictFakeBackend(mismatch=True)
        with self.assertRaisesRegex(seat_admin.RegistryError, "structured audit"):
            invoke(
                backend,
                "retire",
                "--name",
                "worker-a",
                "--boards",
                "board-one",
            )
        self.assertEqual(backend.calls, [("remove", "board-one", "PR-worker")])

    def test_retire_reports_verified_partial_result_when_second_remove_fails(
        self,
    ) -> None:
        backend = StrictFakeBackend()
        backend.remove_failures.add("home")
        backend.seats["seats"]["worker-a"] = {
            "principal_id": "PR-worker",
            "role": "worker",
            "board_mode": ["board-one", "home"],
        }
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            seat_admin.RegistryError, "structured audit"
        ) as raised:
            asyncio.run(
                seat_admin.execute(
                    parse(
                        "retire",
                        "--name",
                        "worker-a",
                        "--boards",
                        "board-one,home",
                    ),
                    backend,
                )
            )

        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "partial-failure")
        self.assertEqual(result["verified_read_back"][0]["board_id"], "board-one")
        self.assertEqual(result["failed_board"], "home")
        self.assertEqual(result["pending_boards"], [])
        self.assertIs(result["seat_registry_preserved"], True)
        self.assertNotIn("sensitive backend failure", output.getvalue())
        self.assertNotIn("sensitive backend failure", str(raised.exception))
        self.assertNotIn("PR-worker", backend.roles["board-one"])
        self.assertIn("PR-worker", backend.roles["home"])
        self.assertIn("worker-a", backend.seats["seats"])
        self.assertFalse(any(call[0] == "state_write" for call in backend.calls))

    def test_retire_reports_verified_partial_result_on_second_readback_mismatch(
        self,
    ) -> None:
        backend = StrictFakeBackend()
        backend.remove_mismatches.add("home")
        backend.seats["seats"]["worker-a"] = {
            "principal_id": "PR-worker",
            "role": "worker",
            "board_mode": ["board-one", "home"],
        }
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            seat_admin.RegistryError, "structured audit"
        ):
            asyncio.run(
                seat_admin.execute(
                    parse(
                        "retire",
                        "--name",
                        "worker-a",
                        "--boards",
                        "board-one,home",
                    ),
                    backend,
                )
            )

        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "partial-failure")
        self.assertEqual(result["verified_read_back"][0]["board_id"], "board-one")
        self.assertEqual(result["failed_board"], "home")
        self.assertEqual(result["failed_board_state"], "unverified")
        self.assertEqual(result["pending_boards"], [])
        self.assertIs(result["seat_registry_preserved"], True)
        self.assertIn("PR-worker", backend.roles["home"])
        self.assertIn("worker-a", backend.seats["seats"])
        self.assertFalse(any(call[0] == "state_write" for call in backend.calls))

    def test_retire_duplicate_name_requires_principal_disambiguation(self) -> None:
        backend = StrictFakeBackend()
        backend.roles["board-two"]["PR-other"] = "member"
        backend.agents["board-two"].append(("PR-other", "worker-a", "stale"))
        backend.last_activity[("board-two", "PR-other", "worker-a")] = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).isoformat()

        with self.assertRaisesRegex(seat_admin.RegistryError, "--principal is required"):
            invoke(backend, "retire", "--name", "worker-a")
        self.assertEqual(backend.calls, [])

        result = json.loads(
            invoke(
                backend,
                "retire",
                "--name",
                "worker-a",
                "--boards",
                "board-one",
                "--principal",
                "PR-worker",
            )
        )
        self.assertEqual(result["principal_id"], "PR-worker")
        self.assertNotIn("PR-worker", backend.roles["board-one"])
        self.assertIn("PR-other", backend.roles["board-two"])

    def test_prune_stale_defaults_to_dry_run_with_zero_writes(self) -> None:
        backend = StrictFakeBackend()
        result = json.loads(
            invoke(backend, "prune-stale", "--older-than-days", "30")
        )

        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(
            [item["principal_id"] for item in result["plan"]], ["PR-worker"]
        )
        self.assertEqual(backend.calls, [])

    def test_prune_commit_refuses_active_claim_before_any_write(self) -> None:
        backend = StrictFakeBackend()
        backend.tickets["board-one"] = [
            {
                "ticket_id": "TK-active",
                "status": "in_progress",
                "claimed_by": "worker-a",
                "claimed_by_agent_id": "AI-board-one-PR-worker-worker-a",
            }
        ]

        dry_run = json.loads(
            invoke(backend, "prune-stale", "--older-than-days", "30")
        )
        self.assertEqual(
            dry_run["plan"][0]["active_claims"][0]["ticket_id"], "TK-active"
        )
        with self.assertRaisesRegex(seat_admin.RegistryError, "TK-active"):
            invoke(
                backend,
                "prune-stale",
                "--older-than-days",
                "30",
                "--commit",
            )
        self.assertEqual(backend.calls, [])

    def test_retire_refuses_incomplete_ticket_scan(self) -> None:
        backend = StrictFakeBackend(omitted_tickets=1)
        with self.assertRaisesRegex(seat_admin.RegistryError, "incomplete"):
            invoke(
                backend,
                "retire",
                "--name",
                "worker-a",
                "--boards",
                "board-one",
            )
        self.assertEqual(backend.calls, [])

    def test_prune_stale_excludes_reviewer_and_protected_names(self) -> None:
        backend = StrictFakeBackend()
        result = json.loads(
            invoke(
                backend,
                "prune-stale",
                "--older-than-days",
                "30",
                "--dry-run",
                "--protect",
                "worker-a",
            )
        )

        self.assertEqual(result["plan"], [])
        excluded = {
            item["principal_id"]: item["reasons"] for item in result["excluded"]
        }
        self.assertIn("protected-name", excluded["PR-worker"])
        self.assertIn("protected-role", excluded["PR-reviewer"])
        self.assertIn("protected-role", excluded[backend.admin_principal])
        self.assertEqual(backend.calls, [])

    def test_prune_commit_does_not_orphan_sessionless_membership(self) -> None:
        backend = StrictFakeBackend()
        backend.roles["board-two"]["PR-worker"] = "member"
        backend.seats["seats"]["worker-a"] = {
            "principal_id": "PR-worker",
            "role": "worker",
            "board_mode": "registry",
        }

        result = json.loads(
            invoke(
                backend,
                "prune-stale",
                "--older-than-days",
                "30",
                "--commit",
            )
        )

        self.assertEqual(result["plan"], [])
        worker = next(
            item
            for item in result["excluded"]
            if item["principal_id"] == "PR-worker"
        )
        self.assertIn("missing-activity-evidence", worker["reasons"])
        self.assertEqual(worker["missing_activity_boards"], ["board-two"])
        self.assertEqual(backend.calls, [])
        self.assertIn("PR-worker", backend.roles["home"])
        self.assertIn("PR-worker", backend.roles["board-one"])
        self.assertIn("PR-worker", backend.roles["board-two"])
        self.assertIn("worker-a", backend.seats["seats"])

    def test_prune_excludes_sessionless_reviewer_membership_with_zero_writes(
        self,
    ) -> None:
        backend = StrictFakeBackend()
        backend.roles["board-two"]["PR-worker"] = "reviewer"

        result = json.loads(
            invoke(
                backend,
                "prune-stale",
                "--older-than-days",
                "30",
                "--commit",
            )
        )

        worker = next(
            item
            for item in result["excluded"]
            if item["principal_id"] == "PR-worker"
        )
        self.assertIn("protected-role", worker["reasons"])
        self.assertIn("missing-activity-evidence", worker["reasons"])
        self.assertEqual(worker["missing_activity_boards"], ["board-two"])
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.roles["board-two"]["PR-worker"], "reviewer")

    def test_prune_stale_commit_removes_and_verifies_worker_only(self) -> None:
        backend = StrictFakeBackend()
        backend.seats["seats"]["worker-a"] = {
            "principal_id": "PR-worker",
            "role": "worker",
            "board_mode": "registry",
        }
        result = json.loads(
            invoke(
                backend,
                "prune-stale",
                "--older-than-days",
                "30",
                "--commit",
            )
        )

        self.assertEqual(result["mode"], "commit")
        self.assertEqual(
            backend.calls,
            [
                ("remove", "board-one", "PR-worker"),
                ("remove", "home", "PR-worker"),
                ("state_write", "home", "seat_registry"),
            ],
        )
        self.assertNotIn("worker-a", backend.seats["seats"])
        self.assertIn("PR-reviewer", backend.roles["board-one"])
        self.assertTrue(all(item["verified"] for item in result["verified_read_back"]))


if __name__ == "__main__":
    unittest.main()
