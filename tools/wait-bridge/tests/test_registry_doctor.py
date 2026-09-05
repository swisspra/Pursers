from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))

MODULE_PATH = ROOT / "registry_doctor.py"
SPEC = importlib.util.spec_from_file_location("registry_doctor", MODULE_PATH)
assert SPEC and SPEC.loader
doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
SPEC.loader.exec_module(doctor)

NOW = datetime(2030, 1, 8, 12, tzinfo=timezone.utc)
TOKEN = "TOKEN-DO-NOT-PRINT"


def stamp(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def completed(
    returncode: int = 0, stdout: str = "true\n"
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout, "")


class FakeBackend:
    def __init__(self, work_dir: Path) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.status_errors: dict[str, Exception] = {}
        self.snapshot_errors: dict[str, Exception] = {}
        self.registry: Any = {
            "schema_version": 1,
            "projects": {
                "alpha": {
                    "board_id": "alpha-board",
                    "work_dir": str(work_dir),
                    "work_dir_owner": "fleet",
                    "status": "active",
                }
            },
        }
        self.coordinator: Any = {
            "schema_version": 1,
            "generated_at": stamp(30),
            "findings": [],
        }
        self.snapshots: dict[str, dict[str, Any]] = {
            "home": {
                "board": {"board_id": "home"},
                "agents": [],
                "tickets": [],
                "omitted_counts": {"agents": 0, "tickets": 0},
            },
            "alpha-board": {
                "board": {"board_id": "alpha-board"},
                "agents": [],
                "tickets": [],
                "omitted_counts": {"agents": 0, "tickets": 0},
            },
        }

    async def board_status(self, board_id: str) -> dict[str, Any]:
        self.calls.append(("board_status", board_id))
        if board_id in self.status_errors:
            raise self.status_errors[board_id]
        return {"ok": True, "board_id": board_id}

    async def board_snapshot(self, board_id: str) -> dict[str, Any]:
        self.calls.append(("board_snapshot", board_id))
        if board_id in self.snapshot_errors:
            raise self.snapshot_errors[board_id]
        return self.snapshots[board_id]

    async def board_state_get(self, board_id: str, key: str) -> dict[str, Any]:
        self.calls.append(("board_state_get", board_id, key))
        value = self.registry if key == doctor.REGISTRY_KEY else self.coordinator
        return {"state": {"key": key, "value": json.dumps(value)}}


def fresh_stats(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "days": {NOW.date().isoformat(): {"seats": {}}},
            }
        ),
        encoding="utf-8",
    )
    return path


def run_report(
    backend: FakeBackend,
    root: Path,
    *,
    git_runner: doctor.GitRunner | None = None,
    stats_path: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        doctor.evaluate(
            backend,
            home_board="home",
            token=TOKEN,
            now=NOW,
            stats_path=stats_path or fresh_stats(root / "stats.json"),
            git_runner=git_runner or (lambda _path, _args: completed()),
        )
    )


