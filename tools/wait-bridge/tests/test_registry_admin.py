from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import pytest
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
CLIENT_SRC = REPOSITORY / "packages" / "client" / "src"
CENTRAL_SRC = REPOSITORY / "packages" / "central" / "src" / "pursers_central"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(CENTRAL_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import central  # noqa: E402
import pursers_client.client as client_module  # noqa: E402
import registry_admin  # noqa: E402
from pursers_client import BoardClient, BoardClientError  # noqa: E402


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
MISSING = object()


class FakeClient:
    def __init__(
        self,
        document: Any = INITIAL,
        *,
        mismatch_after_write: bool = False,
        concurrent_document: Any = None,
    ) -> None:
        self.agent_name = "registry-test"
        self.value = None if document is MISSING else json.dumps(document)
        self.mismatch_after_write = mismatch_after_write
        self.concurrent_document = concurrent_document
        self.get_calls = 0
        self.writes: list[tuple[str, str, str | None, bool]] = []

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        self.get_calls += 1
        if self.value is None:
            raise registry_admin.BoardClientError("state key not found")
        value = self.value
        if self.mismatch_after_write and self.writes:
            value = json.dumps(INITIAL)
        return {"state": {"key": key, "value": value}}

    async def board_state_update(
        self, key: str, value: str, *, expected_sha256: str | None = None
    ) -> dict[str, Any]:
        return await self._update(key, value, expected_sha256, False)

    async def _call(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if name != "board_state_update":
            raise AssertionError(f"unexpected tool call {name}")
        return await self._update(
            arguments["key"],
            arguments["value"],
            arguments.get("expected_sha256"),
            arguments.get("expected_absent", False),
        )

    async def _update(
        self,
        key: str,
        value: str,
        expected_sha256: str | None,
        expected_absent: bool,
    ) -> dict[str, Any]:
        if self.concurrent_document is not None:
            self.value = json.dumps(self.concurrent_document)
            self.concurrent_document = None
        if expected_absent and self.value is not None:
            raise registry_admin.BoardClientError("state precondition failed")
        self.writes.append((key, value, expected_sha256, expected_absent))
        self.value = value
        return {"ok": True}

    def document(self) -> dict[str, Any]:
        assert self.value is not None
        return json.loads(self.value)


class ContextClient(FakeClient):
    async def __aenter__(self) -> ContextClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def parse(*arguments: str) -> argparse.Namespace:
    return registry_admin.build_parser().parse_args(list(arguments))


def invoke(client: FakeClient, *arguments: str) -> str:
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(registry_admin.execute(parse(*arguments), client))
    return output.getvalue()


class RegistryAdminTests(unittest.TestCase):
    def test_home_board_defaults_to_env_then_pursers(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(parse("show").home_board, "pursers")
        with patch.dict(os.environ, {"ONBOARD_BOARD_ID": "env-home"}, clear=True):
            self.assertEqual(parse("show").home_board, "env-home")
        with patch.dict(os.environ, {"ONBOARD_BOARD_ID": "env-home"}, clear=True):
            self.assertEqual(
                parse("--home-board", "explicit-home", "show").home_board,
                "explicit-home",
            )

    def test_help_documents_home_board_override(self) -> None:
        help_text = registry_admin.build_parser().format_help()

        self.assertIn("--home-board", help_text)
        self.assertIn("ONBOARD_BOARD_ID", help_text)

    def test_run_binds_all_registry_io_to_selected_home_board(self) -> None:
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        client = ContextClient()

        def factory(*args: Any, **kwargs: Any) -> ContextClient:
            calls.append((args, kwargs))
            return client

        args = parse("--home-board", "alternate-home", "show")
        asyncio.run(registry_admin.run(args, client_factory=factory))

        self.assertEqual(calls[0][0][2], "alternate-home")
        self.assertEqual(client.get_calls, 1)
        self.assertEqual(client.writes, [])

    def test_invalid_home_board_fails_before_client_open(self) -> None:
        opened = False

        def factory(*args: Any, **kwargs: Any) -> ContextClient:
            nonlocal opened
            opened = True
            return ContextClient()

        for invalid in ("", "bad/board", " spaced ", "x" * 81):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    registry_admin.RegistryError,
                    "home board id must match",
                ):
                    asyncio.run(
                        registry_admin.run(
                            parse("--home-board", invalid, "show"),
                            client_factory=factory,
                        )
                    )
                self.assertFalse(opened)

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
        self.assertEqual(
            client.writes[0][2],
            hashlib.sha256(json.dumps(INITIAL).encode()).hexdigest(),
        )
        self.assertFalse(client.writes[0][3])

    def test_missing_state_first_add_uses_create_only_precondition(self) -> None:
        client = FakeClient(MISSING)

        output = invoke(
            client,
            "add",
            "alpha",
            "--board-id",
            "alpha-board",
            "--work-dir",
            "/synthetic/alpha",
        )

        self.assertEqual(json.loads(output), INITIAL)
        self.assertEqual(client.writes[0][2], None)
        self.assertTrue(client.writes[0][3])

    def test_missing_state_concurrent_creation_fails_without_overwrite(self) -> None:
        concurrent = {
            "schema_version": 1,
            "projects": {
                "winner": {
                    "board_id": "winner-board",
                    "work_dir": "/synthetic/winner",
                    "status": "active",
                }
            },
        }
        client = FakeClient(MISSING, concurrent_document=concurrent)

        with self.assertRaisesRegex(
            registry_admin.BoardClientError, "state precondition failed"
        ):
            invoke(
                client,
                "add",
                "alpha",
                "--board-id",
                "alpha-board",
                "--work-dir",
                "/synthetic/alpha",
            )

        self.assertEqual(client.document(), concurrent)
        self.assertEqual(client.writes, [])

    def test_add_accepts_fleet_clone_routing_fields(self) -> None:
        client = FakeClient()

        invoke(
            client,
            "add",
            "beta",
            "--board-id",
            "beta-board",
            "--work-dir",
            "/operator/beta",
            "--work-dir-owner",
            "operator",
            "--fleet-clone-dir",
            "/fleet/beta",
        )
        self.assertEqual(
            client.document()["projects"]["beta"],
            {
                "board_id": "beta-board",
                "work_dir": "/operator/beta",
                "work_dir_owner": "operator",
                "fleet_clone_dir": "/fleet/beta",
                "status": "active",
            },
        )

    def test_add_and_set_exact_repository_url(self) -> None:
        client = FakeClient()

        invoke(
            client,
            "add", "beta", "--board-id", "beta-board",
            "--work-dir", "/operator/beta",
            "--repository-url", "https://example.test/acme/beta",
        )
        self.assertEqual(
            client.document()["projects"]["beta"]["repository_url"],
            "https://example.test/acme/beta",
        )

        invoke(
            client,
            "set-repository-url", "beta", "https://example.test/acme/beta-v2",
        )
        self.assertEqual(
            client.document()["projects"]["beta"]["repository_url"],
            "https://example.test/acme/beta-v2",
        )

    def test_add_accepts_domain_and_integration_ref_for_intake_onboarding(self) -> None:
        client = FakeClient()

        invoke(
            client,
            "add",
            "beta",
            "--board-id",
            "beta-board",
            "--work-dir",
            "/operator/beta",
            "--repository-url",
            "https://example.test/acme/beta",
            "--integration-ref",
            "release/next",
            "--domain",
            "work",
        )

        self.assertEqual(
            client.document()["projects"]["beta"],
            {
                "board_id": "beta-board",
                "work_dir": "/operator/beta",
                "repository_url": "https://example.test/acme/beta",
                "integration_ref": "release/next",
                "domain": "work",
                "status": "active",
            },
        )

    def test_repository_url_validation_and_same_board_uniqueness(self) -> None:
        client = FakeClient()
        with self.assertRaisesRegex(registry_admin.RegistryError, "HTTPS"):
            invoke(
                client, "set-repository-url", "alpha", "http://example.test/alpha"
            )
        self.assertEqual(client.writes, [])

        duplicate = json.loads(json.dumps(INITIAL))
        duplicate["projects"]["alpha"]["repository_url"] = (
            "https://example.test/acme/shared"
        )
        duplicate["projects"]["beta"] = {
            "board_id": "alpha-board",
            "work_dir": "/synthetic/beta",
            "repository_url": "https://example.test/acme/shared",
            "status": "active",
        }
        with self.assertRaisesRegex(registry_admin.RegistryError, "same active"):
            invoke(FakeClient(duplicate), "show")

    def test_operator_only_flag_persists_fleet_false(self) -> None:
        client = FakeClient()

        invoke(
            client,
            "add", "operator-app", "--board-id", "operator-board",
            "--work-dir", "/operator/app", "--operator-only",
        )

        self.assertIs(
            client.document()["projects"]["operator-app"]["fleet"], False
        )

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

    def test_validation_rejects_non_boolean_fleet_flag(self) -> None:
        malformed = json.loads(json.dumps(INITIAL))
        malformed["projects"]["alpha"]["fleet"] = "false"
        client = FakeClient(malformed)

        with self.assertRaisesRegex(registry_admin.RegistryError, "fleet must be boolean"):
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

    def test_packaged_cli_uses_non_default_home_board(self) -> None:
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
            script = textwrap.dedent(
                """
                import json
                import registry_admin

                expected_board = "packaged-home"

                class FakeClient:
                    def __init__(self, central_url, token, board_id, **kwargs):
                        if board_id != expected_board:
                            raise AssertionError(f"wrong board: {board_id}")

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        return None

                    async def board_state_get(self, key=None):
                        return {"state": {"key": key, "value": json.dumps({
                            "schema_version": 1,
                            "projects": {},
                        })}}

                registry_admin.BoardClient = FakeClient
                raise SystemExit(registry_admin.main([
                    "--home-board", expected_board, "show"
                ]))
                """
            )
            completed = subprocess.run(
                [str(python), "-I", "-c", script],
                check=True,
                capture_output=True,
                text=True,
                cwd=temp,
                env={**os.environ, "ONBOARD_CENTRAL_TOKEN": "TOKEN_PLACEHOLDER"},
            )
            self.assertEqual(
                json.loads(completed.stdout),
                {"schema_version": 1, "projects": {}},
            )


class RegistryAdminRealCentralTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        jwks = self.root / "jwks.json"
        jwks.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
                "ONBOARD_CENTRAL_TOKEN": "TOKEN_PLACEHOLDER",
            },
        )
        self.environment.start()
        self.mcp, self.store = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        scopes = frozenset({"board:read", "board:write"})
        self.owner = central.Principal("PR-registry-owner", "registry-owner", scopes)
        self.stranger = central.Principal(
            "PR-registry-stranger", "registry-stranger", scopes
        )
        self.principal = self.owner
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temporary.cleanup()

    @asynccontextmanager
    async def _http(self):
        yield object()

    def _client_factory(self, *args: Any, **kwargs: Any) -> BoardClient:
        client = BoardClient(*args, **kwargs)
        client._http = self._http  # type: ignore[method-assign]
        return client

    async def test_sequential_commands_reuse_default_name_and_deny_stranger(
        self,
    ) -> None:
        with patch.object(
            client_module, "streamable_http_client", return_value=self.mcp
        ):
            async with self._client_factory(
                "http://central.invalid/mcp",
                "TOKEN_PLACEHOLDER",
                registry_admin.HOME_BOARD_ID,
                agent_name="project-registry-admin",
                allow_takeover=True,
            ) as client:
                await client.board_state_update(
                    registry_admin.REGISTRY_KEY,
                    json.dumps(INITIAL),
                )

            show_output = io.StringIO()
            with redirect_stdout(show_output):
                await registry_admin.run(parse("show"), self._client_factory)
            pause_output = io.StringIO()
            with redirect_stdout(pause_output):
                await registry_admin.run(
                    parse("pause", "alpha"), self._client_factory
                )

            self.principal = self.stranger
            with self.assertRaisesRegex(
                BoardClientError, "board access denied: invite required"
            ):
                await registry_admin.run(parse("show"), self._client_factory)

            self.principal = self.owner
            final_output = io.StringIO()
            with redirect_stdout(final_output):
                await registry_admin.run(parse("show"), self._client_factory)

        self.assertEqual(json.loads(show_output.getvalue()), INITIAL)
        self.assertEqual(
            json.loads(pause_output.getvalue())["projects"]["alpha"]["status"],
            "paused",
        )
        self.assertEqual(
            json.loads(final_output.getvalue())["projects"]["alpha"]["status"],
            "paused",
        )


