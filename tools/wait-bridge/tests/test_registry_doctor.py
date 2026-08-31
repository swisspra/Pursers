from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


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


def completed(returncode: int = 0, stdout: str = "true\n") -> subprocess.CompletedProcess[str]:
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
    tmp_path: Path,
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
            stats_path=stats_path or fresh_stats(tmp_path / "stats.json"),
            git_runner=git_runner or (lambda _path, _args: completed()),
        )
    )


def rows(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {item["check"]: item for item in report["checks"]}


def test_all_checks_pass_with_bounded_read_only_calls(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)

    report = run_report(backend, tmp_path)

    assert report["overall"] == "PASS"
    assert report["exit_code"] == 0
    assert all(item["status"] == "PASS" for item in report["checks"])
    assert {call[0] for call in backend.calls} <= {
        "board_status",
        "board_snapshot",
        "board_state_get",
    }


def test_central_and_registry_failures_are_fail(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    backend.status_errors["home"] = PermissionError(f"denied {TOKEN}")
    backend.registry = {"schema_version": 1, "projects": []}

    report = run_report(backend, tmp_path)
    checks = rows(report)

    assert checks["central"]["status"] == "FAIL"
    assert checks["registry"]["status"] == "FAIL"
    assert report["exit_code"] == 2


def test_project_workdir_git_and_ref_matrix(tmp_path: Path) -> None:
    missing = FakeBackend(tmp_path)
    missing.registry["projects"]["alpha"]["work_dir"] = str(tmp_path / "missing")
    assert rows(run_report(missing, tmp_path))["project:alpha"]["status"] == "FAIL"

    non_git = FakeBackend(tmp_path)
    non_git.registry["projects"]["alpha"]["git_repo"] = False

    def must_not_run(_path: Path, _arguments: Any) -> Any:
        raise AssertionError("explicit non-git project must not run git")

    explicit = run_report(non_git, tmp_path, git_runner=must_not_run)
    assert rows(explicit)["project:alpha"]["status"] == "PASS"

    bad_ref = FakeBackend(tmp_path)

    def fail_ref(_path: Path, arguments: Any) -> subprocess.CompletedProcess[str]:
        return completed(1, "") if "--verify" in arguments else completed()

    unresolved = run_report(bad_ref, tmp_path, git_runner=fail_ref)
    assert rows(unresolved)["project:alpha"]["status"] == "FAIL"
    assert "not resolvable" in rows(unresolved)["project:alpha"]["detail"]


def test_board_access_and_snapshot_failures_are_fail(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    backend.status_errors["alpha-board"] = ConnectionError("offline")
    backend.snapshot_errors["alpha-board"] = TimeoutError("slow")

    checks = rows(run_report(backend, tmp_path))

    assert checks["board:alpha-board"]["status"] == "FAIL"
    assert checks["snapshot:alpha-board"]["status"] == "FAIL"


def test_snapshot_truncation_is_warn_with_counts(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    backend.snapshots["alpha-board"]["truncated"] = True
    backend.snapshots["alpha-board"]["omitted_counts"] = {
        "agents": 2,
        "tickets": 3,
    }

    checks = rows(run_report(backend, tmp_path))
    check = checks["snapshot:alpha-board"]

    assert check["status"] == "WARN"
    assert "agents=2" in check["detail"]
    assert "tickets=3" in check["detail"]
    assert checks["seats"]["status"] == "WARN"
    assert "scan incomplete" in checks["seats"]["detail"]
    assert checks["claims"]["status"] == "WARN"
    assert checks["review-backlog"]["status"] == "WARN"


def test_seat_duplicates_and_staleness_are_warn(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
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

    check = rows(run_report(backend, tmp_path))["seats"]

    assert check["status"] == "WARN"
    assert "duplicate names" in check["detail"]
    assert "stale seats" in check["detail"]


def test_expired_claim_and_review_backlog_are_warn(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
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

    checks = rows(run_report(backend, tmp_path))

    assert checks["claims"]["status"] == "WARN"
    assert "TK-expired" in checks["claims"]["detail"]
    assert checks["review-backlog"]["status"] == "WARN"
    assert "TK-review" in checks["review-backlog"]["detail"]


@pytest.mark.parametrize(
    ("coordinator", "expected"),
    [
        ({"generated_at": stamp(301)}, "WARN"),
        ({"findings": []}, "WARN"),
        ({"generated_at": stamp(300)}, "PASS"),
    ],
)
def test_coordinator_freshness_matrix(
    tmp_path: Path, coordinator: dict[str, Any], expected: str
) -> None:
    backend = FakeBackend(tmp_path)
    backend.coordinator = coordinator

    check = rows(run_report(backend, tmp_path))["coordinator"]

    assert check["status"] == expected
    if coordinator.get("generated_at") == stamp(301):
        assert "coordinator may be down" in check["detail"]


@pytest.mark.parametrize("mode", ["missing", "malformed", "stale"])
def test_bridge_stats_warn_matrix(tmp_path: Path, mode: str) -> None:
    backend = FakeBackend(tmp_path)
    path = tmp_path / f"{mode}.json"
    if mode == "malformed":
        path.write_text("not-json", encoding="utf-8")
    elif mode == "stale":
        path.write_text(
            json.dumps({"schema_version": 1, "days": {"2030-01-07": {}}}),
            encoding="utf-8",
        )

    check = rows(run_report(backend, tmp_path, stats_path=path))["bridge-stats"]

    assert check["status"] == "WARN"


def test_exit_code_aggregates_worst_status() -> None:
    checks = [
        doctor.Check("PASS", "a", "ok"),
        doctor.Check("WARN", "b", "warning"),
    ]
    assert doctor.report_document(checks, NOW)["exit_code"] == 1
    checks.append(doctor.Check("FAIL", "c", "failure"))
    assert doctor.report_document(checks, NOW)["exit_code"] == 2


def test_credential_never_appears_in_human_or_json_output(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    backend.status_errors["home"] = PermissionError(f"auth failed for {TOKEN}")
    backend.snapshot_errors["home"] = RuntimeError(f"transport used {TOKEN}")

    report = run_report(backend, tmp_path)
    human = doctor.render_human(report)
    machine = json.dumps(report)

    assert TOKEN not in human
    assert TOKEN not in machine
    assert all(len(item["detail"]) <= doctor.MAX_DETAIL_CHARS for item in report["checks"])


def test_live_backend_uses_bounded_snapshot_and_has_no_write_surface() -> None:
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

    assert calls == [
        (
            "snapshot",
            "alpha",
            {
                "limit": doctor.SNAPSHOT_LIMIT,
                "max_bytes": doctor.SNAPSHOT_MAX_BYTES,
            },
        )
    ]
    assert not hasattr(backend, "board_state_update")


def test_cli_defaults_and_positive_threshold_validation(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("placeholder", encoding="utf-8")
    args = doctor.build_parser().parse_args(["--token-path", str(token_path), "--json"])
    assert args.review_backlog_seconds == 1_800
    assert args.json_output is True
    args.review_backlog_seconds = 0
    with pytest.raises(doctor.DoctorError, match="positive"):
        asyncio.run(doctor.run(args))