def rows(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {item["check"]: item for item in report["checks"]}


class RegistryDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def backend(self) -> FakeBackend:
        return FakeBackend(self.root)

    def report(
        self,
        backend: FakeBackend,
        *,
        git_runner: doctor.GitRunner | None = None,
        stats_path: Path | None = None,
    ) -> dict[str, Any]:
        return run_report(
            backend,
            self.root,
            git_runner=git_runner,
            stats_path=stats_path,
        )

    def test_all_checks_pass_with_bounded_read_only_calls(self) -> None:
        backend = self.backend()

        report = self.report(backend)

        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(
            all(item["status"] == "PASS" for item in report["checks"])
        )
        self.assertLessEqual(
            {call[0] for call in backend.calls},
            {"board_status", "board_snapshot", "board_state_get"},
        )

    def test_central_and_registry_failures_are_fail(self) -> None:
        backend = self.backend()
        backend.status_errors["home"] = PermissionError(f"denied {TOKEN}")
        backend.registry = {"schema_version": 1, "projects": []}

        report = self.report(backend)
        checks = rows(report)

        self.assertEqual(checks["central"]["status"], "FAIL")
        self.assertEqual(checks["registry"]["status"], "FAIL")
        self.assertEqual(report["exit_code"], 2)

    def test_project_workdir_git_and_ref_matrix(self) -> None:
        missing = self.backend()
        missing.registry["projects"]["alpha"]["work_dir"] = str(
            self.root / "missing"
        )
        self.assertEqual(
            rows(self.report(missing))["project:alpha"]["status"], "FAIL"
        )

        non_git = self.backend()
        non_git.registry["projects"]["alpha"]["git_repo"] = False

        def must_not_run(_path: Path, _arguments: Any) -> Any:
            raise AssertionError("explicit non-git project must not run git")

        explicit = self.report(non_git, git_runner=must_not_run)
        self.assertEqual(rows(explicit)["project:alpha"]["status"], "PASS")

        bad_ref = self.backend()

        def fail_ref(
            _path: Path, arguments: Any
        ) -> subprocess.CompletedProcess[str]:
            return completed(1, "") if "--verify" in arguments else completed()

        unresolved = self.report(bad_ref, git_runner=fail_ref)
        self.assertEqual(rows(unresolved)["project:alpha"]["status"], "FAIL")
        self.assertIn("not resolvable", rows(unresolved)["project:alpha"]["detail"])

    def test_operator_checkout_is_refused_until_fleet_clone_is_configured(self) -> None:
        backend = self.backend()
        entry = backend.registry["projects"]["alpha"]
        entry["work_dir_owner"] = "operator"

        unsafe = rows(self.report(backend))
        self.assertEqual(unsafe["seat-workdir:alpha"]["status"], "FAIL")
        self.assertIn(
            "operator checkout is read-only for seats",
            unsafe["seat-workdir:alpha"]["detail"],
        )

        clone = self.root / "fleet-clone"
        clone.mkdir()
        entry["fleet_clone_dir"] = str(clone)
        seen: list[Path] = []

        def git_runner(path: Path, _arguments: Any) -> subprocess.CompletedProcess[str]:
            seen.append(path)
            return completed()

        safe = rows(self.report(backend, git_runner=git_runner))
        self.assertEqual(safe["seat-workdir:alpha"]["status"], "PASS")
        self.assertEqual(set(seen), {clone})

    def test_board_access_and_snapshot_failures_are_fail(self) -> None:
        backend = self.backend()
        backend.status_errors["alpha-board"] = ConnectionError("offline")
        backend.snapshot_errors["alpha-board"] = TimeoutError("slow")

        checks = rows(self.report(backend))

        self.assertEqual(checks["board:alpha-board"]["status"], "FAIL")
        self.assertEqual(checks["snapshot:alpha-board"]["status"], "FAIL")

    def test_snapshot_truncation_is_warn_with_counts(self) -> None:
        backend = self.backend()
        backend.snapshots["alpha-board"]["truncated"] = True
        backend.snapshots["alpha-board"]["omitted_counts"] = {
            "agents": 2,
            "tickets": 3,
        }

        checks = rows(self.report(backend))
        check = checks["snapshot:alpha-board"]

        self.assertEqual(check["status"], "WARN")
        self.assertIn("agents=2", check["detail"])
        self.assertIn("tickets=3", check["detail"])
        self.assertEqual(checks["seats"]["status"], "WARN")
        self.assertIn("scan incomplete", checks["seats"]["detail"])
        self.assertEqual(checks["claims"]["status"], "WARN")
        self.assertEqual(checks["review-backlog"]["status"], "WARN")

    def test_seat_duplicates_and_staleness_are_warn(self) -> None:
        backend = self.backend()
        backend.snapshots["home"]["agents"] = [
            {
                "agent_name": "pool-worker",
                "principal_id": "PR-one",
                "last_activity_at": stamp(301),
            }
        ]
        backend.snapshots["alpha-board"]["agents"] = [
            {
                "agent_name": "pool-worker",
                "principal_id": "PR-two",
                "last_activity_at": stamp(10),
            }
        ]

        check = rows(self.report(backend))["seats"]

        self.assertEqual(check["status"], "WARN")
        self.assertIn("duplicate names", check["detail"])
        self.assertIn("stale seats", check["detail"])

    def test_expired_claim_and_review_backlog_are_warn(self) -> None:
        backend = self.backend()
        backend.snapshots["alpha-board"]["tickets"] = [
            {
                "ticket_id": "TK-expired",
                "status": "claimed",
                "lease_expires_at": stamp(1),
            },
            {
                "ticket_id": "TK-review",
                "status": "submitted",
                "submitted_at": stamp(1_801),
            },
        ]

        checks = rows(self.report(backend))

        self.assertEqual(checks["claims"]["status"], "WARN")
        self.assertIn("TK-expired", checks["claims"]["detail"])
        self.assertEqual(checks["review-backlog"]["status"], "WARN")
        self.assertIn("TK-review", checks["review-backlog"]["detail"])

    def test_coordinator_freshness_matrix(self) -> None:
        cases = (
            ({"generated_at": stamp(301)}, "WARN", "coordinator may be down"),
            ({"findings": []}, "WARN", "unavailable"),
            ({"generated_at": stamp(300)}, "PASS", "age=300s"),
        )
        for coordinator, expected, detail in cases:
            with self.subTest(coordinator=coordinator):
                backend = self.backend()
                backend.coordinator = coordinator
                check = rows(self.report(backend))["coordinator"]
                self.assertEqual(check["status"], expected)
                self.assertIn(detail, check["detail"])

    def test_bridge_stats_warn_matrix(self) -> None:
        for mode in ("missing", "malformed", "stale"):
            with self.subTest(mode=mode):
                backend = self.backend()
                path = self.root / f"{mode}.json"
                if mode == "malformed":
                    path.write_text("not-json", encoding="utf-8")
                elif mode == "stale":
                    path.write_text(
                        json.dumps(
                            {"schema_version": 1, "days": {"2030-01-07": {}}}
                        ),
                        encoding="utf-8",
                    )
                check = rows(self.report(backend, stats_path=path))["bridge-stats"]
                self.assertEqual(check["status"], "WARN")

    def test_supported_bridge_stats_schemas_are_healthy(self) -> None:
        for schema_version in (2, 3):
            with self.subTest(schema_version=schema_version):
                backend = self.backend()
                path = self.root / f"v{schema_version}-stats.json"
                document = {
                    "schema_version": schema_version,
                    "days": {"2030-01-08": {"seats": {}}},
                    "poll_cycles": {},
                }
                if schema_version == 3:
                    document["model_wait"] = {}
                path.write_text(json.dumps(document), encoding="utf-8")

                check = rows(self.report(backend, stats_path=path))["bridge-stats"]

                self.assertEqual(check["status"], "PASS")

    def test_exit_code_aggregates_worst_status(self) -> None:
        checks = [
            doctor.Check("PASS", "a", "ok"),
            doctor.Check("WARN", "b", "warning"),
        ]
        self.assertEqual(doctor.report_document(checks, NOW)["exit_code"], 1)
        checks.append(doctor.Check("FAIL", "c", "failure"))
        self.assertEqual(doctor.report_document(checks, NOW)["exit_code"], 2)

    def test_credential_never_appears_in_human_or_json_output(self) -> None:
        backend = self.backend()
        backend.status_errors["home"] = PermissionError(f"auth failed for {TOKEN}")
        backend.snapshot_errors["home"] = RuntimeError(f"transport used {TOKEN}")

        report = self.report(backend)
        human = doctor.render_human(report)
        machine = json.dumps(report)

        self.assertNotIn(TOKEN, human)
        self.assertNotIn(TOKEN, machine)
        self.assertTrue(
            all(
                len(item["detail"]) <= doctor.MAX_DETAIL_CHARS
                for item in report["checks"]
            )
        )

    def test_live_backend_uses_bounded_snapshot_and_has_no_write_surface(self) -> None:
        calls: list[tuple[Any, ...]] = []

        class Client:
            def __init__(
                self, _url: str, _token: str, board_id: str, *, agent_name: str
            ) -> None:
                self.board_id = board_id

            async def __aenter__(self) -> "Client":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def board_snapshot(self, **kwargs: Any) -> dict[str, Any]:
                calls.append(("snapshot", self.board_id, kwargs))
                return {}

        backend = doctor.LiveBackend("https://board.invalid", TOKEN, "doctor", Client)
        asyncio.run(backend.board_snapshot("alpha"))

        self.assertEqual(
            calls,
            [
                (
                    "snapshot",
                    "alpha",
                    {
                        "limit": doctor.SNAPSHOT_LIMIT,
                        "max_bytes": doctor.SNAPSHOT_MAX_BYTES,
                    },
                )
            ],
        )
        self.assertFalse(hasattr(backend, "board_state_update"))

    def test_cli_defaults_and_positive_threshold_validation(self) -> None:
        token_path = self.root / "token"
        token_path.write_text("placeholder", encoding="utf-8")
        args = doctor.build_parser().parse_args(
            ["--token-path", str(token_path), "--json"]
        )
        self.assertEqual(args.review_backlog_seconds, 1_800)
        self.assertTrue(args.json_output)
        args.review_backlog_seconds = 0
        with self.assertRaisesRegex(doctor.DoctorError, "positive"):
            asyncio.run(doctor.run(args))


if __name__ == "__main__":
    unittest.main()