if __name__ == "__main__":
    unittest.main()


def test_delivery_policy_survives_admin_and_doctor_roundtrip():
    import copy
    import registry_doctor
    from pursers_client.project_registry import parse_project_registry
    raw=copy.deepcopy(INITIAL)
    policy={'mode':'integration','base_branch':'dev'}
    raw['delivery_defaults']=policy
    raw['projects']['alpha'].update(delivery_workflow=policy,integration_ref='dev',domain='work')
    validated=registry_admin.validate_registry(raw)
    assert validated['projects']['alpha']['integration_ref']=='pursers-integration'
    for parsed in (parse_project_registry({'state':{'value':json.dumps(raw)}}),registry_doctor.parse_registry({'state':{'value':validated}})):
        assert parsed['projects']['alpha']['integration_ref']=='pursers-integration'
        assert parsed['projects']['alpha']['delivery_workflow']['base_branch']=='dev'
        assert parsed['delivery_defaults']['integration_branch']=='pursers-integration'
    assert raw['projects']['alpha']['integration_ref']=='dev'  # no caller mutation


def test_configurable_delivery_policy_hierarchy_survives_admin_and_doctor_roundtrip():
    import copy
    import registry_doctor
    from pursers_client.project_registry import parse_project_registry
    raw = copy.deepcopy(INITIAL)
    raw['delivery_policy_defaults'] = {'validation': {'required_reviewers': 2}}
    raw['delivery_policy_groups'] = {'backend': {'conflict_policy': 'pause'}}
    raw['projects']['alpha'].update(
        delivery_policy_group='backend',
        delivery_policy={'mode': 'branch_only', 'final_pr_target': None},
        delivery_policy_activation={
            'schema_version': 1,
            'state': 'active',
            'policy_revision': 'a' * 40,
            'activation_id': 'policy:' + 'a' * 40,
        },
    )
    validated = registry_admin.validate_registry(raw)
    parsed_client = parse_project_registry({'state': {'value': json.dumps(validated)}})
    parsed_doctor = registry_doctor.parse_registry({'state': {'value': validated}})
    for parsed in (validated, parsed_client, parsed_doctor):
        assert parsed['delivery_policy_defaults']['validation']['required_reviewers'] == 2
        assert parsed['delivery_policy_groups']['backend']['conflict_policy'] == 'pause'
        assert parsed['projects']['alpha']['delivery_policy_group'] == 'backend'
        assert parsed['projects']['alpha']['delivery_policy']['final_pr_target'] is None
        assert parsed['projects']['alpha']['delivery_policy_activation']['policy_revision'] == 'a' * 40


def test_registry_loaders_reject_malformed_delivery_activation():
    import copy
    import registry_doctor
    raw = copy.deepcopy(INITIAL)
    raw['projects']['alpha']['delivery_policy_activation'] = {'state': 'active'}
    with pytest.raises(registry_admin.RegistryError, match='delivery_policy_activation'):
        registry_admin.validate_registry(raw)
    with pytest.raises(registry_doctor.DoctorError, match='delivery_policy_activation'):
        registry_doctor.parse_registry({'state': {'value': raw}})
