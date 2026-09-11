from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import MagicMock

import pytest
import tomllib

MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_dashboard", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)

CENTRAL_SRC = MODULE_PATH.parents[2] / "packages" / "central" / "src" / "pursers_central"
sys.path.insert(0, str(CENTRAL_SRC))
import central  # noqa: E402


def registry(projects: dict) -> dict:
    return {"state": {"value": json.dumps({"schema_version": 1, "projects": projects})}}


def test_registry_includes_home_and_excludes_paused_projects() -> None:
    result = dashboard.parse_project_registry(
        registry(
            {
                "Active": {
                    "board_id": "board-active",
                    "status": "active",
                    "work_dir": "/tmp/a",
                },
                "Paused": {
                    "board_id": "board-paused",
                    "status": "paused",
                    "work_dir": "/tmp/b",
                },
                "Duplicate": {
                    "board_id": "home-board",
                    "status": "active",
                    "work_dir": "/tmp/c",
                },
            }
        ),
        "home-board",
    )

    assert result == [("home-board", "home-board"), ("Active", "board-active")]


def test_registry_projects_authoritative_work_dirs() -> None:
    result = dashboard.parse_project_work_dirs(
        registry(
            {
                "Home": {
                    "board_id": "home-board",
                    "status": "active",
                    "work_dir": "/repo/home",
                },
                "Active": {
                    "board_id": "board-active",
                    "status": "active",
                    "work_dir": "/repo/active",
                },
                "Paused": {
                    "board_id": "paused",
                    "status": "paused",
                    "work_dir": "/repo/paused",
                },
            }
        ),
        "home-board",
    )

    assert result == {
        "home-board": "/repo/home",
        "board-active": "/repo/active",
    }


def test_registry_projects_route_to_fleet_clone() -> None:
    result = dashboard.parse_project_work_dirs(
        registry(
            {
                "Active": {
                    "board_id": "board-active",
                    "status": "active",
                    "work_dir": "/repo/operator",
                    "fleet_clone_dir": "/fleet/active",
                },
            }
        ),
        "home-board",
    )

    assert result == {
        "home-board": None,
        "board-active": "/fleet/active",
    }


def test_prepare_fleet_clone_tracks_origin_main_detached(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(operator)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(operator), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(operator), "config", "user.email", "test@example.invalid"], check=True)
    (operator / "README.md").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(operator), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(operator), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(operator), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(operator), "push", "-u", "origin", "main"], check=True, capture_output=True)

    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "fleet", latest_version=lambda: None
    )
    payload = {
        "registry": {
            "schema_version": 1,
            "projects": {
                "Alpha Project": {
                    "board_id": "alpha",
                    "work_dir": str(operator),
                    "status": "active",
                }
            },
        },
        "expected_sha256": "a" * 64,
    }

    prepared = manager.prepare_fleet_clone(payload, "Alpha Project")
    clone = Path(prepared["registry"]["projects"]["Alpha Project"]["fleet_clone_dir"])
    assert clone != operator
    assert prepared["registry"]["projects"]["Alpha Project"]["work_dir_owner"] == "operator"
    assert prepared["expected_sha256"] == "a" * 64
    assert prepared["clone"] == {
        "path": str(clone),
        "status": "ready",
        "dirty": False,
        "empty_worktree": False,
        "detached": True,
        "ahead": 0,
        "behind": 0,
    }
    assert (clone / "README.md").read_text(encoding="utf-8") == "one\n"
    assert subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", str(clone), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == str(origin)

    (operator / "README.md").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(operator), "commit", "-am", "second"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(operator), "push", "origin", "main"], check=True, capture_output=True)
    refreshed = manager.prepare_fleet_clone(
        {"registry": prepared["registry"], "expected_sha256": "b" * 64},
        "Alpha Project",
    )
    assert refreshed["clone"]["ahead"] == 0
    assert refreshed["clone"]["behind"] == 0
    assert subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == subprocess.run(
        ["git", "-C", str(operator), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert (clone / "README.md").read_text(encoding="utf-8") == "two\n"
    assert subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""

    (clone / "README.md").unlink()
    deleted = manager._clone_state(clone)
    assert deleted["status"] == "dirty"
    assert deleted["empty_worktree"] is False
    assert deleted["dirty"] is True
    registry_view = manager.registry(
        fleet={},
        registry_payload={
            "registry": {
                "schema_version": 1,
                "projects": {
                    "Alpha Project": {
                        "board_id": "alpha",
                        "work_dir": str(operator),
                        "fleet_clone_dir": str(clone),
                        "status": "active",
                    }
                },
            }
        },
    )
    assert registry_view["projects"][0]["clone"]["status"] == "dirty"
    with pytest.raises(ValueError) as exc_info:
        manager.prepare_fleet_clone(
            {"registry": refreshed["registry"], "expected_sha256": "c" * 64},
            "Alpha Project",
        )
    message = str(exc_info.value)
    assert "fleet clone is dirty; refusing to overwrite local changes" in message
    assert str(clone) in message
    assert f"inspect with: git -C {clone} status --short" in message
    assert not (clone / "README.md").exists()

    subprocess.run(
        ["git", "-C", str(clone), "checkout", "--", "README.md"], check=True
    )
    subprocess.run(
        ["git", "-C", str(clone), "rm", "README.md"], check=True, capture_output=True
    )
    staged = manager._clone_state(clone)
    assert staged["status"] == "dirty"
    assert staged["empty_worktree"] is False
    with pytest.raises(ValueError, match="fleet clone is dirty"):
        manager.prepare_fleet_clone(
            {"registry": refreshed["registry"], "expected_sha256": "d" * 64},
            "Alpha Project",
        )
    assert not (clone / "README.md").exists()
    assert subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.startswith("D  README.md")


def test_prepare_fleet_clone_repairs_proven_legacy_no_checkout(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    legacy = tmp_path / "legacy"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(operator)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(operator), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(operator), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (operator / "README.md").write_text("legacy\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(operator), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(operator), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(operator), "branch", "-M", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(operator), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    subprocess.run(
        ["git", "clone", "--no-checkout", "--origin", "origin", "--", str(origin), str(legacy)],
        check=True,
        capture_output=True,
    )
    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "fleet", latest_version=lambda: None
    )
    state = manager._clone_state(legacy)
    assert not (legacy / ".git" / "index").exists()
    assert state["status"] == "empty_worktree"
    assert state["empty_worktree"] is True
    assert state["dirty"] is False

    prepared = manager.prepare_fleet_clone(
        {
            "registry": {
                "schema_version": 1,
                "projects": {
                    "Alpha": {
                        "board_id": "alpha",
                        "work_dir": str(operator),
                        "fleet_clone_dir": str(legacy),
                        "status": "active",
                    }
                },
            },
            "expected_sha256": "a" * 64,
        },
        "Alpha",
    )
    assert prepared["clone"]["status"] == "ready"
    assert (legacy / "README.md").read_text(encoding="utf-8") == "legacy\n"
    assert subprocess.run(
        ["git", "-C", str(legacy), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_prepare_fleet_clone_removes_partial_directory_on_clone_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "clone", str(origin), str(operator)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(operator), "remote", "set-url", "origin", str(origin)],
        check=True,
    )

    def fail_clone(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "ls-remote":
            return subprocess.CompletedProcess(
                command, 0, "a" * 40 + "\trefs/heads/main\n", ""
            )
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir(parents=True)
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr=(
                    "fatal: token=raw-clone-credential at /"
                    + "Users/operator/private-repo"
                ),
            )
        return subprocess.run(command, **kwargs)  # noqa: PLW1510 - forwards check.

    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "fleet",
        latest_version=lambda: None,
        git_runner=fail_clone,
    )
    payload = {
        "registry": {
            "schema_version": 1,
            "projects": {
                "Alpha": {
                    "board_id": "alpha",
                    "work_dir": str(operator),
                    "status": "active",
                }
            },
        },
        "expected_sha256": "a" * 64,
    }
    target = manager._fleet_clone_default("Alpha")

    with pytest.raises(dashboard.FleetCloneGitError, match="git clone failed") as caught:
        manager.prepare_fleet_clone(payload, "Alpha")
    assert not target.exists()
    message = str(caught.value)
    assert "raw-clone-credential" not in message
    assert "operator/private-repo" not in message
    assert "[REDACTED]" in message
    log = capsys.readouterr().err
    assert '"project": "Alpha"' in log
    assert '"subcommand": "clone"' in log
    assert "raw-clone-credential" not in log
    journal = (tmp_path / "fleet/config-actions.jsonl").read_text()
    assert '"subcommand": "clone"' in journal
    assert "raw-clone-credential" not in journal


def test_prepare_fleet_clone_preflight_is_noninteractive_and_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fail_preflight(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[1:4] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://example.invalid/repo\n", "")
        if command[1] == "ls-remote":
            return subprocess.CompletedProcess(
                command,
                128,
                "",
                "fatal: Authorization: "
                + "Bearer "
                + "private-launchd-credential at /"
                + "Users/operator/repo",
            )
        raise AssertionError(command)

    monkeypatch.setenv("PATH", "/usr/bin")
    original_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: str(path) == "/opt/homebrew/bin" or original_is_dir(path),
    )
    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "fleet", latest_version=lambda: None,
        git_runner=fail_preflight,
    )
    payload = {
        "registry": {
            "schema_version": 1,
            "projects": {
                "Alpha": {
                    "board_id": "alpha",
                    "work_dir": str(tmp_path / "operator"),
                    "status": "active",
                }
            },
        },
        "expected_sha256": "a" * 64,
    }

    with pytest.raises(dashboard.FleetCloneGitError) as caught:
        manager.prepare_fleet_clone(payload, "Alpha")

    message = str(caught.value)
    assert "cannot reach origin from the dashboard service" in message
    assert "clone from a shell then re-run to adopt" in message
    assert "git ls-remote failed" in message
    assert "private-launchd-credential" not in message
    assert "operator/repo" not in message
    assert [call[0][1] for call in calls] == ["remote", "ls-remote"]
    for _command, kwargs in calls:
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["HOME"]
        assert env["PATH"].split(os.pathsep)[0] == "/opt/homebrew/bin"
        assert kwargs["timeout"] == 120
    log = capsys.readouterr().err
    assert '"subcommand": "ls-remote"' in log
    assert "private-launchd-credential" not in log


def test_registry_clone_api_returns_scrubbed_git_failure(
    tmp_path: Path,
) -> None:
    def fail_clone(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:4] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(command, 0, "https://example.invalid/repo\n", "")
        if command[1] == "ls-remote":
            return subprocess.CompletedProcess(
                command, 128, "", "fatal: password=private-api-credential"
            )
        raise AssertionError(command)

    class Cache:
        def get_project_registry(self) -> dict[str, object]:
            return {
                "registry": {
                    "schema_version": 1,
                    "projects": {
                        "Alpha": {
                            "board_id": "alpha",
                            "work_dir": str(tmp_path / "operator"),
                            "status": "active",
                        }
                    },
                },
                "expected_sha256": "a" * 64,
            }

    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "fleet",
        latest_version=lambda: None,
        git_runner=fail_clone,
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=manager)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    request = urllib.request.Request(
        base + "/api/config/registry/clone",
        data=json.dumps({"project": "Alpha"}).encode(),
        headers={"Content-Type": "application/json", "Origin": base},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 400
        body = json.loads(caught.value.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert "git ls-remote failed" in body["error"]
    assert "[REDACTED]" in body["error"]
    assert "private-api-credential" not in body["error"]


def test_registry_clone_uses_board_scrub_for_every_error_surface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = "AK" + "IA" + "ABCDEFGHIJKLMNOP"

    def fail_preflight(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:4] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                command, 0, "https://example.invalid/repo\n", ""
            )
        if command[1] == "ls-remote":
            return subprocess.CompletedProcess(
                command, 128, "", f"fatal: credential {credential} rejected"
            )
        raise AssertionError(command)

    registry_payload = {
        "registry": {
            "schema_version": 1,
            "projects": {
                "Alpha": {
                    "board_id": "alpha",
                    "work_dir": str(tmp_path / "operator"),
                    "status": "active",
                }
            },
        },
        "expected_sha256": "a" * 64,
    }
    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "fleet",
        latest_version=lambda: None,
        git_runner=fail_preflight,
    )

    with pytest.raises(dashboard.FleetCloneGitError) as caught:
        manager.prepare_fleet_clone(registry_payload, "Alpha")
    exception_text = str(caught.value)
    direct_log = capsys.readouterr().err
    journal_path = tmp_path / "fleet/config-actions.jsonl"
    direct_journal = journal_path.read_text(encoding="utf-8")

    class Cache:
        def get_project_registry(self) -> dict[str, object]:
            return registry_payload

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=manager)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    request = urllib.request.Request(
        base + "/api/config/registry/clone",
        data=json.dumps({"project": "Alpha"}).encode(),
        headers={"Content-Type": "application/json", "Origin": base},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as api_error:
            urllib.request.urlopen(request)
        assert api_error.value.code == 400
        body = json.loads(api_error.value.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    api_log = capsys.readouterr().err
    final_journal = journal_path.read_text(encoding="utf-8")
    surfaces = (
        exception_text,
        body["error"],
        direct_log,
        api_log,
        direct_journal,
        final_journal,
    )
    for surface in surfaces:
        assert credential not in surface
        assert "[REDACTED:AWS_ACCESS_KEY_ID]" in surface


def test_agents_group_by_principal_and_name_across_board_specific_ids() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=20)).isoformat()
    rows = [
        {
            "label": "One",
            "board_id": "board-one",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-a",
                        "agent_id": "AI-one",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "membership_role": "member",
                        "status": "working",
                    }
                ],
                "tickets": [
                    {
                        "ticket_id": "TK-current",
                        "title": "Current work",
                        "status": "claimed",
                        "claimed_by_agent_id": "AI-one",
                        "updated_at": recent,
                    }
                ],
            },
            "events": [],
        },
        {
            "label": "Two",
            "board_id": "board-two",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-a",
                        "agent_id": "AI-two",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "status": "active",
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
    ]

    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)

    assert len(result["agents"]) == 1
    assert result["agents"][0]["boards"] == ["board-one", "board-two"]
    assert result["agents"][0]["seats"] == [
        {
            "board_id": "board-one",
            "project": "One",
            "role": "member",
            "current_ticket_id": "TK-current",
            "current_ticket_title": "Current work",
            "last_seen": recent,
        },
        {
            "board_id": "board-two",
            "project": "Two",
            "role": None,
            "current_ticket_id": None,
            "current_ticket_title": None,
            "last_seen": recent,
        },
    ]
    assert result["agents"][0]["duplicate_name"] is False
    assert result["agents"][0]["pool_status"] == "busy"
    assert result["pool_summary"] == {
        "online": 1,
        "busy": 1,
        "available": 0,
        "stale": 0,
    }


def test_retired_and_stale_seats_are_outside_active_pool() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    rows = [
        {
            "label": "One",
            "board_id": "board-one",
            "snapshot": {
                "board": {"stale_after_days": 7},
                "agents": [
                    {
                        "principal_id": "PR-active",
                        "agent_name": "active",
                        "agent_id": "AI-active",
                        "last_activity_at": now.isoformat(),
                        "lifecycle_status": "active",
                    },
                    {
                        "principal_id": "PR-retired",
                        "agent_name": "retired",
                        "agent_id": "AI-retired",
                        "last_activity_at": now.isoformat(),
                        "lifecycle_status": "retired",
                    },
                ],
                "tickets": [],
            },
            "events": [],
        }
    ]

    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)

    assert [item["agent_name"] for item in result["agents"]] == ["active"]
    assert result["pool_summary"]["online"] == 1
    assert result["inactive_agents"][0]["agent_name"] == "retired"
    assert result["boards"][0]["stale_after_days"] == 7


def test_available_and_stale_classification() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    agents = [
        {
            "principal_id": "PR-1",
            "agent_name": "recent",
            "agent_id": "AI-1",
            "last_activity_at": (now - timedelta(seconds=299)).isoformat(),
        },
        {
            "principal_id": "PR-2",
            "agent_name": "old",
            "agent_id": "AI-2",
            "last_activity_at": (now - timedelta(seconds=301)).isoformat(),
        },
    ]
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {"agents": agents, "tickets": []},
                "events": [],
            }
        ],
        stale_seconds=300,
        now=now,
    )

    assert {row["agent_name"]: row["pool_status"] for row in result["agents"]} == {
        "recent": "available",
        "old": "stale",
    }


def test_agent_projection_marks_busy_when_claim_is_outside_ticket_window() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [
                        {
                            "principal_id": "PR-1",
                            "agent_name": "worker",
                            "agent_id": "AI-1",
                            "last_activity_at": now.isoformat(),
                            "lifecycle_status": "active",
                            "status": "working",
                        }
                    ],
                    "tickets": [
                        {
                            "ticket_id": f"TK-{index:04d}",
                            "status": "closed",
                        }
                        for index in range(50)
                    ],
                    "omitted_counts": {"tickets": 1},
                    "truncated": True,
                },
                "events": [],
            }
        ],
        stale_seconds=300,
        now=now,
    )

    assert result["agents"][0]["pool_status"] == "busy"
    assert result["pool_summary"]["busy"] == 1


@pytest.mark.parametrize("lifecycle_status", ["handed_off", "inactive"])
def test_non_active_lifecycle_is_not_busy(lifecycle_status: str) -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [
                        {
                            "principal_id": "PR-1",
                            "agent_name": "worker",
                            "last_activity_at": now.isoformat(),
                            "lifecycle_status": lifecycle_status,
                            "status": "working",
                        }
                    ],
                    "tickets": [],
                },
                "events": [],
            }
        ],
        stale_seconds=300,
        now=now,
    )

    assert result["agents"][0]["pool_status"] == "available"


def test_truncated_ticket_counts_are_rendered_as_lower_bounds() -> None:
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [],
                    "tickets": [
                        {"ticket_id": "TK-open", "status": "open"},
                        {"ticket_id": "TK-claimed", "status": "claimed"},
                    ],
                    "omitted_counts": {"tickets": 3},
                    "truncated": True,
                },
                "events": [],
            }
        ],
        stale_seconds=300,
    )

    assert result["boards"][0]["counts"] == {
        "open": ">=1",
        "claimed": ">=1",
        "submitted": ">=0",
        "closed_today": ">=0",
    }


def test_ticket_table_filters_before_bounding_and_includes_submitted() -> None:
    tickets = [
        {"ticket_id": f"TK-closed-{index:03d}", "status": "closed"}
        for index in range(dashboard.SNAPSHOT_LIMIT)
    ]
    tickets.extend(
        [
            {"ticket_id": "TK-open", "status": "open"},
            {
                "ticket_id": "TK-submitted",
                "status": "submitted",
                "review_lease": {
                    "reviewer_agent_name": "reviewer-a",
                    "expires_at": "2030-01-02T13:00:00+00:00",
                },
            },
            {"ticket_id": "TK-claimed", "status": "claimed"},
        ]
    )

    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {"agents": [], "tickets": tickets},
                "events": [],
            }
        ],
        stale_seconds=300,
        now=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
    )

    rows = result["boards"][0]["tickets"]
    assert [row["id"] for row in rows] == [
        "TK-claimed",
        "TK-submitted",
        "TK-open",
    ]
    assert rows[1]["status_label"] == "in review by reviewer-a"


def test_keepalive_only_claim_is_projected_and_flagged_for_attention() -> None:
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [],
                    "tickets": [
                        {
                            "ticket_id": "TK-zombie",
                            "title": "Inactive model claim",
                            "status": "claimed",
                            "claimed_by": "idle-seat",
                            "claim_age_s": 4_000,
                            "lease_renewal_source": "keepalive",
                            "lease_keepalive_only_age_s": 2_701,
                            "ttl_s": 900,
                        }
                    ],
                },
                "events": [],
            }
        ],
        stale_seconds=300,
    )

    row = result["boards"][0]["tickets"][0]
    assert row["claim_age_s"] == 4_000
    assert row["lease_renewal_source"] == "keepalive"
    assert row["lease_keepalive_only_age_s"] == 2_701
    assert "Claim renewed only by keepalive for >" in dashboard.HTML
    assert "keepaliveAge>keepaliveThreshold" in dashboard.HTML


def test_fetcher_requests_central_max_snapshot_bounds() -> None:
    requested: dict[str, int | None] = {}

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_snapshot(
            self, *, limit: int | None = None, max_bytes: int | None = None,
            include_retired: bool = False,
        ) -> dict:
            requested.update(
                limit=limit,
                max_bytes=max_bytes,
                include_retired=include_retired,
            )
            return {"latest_seq": 0, "agents": [], "tickets": []}

        async def board_catchup(self, **_kwargs: object) -> dict:
            return {"events": []}

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5.0,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())

    asyncio.run(fetcher._read_board("Board", "board"))

    assert requested == {
        "limit": 1_000,
        "max_bytes": 300_000,
        "include_retired": True,
    }


def test_output_rows_and_titles_are_bounded() -> None:
    tickets = [
        {
            "ticket_id": f"TK-{index}",
            "title": "x" * 500,
            "status": "open",
            "updated_at": "2030-01-01T00:00:00+00:00",
        }
        for index in range(80)
    ]
    events = [{"seq": index, "kind": "ticket_created"} for index in range(80)]
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {"agents": [], "tickets": tickets, "truncated": True},
                "events": events,
            }
        ],
        stale_seconds=300,
    )
    board = result["boards"][0]

    assert len(board["tickets"]) == dashboard.MAX_TICKET_ROWS
    assert len(board["events"]) == dashboard.MAX_EVENT_ROWS
    assert (
        max(len(row["title"]) for row in board["tickets"]) <= dashboard.MAX_TITLE_CHARS
    )
    assert board["truncated"] is True


def test_detail_projection_is_byte_bounded_and_drops_unbounded_history() -> None:
    tickets = [
        {
            "ticket_id": f"TK-{index:04d}",
            "title": "title " + "x" * 500,
            "description": "d" * 20_000,
            "status": "closed" if index % 2 else "claimed",
            "required_fields": ["field-" + "r" * 200 for _ in range(40)],
            "updated_at": f"2030-01-01T00:{index % 60:02d}:00+00:00",
            "submission_history": [{"summary": "s" * 20_000, "notes": "n" * 100_000}],
            "review_label": "independent-review",
        }
        for index in range(dashboard.SNAPSHOT_LIMIT)
    ]
    detail = dashboard.project_board_detail(
        {
            "label": "Board",
            "board_id": "board",
            "snapshot": {
                "tickets": tickets,
                "total_counts": {"tickets": len(tickets)},
            },
            "events": [
                {"seq": index, "kind": "ticket_status_changed", "ticket_id": "TK-0000"}
                for index in range(150)
            ],
        }
    )
    body = dashboard._json_bytes(detail)

    assert len(body) <= dashboard.API_MAX_BYTES
    assert detail["ticket_omitted"] > 0
    assert detail["ticket_returned"] == len(detail["tickets"])
    assert len(detail["events"]) <= dashboard.DETAIL_EVENT_SCAN_LIMIT
    assert [event["seq"] for event in detail["events"]] == sorted(
        event["seq"] for event in detail["events"]
    )
    assert b"submission_history" not in body
    assert b'"notes"' not in body


def test_detail_projection_and_html_show_bounded_attributed_annotations() -> None:
    ticket = {
        "ticket_id": "TK-annotations",
        "title": "Annotated",
        "description": "Ticket context",
        "status": "open",
        "annotations_omitted_count": 3,
        "annotation_count": 55,
        "annotations": [
            {
                "annotation_id": f"AN-{index:012d}",
                "kind": "evidence",
                "text": "operator result " + "x" * 5_000,
                "by": {
                    "principal_id": "PR-operator",
                    "agent_id": "AI-coordinator",
                    "agent_name": "coordinator-agent",
                },
                "at": "2030-01-01T00:00:00+00:00",
            }
            for index in range(52)
        ],
    }

    projected = dashboard._detail_ticket(ticket)

    assert projected["annotation_count"] == 55
    assert projected["annotations_omitted_count"] == 3
    assert len(projected["annotations"]) == dashboard.MAX_ANNOTATIONS_PER_TICKET
    assert projected["annotations"][0]["annotation_id"] == "AN-000000000002"
    assert len(projected["annotations"][0]["text"]) == 4_000
    assert projected["annotations"][0]["by"]["agent_name"] == "coordinator-agent"
    assert "function annotationView(t)" in dashboard.HTML
    assert "annotation-kind" in dashboard.HTML
    assert "By ${esc(author)}" in dashboard.HTML


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/board/board-1", "board-1"),
        ("/api/board/board-1?fresh=1", "board-1"),
        ("/api/board/", None),
        ("/api/board/board/child", None),
        ("/api/board/%2Fetc", None),
        ("/api/board/board%20name", None),
        ("/other/board-1", None),
    ],
)
def test_detail_route_fallback_is_safe(path: str, expected: str | None) -> None:
    assert dashboard.board_id_from_api_path(path) == expected


def test_unknown_board_does_not_accumulate_detail_cache_entries() -> None:
    class Fetcher:
        async def fetch(self) -> dict:
            return {}

        async def fetch_board(self, board_id: str) -> dict:
            raise KeyError(board_id)

    cache = dashboard.DashboardCache(Fetcher(), 5.0)

    with pytest.raises(KeyError):
        cache.get_board("unknown-board")

    assert cache._details == {}


def test_fetch_board_uses_bounded_snapshot_and_catchup() -> None:
    calls: list[tuple[str, dict]] = []

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_state_get(self, **kwargs: object) -> dict:
            calls.append(("board_state_get", dict(kwargs)))
            return registry(
                {
                    "Board": {
                        "board_id": "board-detail",
                        "status": "active",
                        "work_dir": "/tmp/board",
                    }
                }
            )

        async def board_snapshot(self, **kwargs: object) -> dict:
            calls.append(("board_snapshot", dict(kwargs)))
            return {"latest_seq": 9, "agents": [], "tickets": []}

        async def board_catchup(self, **kwargs: object) -> dict:
            calls.append(("board_catchup", dict(kwargs)))
            return {"events": []}

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="home-board",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5.0,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())

    result = asyncio.run(fetcher.fetch_board("board-detail"))

    assert result["board"]["board_id"] == "board-detail"
    assert (
        "board_snapshot",
        {"limit": 1_000, "max_bytes": 300_000, "include_retired": True},
    ) in calls
    assert (
        "board_catchup",
        {
            "cursor": 0,
            "limit": 100,
            "ack": False,
            "max_events": 100,
            "max_bytes": 100_000,
        },
    ) in calls


def test_timeline_groups_by_utc_day_and_ticket_newest_first() -> None:
    events = [
        {
            "seq": 8,
            "ticket_id": "TK-old",
            "occurred_at": "2030-01-01T23:59:00+00:00",
        },
        {
            "seq": 10,
            "ticket_id": "TK-two",
            "occurred_at": "2030-01-02T02:00:00+00:00",
        },
        {
            "seq": 9,
            "ticket_id": "TK-one",
            "occurred_at": "2030-01-02T01:00:00+00:00",
        },
        {
            "seq": 11,
            "ticket_id": "TK-one",
            "occurred_at": "2030-01-02T03:00:00+00:00",
        },
    ]

    assert dashboard.group_timeline(events) == [
        {
            "day": "2030-01-02",
            "tickets": [
                {"ticket_id": "TK-one", "event_seqs": [11, 9]},
                {"ticket_id": "TK-two", "event_seqs": [10]},
            ],
        },
        {
            "day": "2030-01-01",
            "tickets": [{"ticket_id": "TK-old", "event_seqs": [8]}],
        },
    ]


def test_changes_math_supports_seq_and_default_time_cutoffs() -> None:
    events = [
        {
            "seq": 1,
            "kind": "ticket_created",
            "status_to": "open",
            "occurred_at": "2030-01-01T10:00:00+00:00",
        },
        {
            "seq": 2,
            "kind": "ticket_status_changed",
            "status_to": "claimed",
            "occurred_at": "2030-01-02T11:00:00+00:00",
        },
        {
            "seq": 3,
            "kind": "ticket_status_changed",
            "status_to": "submitted",
            "occurred_at": "2030-01-02T11:10:00+00:00",
        },
        {
            "seq": 4,
            "kind": "ticket_status_changed",
            "status_from": "submitted",
            "status_to": "open",
            "review_verdict": "reject",
            "rejection_count": 1,
            "occurred_at": "2030-01-02T11:20:00+00:00",
        },
        {
            "seq": 5,
            "kind": "ticket_status_changed",
            "status_to": "closed",
            "occurred_at": "2030-01-02T11:30:00+00:00",
        },
    ]

    by_seq = dashboard.summarize_changes(events, since_seq=2)
    assert by_seq == {
        "counts": {
            "created": 0,
            "claimed": 0,
            "submitted": 1,
            "closed": 1,
            "rejected": 1,
        },
        "event_count": 3,
    }
    by_time = dashboard.summarize_changes(
        events, since_time=datetime(2030, 1, 2, 10, tzinfo=timezone.utc)
    )
    assert by_time["counts"] == {
        "created": 0,
        "claimed": 1,
        "submitted": 1,
        "closed": 1,
        "rejected": 1,
    }


def test_ticket_flow_classifies_bounded_rows_and_closed_today() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    rows = [
        {"id": "TK-open", "status": "open"},
        {"id": "TK-work", "status": "in_progress"},
        {"id": "TK-review", "status": "reviewing"},
        {
            "id": "TK-closed-today",
            "status": "closed",
            "closed_at": "2030-01-02T01:00:00+00:00",
        },
        {
            "id": "TK-closed-old",
            "status": "closed",
            "closed_at": "2030-01-01T23:59:00+00:00",
        },
    ]

    assert dashboard.classify_ticket_flow(rows, now=now) == {
        "open": ["TK-open"],
        "claimed": ["TK-work"],
        "submitted": ["TK-review"],
        "closed_today": ["TK-closed-today"],
    }


def test_routes_assemble_event_provenance_rework_and_principal_collisions() -> None:
    now = datetime(2030, 1, 8, 12, tzinfo=timezone.utc)
    agents = [
        {
            "agent_id": "AI-create",
            "agent_name": "shared-name",
            "principal_id": "PR-creator-111111",
        },
        {
            "agent_id": "AI-worker",
            "agent_name": "worker",
            "principal_id": "PR-worker-aaaaaa",
        },
        {
            "agent_id": "AI-review",
            "agent_name": "shared-name",
            "principal_id": "PR-reviewer-222222",
        },
    ]
    events = [
        {
            "seq": 1,
            "kind": "ticket_created",
            "ticket_id": "TK-route",
            "actor": "AI-create",
            "status_from": "missing",
            "status_to": "open",
            "occurred_at": "2030-01-02T12:00:00+00:00",
        },
        {
            "seq": 2,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-route",
            "actor": "AI-worker",
            "status_from": "open",
            "status_to": "claimed",
            "occurred_at": "2030-01-03T12:00:00+00:00",
        },
        {
            "seq": 3,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-route",
            "actor": "AI-worker",
            "status_from": "claimed",
            "status_to": "submitted",
            "occurred_at": "2030-01-04T12:00:00+00:00",
        },
        {
            "seq": 4,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-route",
            "actor": "AI-review",
            "status_from": "submitted",
            "status_to": "open",
            "occurred_at": "2030-01-05T12:00:00+00:00",
            "review_verdict": "reject",
            "submitted_by_agent_id": "AI-worker",
            "submitted_by_agent_name": "worker",
            "submitted_by_principal_id": "PR-worker-aaaaaa",
            "reviewed_by_agent_id": "AI-review",
            "reviewed_by_agent_name": "shared-name",
            "reviewed_by_principal_id": "PR-reviewer-222222",
        },
        {
            "seq": 5,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-route",
            "actor": "AI-worker",
            "status_from": "open",
            "status_to": "claimed",
            "occurred_at": "2030-01-06T12:00:00+00:00",
        },
        {
            "seq": 6,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-route",
            "actor": "AI-worker",
            "status_from": "claimed",
            "status_to": "submitted",
            "occurred_at": "2030-01-07T11:00:00+00:00",
        },
        {
            "seq": 7,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-route",
            "actor": "AI-review",
            "status_from": "submitted",
            "status_to": "closed",
            "occurred_at": "2030-01-07T12:00:00+00:00",
            "review_verdict": "approve",
            "submitted_by_agent_id": "AI-worker",
            "submitted_by_agent_name": "worker",
            "submitted_by_principal_id": "PR-worker-aaaaaa",
            "reviewed_by_agent_id": "AI-review",
            "reviewed_by_agent_name": "shared-name",
            "reviewed_by_principal_id": "PR-reviewer-222222",
        },
    ]

    result = dashboard.assemble_provenance(
        {
            "agents": agents,
            "tickets": [
                {
                    "ticket_id": "TK-route",
                    "title": "A routed ticket",
                    "status": "closed",
                    "updated_at": "2030-01-07T12:00:00+00:00",
                }
            ],
            "total_counts": {"tickets": 1},
            "omitted_counts": {"tickets": 0},
        },
        events,
        now=now,
    )

    route = result["rows"][0]
    assert route["created"]["label"] == "shared-name · …111111"
    assert route["executed"]["label"] == "worker"
    assert route["submitted"]["label"] == "worker"
    assert route["reviewed"]["label"] == "shared-name · …222222"
    assert route["rework_count"] == 1
    assert route["status"] == "closed"
    worker = next(seat for seat in result["seats"] if seat["label"] == "worker")
    assert worker == {
        "label": "worker",
        "created": 0,
        "executed": 1,
        "reviewed": 0,
        "rework_received": 1,
        "rework_received_rate": 100.0,
    }


def test_routes_window_and_truncation_note_are_explicit() -> None:
    now = datetime(2030, 1, 8, 12, tzinfo=timezone.utc)
    result = dashboard.assemble_provenance(
        {
            "agents": [],
            "tickets": [
                {
                    "ticket_id": "TK-recent",
                    "updated_at": "2030-01-08T11:00:00+00:00",
                },
                {
                    "ticket_id": "TK-old",
                    "updated_at": "2029-12-01T00:00:00+00:00",
                },
            ],
            "total_counts": {"tickets": 5},
            "omitted_counts": {"tickets": 3},
            "truncated": True,
        },
        [],
        now=now,
        event_window_truncated=True,
    )

    assert [row["id"] for row in result["rows"]] == ["TK-recent"]
    assert result["window_start"] == "2030-01-01T12:00:00+00:00"
    assert result["truncated"] is True
    assert "Default window: last 7 days by updated_at." in result["truncation_note"]
    assert "2 of 5 snapshot tickets (3 omitted)" in result["truncation_note"]
    assert "ack=false" in result["truncation_note"]


def test_routes_view_escapes_hostile_identity_and_ticket_strings() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    hostile = '<img src=x onerror="alert(1)">'
    route_data = {
        "rows": [
            {
                "id": "TK-hostile",
                "title": hostile,
                "status": "closed",
                "created": {"label": hostile, "at": "2030-01-01T00:00:00Z"},
                "executed": None,
                "submitted": None,
                "reviewed": None,
                "rework_count": 0,
                "updated_at": "2030-01-01T00:00:00Z",
            }
        ],
        "seats": [
            {
                "label": hostile,
                "created": 1,
                "executed": 0,
                "reviewed": 0,
                "rework_received": 0,
                "rework_received_rate": 0,
            }
        ],
        "row_returned": 1,
        "row_total": 1,
        "truncated": True,
        "truncation_note": hostile,
    }
    program = "\n".join(
        [
            source("const esc="),
            source("const fmt="),
            source("const routeStage="),
            source("function routesView("),
            "const filterNeedle='';",
            f"console.log(routesView({{board:{{board_id:'pursers'}},routes:{json.dumps(route_data)}}},{{central:'personal',board:'pursers'}}));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )

    assert hostile not in completed.stdout
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in completed.stdout
    assert 'id="routes-view"' in completed.stdout


def test_detail_views_include_filter_routes_mobile_containment_and_escape_calls() -> (
    None
):
    assert "tickets|timeline|changes|flow|routes" in dashboard.HTML
    assert "g then r" in dashboard.HTML
    assert "boardHref(current.central,current.board,'routes')" in dashboard.HTML
    assert "e.key==='/'" in dashboard.HTML
    assert "filterNeedle" in dashboard.HTML
    assert "overflow-x:hidden" in dashboard.HTML
    assert ".table-scroll" in dashboard.HTML
    assert "Showing last ${esc(d.event_returned)} events" in dashboard.HTML
    assert "${esc(t.title)}" in dashboard.HTML
    assert "${esc(t.claimed_by||'Unassigned')}" in dashboard.HTML


def test_multi_central_routes_and_complete_javascript_are_valid() -> None:
    scripts = re.findall(r"<script>(.*?)</script>", dashboard.HTML, flags=re.DOTALL | re.IGNORECASE)
    completed = subprocess.run(
        ["node", "--check", "-"],
        input="\n".join(scripts),
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "#/central/" in dashboard.HTML
    assert "central=${encodeURIComponent(central)}" in dashboard.HTML
    assert "Independent central trust domain" in dashboard.HTML
    assert "Saved ${body.central}" in dashboard.HTML


def test_global_search_groups_escaped_results_and_enter_jumps_to_item() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    hostile = '<img src=x onerror="alert(1)"> needle'
    fixture = {
        "personal": {
            "boards": [
                {
                    "label": hostile,
                    "board_id": "board-one",
                    "tickets": [
                        {
                            "id": "TK-needle",
                            "title": hostile,
                            "status": "open",
                            "claimed_by": None,
                        }
                    ],
                }
            ],
            "agents": [
                {
                    "agent_name": hostile,
                    "pool_status": "available",
                    "boards": ["board-one"],
                }
            ],
        }
    }
    program = "\n".join(
        [
            source("const esc="),
            source("const fmt="),
            source("const ticketMatches="),
            source("function groupSearchResults("),
            source("function renderSearchResults("),
            source("function jumpSearchResult("),
            f"let fleetData={json.dumps(fixture)},detailData=null,filterNeedle='needle',searchItems=[],searchSelection=0;",
            "const host={innerHTML:'',hidden:true},input={setAttribute(){}};",
            "const document={querySelector:key=>key==='#search-results'?host:input};",
            "const location={hash:''};const sectionStates=new Map();",
            "renderSearchResults();",
            "const grouped=groupSearchResults(fleetData,detailData,filterNeedle);",
            "jumpSearchResult(1);",
            "console.log(JSON.stringify({counts:Object.fromEntries(Object.entries(grouped).map(([k,v])=>[k,v.length])),html:host.innerHTML,hash:location.hash}));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["counts"] == {"Boards": 1, "Tickets": 1, "Agents": 1}
    assert hostile not in result["html"]
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; needle" in result["html"]
    assert (
        result["hash"] == "#/central/personal/board/board-one/tickets?ticket=TK-needle"
    )


def test_collapsed_state_survives_rebinding_without_local_storage() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    line = next(
        item
        for item in script.splitlines()
        if item.startswith("function bindInteractive(")
    )
    program = "\n".join(  # noqa: FLY002 - extracted JS stays line-addressable
        [
            line,
            "const sectionStates=new Map([['ticket:one',true]]);",
            "const makeDetail=()=>({dataset:{stateKey:'ticket:one'},open:false,listeners:{},addEventListener(name,fn){this.listeners[name]=fn}});",
            "const rootFor=detail=>({querySelectorAll(selector){return selector==='details[data-state-key]'?[detail]:[]}});",
            "const first=makeDetail();bindInteractive(rootFor(first));const restoredOpen=first.open;first.open=false;first.listeners.toggle();",
            "const second=makeDetail();bindInteractive(rootFor(second));",
            "console.log(JSON.stringify({restoredOpen,restoredClosed:!second.open,stored:sectionStates.get('ticket:one')}));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "restoredOpen": True,
        "restoredClosed": True,
        "stored": False,
    }
    assert "localStorage" not in dashboard.HTML
    assert "detailSort='newest'" in dashboard.HTML
    assert "filterNeedle=''" in dashboard.HTML


def test_reconnect_banner_keeps_cached_fleet_and_recovers_silently() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    program = "\n".join(
        [
            source("const connectionBannerText="),
            source("function updateConnectionState("),
            source("function markConnectionSuccess("),
            source("function markConnectionFailure("),
            source("async function refreshCentral("),
            "const banner={hidden:true,textContent:''},document={querySelector:()=>banner};",
            "const connectionFailures=new Set();let lastSuccessAt=null;",
            "let fleetData={personal:{central:'personal',cached:true}},fleetErrors={},rendered=0;",
            "const CENTRAL_REQUEST_TIMEOUT_MS=4000;",
            "const apiCentral=x=>x,route=()=>null,renderFleet=()=>rendered++;",
            "const fetchWithTimeout=async()=>{throw new Error('offline secret')};",
            "(async()=>{markConnectionSuccess('fleet:personal',new Date('2030-01-02T03:04:05Z'));await refreshCentral('personal');const failed={hidden:banner.hidden,text:banner.textContent,cached:fleetData.personal.cached,error:fleetErrors.personal,rendered};markConnectionSuccess('fleet:personal',new Date('2030-01-02T03:05:06Z'));console.log(JSON.stringify({failed,recovered:{hidden:banner.hidden,text:banner.textContent}}))})()",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["failed"]["hidden"] is False
    assert result["failed"]["text"].startswith("reconnecting… last success ")
    assert result["failed"]["cached"] is True
    assert result["failed"]["rendered"] == 1
    assert result["recovered"] == {"hidden": True, "text": ""}


def test_help_theme_density_and_keyboard_controls_render() -> None:
    assert 'id="help-overlay"' in dashboard.HTML
    assert "g then f" in dashboard.HTML
    assert "Move within tables or search results" in dashboard.HTML
    assert 'id="theme-toggle"' in dashboard.HTML
    assert 'id="density-toggle"' in dashboard.HTML
    assert ':root[data-theme="light"]' in dashboard.HTML
    assert "@media print" in dashboard.HTML
    assert "e.key==='g'" in dashboard.HTML


def test_two_central_dom_groups_are_rendered_without_pool_merge() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    def fleet(label: str, agent: str) -> dict:
        return {
            "central": label,
            "generated_at": "2030-01-01T00:00:00Z",
            "pool_summary": {
                "online": 1,
                "busy": 0,
                "available": 1,
                "stale": 0,
            },
            "boards": [],
            "agents": [
                {
                    "agent_name": agent,
                    "pool_status": "available",
                    "boards": [],
                    "seats": [],
                    "duplicate_name": False,
                    "last_seen": "2030-01-01T00:00:00Z",
                }
            ],
        }

    program = "\n".join(
        [
            source("const esc="),
            source("const fmt="),
            source("const ticketMatches="),
            source("const filterHomeBoards="),
            source("function renderCentral("),
            source("function renderFleet("),
            "const elements={'#central-sections':{innerHTML:''},'#state':{textContent:''}};",
            "const document={querySelector:key=>elements[key]};",
            "let filterNeedle='',centralLabels=['personal','work'],fleetErrors={};",
            f"let fleetData={{personal:{json.dumps(fleet('personal', 'personal-seat'))},work:{json.dumps(fleet('work', 'work-seat'))}}};",
            "renderFleet();",
            "console.log(elements['#central-sections'].innerHTML);",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    output = completed.stdout
    assert output.count('class="central-group"') == 2
    assert 'data-central="personal"' in output
    assert 'data-central="work"' in output
    personal_group, work_group = output.split('data-central="work"', 1)
    assert "personal-seat" in personal_group
    assert "work-seat" not in personal_group
    assert "work-seat" in work_group


def test_hung_central_times_out_after_healthy_central_renders() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    program = "\n".join(
        [
            source("const CENTRAL_REQUEST_TIMEOUT_MS="),
            source("async function fetchJson("),
            source("async function fetchWithTimeout("),
            source("async function refreshCentral("),
            source("async function refreshFleet("),
            "const apiCentral=label=>`central=${encodeURIComponent(label)}`;",
            "const route=()=>null;",
            "let centralLabels=['personal','work'],fleetData={},fleetErrors={};",
            "const renders=[];",
            "function renderFleet(){renders.push({data:Object.keys(fleetData),errors:{...fleetErrors}})}",
            "global.fetch=path=>path.includes('work')?new Promise(()=>{}):Promise.resolve({ok:true,json:async()=>({central:'personal'})});",
            "refreshFleet(20).then(()=>console.log(JSON.stringify(renders)));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    renders = json.loads(completed.stdout)

    assert renders[0] == {"data": ["personal"], "errors": {}}
    assert renders[-1]["data"] == ["personal"]
    assert renders[-1]["errors"] == {"work": "central request timed out"}


def test_filter_behavior_removes_unrelated_home_rows_and_change_counts() -> None:
    script = dashboard.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    fixtures = {
        "fleet": {
            "central": "personal",
            "pool_summary": {"online": 0, "busy": 0, "available": 0, "stale": 0},
            "boards": [
                {
                    "label": "Alpha project",
                    "board_id": "board-one",
                    "tickets": [
                        {
                            "id": "TK-alpha",
                            "title": "Alpha work",
                            "status": "open",
                            "claimed_by": None,
                        },
                        {
                            "id": "TK-beta",
                            "title": "Beta work",
                            "status": "submitted",
                            "claimed_by": "worker-beta",
                        },
                    ],
                    "counts": {"open": 1, "submitted": 1},
                    "truncated": False,
                    "error": None,
                }
            ],
            "agents": [],
            "generated_at": "2030-01-02T12:00:00Z",
        },
        "events": [
            {
                "seq": 1,
                "ticket_id": "TK-alpha",
                "kind": "ticket_created",
                "status_to": "open",
                "actor": "worker-alpha",
                "occurred_at": "2030-01-02T11:00:00Z",
            },
            {
                "seq": 2,
                "ticket_id": "TK-beta",
                "kind": "ticket_status_changed",
                "status_to": "submitted",
                "actor": "worker-beta",
                "occurred_at": "2030-01-02T11:10:00Z",
            },
            {
                "seq": 3,
                "ticket_id": "TK-beta",
                "kind": "ticket_status_changed",
                "status_to": "closed",
                "actor": "reviewer-beta",
                "occurred_at": "2030-01-02T11:20:00Z",
            },
        ],
    }
    program = "\n".join(
        [
            source("const esc="),
            source("const fmt="),
            source("const ticketMatches="),
            source("const filterHomeBoards="),
            source("const eventMatches="),
            source("const filterChangeEvents="),
            source("function renderCentral("),
            source("function renderFleet("),
            source("function changesFor("),
            source("function changesView("),
            f"const fixture={json.dumps(fixtures)};",
            "const elements=Object.fromEntries(['#central-sections','#state'].map(key=>[key,{innerHTML:'',textContent:''}]));",
            "const document={querySelector:key=>elements[key]};",
            "let filterNeedle='alpha',centralLabels=['personal'],fleetData={personal:fixture.fleet},fleetErrors={};",
            "renderFleet();",
            "const originalChangesFor=changesFor;let changeEvents=[],changeSummary=null;",
            "changesFor=(events,since,generatedAt)=>{changeEvents=events;changeSummary=originalChangesFor(events,since,generatedAt);return changeSummary};",
            "changesView({events:fixture.events,event_returned:fixture.events.length,generated_at:'2030-01-02T12:00:00Z'},{since:'0'});",
            "console.log(JSON.stringify({home:elements['#central-sections'].innerHTML,changeEvents,changeSummary}));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert "TK-alpha" in result["home"]
    assert "TK-beta" not in result["home"]
    assert [event["ticket_id"] for event in result["changeEvents"]] == ["TK-alpha"]
    assert result["changeSummary"] == {
        "counts": {
            "created": 1,
            "claimed": 0,
            "submitted": 0,
            "closed": 0,
            "rejected": 0,
        },
        "event_count": 1,
    }


def test_hostile_script_rtl_and_zero_width_values_stay_data_for_client_escape() -> None:
    hostile = "<script>alert(1)</script>\u202eRTL\u200b"
    detail = dashboard.project_board_detail(
        {
            "label": hostile,
            "board_id": "safe-board",
            "snapshot": {
                "tickets": [
                    {
                        "ticket_id": "TK-hostile",
                        "title": hostile,
                        "description": hostile,
                        "claimed_by": hostile,
                        "status": "open",
                    }
                ]
            },
            "events": [
                {
                    "seq": 1,
                    "kind": hostile,
                    "ticket_id": "TK-hostile",
                    "occurred_at": "2030-01-02T01:00:00+00:00",
                    "actor": hostile,
                }
            ],
        },
        now=datetime(2030, 1, 2, 12, tzinfo=timezone.utc),
    )

    assert detail["board"]["label"] == hostile
    assert detail["tickets"][0]["title"] == hostile
    assert detail["events"][0]["kind"] == hostile
    assert "const esc=" in dashboard.HTML
    assert hostile not in dashboard.HTML


def test_non_loopback_host_is_refused() -> None:
    with pytest.raises(SystemExit):
        dashboard.parse_args(["--host", "0.0.0.0"])


@pytest.mark.parametrize(
    ("contents", "expected_status"),
    [(None, "missing"), ("not-json", "malformed")],
)
def test_overhead_endpoint_returns_empty_for_missing_or_malformed_stats(
    contents: str | None, expected_status: str
) -> None:
    class Cache:
        def get(self) -> dict:
            return {}

        def get_board(self, _board_id: str) -> dict:
            return {}

    with tempfile.TemporaryDirectory() as raw:
        stats = Path(raw) / "stats.json"
        if contents is not None:
            stats.write_text(contents, encoding="utf-8")
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.make_handler(Cache(), stats)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/overhead"
            ) as response:
                result = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    assert result["source_status"] == expected_status
    assert result["sessions"] == []
    assert result["seats"] == []
    assert result["note"] == "protocol overhead (estimated), not provider billing"


def test_overhead_projection_aggregates_today_and_seven_day_tools() -> None:
    now = datetime(2030, 1, 7, 12, tzinfo=timezone.utc)
    document = {
        "schema_version": 1,
        "days": {
            "2030-01-01": {
                "seats": {
                    "old": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "request_bytes": 40,
                        "response_bytes": 60,
                        "calls": {
                            "ticket_list": {
                                "count": 1,
                                "request_bytes": 40,
                                "response_bytes": 60,
                            }
                        },
                    }
                }
            },
            "2030-01-07": {
                "seats": {
                    "today": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "request_bytes": 100,
                        "response_bytes": 300,
                        "calls": {
                            "board_catchup": {
                                "count": 2,
                                "request_bytes": 100,
                                "response_bytes": 300,
                            }
                        },
                    }
                }
            },
        },
    }
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = dashboard.read_overhead_stats(path, now=now)

    seat = result["seats"][0]
    assert seat["today_bytes"] == 400
    assert seat["today_estimated_tokens"] == 100
    assert seat["seven_day_bytes"] == 500
    assert seat["seven_day_estimated_tokens"] == 125
    assert seat["seven_day_calls"] == 3
    assert [tool["tool"] for tool in seat["top_tools"]] == [
        "board_catchup",
        "ticket_list",
    ]


def test_overhead_uses_inclusive_seven_utc_calendar_days() -> None:
    document = {
        "schema_version": 1,
        "days": {
            "2030-01-01": {
                "seats": {
                    "old": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "request_bytes": 40,
                        "response_bytes": 60,
                        "calls": {},
                    }
                }
            },
            "2030-01-10": {
                "seats": {
                    "today": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "request_bytes": 100,
                        "response_bytes": 300,
                        "calls": {},
                    }
                }
            },
        },
    }
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = dashboard.read_overhead_stats(
            path, now=datetime(2030, 1, 10, 12, tzinfo=timezone.utc)
        )

    assert result["seats"][0]["seven_day_bytes"] == 400


def test_overhead_projects_model_visible_wait_cost_separately() -> None:
    document = {
        "schema_version": 3,
        "days": {},
        "poll_cycles": {},
        "model_wait": {
            "seat": {
                "board_id": "board",
                "agent_name": "worker",
                "hours": {
                    "2030-01-10T12:00:00Z": {
                        "returns": 2,
                        "response_bytes": 800,
                        "outcomes": {"cue": 1, "timeout": 1},
                    }
                },
            }
        },
    }
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = dashboard.read_overhead_stats(
            path, now=datetime(2030, 1, 10, 12, 30, tzinfo=timezone.utc)
        )

    assert result["model_wait"] == [
        {
            "board_id": "board",
            "agent_name": "worker",
            "returns_per_hour": 2,
            "context_bytes_per_hour": 800,
            "estimated_tokens_per_hour": 200,
            "last_24h_returns": 2,
            "last_24h_context_bytes": 800,
            "outcomes": {"cue": 1, "timeout": 1},
        }
    ]


def test_overhead_surfaces_push_unavailable_warning(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "days": {},
                "push_unavailable": {
                    "seat": {
                        "board_id": "pursers",
                        "agent_name": "worker-one",
                        "reason": "unknown event kinds: ['board_claim_ttl_changed']",
                        "observed_at": "2030-01-10T12:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = dashboard.read_overhead_stats(
        path, now=datetime(2030, 1, 10, 12, 30, tzinfo=timezone.utc)
    )

    assert result["push_unavailable"] == [
        {
            "board_id": "pursers",
            "agent_name": "worker-one",
            "reason": "unknown event kinds: ['board_claim_ttl_changed']",
            "observed_at": "2030-01-10T12:00:00+00:00",
            "warning": "push unavailable: unknown event kinds: ['board_claim_ttl_changed']",
        }
    ]


def test_session_pressure_thresholds_trend_and_worst_first_sorting() -> None:
    def cycle(board: str, agent: str, latest_bytes: int, samples: list[int]) -> dict:
        return {
            "board_id": board,
            "agent_name": agent,
            "latest_at": "2030-01-10T12:00:00+00:00",
            "latest_response_bytes": latest_bytes,
            "samples": [
                {
                    "at": f"2030-01-10T11:{index:02d}:00+00:00",
                    "response_bytes": value,
                }
                for index, value in enumerate(samples)
            ],
        }

    document = {
        "schema_version": 2,
        "days": {},
        "poll_cycles": {
            "ok": cycle("board-ok", "ok-seat", 100_000, [100_000] * 24),
            "watch": cycle("board-watch", "watch-seat", 320_000, [320_000] * 24),
            "size": cycle("board-size", "size-seat", 400_004, [400_004] * 24),
            "trend": cycle(
                "board-trend",
                "trend-seat",
                200_000,
                [100_000] * 23 + [200_000],
            ),
        },
    }
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = dashboard.read_overhead_stats(
            path, now=datetime(2030, 1, 10, 12, tzinfo=timezone.utc)
        )

    assert [row["agent_name"] for row in result["sessions"]] == [
        "size-seat",
        "trend-seat",
        "watch-seat",
        "ok-seat",
    ]
    by_name = {row["agent_name"]: row for row in result["sessions"]}
    assert by_name["ok-seat"]["pressure"] == "ok"
    assert by_name["watch-seat"]["pressure"] == "watch"
    assert by_name["watch-seat"]["latest_estimated_tokens"] == 80_000
    assert by_name["size-seat"]["pressure"] == "compact"
    assert by_name["trend-seat"]["pressure"] == "compact"
    assert by_name["trend-seat"]["trend"] == "↑"
    assert by_name["trend-seat"]["trend_ratio"] == 2.0
    assert "journal compaction" in by_name["trend-seat"]["next_action"]


def test_session_pressure_coordinator_override_and_calm_empty_state(
    tmp_path: Path,
) -> None:
    missing = dashboard.read_overhead_stats(tmp_path / "missing.json")
    assert missing["sessions"] == []
    assert "context pressure is calm" in dashboard.HTML

    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "days": {},
                "poll_cycles": {
                    "seat": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "latest_at": "2030-01-10T12:00:00+00:00",
                        "latest_response_bytes": 80_000,
                        "samples": [
                            {
                                "at": "2030-01-10T11:00:00+00:00",
                                "response_bytes": 80_000,
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = dashboard.read_overhead_stats(
        path,
        now=datetime(2030, 1, 10, 12, tzinfo=timezone.utc),
        thresholds={
            "context_watch_tokens_per_poll": 10_000,
            "context_compact_tokens_per_poll": 25_000,
            "context_trend_compact_ratio": 3.0,
        },
    )

    assert result["sessions"][0]["pressure"] == "watch"
    assert result["pressure_thresholds"] == {
        "context_watch_tokens_per_poll": 10_000,
        "context_compact_tokens_per_poll": 25_000,
        "context_trend_compact_ratio": 3.0,
    }


def test_findings_panel_projection_handles_absent_present_and_truncated_state() -> None:
    absent = dashboard.project_board_detail(
        {
            "label": "Board",
            "board_id": "board",
            "snapshot": {"tickets": [], "state": {}},
            "events": [],
        }
    )
    assert absent["coordinator_findings"] is None

    findings = [
        {
            "kind": (
                "review-backlog"
                if index == 0
                else "board-degraded"
                if index == 1
                else f"finding-{index}"
            ),
            "level": "critical" if index == 0 else "warn",
            "message": "x" * 700,
            "ticket_id": "TK-one",
            "evidence": "observed=3600; threshold=1800",
            "next_action": "Review the finding safely.",
        }
        for index in range(dashboard.MAX_FINDINGS + 3)
    ]
    present = dashboard.project_board_detail(
        {
            "label": "Board",
            "board_id": "board",
            "snapshot": {
                "tickets": [],
                "state": {
                    "coordinator_findings": {
                        "value": json.dumps(
                            {
                                "schema_version": 1,
                                "findings": findings,
                                "truncation": {"findings": 2},
                            }
                        )
                    }
                },
            },
            "events": [],
        }
    )["coordinator_findings"]

    assert len(present["items"]) == dashboard.MAX_FINDINGS
    assert present["truncated_count"] == 5
    assert present["items"][0]["level"] == "critical"
    assert [item["kind"] for item in present["items"][:2]] == [
        "review-backlog",
        "board-degraded",
    ]
    assert len(present["items"][0]["text"]) <= dashboard.MAX_FINDING_CHARS
    assert "Coordinator findings" in dashboard.HTML
    assert "/api/overhead" in dashboard.HTML


def test_overhead_endpoint_treats_invalid_utf8_as_malformed_empty_state() -> None:
    class Cache:
        def get(self) -> dict:
            return {}

        def get_board(self, _board_id: str) -> dict:
            return {}

    with tempfile.TemporaryDirectory() as raw:
        stats = Path(raw) / "stats.json"
        stats.write_bytes(b"\xff\xfe")
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.make_handler(Cache(), stats)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/overhead"
            ) as response:
                result = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    assert result["source_status"] == "malformed"
    assert result["seats"] == []


def test_overhead_endpoint_treats_integer_digit_limit_as_malformed() -> None:
    class Cache:
        def get(self) -> dict:
            return {}

        def get_board(self, _board_id: str) -> dict:
            return {}

    with tempfile.TemporaryDirectory() as raw:
        stats = Path(raw) / "stats.json"
        stats.write_text(
            '{"schema_version":1,"days":{"2030-01-01":{"seats":{},"bad":'
            + "9" * 5_000
            + "}}}",
            encoding="utf-8",
        )
        assert stats.stat().st_size < dashboard.MAX_OVERHEAD_FILE_BYTES
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.make_handler(Cache(), stats)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/overhead"
            ) as response:
                result = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    assert result["source_status"] == "malformed"
    assert result["seats"] == []


def valid_coordinator_config() -> dict:
    return {
        "schema_version": 1,
        "thresholds": {
            "stale_seconds": 300,
            "lease_warning_ratio": 0.8,
            "grace_seconds": 600,
            "starved_seconds": 1800,
            "critical_starved_seconds": 600,
            "review_backlog_seconds": 1800,
            "abandoner_drops": 3,
            "abandoner_window_days": 7,
        },
        "integration_watch_since": None,
        "intake": {
            "enabled": False,
            "token_path": "/var/run/pursers/coordinator-intake.jwt",
            "auto_categories": ["docs", "tests", "audit-analysis", "bug"],
            "always_ask_categories": [
                "production-code",
                "release-ci",
                "membership-roles",
                "board-registry",
            ],
            "work_domain_always_ask": True,
            "rate_per_hour": 5,
        },
    }


class FakeCentralFetcher:
    def __init__(
        self, label: str, *, fail: bool = False, overhead_path: Path | None = None
    ) -> None:
        self.config = dashboard.Config(
            url=f"https://{label}.example/mcp",
            token=f"secret-{label}",
            home_board=f"{label}-home",
            agent_name="viewer",
            stale_seconds=300,
            cache_seconds=5,
            label=label,
            overhead_path=overhead_path,
        )
        self.fail = fail
        self.saved: list[tuple[dict, str | None]] = []

    async def fetch(self) -> dict:
        if self.fail:
            raise RuntimeError("central unavailable")
        return {
            "generated_at": "2030-01-01T00:00:00+00:00",
            "pool_summary": {
                "online": 1,
                "busy": 0,
                "available": 1,
                "stale": 0,
            },
            "boards": [{"board_id": self.config.home_board}],
            "agents": [],
        }

    async def fetch_board(self, board_id: str) -> dict:
        if board_id != self.config.home_board:
            raise KeyError(board_id)
        return {"board": {"board_id": board_id}, "tickets": []}

    async def fetch_config(self) -> dict:
        return {"config": {"owner": self.config.label}}

    async def save_config(self, value: dict, expected: str | None) -> dict:
        self.saved.append((value, expected))
        return {"ok": True, "concurrency": "cas"}


def _serve_cache(cache: object) -> tuple[object, threading.Thread]:
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_two_fake_central_aggregation_and_failure_isolation() -> None:
    personal = FakeCentralFetcher("personal")
    work = FakeCentralFetcher("work")
    cache = dashboard.DashboardCache([personal, work], 60)
    server, thread = _serve_cache(cache)
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{root}/api/centrals") as response:
            index = json.load(response)
        with urllib.request.urlopen(f"{root}/api/fleet?central=personal") as response:
            personal_result = json.load(response)
        with urllib.request.urlopen(f"{root}/api/fleet?central=work") as response:
            work_result = json.load(response)
        work.fail = True
        cache._fleets["work"]._expires_at = 0
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(f"{root}/api/fleet?central=work")
        assert captured.value.code == 503
        assert json.load(captured.value)["central"] == "work"
        with urllib.request.urlopen(f"{root}/api/fleet?central=personal") as response:
            isolated = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert index == {"centrals": ["personal", "work"], "default": "personal"}
    assert personal_result["central"] == "personal"
    assert personal_result["boards"][0]["board_id"] == "personal-home"
    assert work_result["central"] == "work"
    assert work_result["boards"][0]["board_id"] == "work-home"
    assert isolated["central"] == "personal"
    assert "secret-personal" not in json.dumps(index)
    assert "secret-work" not in json.dumps([personal_result, work_result])
    assert "personal.example" not in json.dumps(index)


def test_multi_central_overhead_is_scoped_even_with_identical_board_ids(
    tmp_path: Path,
) -> None:
    today = datetime.now(timezone.utc).date().isoformat()

    def stats(agent_name: str, byte_count: int) -> dict:
        return {
            "schema_version": 1,
            "days": {
                today: {
                    "seats": {
                        "same-seat-key": {
                            "board_id": "shared-board-id",
                            "agent_name": agent_name,
                            "request_bytes": byte_count,
                            "response_bytes": 0,
                            "calls": {},
                        }
                    }
                }
            },
        }

    personal_stats = tmp_path / "personal-stats.json"
    work_stats = tmp_path / "work-stats.json"
    personal_stats.write_text(json.dumps(stats("personal-seat", 101)), encoding="utf-8")
    work_stats.write_text(json.dumps(stats("work-seat", 202)), encoding="utf-8")
    cache = dashboard.DashboardCache(
        [
            FakeCentralFetcher("personal", overhead_path=personal_stats),
            FakeCentralFetcher("work", overhead_path=work_stats),
        ],
        60,
    )
    server, thread = _serve_cache(cache)
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(
            f"{root}/api/overhead?central=personal"
        ) as response:
            personal = json.load(response)
        with urllib.request.urlopen(f"{root}/api/overhead?central=work") as response:
            work = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert personal["central"] == "personal"
    assert personal["seats"] == [
        {
            **personal["seats"][0],
            "board_id": "shared-board-id",
            "agent_name": "personal-seat",
            "today_bytes": 101,
        }
    ]
    assert work["central"] == "work"
    assert work["seats"] == [
        {
            **work["seats"][0],
            "board_id": "shared-board-id",
            "agent_name": "work-seat",
            "today_bytes": 202,
        }
    ]
    assert "work-seat" not in json.dumps(personal)
    assert "personal-seat" not in json.dumps(work)


def test_multi_central_overhead_without_scoped_source_fails_closed(
    tmp_path: Path,
) -> None:
    global_stats = tmp_path / "global-stats.json"
    global_stats.write_text(
        json.dumps({"schema_version": 1, "days": {}}), encoding="utf-8"
    )
    cache = dashboard.DashboardCache(
        [FakeCentralFetcher("personal"), FakeCentralFetcher("work")], 60
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache, global_stats)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/overhead?central=personal"
            )
        assert captured.value.code == 503
        body = json.load(captured.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert body == {
        "error": "central overhead source is not configured",
        "central": "personal",
    }


def test_multi_central_param_routing_and_config_save_target() -> None:
    personal = FakeCentralFetcher("personal")
    work = FakeCentralFetcher("work")
    cache = dashboard.DashboardCache([personal, work], 60)
    server, thread = _serve_cache(cache)
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{root}/api/fleet") as response:
            default_result = json.load(response)
        with urllib.request.urlopen(
            f"{root}/api/board/work-home?central=work"
        ) as response:
            detail = json.load(response)
        with urllib.request.urlopen(f"{root}/api/config?central=work") as response:
            config = json.load(response)
        request = urllib.request.Request(
            f"{root}/api/config?central=work",
            data=json.dumps(
                {
                    "config": valid_coordinator_config(),
                    "expected_sha256": "a" * 64,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            saved = json.load(response)
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(f"{root}/api/fleet?central=missing")
        assert captured.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert default_result["central"] == "personal"
    assert detail["central"] == "work"
    assert detail["board"]["board_id"] == "work-home"
    assert config == {"config": {"owner": "work"}, "central": "work"}
    assert saved["central"] == "work"
    assert personal.saved == []
    assert work.saved == [(valid_coordinator_config(), "a" * 64)]


def test_single_central_flags_and_response_shape_remain_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONBOARD_CENTRAL_URL", raising=False)
    monkeypatch.delenv("PURSERS_CENTRAL_URL", raising=False)
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "single-secret")
    args = dashboard.parse_args(["--port", "8899"])
    args.token_file = None
    configs = dashboard.load_central_configs(args)
    assert len(configs) == 1
    assert configs[0].label == "default"
    assert configs[0].url == dashboard.DEFAULT_URL
    assert configs[0].home_board == dashboard.DEFAULT_HOME_BOARD

    result = dashboard.DashboardCache(FakeCentralFetcher("only"), 60).get()
    assert result["central"] == "only"
    assert result["pool_summary"]["online"] == 1
    assert "boards" in result and "agents" in result

    inherited_url = "https://central.example.test:8766/mcp"
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", inherited_url)
    args_inherited = dashboard.parse_args(["--port", "8899"])
    args_inherited.token_file = None
    configs_inherited = dashboard.load_central_configs(args_inherited)
    assert len(configs_inherited) == 1
    assert configs_inherited[0].url == inherited_url


def test_centrals_file_and_tokens_require_0600(tmp_path: Path) -> None:
    personal_token = tmp_path / "personal.token"
    work_token = tmp_path / "work.token"
    personal_token.write_text("personal-secret", encoding="utf-8")
    work_token.write_text("work-secret", encoding="utf-8")
    personal_token.chmod(0o600)
    work_token.chmod(0o600)
    central_file = tmp_path / "centrals.json"
    central_file.write_text(
        json.dumps(
            [
                {
                    "label": "personal",
                    "url": "https://personal.example/mcp",
                    "token_path": "personal.token",
                    "home_board": "personal-home",
                    "stats_path": "personal-stats.json",
                },
                {
                    "label": "work",
                    "url": "https://work.example/mcp",
                    "token_path": "work.token",
                    "home_board": "work-home",
                },
            ]
        ),
        encoding="utf-8",
    )
    central_file.chmod(0o600)
    args = SimpleNamespace(
        centrals=str(central_file),
        url="ignored",
        token_file=None,
        home_board="ignored",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
    )

    configs = dashboard.load_central_configs(args)
    assert [(item.label, item.token) for item in configs] == [
        ("personal", "personal-secret"),
        ("work", "work-secret"),
    ]
    assert configs[0].overhead_path == tmp_path / "personal-stats.json"
    assert configs[1].overhead_path is None

    central_file.chmod(0o644)
    with pytest.raises(SystemExit, match="centrals config must be a regular 0600 file"):
        dashboard.load_central_configs(args)

    central_file.chmod(0o600)
    work_token.chmod(0o644)
    with pytest.raises(
        SystemExit, match="token file for central work must be a regular 0600 file"
    ):
        dashboard.load_central_configs(args)


@pytest.mark.parametrize(
    ("section", "field", "bad_value"),
    [
        ("thresholds", "stale_seconds", 9),
        ("thresholds", "lease_warning_ratio", 1.1),
        ("thresholds", "grace_seconds", 86_401),
        ("thresholds", "starved_seconds", 9),
        ("thresholds", "critical_starved_seconds", 86_401),
        ("thresholds", "review_backlog_seconds", 9),
        ("thresholds", "abandoner_drops", 0),
        ("thresholds", "abandoner_window_days", 366),
        ("thresholds", "context_watch_tokens_per_poll", 999),
        ("thresholds", "context_compact_tokens_per_poll", 1_000),
        ("thresholds", "context_trend_compact_ratio", 1.0),
        ("intake", "rate_per_hour", 21),
    ],
)
def test_config_endpoint_rejects_every_out_of_range_field(
    section: str, field: str, bad_value: object
) -> None:
    class Cache:
        def save_config(self, value: object, _expected: str | None) -> dict:
            dashboard.validate_coordinator_config(value)
            return {"ok": True}

    config = valid_coordinator_config()
    config[section][field] = bad_value
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/config",
            data=json.dumps({"config": config, "expected_sha256": None}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request)
        assert captured.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_config_save_writes_only_coordinator_config_with_cas() -> None:
    assert dashboard.DASHBOARD_WRITE_KEYS == frozenset(
        {"coordinator_config", "coordinator_intake"}
    )
    calls: list[tuple[str, dict]] = []

    class Client:
        value = json.dumps(
            valid_coordinator_config(), sort_keys=True, separators=(",", ":")
        )

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_state_get(self, *, key: str) -> dict:
            assert key == "coordinator_config"
            return {"state": {"value": self.value}}

        async def _call(self, name: str, arguments: dict) -> dict:
            calls.append((name, arguments))
            return {"ok": True}

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="dashboard-seat",
        stale_seconds=300,
        cache_seconds=5,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())
    digest = hashlib.sha256(Client.value.encode()).hexdigest()

    result = asyncio.run(fetcher.save_config(valid_coordinator_config(), digest))

    assert result["concurrency"] == "cas"
    assert calls[0][0] == "board_state_update"
    assert calls[0][1]["key"] == "coordinator_config"
    assert calls[0][1]["expected_sha256"] == digest
    assert json.loads(calls[0][1]["value"])["updated_by"] == "dashboard-seat"


def test_config_endpoint_enforces_create_then_cas() -> None:
    class Client:
        value: str | None = None

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_state_get(self, *, key: str) -> dict:
            assert key == "coordinator_config"
            if self.value is None:
                raise dashboard.BoardClientError("state key not found")
            return {"state": {"value": self.value}}

        async def _call(self, name: str, arguments: dict) -> dict:
            assert name == "board_state_update"
            if self.value is not None:
                digest = hashlib.sha256(self.value.encode()).hexdigest()
                assert arguments["expected_sha256"] == digest
            self.value = arguments["value"]
            return {"ok": True}

    client = Client()
    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="dashboard-seat",
        stale_seconds=300,
        cache_seconds=5,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: client)
    cache = dashboard.DashboardCache(fetcher, 5)
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(expected: str | None, *, include_expected: bool = True) -> int:
        body = {"config": valid_coordinator_config()}
        if include_expected:
            body["expected_sha256"] = expected
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/config",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert post(None) == 200  # First create may use LWW.
        assert post(None) == 409
        assert post("0" * 64) == 409
        assert post(None, include_expected=False) == 400
        matching = hashlib.sha256(client.value.encode()).hexdigest()
        assert post(matching) == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_config_hash_page_renders_all_knobs_and_sources() -> None:
    assert 'href="#/config"' in dashboard.HTML
    assert 'id="config-view"' in dashboard.HTML
    assert "Mode changes require a restart." in dashboard.HTML
    for field in (
        *dashboard.CONFIG_THRESHOLD_FIELDS,
        *dashboard.CONFIG_PRESSURE_FIELDS,
        "integration_watch_since",
        "token_path",
        "rate_per_hour",
    ):
        assert field in dashboard.HTML
    assert "source:" in dashboard.HTML


def test_config_round_trips_optional_intake_token_path() -> None:
    config = valid_coordinator_config()

    assert dashboard.validate_coordinator_config(config) == config
    config["intake"].pop("token_path")
    assert dashboard.validate_coordinator_config(config) == config
    config["intake"]["token_path"] = None
    assert dashboard.validate_coordinator_config(config) == config
    assert 'name="token_path"' in dashboard.HTML
    assert "token_path:f.get('token_path').trim()||null" in dashboard.HTML


@pytest.mark.parametrize(
    "value",
    ["", "relative/intake.jwt", " /absolute/intake.jwt", "/tmp/../intake.jwt", "/tmp/intake\n.jwt", 7],
)
def test_config_rejects_unsafe_intake_token_path(value: object) -> None:
    config = valid_coordinator_config()
    config["intake"]["token_path"] = value

    with pytest.raises(ValueError, match="safe absolute path"):
        dashboard.validate_coordinator_config(config)


def test_config_accepts_optional_context_pressure_thresholds() -> None:
    config = valid_coordinator_config()
    config["thresholds"].update(
        {
            "context_watch_tokens_per_poll": 20_000,
            "context_compact_tokens_per_poll": 60_000,
            "context_trend_compact_ratio": 1.75,
        }
    )

    assert dashboard.validate_coordinator_config(config) == config

    invalid = valid_coordinator_config()
    invalid["thresholds"]["context_watch_tokens_per_poll"] = 90_000
    with pytest.raises(ValueError, match="context_watch_tokens_per_poll is invalid"):
        dashboard.validate_coordinator_config(invalid)


class FakeIntakeCentral:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.force_conflict = False

    def client(self, board_id: str) -> object:
        owner = self

        class Client:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def board_state_get(self, *, key: str) -> dict:
                if key == "project_registry":
                    return registry(
                        {
                            "Pursers": {
                                "board_id": "pursers",
                                "status": "active",
                                "work_dir": "/tmp/pursers",
                            },
                            "Paused": {
                                "board_id": "paused",
                                "status": "paused",
                                "work_dir": "/tmp/paused",
                            },
                        }
                    )
                value = owner.values.get((board_id, key))
                if value is None:
                    raise dashboard.BoardClientError("state key not found")
                return {"state": {"value": value}}

            async def _call(self, name: str, arguments: dict) -> dict:
                assert name == "board_state_update"
                owner.calls.append((board_id, name, dict(arguments)))
                if owner.force_conflict:
                    raise dashboard.BoardClientError("state precondition failed")
                current = owner.values.get((board_id, arguments["key"]))
                expected = arguments.get("expected_sha256")
                if (
                    current is not None
                    and expected != hashlib.sha256(current.encode()).hexdigest()
                ):
                    raise dashboard.BoardClientError("state precondition failed")
                owner.values[(board_id, arguments["key"])] = arguments["value"]
                return {"ok": True}

        return Client()


def intake_fetcher(
    central: FakeIntakeCentral,
    *,
    now: datetime = datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
) -> dashboard.FleetFetcher:
    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="dashboard-seat",
        stale_seconds=300,
        cache_seconds=5,
    )
    return dashboard.FleetFetcher(
        config,
        client_factory=lambda _url, _token, board_id, **_kwargs: central.client(
            board_id
        ),
        now_factory=lambda: now,
    )


def seed_intake_draft(
    central: FakeIntakeCentral,
    ask: dict,
    *,
    title: str = "Publish release 2.0",
    category: str = "release-ci",
) -> None:
    evidence = json.dumps(
        {
            "ask_id": ask["id"],
            "category": category,
            "decision": "ask",
            "draft": {"title": title},
        }
    )
    central.values[(ask["board_id"], "coordinator_findings")] = json.dumps(
        {
            "findings": [
                {
                    "kind": "intake-pending",
                    "ask_id": ask["id"],
                    "evidence": evidence,
                }
            ]
        }
    )


@pytest.mark.parametrize(
    ("board_id", "text", "message"),
    [
        (None, "Valid intake ask", "invalid board_id"),
        ("bad/board", "Valid intake ask", "invalid board_id"),
        ("paused", "Valid intake ask", "not registry-active"),
        ("pursers", None, "text must be a string"),
        ("pursers", "four", "between 5 and 500"),
        ("pursers", "x" * 501, "between 5 and 500"),
    ],
)
def test_intake_validation_matrix(board_id: object, text: object, message: str) -> None:
    fetcher = intake_fetcher(FakeIntakeCentral())
    with pytest.raises(ValueError, match=message):
        asyncio.run(fetcher.save_intake(board_id, text))


def test_intake_append_uses_create_then_cas_and_coordinator_shape() -> None:
    central = FakeIntakeCentral()
    fetcher = intake_fetcher(central)

    first = asyncio.run(fetcher.save_intake("pursers", "Update the operator guide"))
    second = asyncio.run(fetcher.save_intake("pursers", "Add regression tests"))

    assert first["concurrency"] == "lww"
    assert second["concurrency"] == "cas"
    assert "expected_sha256" not in central.calls[0][2]
    assert re.fullmatch(r"[0-9a-f]{64}", central.calls[1][2]["expected_sha256"])
    assert {call[2]["key"] for call in central.calls} == {"coordinator_intake"}
    ask = first["ask"]
    assert uuid.UUID(ask["id"]).version == 5
    assert ask == {
        "id": ask["id"],
        "text": "Update the operator guide",
        "requested_by": "dashboard-seat",
        "board_id": "pursers",
        "created_at": "2030-01-01T12:00:00+00:00",
    }

    coordinator_path = Path(__file__).parents[2] / "coordinator" / "coordinator.py"
    spec = importlib.util.spec_from_file_location("coordinator_shape", coordinator_path)
    assert spec and spec.loader
    coordinator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = coordinator
    spec.loader.exec_module(coordinator)
    parsed = coordinator.parse_intake(
        {"state": {"value": json.dumps([ask])}}, "pursers"
    )
    assert [
        (item.ask_id, item.text, item.requested_by, item.board_id) for item in parsed
    ] == [(ask["id"], ask["text"], "dashboard-seat", "pursers")]


def test_intake_uuid_is_deterministic_for_content_and_time() -> None:
    first = asyncio.run(
        intake_fetcher(FakeIntakeCentral()).save_intake("pursers", "Update docs")
    )
    second = asyncio.run(
        intake_fetcher(FakeIntakeCentral()).save_intake("pursers", "Update docs")
    )
    assert first["ask"]["id"] == second["ask"]["id"]


def test_intake_endpoint_returns_409_on_cas_conflict() -> None:
    central = FakeIntakeCentral()
    central.values[("pursers", "coordinator_intake")] = "[]"
    central.force_conflict = True
    cache = dashboard.DashboardCache(intake_fetcher(central), 5)
    server, thread = _serve_cache(cache)
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/api/intake",
        data=json.dumps(
            {"board_id": "pursers", "text": "Update the operator guide"}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request)
        assert captured.value.code == 409
        assert "changed" in json.load(captured.value)["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_intake_endpoint_enforces_ten_asks_per_hour() -> None:
    central = FakeIntakeCentral()
    cache = dashboard.DashboardCache(intake_fetcher(central), 5)
    server, thread = _serve_cache(cache)
    root = f"http://127.0.0.1:{server.server_port}/api/intake"

    def post(index: int) -> int:
        request = urllib.request.Request(
            root,
            data=json.dumps(
                {"board_id": "pursers", "text": f"Update guide item {index}"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert [post(index) for index in range(10)] == [200] * 10
        assert post(10) == 429
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_intake_get_endpoint_and_ui_render_waiting_and_consumed_states() -> None:
    central = FakeIntakeCentral()
    fetcher = intake_fetcher(central)
    created = asyncio.run(fetcher.save_intake("pursers", "Update the dashboard guide"))
    cache = dashboard.DashboardCache(fetcher, 5)
    server, thread = _serve_cache(cache)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/intake?board_id=pursers"
        ) as response:
            body = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert body["waiting"] == [created["ask"]]
    assert body["rate_limit"] == {"asks": 10, "window_seconds": 3600}
    assert "สั่งงาน / new ask" in dashboard.HTML
    assert "Pending asks" in dashboard.HTML
    assert "consumed (gone)" in dashboard.HTML
    assert "Review the drafted title/category, then Approve or Decline" in dashboard.HTML


def test_intake_approve_and_decline_are_cas_guarded() -> None:
    central = FakeIntakeCentral()
    fetcher = intake_fetcher(central)
    first = asyncio.run(fetcher.save_intake("pursers", "Publish the release notes"))
    seed_intake_draft(central, first["ask"])
    approved = asyncio.run(
        fetcher.decide_intake(
            "pursers",
            first["ask"]["id"],
            "approve",
            first["expected_sha256"],
            "Edited release ticket",
        )
    )

    assert approved["ask"] == {
        **first["ask"],
        "approved": True,
        "approved_by": "dashboard-seat",
        "approved_at": "2030-01-01T12:00:00+00:00",
        "approved_title": "Edited release ticket",
    }
    assert approved["concurrency"] == "cas"
    with pytest.raises(dashboard.ConfigConflictError, match="changed"):
        asyncio.run(
            fetcher.decide_intake(
                "pursers",
                first["ask"]["id"],
                "decline",
                first["expected_sha256"],
            )
        )

    second = asyncio.run(fetcher.save_intake("pursers", "Deploy the next release"))
    declined = asyncio.run(
        fetcher.decide_intake(
            "pursers",
            second["ask"]["id"],
            "decline",
            second["expected_sha256"],
        )
    )
    state = asyncio.run(fetcher.fetch_intake("pursers"))
    assert declined["tombstone"] == {
        "id": second["ask"]["id"],
        "text": "Deploy the next release",
        "board_id": "pursers",
        "declined_by": "dashboard-seat",
        "declined_at": "2030-01-01T12:00:00+00:00",
    }
    assert [row["id"] for row in state["waiting"]] == [first["ask"]["id"]]
    assert state["declined"] == [declined["tombstone"]]
    assert {call[2]["key"] for call in central.calls} == {"coordinator_intake"}
    assert all("expected_sha256" in call[2] for call in central.calls[1:])


def test_intake_decision_endpoint_returns_conflict_for_stale_cas() -> None:
    central = FakeIntakeCentral()
    fetcher = intake_fetcher(central)
    created = asyncio.run(fetcher.save_intake("pursers", "Publish release notes"))
    seed_intake_draft(central, created["ask"])
    cache = dashboard.DashboardCache(fetcher, 5)
    server, thread = _serve_cache(cache)
    payload = {
        "board_id": "pursers",
        "ask_id": created["ask"]["id"],
        "action": "approve",
        "expected_sha256": created["expected_sha256"],
        "title": "Approved release notes",
    }

    def request() -> urllib.request.Request:
        return urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/intake",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    try:
        with urllib.request.urlopen(request()) as response:
            assert response.status == 200
            assert json.load(response)["ask"]["approved_title"] == (
                "Approved release notes"
            )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request())
        assert captured.value.code == 409
        assert "changed" in json.load(captured.value)["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_intake_decline_tombstones_are_bounded_to_twenty() -> None:
    central = FakeIntakeCentral()
    asks = [
        {
            "id": "ask-current",
            "text": "Publish current release",
            "requested_by": "operator",
            "board_id": "pursers",
        }
    ]
    tombstones = [
        {
            "id": f"ask-old-{index}",
            "text": f"Old ask {index}",
            "board_id": "pursers",
            "declined_by": "operator",
            "declined_at": f"2030-01-01T11:{index:02d}:00+00:00",
        }
        for index in range(20)
    ]
    encoded = dashboard._encode_intake_document(asks, tombstones)
    central.values[("pursers", "coordinator_intake")] = encoded
    result = asyncio.run(
        intake_fetcher(central).decide_intake(
            "pursers",
            "ask-current",
            "decline",
            hashlib.sha256(encoded.encode()).hexdigest(),
        )
    )
    state = asyncio.run(intake_fetcher(central).fetch_intake("pursers"))

    assert len(state["declined"]) == 20
    assert state["declined"][0]["id"] == "ask-old-1"
    assert state["declined"][-1] == result["tombstone"]


def test_intake_pending_finding_exposes_bounded_draft_preview() -> None:
    evidence = json.dumps(
        {
            "ask_id": "ask-preview",
            "category": "release-ci",
            "decision": "ask",
            "draft": {"title": "Publish release 2.0"},
        }
    )
    result = dashboard.project_coordinator_findings(
        {
            "state": {
                "coordinator_findings": {
                    "value": json.dumps(
                        {
                            "findings": [
                                {
                                    "kind": "intake-pending",
                                    "level": "warn",
                                    "message": "Approval required",
                                    "ask_id": "ask-preview",
                                    "evidence": evidence,
                                }
                            ]
                        }
                    )
                }
            }
        }
    )

    assert result is not None
    assert result["items"][0]["ask_id"] == "ask-preview"
    assert result["items"][0]["draft"] == {
        "title": "Publish release 2.0",
        "category": "release-ci",
    }
    assert "data-intake-action=\"approve\"" in dashboard.HTML
    assert "data-intake-action=\"decline\"" in dashboard.HTML
    assert "Draft ticket title" in dashboard.HTML


def test_intake_approval_waits_for_matching_validated_coordinator_draft() -> None:
    central = FakeIntakeCentral()
    fetcher = intake_fetcher(central)
    created = asyncio.run(fetcher.save_intake("pursers", "Publish release notes"))
    ask = created["ask"]

    with pytest.raises(dashboard.ConfigConflictError, match="draft is not ready"):
        asyncio.run(
            fetcher.decide_intake(
                "pursers",
                ask["id"],
                "approve",
                created["expected_sha256"],
            )
        )
    assert len(central.calls) == 1
    assert "Waiting for the coordinator draft" in dashboard.HTML
    assert "${x.draft?'<button type=\"button\" class=\"approve\"" in dashboard.HTML
    assert "data-intake-action=\"decline\"" in dashboard.HTML
    assert "actionable:false,intake_status:'consumed (gone)'" in dashboard.HTML
    assert "${x.actionable&&!x.approved?" in dashboard.HTML

    seed_intake_draft(central, ask, title="Coordinator release draft")
    approved = asyncio.run(
        fetcher.decide_intake(
            "pursers",
            ask["id"],
            "approve",
            created["expected_sha256"],
        )
    )
    assert approved["ask"]["approved_title"] == "Coordinator release draft"


def test_dashboard_write_whitelist_is_exact_across_both_writes() -> None:
    central = FakeIntakeCentral()
    fetcher = intake_fetcher(central)
    config_text = json.dumps(
        valid_coordinator_config(), sort_keys=True, separators=(",", ":")
    )
    central.values[("pursers", "coordinator_config")] = config_text
    asyncio.run(
        fetcher.save_config(
            valid_coordinator_config(), hashlib.sha256(config_text.encode()).hexdigest()
        )
    )
    asyncio.run(fetcher.save_intake("pursers", "Update the dashboard guide"))

    written = {arguments["key"] for _board, _name, arguments in central.calls}
    assert (
        dashboard.DASHBOARD_WRITE_KEYS
        == frozenset(written)
        == frozenset({"coordinator_config", "coordinator_intake"})
    )
    with pytest.raises(ValueError, match="not writable"):
        dashboard._dashboard_state_update_arguments(
            agent_name="dashboard-seat", key="other_state", value="{}"
        )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://provider.invalid/v1",
        "http://provider.invalid/v1",
        "https://user:pass@provider.invalid/v1",
        "https://provider.invalid/v1?token=x",
        "https://provider.invalid/v1#fragment",
    ],
)
def test_worker_url_validation_rejects_unsafe_urls(value: str) -> None:
    with pytest.raises(ValueError):
        dashboard.validate_worker_url(value)


def test_worker_manager_keychain_config_lifecycle_and_adoption(tmp_path: Path) -> None:
    secret = "SYNTHETIC_KEYCHAIN_SECRET"
    commands: list[list[str]] = []
    processes: list[object] = []

    def command_runner(argv: list[str], **_kwargs: object) -> object:
        commands.append(argv)
        stdout = secret + "\n" if argv[1] == "find-generic-password" else ""
        return SimpleNamespace(stdout=stdout)

    class Process:
        pid = 43210

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False

        def poll(self) -> int | None:
            return None if self.alive else 0

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

        def wait(self, timeout: int) -> int:
            assert timeout == 10
            return 0

    def process_factory(argv: list[str], **kwargs: object) -> Process:
        assert argv[1].endswith("pursers_worker.py")
        assert argv[2].endswith("worker-one.toml")
        assert kwargs["start_new_session"] is True
        process = Process()
        processes.append(process)
        return process

    manager = dashboard.WorkerManager(
        tmp_path / "workers",
        platform="darwin",
        command_runner=command_runner,
        process_factory=process_factory,
        process_matches=lambda pid, path: (
            pid == 9876 and path.name == "worker-one.toml"
        ),
    )
    result = manager.save(
        {
            "name": "worker-one",
            "provider": "custom",
            "base_url": "https://provider.invalid/v1",
            "model": "model-one",
            "api_key": secret,
        },
        "http://127.0.0.1:8766/mcp",
    )
    config_path = tmp_path / "workers" / "worker-one.toml"
    document = tomllib.loads(config_path.read_text())
    assert result == {"ok": True, "name": "worker-one", "key_stored": True}
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert document["boards"] == "registry"
    assert document["seat"]["role"] == "worker"
    assert document["claim"]["max_tier"] == "heavy"
    assert document["llm"]["api_key_keychain"] == "worker-one"
    assert secret not in config_path.read_text()
    assert commands[0] == [
        "/usr/bin/security",
        "add-generic-password",
        "-s",
        "pursers-worker",
        "-a",
        "worker-one",
        "-U",
        "-w",
        secret,
    ]

    token_path = tmp_path / "seats" / "worker-one.jwt"
    token_path.parent.mkdir()
    token_path.write_text("SEAT_TOKEN")
    token_path.chmod(0o600)
    with pytest.raises(ValueError, match="seat missing"):
        manager.start("worker-one", seat_exists=False)
    started = manager.start("worker-one", seat_exists=True)
    pid_path = tmp_path / "workers" / "worker-one.pid"
    assert started["running"] is True
    assert stat.S_IMODE(pid_path.stat().st_mode) == 0o600
    runtime_log_path = manager._log_path("worker-one")
    runtime_log_path.write_text(
        json.dumps(
            {
                "event": "review_started",
                "board_id": "pursers",
                "ticket_id": "TK-interrupted",
            }
        )
        + "\n"
    )
    manager._store_active_review(
        "worker-one", {"board_id": "pursers", "ticket_id": "TK-interrupted"}
    )
    review_state_path = manager._review_state_path("worker-one")
    assert review_state_path.exists()
    assert manager.stop("worker-one")["running"] is False
    assert processes[0].terminated is True
    assert not pid_path.exists()
    assert not review_state_path.exists()
    assert json.loads(runtime_log_path.read_text().splitlines()[-1]) == {
        "event": "review_session_reset",
        "reason": "managed_stop",
    }

    manager._write_private(  # exercise safe adoption of a matching pidfile
        pid_path, dashboard._json_bytes({"pid": 9876, "name": "worker-one"})
    )
    adopted = manager.list({"worker-one"})[0]
    assert adopted["running"] is True
    assert adopted["adopted"] is True
    assert adopted["seat_exists"] is True
    assert secret not in json.dumps(adopted)

    log_path = tmp_path / "workers" / "worker-one.session.log"
    log_path.write_text("\n".join(f"line-{index}" for index in range(25)))
    assert manager.log_tail("worker-one") == [f"line-{index}" for index in range(5, 25)]


def test_active_review_requires_explicit_unfinished_lifecycle() -> None:
    started = json.dumps(
        {
            "event": "review_started",
            "board_id": "board-one",
            "ticket_id": "TK-review",
        }
    )
    finished = json.dumps(
        {
            "event": "review_finished",
            "board_id": "board-one",
            "ticket_id": "TK-review",
            "outcome": "approve",
        }
    )

    assert dashboard._active_review_from_log(["not json", started]) == {
        "board_id": "board-one",
        "ticket_id": "TK-review",
    }
    assert dashboard._active_review_from_log([started, finished]) is None
    unrelated_finish = json.dumps(
        {
            "event": "review_finished",
            "board_id": "other",
            "ticket_id": "TK-review",
        }
    )
    assert dashboard._active_review_from_log(
        [started, unrelated_finish]
    ) == {"board_id": "board-one", "ticket_id": "TK-review"}


def test_active_review_runtime_session_fence_invalidates_stale_lifecycle() -> None:
    started = json.dumps(
        {
            "event": "review_started",
            "board_id": "board-one",
            "ticket_id": "TK-stale-review",
        }
    )
    session_fence = json.dumps(
        {
            "event": "runtime_session_started",
            "role": "reviewer",
            "session_id": "replacement",
        }
    )

    assert dashboard._active_review_from_log([started, session_fence]) is None


def test_stop_on_never_created_root_is_safe_and_creates_it_on_first_write(
    tmp_path: Path,
) -> None:
    """Idempotent stop must honour the lazy worker-root contract on darwin."""
    root = tmp_path / "never-created" / "workers"
    manager = dashboard.WorkerManager(root, platform="darwin")
    # Construction stays lazy: nothing exists until the first private write.
    assert not root.exists()

    # Regression: the fence write used to raise FileNotFoundError here because
    # stop() no longer had a constructor-created root to write into.
    stopped = manager.stop("worker-one")

    assert stopped == {"ok": True, "name": "worker-one", "running": False}
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    log_path = root / "worker-one.session.log"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert json.loads(log_path.read_text().splitlines()[-1]) == {
        "event": "review_session_reset",
        "reason": "managed_stopped",
    }
    assert not manager._review_state_path("worker-one").exists()


def test_worker_provider_test_uses_keychain_without_echoing_secret(
    tmp_path: Path,
) -> None:
    secret = "PROVIDER_TEST_SECRET"
    authorization: list[str | None] = []

    class Handler(dashboard.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            authorization.append(self.headers.get("Authorization"))
            body = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def command_runner(argv: list[str], **_kwargs: object) -> object:
        return SimpleNamespace(
            stdout=(secret + "\n") if argv[1] == "find-generic-password" else ""
        )

    manager = dashboard.WorkerManager(
        tmp_path / "workers", platform="darwin", command_runner=command_runner
    )
    try:
        manager.save(
            {
                "name": "provider-test",
                "provider": "custom",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "model-one",
                "api_key": secret,
            },
            "http://127.0.0.1:8766/mcp",
        )
        result = manager.test_provider("provider-test")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert result == {"ok": True, "name": "provider-test", "provider_reachable": True}
    assert authorization == ["".join(("Bea", "rer")) + " " + secret]  # noqa: FLY002
    assert secret not in json.dumps(result)


def test_worker_api_is_local_and_board_write_surface_is_unchanged(
    tmp_path: Path,
) -> None:
    secret = "API_ENDPOINT_SECRET"

    def command_runner(argv: list[str], **_kwargs: object) -> object:
        return SimpleNamespace(stdout="")

    manager = dashboard.WorkerManager(
        tmp_path / "workers",
        platform="darwin",
        command_runner=command_runner,
        process_matches=lambda pid, path: (
            pid == 24680 and path.name == "endpoint-worker.toml"
        ),
    )

    class Cache:
        def resolve_central(self, value: str | None) -> str:
            if value not in {None, "default"}:
                raise KeyError(value)
            return "default"

        def central_url(self, _value: str | None) -> str:
            return "http://127.0.0.1:8766/mcp"

        def get(self) -> dict:
            return {"agents": [{"agent_name": "endpoint-worker"}]}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), worker_manager=manager)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = {
            "name": "endpoint-worker",
            "provider": "custom",
            "base_url": "https://provider.invalid/v1",
            "model": "model-one",
            "api_key": secret,
        }
        request = urllib.request.Request(
            base + "/api/workers",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            saved_body = response.read().decode()
        log_path = tmp_path / "workers" / "endpoint-worker.session.log"
        pid_path = tmp_path / "workers" / "endpoint-worker.pid"
        manager._write_private(
            pid_path,
            dashboard._json_bytes({"pid": 24680, "name": "endpoint-worker"}),
        )
        events: list[dict[str, str]] = []

        def append_event(event: str, **fields: str) -> None:
            record = {"event": event, **fields}
            events.append(record)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")

        append_event("runtime_session_started", role="reviewer", session_id="A")
        append_event(
            "review_started", board_id="pursers", ticket_id="TK-stale-review"
        )
        manager._store_active_review(
            "endpoint-worker",
            {"board_id": "pursers", "ticket_id": "TK-stale-review"},
        )
        for index in range(25):
            append_event("review_run_shell", command=f"stale-{index}")
        append_event("runtime_session_started", role="reviewer", session_id="B")
        manager._store_active_review("endpoint-worker", None)
        with urllib.request.urlopen(base + "/api/workers") as response:
            restarted_body = response.read().decode()
        assert json.loads(restarted_body)["workers"][0]["current_work"] == []

        append_event(
            "review_started", board_id="pursers", ticket_id="TK-active-review"
        )
        manager._store_active_review(
            "endpoint-worker",
            {"board_id": "pursers", "ticket_id": "TK-active-review"},
        )
        for index in range(25):
            append_event("review_run_shell", command=f"verification-{index}")
        with urllib.request.urlopen(base + "/api/workers") as response:
            listed_body = response.read().decode()
        review_state_path = manager._review_state_path("endpoint-worker")
        assert json.loads(review_state_path.read_text()) == {
            "schema": 1,
            "board_id": "pursers",
            "ticket_id": "TK-active-review",
        }
        assert stat.S_IMODE(review_state_path.stat().st_mode) == 0o600
        dashboard_restart = dashboard.WorkerManager(
            tmp_path / "workers",
            platform="darwin",
            command_runner=command_runner,
            process_matches=lambda pid, path: (
                pid == 24680 and path.name == "endpoint-worker.toml"
            ),
        )
        assert dashboard_restart.active_review("endpoint-worker") == {
            "board_id": "pursers",
            "ticket_id": "TK-active-review",
        }
        pid_path.unlink()
        with urllib.request.urlopen(base + "/api/workers") as response:
            stopped_body = response.read().decode()
        assert json.loads(stopped_body)["workers"][0]["current_work"] == []
        assert not review_state_path.exists()
        append_event(
            "review_finished",
            board_id="pursers",
            ticket_id="TK-active-review",
            outcome="reject",
        )
        manager._store_active_review("endpoint-worker", None)
        with urllib.request.urlopen(base + "/api/workers") as response:
            finished_body = response.read().decode()
        board_write = urllib.request.Request(
            base + "/api/board/pursers",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(board_write)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert captured.value.code == 404
    assert secret not in saved_body
    assert secret not in listed_body
    listed_worker = json.loads(listed_body)["workers"][0]
    assert listed_worker["seat_exists"] is True
    assert len(listed_worker["log_tail"]) == 20
    assert all("review_started" not in line for line in listed_worker["log_tail"])
    assert listed_worker["current_work"] == [
        {
            "board_id": "pursers",
            "role": "reviewer",
            "ticket_id": "TK-active-review",
            "ticket_title": "TK-active-review",
        }
    ]
    assert json.loads(finished_body)["workers"][0]["current_work"] == []
    assert not review_state_path.exists()


def test_workers_tab_renders_presets_actions_and_keychain_copy() -> None:
    assert "API workers ·" in dashboard.HTML
    assert "DeepSeek" not in dashboard.HTML  # presets arrive from the local API
    assert 'data-worker-action="test"' in dashboard.HTML
    assert 'data-worker-action="start"' in dashboard.HTML
    assert 'data-worker-action="stop"' in dashboard.HTML
    assert "Copy seat command" in dashboard.HTML
    assert "macOS Keychain" in dashboard.HTML
    assert "workerFormDirty&&!force" in dashboard.HTML
    assert ".onclick=workerClick" in dashboard.HTML
    assert "workerActionMessage" in dashboard.HTML
    assert "board_state_update" not in dashboard.HTML


def test_timer_refresh_pauses_while_operator_edits() -> None:
    html = dashboard.HTML
    assert "function refreshPaused()" in html
    assert "Refresh paused while editing" in html
    # fixed-position pill with a Resume button, visible wherever the operator is scrolled
    assert "pill.id='refresh-paused'" in html
    assert 'id=\"refresh-resume\">Resume</button>' in html
    assert "e.target.closest?.('#state,#refresh-resume')" in html
    assert "#refresh-paused{position:fixed;" in html
    # real input events mark the form dirty; programmatic value changes never lock refresh
    assert "t.form.dataset.dirty='1'" in html
    assert "delete e.target.dataset.dirty" in html
    # the search box never pauses refresh
    assert "a.id!=='filter'" in html
    for fn in (
        "async function refreshFleet(timeoutMs=CENTRAL_REQUEST_TIMEOUT_MS){if(typeof refreshPaused==='function'&&refreshPaused())return;",
        "r.kind!=='board'||refreshPaused()",
        "r.kind!=='overhead'||refreshPaused()",
        "r.kind!=='config'||refreshPaused()",
        "(!force&&refreshPaused())",
        "!centralLabels.length||refreshPaused()",
        "async function refreshAttentionState(){if(refreshPaused())return;",
        # in-flight requests that started before editing must not render on completion
        "if(!route()&&!(typeof refreshPaused==='function'&&refreshPaused()))renderFleet()",
        "if(hubKinds.has(route()?.kind)&&!refreshPaused())renderHub()",
        "navKind()==='seats'&&centralLabels.length&&!refreshPaused())await refreshSeats()",
        "includes(navKind())&&!refreshPaused())renderHub()",
        "navKind()==='overview'&&!refreshPaused())renderHub()",
        "current?.board!==r.board||refreshPaused())return;detailData=data",
        "route()?.central!==r.central||refreshPaused())return;renderOverhead(data)",
        "route()?.central===r.central&&!refreshPaused()){renderConfig(data)",
        "if(navKind()==='seats'&&!refreshPaused())renderHub()",
    ):
        assert fn in html, fn
    assert "if(navKind()==='seats')renderHub()" not in html


def test_dashboard_v2_ia_agents_and_responsive_contract() -> None:
    html = dashboard.HTML

    assert 'class="app-shell"' in html
    assert 'aria-label="Primary navigation"' in html
    assert 'href="#/boards"' in html
    assert 'href="#/agents"' in html
    assert 'href="#/operations"' in html
    assert "Fleet overview" in html
    assert "Board workspaces" in html
    assert "Unified agent pool" in html
    assert "Needs attention" in html
    assert "Coordinator heartbeat" in html
    assert "New API agent" in html
    assert "Worker-only until reviewer runtime is installed." in html
    assert "Log tail · last 20 lines" in html
    assert 'data-hub-agent-action="restart"' in html
    assert "if(hubKinds.has(r?.kind)){syncHub();return}" in html
    assert "max_tier" in html
    assert "@media(max-width:800px)" in html
    assert "overflow-x:hidden" in html
    assert "https://cdn" not in html
    assert "http://cdn" not in html


def test_seat_config_manager_plan_apply_backup_restart_and_no_token_leak(
    tmp_path: Path,
) -> None:
    secret = ".".join(  # noqa: FLY002 - avoid a literal token-shaped fixture.
        ("eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJwcmluY2lwYWwifQ", "signature12345")
    )
    config = tmp_path / "config.toml"
    config.write_text(f'api_token = "{secret}"\n')
    token = tmp_path / "central.token"
    token.write_text(secret)
    ca = tmp_path / "ca.pem"
    ca.write_text("CA")

    class Bridge:
        version = "0.1.0a16"

        def inspect(self) -> dict:
            return {
                "version": self.version,
                "installed_version": self.version,
                "reported_version": "0.1.0a1",
                "pinned_version": self.version,
                "latest_pypi_version": self.version,
                "resolution_source": "well-known:uv-tool",
                "command": None,
                "installed": False,
                "private_ca_active": False,
            }

        def install(self) -> str:
            return "/tmp/pursers-wait-bridge"

    manager = dashboard.SeatConfigManager(
        tmp_path / "state/seats.json",
        state_dir=tmp_path / "state",
        bridge_installer=Bridge(),
        latest_version=lambda: "0.1.0a16",
    )
    desired = {
        "host": "codex",
        "role": "worker",
        "name": "worker-one",
        "central_url": "http://127.0.0.1:8766/mcp",
        "home_board": "pursers",
        "token_file": str(token),
        "ca_file": str(ca),
        "bridge_command": "/tmp/pursers-wait-bridge",
        "config_path": str(config),
    }

    plan = manager.plan(desired)
    encoded_plan = json.dumps(plan)
    assert plan["token_file_exists"] is True
    assert plan["ca_file_exists"] is True
    assert secret not in encoded_plan
    assert "[REDACTED]" in encoded_plan

    result = manager.apply(plan["plan_id"])
    assert result["needs_restart"] is True
    assert result["backup_path"]
    assert "worker-one" in result["prompt"]
    assert secret not in json.dumps(result)
    assert Path(result["backup_path"]).read_text() == f'api_token = "{secret}"\n'
    manager.inventory.upsert(
        dashboard.DesiredSeat.from_dict(desired),
        bridge_version="0.1.0a16",
        doctor={
            "overall": "WARN",
            "checks": [
                {
                    "seat": "worker-one",
                    "check": "restart",
                    "status": "WARN",
                    "message": "restart required",
                }
            ],
        },
    )
    row = manager.seats()["seats"][0]
    assert row["principal_label"] == "worker"
    assert row["needs_restart"] is True
    bridge = manager.bridge()
    assert bridge["installed_version"] == "0.1.0a16"
    assert bridge["reported_version"] == "0.1.0a1"
    assert bridge["pinned_version"] == "0.1.0a16"
    assert bridge["latest_pypi_version"] == "0.1.0a16"
    assert bridge["resolution_source"] == "well-known:uv-tool"
    journal = (tmp_path / "state/config-actions.jsonl").read_text()
    assert secret not in journal


def test_seat_config_manager_reviews_imports_and_doctors_discovered_seats(
    tmp_path: Path,
) -> None:
    discovered = []
    before = {}
    for host, adapter_type in (
        ("codex", dashboard.adapter_for),
        ("goose", dashboard.adapter_for),
        ("claude-desktop", dashboard.adapter_for),
    ):
        root = tmp_path / host
        root.mkdir()
        token = root / "seat.jwt"
        token.write_text("header.synthetic.signature")
        config = root / "config"
        desired = dashboard.DesiredSeat(
            host=host,
            role="worker",
            name=f"{host}-worker",
            central_url="https://central.example/mcp",
            home_board="pursers",
            token_file=str(token),
            ca_file=str(root / "ca.pem"),
            bridge_command=str(root / "pursers-wait-bridge"),
            config_path=str(config),
        )
        adapter = adapter_type(desired)
        adapter.apply(adapter.plan(desired))
        discovered.append((host, config))
        before[config] = config.read_text()

    class Bridge:
        version = "0.1.0a16"

        def inspect(self) -> dict:
            return {"version": self.version, "command": None}

    doctor_done = threading.Event()
    doctor_calls = []

    class FakeDoctor:
        def run(self, desired: dashboard.DesiredSeat) -> list:
            doctor_calls.append(desired.name)
            if len(doctor_calls) == 3:
                doctor_done.set()
            return [SimpleNamespace(seat=desired.name, check="config", status="PASS", message="matches")]

    manager = dashboard.SeatConfigManager(
        tmp_path / "state/seats.json",
        state_dir=tmp_path / "state",
        bridge_installer=Bridge(),
        latest_version=lambda: None,
        doctor_factory=FakeDoctor,
        discovered_configs=discovered,
    )

    review = manager.import_review()
    assert review["conflicts"] == []
    assert len(review["candidates"]) == 3
    assert all(row["zero_diff"] for row in review["candidates"])
    assert all(row["boards"] == "home" for row in review["candidates"])  # named home board = dedicated
    assert all(row["tier_max"] == 2 for row in review["candidates"])
    assert all(row["can_work"] is True for row in review["candidates"])
    assert all(row["bridge_connector_name"] for row in review["candidates"])

    result = manager.import_discovered()

    assert {row["name"] for row in result["imported"]} == {
        "codex-worker",
        "goose-worker",
        "claude-desktop-worker",
    }
    assert result["doctor_job"]["status"] == "queued"
    assert doctor_done.wait(2)
    for _ in range(200):
        if manager.job(result["doctor_job"]["job_id"])["status"] == "succeeded":
            break
        threading.Event().wait(0.01)
    assert manager.job(result["doctor_job"]["job_id"])["status"] == "succeeded"
    assert len(manager.inventory.load()["seats"]) == 3
    assert manager.import_review()["candidates"] == []
    assert len(manager.import_review()["already_imported"]) == 3
    assert all(path.read_text() == content for path, content in before.items())

    conflicting = dashboard.DesiredSeat.from_dict(
        {
            **manager.inventory.load()["seats"][0],
            "central_url": "https://different.example/mcp",
        }
    )
    manager.inventory.upsert(conflicting, bridge_version="0.1.0a16")
    conflict_review = manager.import_review()
    assert any("different settings" in row["reason"] for row in conflict_review["conflicts"])


def test_import_review_pairs_shared_codex_connectors_and_ignores_auxiliary(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    seats = []
    for role, name, bridge_name, board_name, token_env in (
        (
            "worker",
            "codex-worker",
            "pursers-wait-codex-worker",
            "pursers-dev",
            "ONBOARD_CENTRAL_TOKEN",
        ),
        (
            "reviewer",
            "codex-reviewer",
            "pursers-wait-codex-reviewer",
            "pursers-review",
            "PURSERS_REVIEW_TOKEN",
        ),
    ):
        token = tmp_path / f"{name}.jwt"
        token.write_text(f"header.{name}.signature")
        desired = dashboard.DesiredSeat(
            host="codex",
            role=role,
            name=name,
            central_url="https://central.example/mcp",
            home_board="pursers",
            token_file=str(token),
            token_env_var=token_env,
            ca_file=str(tmp_path / "ca.pem"),
            bridge_command=str(tmp_path / "pursers-wait-bridge"),
            config_path=str(config),
            bridge_name=bridge_name,
            board_connector_name=board_name,
        )
        adapter = dashboard.adapter_for(desired)
        adapter.apply(adapter.plan(desired))
        seats.append(desired)

    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pursers-personal": {
                        "command": "pursers-personal",
                        "env": {"ONBOARD_AGENT_NAME": "auxiliary-session"},
                    }
                }
            }
        )
    )

    class Bridge:
        version = "0.1.0a16"

        def inspect(self) -> dict:
            return {"version": self.version, "command": None}

    manager = dashboard.SeatConfigManager(
        tmp_path / "state/seats.json",
        state_dir=tmp_path / "state",
        bridge_installer=Bridge(),
        latest_version=lambda: None,
        discovered_configs=[
            ("codex", config),
            ("claude-desktop", claude_config),
        ],
    )

    review = manager.import_review()

    assert review["conflicts"] == []
    assert {row["name"] for row in review["candidates"]} == {
        "codex-worker",
        "codex-reviewer",
    }
    mappings = {row["name"]: row for row in review["candidates"]}
    assert mappings["codex-worker"]["board_connector_name"] == "pursers-dev"
    assert mappings["codex-worker"]["token_env_var"] == "ONBOARD_CENTRAL_TOKEN"
    assert mappings["codex-reviewer"]["board_connector_name"] == "pursers-review"
    assert mappings["codex-reviewer"]["token_env_var"] == "PURSERS_REVIEW_TOKEN"
    assert all(row["zero_diff"] for row in mappings.values())


def test_seat_config_registry_coverage_uses_live_fleet_seats(tmp_path: Path) -> None:
    class Bridge:
        version = "0.1.0a16"

        def inspect(self) -> dict:
            return {"version": self.version, "command": None}

    manager = dashboard.SeatConfigManager(
        tmp_path / "seats.json",
        state_dir=tmp_path,
        bridge_installer=Bridge(),
        latest_version=lambda: None,
    )
    desired = dashboard.DesiredSeat(
        host="codex",
        role="worker",
        name="covered-seat",
        central_url="http://127.0.0.1:8766/mcp",
        home_board="board-one",
        token_file=str(tmp_path / "token"),
        ca_file=str(tmp_path / "ca.pem"),
        bridge_command="/tmp/pursers-wait-bridge",
        config_path=str(tmp_path / "config.toml"),
    )
    manager.inventory.upsert(desired, bridge_version="0.1.0a16")

    result = manager.registry(
        {
            "boards": [{"board_id": "board-one", "label": "One"}],
            "agents": [
                {
                    "agent_name": "covered-seat",
                    "seats": [{"board_id": "board-one"}],
                }
            ],
        }
    )

    assert result == {
        "boards": [
            {
                "board_id": "board-one",
                "label": "One",
                "seat_coverage": 1,
                "configured_seats": 1,
            }
        ],
        "seats": {
            "covered-seat": {
                "status": None,
                "current_offer": None,
                "capabilities": {},
            }
        },
        "read_only": True,
    }


def test_seat_config_doctor_reports_operator_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Bridge:
        version = "0.1.0a16"

        def inspect(self) -> dict:
            return {"version": self.version, "command": None}

    class EmptyDoctor:
        def run(self, _desired: object) -> list:
            return []

    manager = dashboard.SeatConfigManager(
        tmp_path / "seats.json",
        state_dir=tmp_path,
        bridge_installer=Bridge(),
        doctor_factory=EmptyDoctor,
        latest_version=lambda: None,
    )
    desired = dashboard.DesiredSeat(
        host="codex",
        role="worker",
        name="worker-one",
        central_url="http://127.0.0.1:8766/mcp",
        home_board="pursers",
        token_file=str(tmp_path / "token"),
        ca_file=str(tmp_path / "ca.pem"),
        bridge_command="/tmp/pursers-wait-bridge",
        config_path=str(tmp_path / "config.toml"),
    )
    manager.inventory.upsert(desired, bridge_version=Bridge.version)

    seat_two_dir = tmp_path / "seat-two"
    (seat_two_dir / "Alpha" / ".git").mkdir(parents=True)
    desired_two = dashboard.DesiredSeat(
        host="goose",
        role="worker",
        name="worker-two",
        central_url="http://127.0.0.1:8766/mcp",
        home_board="pursers",
        token_file=str(tmp_path / "token2"),
        ca_file=str(tmp_path / "ca2.pem"),
        bridge_command="/tmp/pursers-wait-bridge",
        config_path=str(tmp_path / "config2.yaml"),
        seat_dir=str(seat_two_dir),
        repository="https://example.invalid/Alpha.git",
    )
    manager.inventory.upsert(desired_two, bridge_version=Bridge.version)

    empty_clone = (tmp_path / "empty-clone").resolve()
    dirty_clone = (tmp_path / "dirty-clone").resolve()

    def clone_state(path: Path) -> dict:
        if path == empty_clone:
            return {"status": "empty_worktree"}
        if path == dirty_clone:
            return {"status": "dirty"}
        return {"status": "missing"}

    monkeypatch.setattr(manager, "_clone_state", clone_state)

    registry_payload = {
        "registry": {
            "schema_version": 1,
            "projects": {
                "Alpha": {
                    "board_id": "alpha",
                    "work_dir": "/operator/alpha",
                    "status": "active",
                },
                "Beta": {
                    "board_id": "beta",
                    "work_dir": "/operator/beta",
                    "fleet_clone_dir": str(empty_clone),
                    "status": "active",
                },
                "Gamma": {
                    "board_id": "gamma",
                    "work_dir": "/operator/gamma",
                    "fleet_clone_dir": str(dirty_clone),
                    "status": "active",
                },
            },
        }
    }
    reg = manager.registry(fleet={}, registry_payload=registry_payload)
    alpha_proj = next(p for p in reg["projects"] if p["name"] == "Alpha")
    assert alpha_proj["operator_checkout_seats"] == ["worker-one"]
    beta_proj = next(p for p in reg["projects"] if p["name"] == "Beta")
    assert beta_proj["operator_checkout_seats"] == []
    assert beta_proj["clone"]["status"] == "empty_worktree"
    gamma_proj = next(p for p in reg["projects"] if p["name"] == "Gamma")
    assert gamma_proj["clone"]["status"] == "dirty"

    job = manager.doctor(
        ["worker-one", "worker-two"],
        registry_payload,
    )
    deadline = time.monotonic() + 2
    while (result := manager.job(job["job_id"]))["status"] not in {
        "succeeded",
        "failed",
    } and time.monotonic() < deadline:
        time.sleep(0.01)

    assert result["status"] == "succeeded"
    seats_by_name = {s["seat"]: s for s in result["result"]["seats"]}
    worker_one_tree = next(
        c
        for c in seats_by_name["worker-one"]["checks"]
        if c["check"] == "working-tree"
    )
    assert worker_one_tree["status"] == "FAIL"
    assert "operator checkout is read-only for seats" in worker_one_tree["message"]

    worker_two_tree = next(
        c
        for c in seats_by_name["worker-two"]["checks"]
        if c["check"] == "working-tree"
    )
    assert worker_two_tree["status"] == "PASS"
    assert "working tree routes to fleet-owned or seat-owned clone" in worker_two_tree["message"]
    clone_health = next(
        c
        for c in seats_by_name["worker-two"]["checks"]
        if c["check"] == "fleet-clones"
    )
    assert clone_health["status"] == "FAIL"
    assert f"empty worktree: Beta ({empty_clone})" in clone_health["message"]
    assert f"local changes: Gamma ({dirty_clone})" in clone_health["message"]


def test_seat_config_doctor_reports_clone_preflight_per_project(
    tmp_path: Path,
) -> None:
    def git_runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:4] == ["remote", "get-url", "origin"]:
            origin = f"https://example.invalid/{Path(_kwargs['cwd']).name}.git"
            return subprocess.CompletedProcess(command, 0, origin + "\n", "")
        if command[1] == "ls-remote":
            if any("offline.git" in item for item in command):
                return subprocess.CompletedProcess(command, 128, "", "network unavailable")
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\trefs/heads/main\n", "")
        raise AssertionError(command)

    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path / "state",
        latest_version=lambda: None,
        git_runner=git_runner,
    )
    registry_payload = {
        "registry": {
            "schema_version": 1,
            "projects": {
                "Online": {
                    "board_id": "online",
                    "work_dir": str(tmp_path / "online"),
                    "status": "active",
                },
                "Offline": {
                    "board_id": "offline",
                    "work_dir": str(tmp_path / "offline"),
                    "status": "active",
                },
            },
        }
    }

    job = manager.doctor(registry_payload=registry_payload)
    deadline = time.monotonic() + 2
    while (result := manager.job(job["job_id"]))["status"] not in {
        "succeeded",
        "failed",
    } and time.monotonic() < deadline:
        time.sleep(0.01)

    assert result["status"] == "succeeded"
    reports = {row["project"]: row for row in result["result"]["projects"]}
    assert reports["Online"]["status"] == "PASS"
    assert reports["Online"]["message"] == "origin exposes main"
    assert reports["Offline"]["status"] == "FAIL"
    assert "cannot reach origin from the dashboard service" in reports["Offline"]["message"]


def test_config_api_and_ui_contract_are_separate_from_coordinator_config() -> None:
    calls: list[tuple[str, object]] = []

    class Cache:
        def resolve_central(self, value: str | None) -> str:
            return value or "default"

        def get(self) -> dict:
            return {"boards": []}

        def get_project_registry(self) -> dict:
            return {
                "registry": {"schema_version": 1, "projects": {}},
                "expected_sha256": "a" * 64,
            }

        def save_project_registry(
            self, value: object, expected_sha256: object
        ) -> dict:
            calls.append(("save-registry", (value, expected_sha256)))
            return {
                "registry": value,
                "expected_sha256": "b" * 64,
            }

    class Seats:
        def seats(self) -> dict:
            return {"seats": [], "discovered_configs": []}

        def bridge(self) -> dict:
            return {
                "installed_version": "0.1.0a16",
                "reported_version": "0.1.0a6",
                "package_metadata_version": "0.1.0a16",
                "pinned_version": "0.1.0a16",
                "latest_pypi_version": "0.1.0a16",
                "resolution_source": "config:codex",
                "status": "WARN",
                "message": (
                    "version string stale; reported=0.1.0a6; package=0.1.0a16"
                ),
            }

        def registry(self, fleet: dict, registry_payload: dict) -> dict:
            return {"boards": [], "projects": [], "read_only": False}

        def prepare_fleet_clone(
            self, registry_payload: dict, project: object
        ) -> dict:
            calls.append(("prepare-clone", project))
            return {
                "registry": registry_payload["registry"],
                "expected_sha256": registry_payload["expected_sha256"],
                "project": project,
                "clone": {"status": "ready"},
            }

        def plan(self, request: object) -> dict:
            calls.append(("plan", request))
            return {"plan_id": "a" * 32, "changes": []}

        def apply(self, plan_id: object) -> dict:
            calls.append(("apply", plan_id))
            return {"backup_path": "/tmp/config.backup", "needs_restart": True}

        def prompt(self, request: object) -> dict:
            calls.append(("prompt", request))
            return {"prompt": "session prompt"}

        def doctor(self, names: object, registry_payload: dict) -> dict:
            calls.append(("doctor", names))
            return {"job_id": "b" * 32, "status": "queued"}

        def import_discovered(self, names: object) -> dict:
            calls.append(("import", names))
            return {"imported": [], "conflicts": [], "doctor_job": None}

        def install_bridge(self) -> dict:
            calls.append(("install", {}))
            return {"job_id": "c" * 32, "status": "queued"}

        def upgrade_all(self) -> dict:
            calls.append(("upgrade-all", {}))
            return {"job_id": "d" * 32, "status": "queued"}

        def job(self, job_id: str) -> dict:
            calls.append(("job", job_id))
            return {"job_id": job_id, "status": "succeeded", "result": {}}

        def release_status(self) -> dict:
            calls.append(("release_status", {}))
            return {"latest_tag": "v5.0.0a20", "versions": {"product": "5.0.0a20"}}

        def prepare_ops_action(self, action: str, **kwargs: Any) -> dict:
            calls.append(("prepare_ops_action", action, kwargs))
            return {
                "plan_id": "e" * 32,
                "digest": "f" * 64,
                "action": action,
                "command": f"cmd {action}",
            }

        def ops_action(self, plan_id: str, digest: str) -> dict:
            calls.append(("ops_action", plan_id, digest))
            return {"ok": True, "job_id": "9" * 32, "command": "cmd"}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=Seats())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: object) -> dict:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    try:
        with urllib.request.urlopen(base + "/api/config/seats") as response:
            assert json.load(response)["seats"] == []
        with urllib.request.urlopen(base + "/api/config/bridge") as response:
            bridge = json.load(response)
            assert bridge == {
                "installed_version": "0.1.0a16",
                "reported_version": "0.1.0a6",
                "package_metadata_version": "0.1.0a16",
                "pinned_version": "0.1.0a16",
                "latest_pypi_version": "0.1.0a16",
                "resolution_source": "config:codex",
                "status": "WARN",
                "message": (
                    "version string stale; reported=0.1.0a6; package=0.1.0a16"
                ),
            }
        with urllib.request.urlopen(base + "/api/config/registry") as response:
            assert json.load(response)["read_only"] is False
        with urllib.request.urlopen(base + "/api/config/release") as response:
            assert json.load(response)["latest_tag"] == "v5.0.0a20"
        clone = post("/api/config/registry/clone", {"project": "fixture"})
        assert clone["clone"]["status"] == "ready"
        assert post("/api/config/plan", {"name": "fixture"})["plan_id"] == "a" * 32
        assert post("/api/config/apply", {"plan_id": "a" * 32})["backup_path"]
        assert post("/api/config/prompt", {"name": "fixture"})["prompt"]
        assert post("/api/config/doctor", {"names": ["fixture"]})["status"] == "queued"
        assert post("/api/config/import", {})["imported"] == []
        assert post("/api/config/bridge/install", {})["job_id"] == "c" * 32
        assert post("/api/config/bridge/upgrade-all", {})["job_id"] == "d" * 32
        ops_plan = post(
            "/api/config/ops/plan",
            {"action": "publish_from_tag", "tag": "v5.0.0a20"},
        )
        assert post(
            "/api/config/ops",
            {"plan_id": ops_plan["plan_id"], "digest": ops_plan["digest"]},
        )["ok"] is True
        with urllib.request.urlopen(base + "/api/config/jobs/" + "b" * 32) as response:
            assert json.load(response)["status"] == "succeeded"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert calls == [
        ("release_status", {}),
        ("prepare-clone", "fixture"),
        (
            "save-registry",
            ({"schema_version": 1, "projects": {}}, "a" * 64),
        ),
        ("plan", {"name": "fixture"}),
        ("apply", "a" * 32),
        ("prompt", {"name": "fixture"}),
        ("doctor", ["fixture"]),
        ("import", None),
        ("install", {}),
        ("upgrade-all", {}),
        ("prepare_ops_action", "publish_from_tag", {"tag": "v5.0.0a20"}),
        ("ops_action", "e" * 32, "f" * 64),
        ("job", "b" * 32),
    ]
    assert 'href="#/seats"' in dashboard.HTML
    assert "Release & Operations" in dashboard.HTML
    assert "Release card" in dashboard.HTML
    assert "Seat restart checklist" in dashboard.HTML
    assert "data-ops-action=" in dashboard.HTML
    assert "Publish from tag" in dashboard.HTML
    assert "Stage Central" in dashboard.HTML
    assert "Kickstart Central" in dashboard.HTML
    assert "Restart dashboard" in dashboard.HTML
    assert "Preview exact changes" in dashboard.HTML
    assert "Copy session prompt" in dashboard.HTML
    assert "Token file path · token never enters this page" in dashboard.HTML
    assert "latest_pypi_version" in dashboard.HTML
    assert "resolution_source" in dashboard.HTML
    assert "seatBridge.status" in dashboard.HTML
    assert "seatBridge.message" in dashboard.HTML
    assert "Create fleet clone" in dashboard.HTML
    assert "operator checkout is read-only for seats" in dashboard.HTML
    assert "Resolved via" in dashboard.HTML
    assert "setInterval(async()=>" in dashboard.HTML
    assert "Import discovered seats" in dashboard.HTML
    assert "Import and run Doctor" in dashboard.HTML
    assert "/api/config/import" in dashboard.HTML
    assert "doctor_summary" in dashboard.HTML
    assert ",1000)" in dashboard.HTML
    assert "/api/config" in dashboard.HTML  # original coordinator route remains.


@pytest.mark.parametrize(
    "path",
    ["/api/config/bridge/install", "/api/config/bridge/upgrade-all"],
)
def test_config_mutations_reject_cross_origin_text_plain_before_action(
    path: str,
) -> None:
    calls: list[str] = []

    class Cache:
        def resolve_central(self, value: str | None) -> str:
            return value or "default"

    class Seats:
        def install_bridge(self) -> dict:
            calls.append("install")
            return {"job_id": "a" * 32, "status": "queued"}

        def upgrade_all(self) -> dict:
            calls.append("upgrade-all")
            return {"job_id": "b" * 32, "status": "queued"}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=Seats())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    cross_origin = urllib.request.Request(
        base + path,
        data=b"{}",
        headers={"Content-Type": "text/plain", "Origin": "https://attacker.invalid"},
        method="POST",
    )
    same_origin_text = urllib.request.Request(
        base + path,
        data=b"{}",
        headers={"Content-Type": "text/plain", "Origin": base},
        method="POST",
    )
    invalid_host = urllib.request.Request(
        base + path,
        data=b"{}",
        headers={"Content-Type": "application/json", "Host": "attacker.invalid"},
        method="POST",
    )
    same_origin = urllib.request.Request(
        base + path,
        data=b"{}",
        headers={"Content-Type": "application/json", "Origin": base},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(cross_origin)
        assert captured.value.code == 403
        assert calls == []
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(same_origin_text)
        assert captured.value.code == 415
        assert calls == []
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(invalid_host)
        assert captured.value.code == 403
        assert calls == []
        with urllib.request.urlopen(same_origin) as response:
            assert json.load(response)["status"] == "queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert calls == ["install" if path.endswith("install") else "upgrade-all"]


def test_agents_hub_defaults_to_active_sorted_status_with_toggle_and_live_work() -> None:
    script = "\n".join(re.findall(r"<script>(.*?)</script>", dashboard.HTML, re.DOTALL | re.IGNORECASE))
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    long_title = "Ticket title " + "x" * 80
    agents = [
        {
            "agent_name": "m-stale",
            "pool_status": "stale",
            "boards": ["pursers"],
            "seats": [],
            "last_seen": "2030-01-01T09:00:00Z",
        },
        {
            "agent_name": "a-available",
            "pool_status": "available",
            "boards": ["pursers"],
            "seats": [],
            "last_seen": "2030-01-01T11:58:00Z",
        },
        {
            "agent_name": "z-busy",
            "pool_status": "busy",
            "boards": ["pursers"],
            "seats": [
                {
                    "board_id": "pursers",
                    "project": "Pursers",
                    "role": "worker",
                    "current_ticket_id": "TK-live",
                    "current_ticket_title": long_title,
                    "last_seen": "2030-01-01T11:58:00Z",
                }
            ],
            "last_seen": "2030-01-01T11:58:00Z",
        },
    ]
    program = "\n".join(
        [
            source("const esc="),
            source("const fmt="),
            source("const agentStatusRank="),
            source("function compareAgents("),
            source("function visibleAgents("),
            source("function relativeAge("),
            source("function clippedAgentTitle("),
            source("function agentLiveWork("),
            source("function agentTicketLink("),
            source("function agentVisibilityToggle("),
            source("function pageHead("),
            source("function workerByName("),
            source("function renderRoleChips("),
            source("function liveAgentCard("),
            source("function renderGuide("),
            source("function renderAgentsHub("),
            "Date.now=()=>new Date('2030-01-01T12:00:00Z').getTime();",
            f"let fleetData={{personal:{{agents:{json.dumps(agents)}}}}},hubWorkers={{}},hubGuide=null,showStaleAgents=false;",
            "const active=renderAgentsHub();showStaleAgents=true;const all=renderAgentsHub();",
            "console.log(JSON.stringify({active,all}));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    active = result["active"]
    all_agents = result["all"]

    assert "m-stale" not in active
    assert active.index("z-busy") < active.index("a-available")
    assert "Show stale" in active
    assert 'aria-pressed="false"' in active
    assert "TK-live" in active
    assert "Ticket title " + "x" * 34 + "…" in active
    assert "2m ago" in active
    assert "ว่าง/idle" in active
    assert all_agents.index("z-busy") < all_agents.index("a-available")
    assert all_agents.index("a-available") < all_agents.index("m-stale")
    assert "Show active only" in all_agents
    assert 'aria-pressed="true"' in all_agents


def test_agent_pool_rows_keep_details_and_default_to_active() -> None:
    script = "\n".join(re.findall(r"<script>(.*?)</script>", dashboard.HTML, re.DOTALL | re.IGNORECASE))
    lines = script.splitlines()

    def source(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    agents = [
        {
            "agent_name": "available-agent",
            "pool_status": "available",
            "boards": ["pursers"],
            "seats": [],
            "last_seen": "2030-01-01T11:58:00Z",
            "duplicate_name": False,
        },
        {
            "agent_name": "busy-agent",
            "pool_status": "busy",
            "boards": ["pursers"],
            "seats": [
                {
                    "board_id": "pursers",
                    "project": "Pursers",
                    "role": "worker",
                    "current_ticket_id": "TK-held",
                    "current_ticket_title": "Held ticket",
                    "last_seen": "2030-01-01T11:58:00Z",
                }
            ],
            "last_seen": "2030-01-01T11:58:00Z",
            "duplicate_name": False,
        },
        {
            "agent_name": "stale-agent",
            "pool_status": "stale",
            "boards": ["pursers"],
            "seats": [],
            "last_seen": "2029-12-01T00:00:00Z",
            "duplicate_name": False,
        },
    ]
    fleet = {
        "central": "personal",
        "pool_summary": {"online": 2, "busy": 1, "available": 1, "stale": 1},
        "boards": [],
        "agents": agents,
    }
    program = "\n".join(
        [
            source("const esc="),
            source("const fmt="),
            source("const ticketMatches="),
            source("const filterHomeBoards="),
            source("const agentStatusRank="),
            source("function compareAgents("),
            source("function visibleAgents("),
            source("function relativeAge("),
            source("function clippedAgentTitle("),
            source("function agentLiveWork("),
            source("function agentTicketLink("),
            source("function agentStateSummary("),
            source("function agentVisibilityToggle("),
            source("function renderCentral("),
            source("const legacyRenderCentralAgentsV1="),
            source("renderCentral=function("),
            "Date.now=()=>new Date('2030-01-01T12:00:00Z').getTime();",
            f"let filterNeedle='',showStaleAgents=false;const fleet={json.dumps(fleet)};",
            "const active=renderCentral(fleet);showStaleAgents=true;const all=renderCentral(fleet);",
            "console.log(JSON.stringify({active,all}));",
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert "stale-agent" not in result["active"]
    assert result["active"].index("busy-agent") < result["active"].index(
        "available-agent"
    )
    assert "TK-held" in result["active"]
    assert "Held ticket" in result["active"]
    assert "2m ago" in result["active"]
    assert '<div class="agent-body table-scroll"><table>' in result["active"]
    assert "stale-agent" in result["all"]


def test_worker_manager_accepts_v2_role_and_tier_only_when_supported(
    tmp_path: Path,
) -> None:
    worker_script = tmp_path / "reviewer-runtime.py"
    worker_script.write_text(
        'role = seat.get("role", "worker")\nif role == "reviewer": pass\n'
    )
    manager = dashboard.WorkerManager(
        tmp_path / "workers",
        worker_script=worker_script,
        platform="darwin",
        command_runner=lambda *_args, **_kwargs: SimpleNamespace(stdout=""),
    )

    manager.save(
        {
            "name": "reviewer-one",
            "role": "reviewer",
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local-model",
            "api_key": "",
            "max_tier": "standard",
        },
        "http://127.0.0.1:8766/mcp",
    )

    definition = manager.list()[0]
    assert manager.roles == ("worker", "reviewer")
    assert definition["role"] == "reviewer"
    assert definition["max_tier"] == "standard"
    assert "--role reviewer" in definition["seat_admin_command"]


def test_aggregate_fleet_seats_preserve_board_id_and_role_for_chip_labeling() -> None:
    """Multi-board seats carry board_id + role so the JS can label each chip."""
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=20)).isoformat()
    rows = [
        {
            "label": "Full Platts",
            "board_id": "fullplatts",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-a",
                        "agent_id": "AI-one",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "membership_role": "member",
                        "status": "working",
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
        {
            "label": "Pursers",
            "board_id": "pursers",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-a",
                        "agent_id": "AI-two",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "membership_role": "reviewer",
                        "status": "active",
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
    ]

    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)

    agent = result["agents"][0]
    assert len(agent["seats"]) == 2
    seat_roles = {(s["board_id"], s["role"]) for s in agent["seats"]}
    assert ("fullplatts", "member") in seat_roles
    assert ("pursers", "reviewer") in seat_roles


def test_aggregate_fleet_identical_roles_still_keep_board_ids() -> None:
    """When the same role appears on every board, seats still carry board_id."""
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=20)).isoformat()
    rows = [
        {
            "label": "Alpha",
            "board_id": "board-a",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-b",
                        "agent_id": "AI-a",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "membership_role": "worker",
                        "status": "active",
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
        {
            "label": "Beta",
            "board_id": "board-b",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-b",
                        "agent_id": "AI-b",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "membership_role": "worker",
                        "status": "active",
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
    ]

    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)

    agent = result["agents"][0]
    assert len(agent["seats"]) == 2
    roles = {s["role"] for s in agent["seats"]}
    assert roles == {"worker"}
    assert {s["board_id"] for s in agent["seats"]} == {"board-a", "board-b"}


def test_aggregate_fleet_agent_without_role_produces_null_role() -> None:
    """An agent without membership_role or role gets role=None in the seat."""
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=20)).isoformat()
    rows = [
        {
            "label": "No Role Board",
            "board_id": "norole",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "ghost",
                        "agent_id": "AI-ghost",
                        "last_activity_at": recent,
                        "lifecycle_status": "active",
                        "status": "active",
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
    ]

    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)

    agent = result["agents"][0]
    assert agent["seats"][0]["role"] is None


# ── Role-chip rendering tests (JS layer via node) ──────────────────────

def _extract_js_function(html: str, name: str) -> str:
    """Extract a named JS function definition from the dashboard HTML.
    
    The dashboard's JS functions are minified onto single lines, so we
    extract the full line containing the function definition.
    """
    import re as _re
    # Match the start of the function definition on its line
    pattern = _re.compile(
        rf"(?:const|function)\s+{_re.escape(name)}\s*[=(][^\n]+",
    )
    match = pattern.search(html)
    assert match is not None, f"JS function {name} not found in HTML"
    return match.group(0)


def _run_js_with_seats(esc_fn: str, render_fn: str, seats: list[dict], fallback: str = "worker") -> str:
    """Execute renderRoleChips with given seats in node and return stdout."""
    seats_json = json.dumps(seats)
    fallback_json = json.dumps(fallback)
    script = (
        f"{esc_fn};\n"
        f"{render_fn};\n"
        f"console.log(renderRoleChips({seats_json},{fallback_json}));\n"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_operator_and_worker_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    worker_dir = tmp_path_factory.mktemp("workers")
    state_dir = tmp_path_factory.mktemp("fleet_dashboard_state")
    monkeypatch.setattr(dashboard, "DEFAULT_WORKERS_DIR", worker_dir)
    monkeypatch.setattr(dashboard, "CONFIG_STATE_DIR", state_dir)
    monkeypatch.setattr(
        dashboard.WorkerManager,
        "_default_process_matches",
        lambda self, pid, config_path: False,
    )


@pytest.fixture(scope="module")
def _role_chip_js():
    """Extract esc and renderRoleChips from the dashboard HTML once per module."""
    esc_fn = _extract_js_function(dashboard.HTML, "esc")
    render_fn = _extract_js_function(dashboard.HTML, "renderRoleChips")
    return esc_fn, render_fn


def test_render_role_chips_differing_roles_produce_labeled_per_board_chips(_role_chip_js):
    """Multi-board differing roles: each role chip is labeled with its board_id."""
    esc_fn, render_fn = _role_chip_js
    seats = [
        {"board_id": "fullplatts", "role": "member"},
        {"board_id": "pursers", "role": "reviewer"},
    ]
    html = _run_js_with_seats(esc_fn, render_fn, seats)

    # Two separate role-chip spans
    assert html.count('class="role-chip"') == 2
    # Each chip has a board label and role
    assert '<span class="chip-board">fullplatts</span>' in html
    assert '<span class="chip-role">member</span>' in html
    assert '<span class="chip-board">pursers</span>' in html
    assert '<span class="chip-role">reviewer</span>' in html
    # Does NOT collapse to (all boards)
    assert "(all boards)" not in html


def test_render_role_chips_identical_roles_collapse_to_single_chip(_role_chip_js):
    """Identical roles on every board collapse to one '(all boards)' chip."""
    esc_fn, render_fn = _role_chip_js
    seats = [
        {"board_id": "board-a", "role": "worker"},
        {"board_id": "board-b", "role": "worker"},
    ]
    html = _run_js_with_seats(esc_fn, render_fn, seats)

    # Single role-chip span
    assert html.count('class="role-chip"') == 1
    assert "(all boards)" in html
    assert "worker" in html
    # Board names are in the title attribute
    assert "board-a" in html
    assert "board-b" in html


def test_render_role_chips_hostile_board_names_are_escaped(_role_chip_js):
    """Hostile board names are HTML-escaped before interpolation."""
    esc_fn, render_fn = _role_chip_js
    seats = [
        {"board_id": "<script>alert('xss')</script>", "role": "member"},
        {"board_id": 'evil" onclick="alert(1)', "role": "reviewer"},
    ]
    html = _run_js_with_seats(esc_fn, render_fn, seats)

    # No raw script tag
    assert "<script>" not in html
    assert "</script>" not in html
    # Escaped versions present
    assert "&lt;script&gt;" in html
    assert "&lt;/script&gt;" in html
    # Quotes escaped
    assert "&quot;" in html
    # No raw onclick
    assert 'onclick="alert(1)"' not in html
    # Two chips (roles differ: member vs reviewer)
    assert html.count('class="role-chip"') == 2


def test_render_role_chips_empty_seats_uses_fallback(_role_chip_js):
    """Empty or null seats return the fallback string."""
    esc_fn, render_fn = _role_chip_js
    assert _run_js_with_seats(esc_fn, render_fn, []) == "worker"
    assert _run_js_with_seats(esc_fn, render_fn, [], "reviewer") == "reviewer"


def test_render_role_chips_missing_fields_are_skipped(_role_chip_js):
    """Seats without role or board_id are skipped."""
    esc_fn, render_fn = _role_chip_js
    seats = [
        {"board_id": "board-a", "role": None},       # skipped
        {"board_id": "board-b", "role": "member"},     # kept
        {"board_id": None, "role": "reviewer"},        # skipped
    ]
    html = _run_js_with_seats(esc_fn, render_fn, seats)
    assert "board-b" in html
    assert "member" in html
    assert "board-a" not in html


def test_live_agent_card_uses_render_role_chips():
    """The liveAgentCard JS function calls renderRoleChips for the agent-role span."""
    # Verify the integration point: liveAgentCard references renderRoleChips
    assert "renderRoleChips(a.seats" in dashboard.HTML


def test_role_chip_css_is_present():
    """The .role-chip CSS rules are in the HTML stylesheet."""
    assert ".role-chip{" in dashboard.HTML
    assert ".chip-board{" in dashboard.HTML
    assert ".chip-role{" in dashboard.HTML


def test_dispatch_fetcher_projects_policy_gaps_offers_and_timeline() -> None:
    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_state_get(self, **_kwargs: object) -> dict:
            return registry(
                {
                    "Pursers": {
                        "board_id": "pursers",
                        "status": "active",
                        "work_dir": "/repo",
                    }
                }
            )

        async def board_status(self) -> dict:
            return {
                "latest_seq": 8,
                "claim_ttl_s": 900,
                "dispatch_policy": {
                    "offer_ttl_s": 120,
                    "second_opinion": True,
                    "fallback_broadcast": False,
                },
                "unassignable_tickets": [
                    {"ticket_id": "TK-hard", "reason": "no_eligible_worker"}
                ],
                "unclaimed_tickets": [
                    {
                        "ticket_id": "TK-starved",
                        "reason": "unclaimed_broadcast",
                        "unclaimed_for_s": 700,
                        "threshold_s": 600,
                    }
                ],
                "agents": [
                    {
                        "agent_name": "worker-low",
                        "capabilities": {
                            "tier_max": 1,
                            "skills": [],
                            "can_work": True,
                            "can_review": False,
                        },
                        "current_offer": {
                            "ticket_id": "TK-offer",
                            "expires_at": "2030-01-01T00:02:00Z",
                        },
                    }
                ],
            }

        async def ticket_list(self, **kwargs: object) -> dict:
            assert kwargs == {"include_closed": False, "limit": 500}
            return {
                "tickets": [
                    {
                        "ticket_id": "TK-hard",
                        "title": "Hard Python work",
                        "tier": 3,
                        "skills_required": ["python"],
                        "dispatch_state": {"state": "unassignable", "kind": "work"},
                    }
                ]
            }

        async def board_dispatch_events(self, **kwargs: object) -> dict:
            assert kwargs == {"limit": 25}
            return {
                "events": [
                    {
                        "seq": 8,
                        "kind": "ticket_offered",
                        "ticket_id": "TK-offer",
                        "occurred_at": "2030-01-01T00:00:00Z",
                    }
                ]
            }

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="dashboard-seat",
        stale_seconds=300,
        cache_seconds=5,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())

    result = asyncio.run(fetcher.fetch_dispatch("pursers"))

    assert result["dispatch_policy"]["offer_ttl_s"] == 120
    assert result["claim_ttl_s"] == 900
    assert result["offers"][0]["agent_name"] == "worker-low"
    assert result["unassignable_tickets"][0]["missing"] == [
        "tier_max>=3",
        "skill:python",
    ]
    assert result["unclaimed_tickets"][0]["ticket_id"] == "TK-starved"
    assert result["timeline"][0]["kind"] == "ticket_offered"


def test_dispatch_timeline_reads_cross_seat_central_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text('{"keys": []}', encoding="utf-8")
    for key, value in {
        "CENTRAL_AUTH_MODE": "jwt",
        "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
        "CENTRAL_JWKS_PATH": str(jwks_path),
        "CENTRAL_ADMISSION": "invite",
        "STORE_BACKEND": "sqlite",
    }.items():
        monkeypatch.setenv(key, value)
    mcp, service = central.build_server("localhost", 8765, tmp_path / "central")
    review_scopes = frozenset({"board:read", "board:write", "board:review"})
    work_scopes = frozenset({"board:read", "board:write"})
    admin = central.Principal("PR-admin", "admin", review_scopes)
    dashboard_principal = central.Principal(
        "PR-dashboard", "dashboard", work_scopes
    )
    worker_a = central.Principal("PR-worker-a", "worker-a", work_scopes)
    worker_b = central.Principal("PR-worker-b", "worker-b", work_scopes)
    reviewer = central.Principal("PR-reviewer", "reviewer", review_scopes)
    state: dict[str, object] = {"principal": admin, "now": 1000.0}
    monkeypatch.setattr(central, "current_principal", lambda: state["principal"])
    monkeypatch.setattr(central.time, "time", lambda: state["now"])

    async def call(name: str, principal: central.Principal, **arguments: object) -> dict:
        state["principal"] = principal
        result = await mcp.call_tool(name, {"board_id": "pursers", **arguments})
        return result.structured_content

    async def add_seat(
        principal: central.Principal,
        name: str,
        capabilities: dict[str, object],
        *,
        role: str = "member",
    ) -> str:
        await call(
            "board_member_add",
            admin,
            agent_name="admin-agent",
            principal_id=principal.principal_id,
            role=role,
        )
        joined = await call(
            "board_join", principal, agent_name=name,
            role="reviewer" if role == "reviewer" else "worker",
            capabilities=capabilities,
        )
        return joined["agent_id"]

    async def scenario() -> None:
        await call(
            "board_join",
            admin,
            agent_name="admin-agent",
            capabilities={"can_work": False, "can_review": False},
        )
        await add_seat(
            dashboard_principal,
            "dashboard-seat",
            {"can_work": False, "can_review": False},
        )
        worker_a_id = await add_seat(
            worker_a, "worker-a", {"tier_max": 2, "can_work": True}
        )
        worker_b_id = await add_seat(
            worker_b, "worker-b", {"tier_max": 2, "can_work": True}
        )
        reviewer_id = await add_seat(
            reviewer,
            "reviewer",
            {"tier_max": 2, "can_work": False, "can_review": True},
            role="reviewer",
        )
        service.register_listener("pursers", worker_a_id)
        service.register_listener("pursers", reviewer_id)
        await call(
            "board_dispatch_policy_set",
            admin,
            agent_name="admin-agent",
            offer_ttl_s=1,
        )
        created = await call(
            "ticket_create",
            admin,
            agent_name="admin-agent",
            title="expiry and revoke",
            description="exercise dashboard dispatch history",
            target_url="pursers/tools/fleet-dashboard",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[worker_a_id],
        )
        await call(
            "ticket_update",
            admin,
            agent_name="admin-agent",
            ticket_id=created["ticket"]["ticket_id"],
            exclude_agents=[worker_a_id],
        )
        state["now"] = 1002.0
        await call("board_reap", admin)

        state["now"] = 2000.0
        review_target = await call(
            "ticket_create",
            admin,
            agent_name="admin-agent",
            title="review offer",
            description="exercise dashboard review history",
            target_url="pursers/tools/fleet-dashboard",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[worker_a_id],
        )
        state["now"] = 2000.5
        review_ticket_id = review_target["ticket"]["ticket_id"]
        await call(
            "ticket_claim",
            worker_a,
            agent_name="worker-a",
            ticket_id=review_ticket_id,
        )
        await call(
            "ticket_submit",
            worker_a,
            agent_name="worker-a",
            ticket_id=review_ticket_id,
            summary="ready",
        )

        class Client:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def board_status(self) -> dict:
                return await call("board_status", dashboard_principal)

            async def ticket_list(self, **arguments: object) -> dict:
                return await call("ticket_list", dashboard_principal, **arguments)

            async def board_dispatch_events(self, **arguments: object) -> dict:
                return await call(
                    "board_dispatch_events", dashboard_principal, **arguments
                )

        config = dashboard.Config(
            url="http://127.0.0.1:8766/mcp",
            token="token",
            home_board="pursers",
            agent_name="dashboard-seat",
            stale_seconds=300,
            cache_seconds=5,
        )
        fetcher = dashboard.FleetFetcher(
            config, client_factory=lambda *_args, **_kwargs: Client()
        )

        async def boards() -> list[tuple[str, str]]:
            return [("pursers", "pursers")]

        fetcher._boards = boards
        result = await fetcher.fetch_dispatch("pursers")
        kinds = {event["kind"] for event in result["timeline"]}
        assert {
            "ticket_offered",
            "review_offered",
            "offer_expired",
            "offer_revoked",
        } <= kinds
        assert worker_a_id != worker_b_id

    asyncio.run(scenario())


def test_dispatch_policy_save_validates_and_forwards_exact_contract() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_state_get(self, **_kwargs: object) -> dict:
            return registry(
                {
                    "Pursers": {
                        "board_id": "pursers",
                        "status": "active",
                        "work_dir": "/repo",
                    }
                }
            )

        async def board_dispatch_policy_set(self, **kwargs: object) -> dict:
            calls.append(("dispatch", dict(kwargs)))
            return {"dispatch_policy": dict(kwargs)}

        async def board_claim_ttl_set(self, claim_ttl_s: int) -> dict:
            calls.append(("claim_ttl", claim_ttl_s))
            return {"claim_ttl_s": claim_ttl_s, "previous_claim_ttl_s": 900}

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="dashboard-seat",
        stale_seconds=300,
        cache_seconds=5,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())
    policy = {
        "claim_ttl_s": 300,
        "offer_ttl_s": 60,
        "broadcast_reoffer_s": 600,
        "second_opinion": False,
        "fallback_broadcast": True,
    }

    result = asyncio.run(fetcher.save_dispatch("pursers", policy))

    assert result["dispatch_policy"] == {
        "offer_ttl_s": 60,
        "broadcast_reoffer_s": 600,
        "second_opinion": False,
        "fallback_broadcast": True,
    }
    assert result["claim_ttl_s"] == 300
    assert calls == [
        (
            "dispatch",
            {
                "offer_ttl_s": 60,
                "broadcast_reoffer_s": 600,
                "second_opinion": False,
                "fallback_broadcast": True,
            },
        ),
        ("claim_ttl", 300),
    ]
    with pytest.raises(ValueError, match="dispatch policy fields"):
        asyncio.run(fetcher.save_dispatch("pursers", {**policy, "extra": True}))


def test_capability_and_dispatch_ui_contract_is_present() -> None:
    for name in ("tier_max", "skills", "can_review", "can_work"):
        assert f'name="{name}"' in dashboard.HTML
    assert "Suggest skills from connectors" in dashboard.HTML
    assert "Dispatch by board" in dashboard.HTML
    assert "/api/config/suggestions" in dashboard.HTML
    assert "/api/dispatch" in dashboard.HTML
    assert "Dispatch unavailable:" in dashboard.HTML
    assert "Current offer" in dashboard.HTML
    assert 'name="claim_ttl_s"' in dashboard.HTML
    assert 'name="broadcast_reoffer_s"' in dashboard.HTML
    assert "Needs attention · unclaimed broadcasts" in dashboard.HTML
    assert "Lease lapsed ${t.abandoned_count} times" in dashboard.HTML
    assert "Runtime consumption requires Dispatch Part 2" in dashboard.HTML
    assert ">coordinator</option>" in dashboard.HTML
    assert "review.checked=role==='reviewer'" in dashboard.HTML
    assert "work.checked=role==='worker'" in dashboard.HTML
    assert "Retired / stale" in dashboard.HTML
    assert "data-retire-agent" in dashboard.HTML
    assert "data-retire-inert" in dashboard.HTML
    assert "/api/agents/retire" in dashboard.HTML
    assert "stale_after_days" in dashboard.HTML


def test_lifecycle_http_endpoints_use_same_origin_json_guard() -> None:
    calls: list[tuple[str, str, str | None]] = []

    class Cache:
        def resolve_central(self, value: str | None) -> str:
            return value or "default"

        def retire_agent(self, board_id: str, agent_id: str) -> dict:
            calls.append(("agent", board_id, agent_id))
            return {"ok": True}

        def retire_inert(self, board_id: str) -> dict:
            calls.append(("inert", board_id, None))
            return {"ok": True}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=SimpleNamespace())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path, payload in (
            (
                "/api/agents/retire",
                {"board_id": "pursers", "agent_id": "AI-target"},
            ),
            ("/api/agents/retire-inert", {"board_id": "pursers"}),
        ):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Origin": base},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                assert json.load(response)["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert calls == [
        ("agent", "pursers", "AI-target"),
        ("inert", "pursers", None),
    ]


def test_dispatch_http_endpoints_use_same_origin_json_guard() -> None:
    calls: list[tuple[str, object]] = []

    class Cache:
        def resolve_central(self, value: str | None) -> str:
            return value or "default"

        def get_dispatch(self, board_id: str) -> dict:
            calls.append(("get", board_id))
            return {"board_id": board_id, "dispatch_policy": {"offer_ttl_s": 120}}

        def save_dispatch(self, board_id: str, value: object) -> dict:
            calls.append((board_id, value))
            return {"board_id": board_id, "dispatch_policy": value}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=SimpleNamespace())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    policy = {
        "offer_ttl_s": 60,
        "second_opinion": True,
        "fallback_broadcast": False,
    }
    try:
        with urllib.request.urlopen(base + "/api/dispatch?board_id=pursers") as response:
            assert json.load(response)["board_id"] == "pursers"
        request = urllib.request.Request(
            base + "/api/dispatch",
            data=json.dumps({"board_id": "pursers", "policy": policy}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["dispatch_policy"] == policy
        blocked = urllib.request.Request(
            base + "/api/dispatch",
            data=json.dumps({"board_id": "pursers", "policy": policy}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://bad.test"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(blocked)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert calls == [("get", "pursers"), ("pursers", policy)]


def test_origin_verification_uses_exact_remote_branch_and_commit(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    commit = "a" * 40

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "check": False,
        }
        return SimpleNamespace(
            returncode=0,
            stdout=f"{commit}\trefs/heads/codex/TK-exact\n",
        )

    result = dashboard.verify_ticket_commit_on_origin(
        {
            "ticket_id": "TK-exact",
            "status": "submitted",
            "notes": f"branch_and_commit: codex/TK-exact @ {commit}",
        },
        tmp_path,
        runner=runner,
    )

    assert result == {
        "status": "verified",
        "branch": "codex/TK-exact",
        "commit": commit,
    }
    assert calls[0][-1] == "refs/heads/codex/TK-exact"


def test_origin_verification_handles_mismatch_invalid_dir_and_old_closed(
    tmp_path: Path,
) -> None:
    commit = "b" * 40
    ticket = {
        "status": "submitted",
        "notes": f"branch_and_commit: codex/TK-one @ {commit}",
    }
    mismatch = dashboard.verify_ticket_commit_on_origin(
        ticket,
        tmp_path,
        runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="c" * 40 + "\trefs/heads/codex/TK-one\n"),
    )
    invalid = dashboard.verify_ticket_commit_on_origin(ticket, tmp_path / "missing")
    old = dashboard.verify_ticket_commit_on_origin(
        {
            **ticket,
            "status": "closed",
            "closed_at": "2030-01-01T00:00:00+00:00",
        },
        tmp_path / "missing",
        now=datetime(2030, 1, 10, tzinfo=timezone.utc),
    )

    assert mismatch["status"] == "mismatch"
    assert invalid == {
        "status": "cannot_verify",
        "reason": "registry work_dir does not exist",
    }
    assert old == {"status": "stale_closed"}


def test_findings_make_board_large_informational_and_dedupe_origin_failure() -> None:
    findings = [
        {"kind": "board-large", "level": "warn", "message": "large"},
        {
            "kind": "unverifiable-commit",
            "level": "warn",
            "message": "one",
            "ticket_id": "TK-one",
        },
        {
            "kind": "unverifiable-commit",
            "level": "warn",
            "message": "two",
            "ticket_id": "TK-two",
        },
    ]
    snapshot = {
        "state": {"coordinator_findings": {"value": json.dumps({"findings": findings})}},
        "_snapshot_truncation": {"hidden_active": 0},
        "_commit_verification": {
            "TK-one": {"status": "cannot_verify", "reason": "bad registry path"},
            "TK-two": {"status": "cannot_verify", "reason": "bad registry path"},
        },
    }

    result = dashboard.project_coordinator_findings(snapshot)

    assert result is not None
    assert [item["kind"] for item in result["items"]] == ["cannot-verify-origin"]
    snapshot["_snapshot_truncation"]["hidden_active"] = 2
    result = dashboard.project_coordinator_findings(snapshot)
    assert result is not None
    assert [item["kind"] for item in result["items"]] == [
        "board-active-truncated",
        "cannot-verify-origin",
    ]


def test_findings_clear_when_the_coordinator_source_is_stale() -> None:
    result = dashboard.project_coordinator_findings(
        {
            "state": {
                "coordinator_findings": {
                    "updated_at": "2030-01-01T00:00:00+00:00",
                    "value": json.dumps(
                        {
                            "findings": [
                                {
                                    "kind": "stale-source",
                                    "level": "warn",
                                    "message": "old condition",
                                }
                            ]
                        }
                    ),
                }
            }
        },
        now=datetime(2030, 1, 3, tzinfo=timezone.utc),
    )

    assert result is None


def test_coordinator_findings_stale_after_fifteen_minutes() -> None:
    snapshot = {
        "state": {
            "coordinator_findings": {
                "updated_at": "2030-01-01T00:00:00+00:00",
                "value": json.dumps({"findings": []}),
            }
        }
    }

    assert dashboard.coordinator_findings_stale(
        snapshot, now=datetime(2030, 1, 1, 0, 14, tzinfo=timezone.utc)
    ) is False
    assert dashboard.coordinator_findings_stale(
        snapshot, now=datetime(2030, 1, 1, 0, 16, tzinfo=timezone.utc)
    ) is True
    assert "coordinator findings stale > 15 min" in dashboard.HTML


def test_board_unreachable_finding_shows_scrubbed_reason() -> None:
    result = dashboard.project_coordinator_findings(
        {
            "state": {
                "coordinator_findings": {
                    "value": json.dumps(
                        {
                            "findings": [
                                {
                                    "kind": "board_unreachable",
                                    "level": "critical",
                                    "message": "generic message",
                                    "reason": "PermissionError: lacks board:coordinate authorization",
                                }
                            ]
                        }
                    )
                }
            }
        }
    )

    assert result is not None
    assert result["items"] == [
        {
            "kind": "board_unreachable",
            "level": "critical",
            "text": "PermissionError: lacks board:coordinate authorization",
            "ticket_id": None,
            "ask_id": None,
            "draft": None,
        }
    ]


def test_truncated_snapshot_splices_active_ticket_list() -> None:
    calls: list[tuple[str, dict]] = []

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_snapshot(self, **kwargs: object) -> dict:
            calls.append(("snapshot", dict(kwargs)))
            return {
                "latest_seq": 4,
                "truncated": True,
                "total_counts": {"tickets": 3},
                "omitted_counts": {"tickets": 2},
                "agents": [],
                "tickets": [{"ticket_id": "TK-closed", "status": "closed"}],
            }

        async def ticket_list(self, **kwargs: object) -> dict:
            calls.append(("ticket_list", dict(kwargs)))
            if kwargs.get("status") == "open":
                return {"tickets": [{"ticket_id": "TK-open", "title": "Open", "status": "open"}], "total_matching": 1}
            if kwargs.get("status") == "submitted":
                return {"tickets": [{"ticket_id": "TK-submitted", "title": "Submitted", "status": "submitted"}], "total_matching": 1}
            if kwargs.get("status") == "claimed":
                return {"tickets": [], "total_matching": 0}
            return {
                "tickets": [{"ticket_id": "TK-open", "title": "Open", "status": "open"}],
                "total_matching": 2,
            }

        async def board_catchup(self, **kwargs: object) -> dict:
            return {"events": []}

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())
    raw = asyncio.run(fetcher._read_board("Board", "board"))
    board = dashboard.aggregate_fleet([raw], stale_seconds=300)["boards"][0]

    assert ("ticket_list", {"include_closed": False, "limit": 500}) in calls
    assert all(call[1]["limit"] <= 500 for call in calls if call[0] == "ticket_list")
    assert raw["snapshot"]["_snapshot_truncation"] == {
        "returned": 1,
        "total": 3,
        "omitted": 2,
        "hidden_active": 0,
    }
    assert {ticket["id"] for ticket in board["tickets"]} == {
        "TK-open",
        "TK-submitted",
    }


def test_push_wait_pressure_supersedes_poll_and_exposes_return_rate(
    tmp_path: Path,
) -> None:
    document = {
        "schema_version": 3,
        "days": {},
        "model_wait": {
            "push": {
                "board_id": "board",
                "agent_name": "worker",
                "hours": {
                    "2030-01-10T12:00:00Z": {
                        "returns": 2,
                        "response_bytes": 400_000,
                        "outcomes": {"cue": 2},
                    }
                },
                "returns": [
                    {"at": "2030-01-10T12:00:00Z", "response_bytes": 200_000, "outcome": "cue", "mode": "push", "reason": "offer"},
                    {"at": "2030-01-10T12:05:00Z", "response_bytes": 200_000, "outcome": "cue", "mode": "push", "reason": "cue"},
                ],
            }
        },
        "poll_cycles": {
            "same": {
                "board_id": "board",
                "agent_name": "worker",
                "latest_at": "2030-01-10T12:10:00Z",
                "latest_response_bytes": 900_000,
                "mode": "poll",
                "samples": [],
            },
            "old": {
                "board_id": "old",
                "agent_name": "old-worker",
                "latest_at": "2030-01-08T12:10:00Z",
                "latest_response_bytes": 900_000,
                "samples": [],
            },
        },
    }
    path = tmp_path / "stats.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = dashboard.read_overhead_stats(
        path, now=datetime(2030, 1, 10, 12, 30, tzinfo=timezone.utc)
    )

    assert len(result["sessions"]) == 1
    session = result["sessions"][0]
    assert session["mode"] == "push"
    assert session["reason"] in {"offer", "cue"}
    assert session["returns_per_hour"] == 2
    assert session["estimated_tokens_per_return"] == 50_000
    assert session["estimated_tokens_per_hour"] == 100_000


def test_non_push_wait_return_does_not_suppress_genuine_poll_history(
    tmp_path: Path,
) -> None:
    document = {
        "schema_version": 3,
        "days": {},
        "model_wait": {
            "fallback": {
                "board_id": "board",
                "agent_name": "worker",
                "hours": {},
                "returns": [
                    {
                        "at": "2030-01-10T12:00:00Z",
                        "response_bytes": 200_000,
                        "outcome": "timeout",
                        "mode": "poll",
                        "reason": "timeout",
                    }
                ],
            }
        },
        "poll_cycles": {
            "same": {
                "board_id": "board",
                "agent_name": "worker",
                "latest_at": "2030-01-10T12:10:00Z",
                "latest_response_bytes": 900_000,
                "mode": "poll",
                "samples": [],
            }
        },
    }
    path = tmp_path / "stats.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = dashboard.read_overhead_stats(
        path, now=datetime(2030, 1, 10, 12, 30, tzinfo=timezone.utc)
    )

    assert len(result["sessions"]) == 2
    assert {session["reason"] for session in result["sessions"]} == {
        "timeout",
        "legacy",
    }
    assert {session["mode"] for session in result["sessions"]} == {"poll"}


def test_single_return_over_one_million_tokens_is_stats_anomaly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "days": {},
                "poll_cycles": {},
                "model_wait": {
                    "seat": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "hours": {
                            "2030-01-10T12:00:00Z": {
                                "returns": 2,
                                "response_bytes": 4_100_004,
                                "outcomes": {"timeout": 2},
                            }
                        },
                        "returns": [
                            {"at": "2030-01-10T12:00:00Z", "response_bytes": 100_000, "outcome": "timeout", "mode": "push", "reason": "timeout"},
                            {"at": "2030-01-10T12:05:00Z", "response_bytes": 4_000_004, "outcome": "timeout", "mode": "push", "reason": "timeout"},
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = dashboard.read_overhead_stats(
        path, now=datetime(2030, 1, 10, 12, 30, tzinfo=timezone.utc)
    )

    session = result["sessions"][0]
    assert session["pressure"] == "anomaly"
    assert session["raw_record"]["response_bytes"] == 4_000_004
    assert "do not compact" in session["next_action"]


def test_attention_state_dedupes_persists_and_auto_clears() -> None:
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    candidate = {"key": "finding|one", "fingerprint": "warn|one"}
    state, visible = dashboard.reconcile_attention_state({}, [candidate, candidate], now=now)
    assert visible == [candidate]
    first_seen = state["finding|one"]["first_seen"]

    state["finding|one"]["acknowledged"] = True
    persisted, visible = dashboard.reconcile_attention_state(
        state, [candidate], now=now + timedelta(minutes=5)
    )
    assert visible == []
    assert persisted["finding|one"]["first_seen"] == first_seen

    changed = {**candidate, "fingerprint": "critical|one"}
    changed_state, visible = dashboard.reconcile_attention_state(
        persisted, [changed], now=now + timedelta(minutes=10)
    )
    assert visible == [changed]
    changed_state["finding|one"]["snooze_until"] = (
        now + timedelta(hours=24)
    ).isoformat()
    _, visible = dashboard.reconcile_attention_state(
        changed_state, [changed], now=now + timedelta(hours=1)
    )
    assert visible == []
    cleared, visible = dashboard.reconcile_attention_state(
        changed_state, [], now=now + timedelta(hours=2)
    )
    assert cleared == {}
    assert visible == []


def test_attention_and_truncation_controls_are_rendered() -> None:
    assert "sessionStorage" not in dashboard.HTML
    assert "/api/attention" in dashboard.HTML
    assert "data-attention-action=\"ack\"" in dashboard.HTML
    assert "push unavailable: ${p.reason}" in dashboard.HTML
    assert "Snooze 24h" in dashboard.HTML
    assert "snapshot truncated to ${esc(tr.returned)} of ${esc(tr.total)} tickets" in dashboard.HTML
    assert "tokens / return" in dashboard.HTML


def test_attention_state_persists_across_manager_instances(tmp_path: Path) -> None:
    state_dir = tmp_path / "dashboard-state"
    first = dashboard.SeatConfigManager(state_dir=state_dir)
    value = {"finding|one": {"fingerprint": "warn|one", "acknowledged": True}}

    assert first.save_attention_state(value) == {"items": value}
    second = dashboard.SeatConfigManager(state_dir=state_dir)

    assert second.attention_state() == {"items": value}
    assert (state_dir / "attention-state.json").stat().st_mode & 0o777 == 0o600


def test_config_ops_endpoint_guards_and_execution() -> None:
    calls: list[tuple[str, dict]] = []

    class Cache:
        def resolve_central(self, value: str | None) -> str:
            return value or "default"

    class Seats:
        def release_status(self) -> dict:
            calls.append(("release_status", {}))
            return {"schema_version": 1, "latest_tag": "v5.0.0a20"}

        def prepare_ops_action(self, action: str, **kwargs: Any) -> dict:
            calls.append(("prepare_ops_action", {"action": action, **kwargs}))
            return {
                "plan_id": "a" * 32,
                "digest": "b" * 64,
                "action": action,
                "command": f"cmd {action}",
                "expires_in_s": 120,
            }

        def ops_action(self, plan_id: str, digest: str) -> dict:
            calls.append(("ops_action", {"plan_id": plan_id, "digest": digest}))
            return {"ok": True, "action": "planned", "command": "cmd", "output": "ok"}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=Seats())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: object, headers: dict | None = None) -> tuple[int, dict]:
        req_headers = {"Content-Type": "application/json", "Origin": base}
        if headers:
            req_headers.update(headers)
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    try:
        # Cross origin rejected
        status, _ = post("/api/config/ops/plan", {"action": "stage_central"}, {"Origin": "https://attacker.invalid"})
        assert status == 403

        # text/plain rejected
        status, _ = post("/api/config/ops/plan", {"action": "stage_central"}, {"Content-Type": "text/plain"})
        assert status == 415

        # Invalid host rejected
        status, _ = post("/api/config/ops/plan", {"action": "stage_central"}, {"Host": "attacker.invalid"})
        assert status == 403

        # Missing action rejected at planning; unbound execution is also rejected.
        status, _ = post("/api/config/ops/plan", {})
        assert status == 503 or status == 400
        status, _ = post("/api/config/ops", {"action": "stage_central"})
        assert status == 400

        # Every successful execution is bound to the returned immutable plan.
        for payload in (
            {"action": "publish_from_tag", "tag": "v5.0.0a20"},
            {"action": "stage_central"},
            {"action": "kickstart_central"},
            {"action": "restart_dashboard"},
        ):
            status, plan = post("/api/config/ops/plan", payload)
            assert status == 200
            status, res = post(
                "/api/config/ops",
                {"plan_id": plan["plan_id"], "digest": plan["digest"]},
            )
            assert status == 200
            assert res["ok"] is True

        # GET /api/config/release
        with urllib.request.urlopen(base + "/api/config/release") as response:
            assert response.status == 200
            assert json.load(response)["latest_tag"] == "v5.0.0a20"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    actions = [c[1].get("action") for c in calls if c[0] == "prepare_ops_action"]
    assert actions == ["publish_from_tag", "stage_central", "kickstart_central", "restart_dashboard"]


def test_seat_config_manager_ops_action_parameter_rejections_and_jobs(tmp_path: Path) -> None:
    release_ops = MagicMock()
    release_ops.resolve_action_plan.side_effect = lambda action, tag=None: {
        "action": action,
        "command": f"command {action}",
        "tag": tag,
    }
    release_ops.plan_digest.return_value = "d" * 64
    release_ops.execute_action_plan.return_value = {"ok": True, "output": "done"}

    manager = dashboard.SeatConfigManager(state_dir=tmp_path, release_ops_manager=release_ops)

    # Unknown parameter rejections
    with pytest.raises(ValueError, match="unknown parameters for stage_central"):
        manager.prepare_ops_action("stage_central", profile_path="/etc/passwd")

    with pytest.raises(ValueError, match="unknown parameters for stage_central"):
        manager.prepare_ops_action("stage_central", venv_python="/bin/sh")

    with pytest.raises(ValueError, match="unknown parameters for stage_central"):
        manager.prepare_ops_action(
            "stage_central",
            wheel_path="/tmp/pursers_central-0.1.0a24-py3-none-any.whl",
        )

    with pytest.raises(ValueError, match="unknown parameters for kickstart_central"):
        manager.prepare_ops_action("kickstart_central", job_label="com.evil.service")

    with pytest.raises(ValueError, match="unknown parameters for restart_dashboard"):
        manager.prepare_ops_action("restart_dashboard", job_label="com.evil.service")

    with pytest.raises(ValueError, match="unknown parameters for publish_from_tag"):
        manager.prepare_ops_action("publish_from_tag", bad_key="val")

    # Valid job dispatch
    plan = manager.prepare_ops_action("kickstart_central")
    assert plan["command"] == "command kickstart_central"
    assert plan["digest"] == "d" * 64
    job = manager.ops_action(plan["plan_id"], plan["digest"])
    assert job["action"] == "kickstart_central"
    assert job["status"] == "queued"
    assert job["command"] == "command kickstart_central"

    # Poll job until succeeded
    for _ in range(50):
        state = manager.job(job["job_id"])
        if state["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)

    assert state["status"] == "succeeded"
    assert any("Queued kickstart_central" in line for line in state["logs"])
    assert "SUCCEEDED" in state["logs"]
    assert "/api/config/ops/plan" in dashboard.HTML
    assert "Plan digest:" in dashboard.HTML


@pytest.mark.parametrize("action", ["stage_central", "publish_from_tag"])
def test_ops_jobs_reject_concurrent_duplicate_actions(
    tmp_path: Path, action: str
) -> None:
    release_ops = MagicMock()
    release_ops.resolve_action_plan.side_effect = lambda name, tag=None: {
        "action": name,
        "command": f"command {name}",
        "tag": tag,
    }
    release_ops.plan_digest.return_value = "c" * 64
    started = threading.Event()
    finish = threading.Event()

    def execute(plan: dict, *, log_callback: Any = None) -> dict:
        started.set()
        assert finish.wait(2)
        return {"ok": True}

    release_ops.execute_action_plan.side_effect = execute
    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path, release_ops_manager=release_ops
    )
    first = manager.prepare_ops_action(action)
    first_job = manager.ops_action(first["plan_id"], first["digest"])
    assert started.wait(1)

    second = manager.prepare_ops_action(action)
    with pytest.raises(RuntimeError, match="already queued or running"):
        manager.ops_action(second["plan_id"], second["digest"])

    release_ops.record_attempt.assert_called_with(
        action,
        phase="queue",
        ok=False,
        error="duplicate or concurrent job refused",
    )
    finish.set()
    for _ in range(50):
        if manager.job(first_job["job_id"])["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert manager.job(first_job["job_id"])["status"] == "succeeded"


def test_ops_confirmation_plan_is_one_time_and_digest_bound(tmp_path: Path) -> None:
    release_ops = MagicMock()
    release_ops.resolve_action_plan.return_value = {
        "action": "kickstart_central",
        "command": "launchctl kickstart -k gui/501/com.onboard.central",
    }
    release_ops.plan_digest.return_value = "a" * 64
    manager = dashboard.SeatConfigManager(
        state_dir=tmp_path, release_ops_manager=release_ops
    )
    plan = manager.prepare_ops_action("kickstart_central")

    with pytest.raises(ValueError, match="digest mismatch"):
        manager.ops_action(plan["plan_id"], "b" * 64)
    with pytest.raises(KeyError):
        manager.ops_action(plan["plan_id"], plan["digest"])


# --- DOORS & ADD PROJECT TESTS (TK-764a0f67271c) ---

JWT_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
DOOR_SHAPE = re.compile(r"prs1\.[A-Za-z0-9_-]+")


class FakeDoorBoardClient:
    def __init__(self, board_id: str, *, central: Any = None, is_admin: bool = True) -> None:
        self.board_id = board_id
        self.central = central
        self.is_admin = is_admin
        self.memberships: dict[str, str] = {}
        self.onboarded = False
        self.dispatch_policy = {
            "offer_ttl_s": 120,
            "broadcast_reoffer_s": 600,
            "second_opinion": False,
            "fallback_broadcast": False,
        }
        self.review_policy = "workflow"

    async def __aenter__(self) -> Self:
        # Faithful to BoardClient entry behavior: BoardClient.__aenter__ joins the board.
        # Joining a fresh board on Central admits the creator as admin and creates the board.
        if self.central is not None and self.is_admin:
            self.central.created_boards.add(self.board_id)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass

    async def board_list(self) -> dict:
        boards = (
            sorted(self.central.created_boards)
            if self.central is not None
            else ["pursers", self.board_id]
        )
        return {
            "ok": True,
            "boards": [
                {
                    "board_id": board,
                    "membership_role": "admin" if self.is_admin else "member",
                }
                for board in boards
            ],
        }

    async def board_onboard(self, **kwargs: object) -> dict:
        if not self.is_admin:
            raise PermissionError("board access denied: require admin")
        self.onboarded = True
        if self.central is not None:
            self.central.created_boards.add(self.board_id)
        return {"ok": True, "board_id": self.board_id}

    async def board_members(self) -> dict:
        members = [
            {"principal_id": pid, "role": role, "agent_names": ["seat-1"]}
            for pid, role in self.memberships.items()
        ]
        return {"ok": True, "members": members, "principal_member_count": len(members)}

    async def board_member_add(self, principal_id: str, role: str = "member", **kwargs: object) -> dict:
        if not self.is_admin:
            raise PermissionError("board access denied: require admin")
        self.memberships[principal_id] = role
        return {"ok": True}

    async def board_status(self) -> dict:
        return {
            "ok": True,
            "board_id": self.board_id,
            "dispatch_policy": self.dispatch_policy,
            "review_policy": self.review_policy,
        }

    async def board_dispatch_policy_set(self, **kwargs: object) -> dict:
        if not self.is_admin:
            raise PermissionError("board access denied: require admin")
        self.dispatch_policy.update(kwargs)
        return {"ok": True}

    async def board_review_policy_set(self, review_policy: str, **kwargs: object) -> dict:
        if not self.is_admin:
            raise PermissionError("board access denied: require admin")
        self.review_policy = review_policy
        return {"ok": True}

    async def board_state_get(self, key: str | None = None) -> dict:
        return {"state": {"value": json.dumps({"schema_version": 1, "projects": {}})}}

    async def _call(self, name: str, arguments: dict) -> dict:
        if name == "board_members":
            return await self.board_members()
        if name == "board_member_add":
            return await self.board_member_add(arguments["principal_id"], arguments.get("role", "member"))
        raise NotImplementedError(name)


class FakeDoorCentral:
    def __init__(self, *, is_admin: bool = True) -> None:
        self.is_admin = is_admin
        self.registry_data = {
            "schema_version": 1,
            "projects": {
                "existing-proj": {
                    "board_id": "existing-board",
                    "work_dir": "/PATH/TO/EXISTING",
                    "status": "active",
                }
            },
        }
        self.created_boards: set[str] = {"pursers", "existing-board"}
        self.boards: dict[str, FakeDoorBoardClient] = {}

    def client_factory(self, url: str, token: str, board_id: str, **kwargs: object) -> FakeDoorBoardClient:
        if board_id not in self.boards:
            self.boards[board_id] = FakeDoorBoardClient(board_id, central=self, is_admin=self.is_admin)
        board = self.boards[board_id]

        async def state_get(key: str | None = None) -> dict:
            return {"state": {"value": json.dumps(self.registry_data)}}

        async def state_update(key: str, value: str, **kwargs: object) -> dict:
            self.registry_data = json.loads(value)
            return {"ok": True}

        async def call(name: str, arguments: dict) -> dict:
            if name == "board_state_update":
                self.registry_data = json.loads(arguments["value"])
                return {"ok": True}
            return await board._call(name, arguments)

        board.board_state_get = state_get
        board.board_state_update = state_update
        board._call = call
        return board


def test_doors_config_validation_and_missing_error(tmp_path: Path) -> None:
    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=None,
        jwks_path=None,
    )
    fetcher = dashboard.FleetFetcher(config)
    with pytest.raises(ValueError, match="Doors configuration missing"):
        asyncio.run(fetcher.fetch_doors())

    with pytest.raises(ValueError, match="Doors configuration missing"):
        asyncio.run(fetcher.copy_door("pursers", "worker"))

    with pytest.raises(ValueError, match="Doors configuration missing"):
        asyncio.run(fetcher.rotate_door("pursers", "worker"))

    with pytest.raises(ValueError, match="Doors configuration missing"):
        asyncio.run(fetcher.add_project(project_name="demo", board_id="demo", work_dir="/PATH/TO/DEMO"))


def test_doors_endpoints_and_library_calls(tmp_path: Path) -> None:
    keys_dir = tmp_path / "keys"
    jwks_path = tmp_path / "jwks.json"
    fake_central = FakeDoorCentral()

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=keys_dir,
        jwks_path=jwks_path,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=fake_central.client_factory)
    cache = dashboard.DashboardCache([fetcher], 60)

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache, seat_manager=SimpleNamespace())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    try:
        # GET /api/doors listing
        with urllib.request.urlopen(base + "/api/doors") as response:
            assert response.status == 200
            data = json.load(response)
            assert data["ok"] is True
            assert len(data["doors"]) == 2  # worker and reviewer for existing-proj
            worker_entry = next(d for d in data["doors"] if d["role"] == "worker")
            assert worker_entry["project"] == "existing-proj"
            assert worker_entry["board_id"] == "existing-board"

        # POST /api/doors/copy
        req = urllib.request.Request(
            base + "/api/doors/copy",
            data=json.dumps({"board": "existing-board", "role": "worker"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
            assert response.headers.get("Cache-Control") == "no-store"
            copy_data = json.load(response)
            assert copy_data["ok"] is True
            assert copy_data["door_string"].startswith("prs1.")
            initial_kid = copy_data["kid"]
            assert initial_kid

        # POST /api/doors/rotate
        req_rot = urllib.request.Request(
            base + "/api/doors/rotate",
            data=json.dumps({"board": "existing-board", "role": "worker"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req_rot) as response:
            assert response.status == 200
            assert response.headers.get("Cache-Control") == "no-store"
            rot_data = json.load(response)
            assert rot_data["ok"] is True
            assert rot_data["door_string"].startswith("prs1.")
            assert rot_data["kid"] != initial_kid
            assert "warning" in rot_data

        # Verify JWKS was updated
        jwks_content = json.loads(jwks_path.read_text(encoding="utf-8"))
        active_kids = [k["kid"] for k in jwks_content["keys"]]
        assert rot_data["kid"] in active_kids
        assert initial_kid not in active_kids
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_add_project_single_action_happy_path_and_idempotent_rerun(tmp_path: Path) -> None:
    keys_dir = tmp_path / "keys"
    jwks_path = tmp_path / "jwks.json"
    fake_central = FakeDoorCentral()

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=keys_dir,
        jwks_path=jwks_path,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=fake_central.client_factory)
    cache = dashboard.DashboardCache([fetcher], 60)

    clone_dir = tmp_path / "fleet-clone"
    clone_dir.mkdir()

    class FakeSeatManager:
        def __init__(self) -> None:
            self.calls = 0

        def _clone_state(self, path: Path, ref: str = "main") -> dict:
            return {"status": "ready", "dirty": False}

        def prepare_fleet_clone(self, registry_payload: dict, project_name: str) -> dict:
            self.calls += 1
            reg = copy.deepcopy(registry_payload["registry"])
            reg["projects"][project_name]["fleet_clone_dir"] = str(clone_dir)
            return {
                "project": project_name,
                "clone": {"path": str(clone_dir), "status": "ready"},
                "registry": reg,
                "expected_sha256": registry_payload["expected_sha256"],
            }

    seats = FakeSeatManager()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache, seat_manager=seats)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    payload = {
        "name": "new-svc",
        "board_id": "new-board",
        "work_dir": str(tmp_path / "work"),
        "integration_ref": "main",
    }
    (tmp_path / "work").mkdir()

    try:
        # First call: Happy path
        req = urllib.request.Request(
            base + "/api/projects/add",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
            assert response.headers.get("Cache-Control") == "no-store"
            result = json.load(response)
            assert result["ok"] is True
            assert result["doors"]["worker"].startswith("prs1.")
            assert result["doors"]["reviewer"].startswith("prs1.")
            statuses = {s["step"]: s["status"] for s in result["steps"]}
            assert statuses["registry_admin"] == "created"
            assert statuses["board_create"] == "created"
            assert statuses["door_principals"] == "created"
            assert statuses["policies"] == "configured"
            assert statuses["fleet_clone"] == "prepared"

        # Verify board state on fake central
        new_board = fake_central.boards["new-board"]
        assert new_board.onboarded is True
        assert new_board.dispatch_policy["offer_ttl_s"] == 600
        assert new_board.dispatch_policy["broadcast_reoffer_s"] == 180
        assert new_board.dispatch_policy["second_opinion"] is True
        assert new_board.dispatch_policy["fallback_broadcast"] is True
        assert new_board.review_policy == "strict"
        assert len(new_board.memberships) == 2  # worker and reviewer doors

        # Second call: Idempotent re-run
        req2 = urllib.request.Request(
            base + "/api/projects/add",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req2) as response:
            assert response.status == 200
            result2 = json.load(response)
            assert result2["ok"] is True
            statuses2 = {s["step"]: s["status"] for s in result2["steps"]}
            assert statuses2["registry_admin"] == "already present"
            assert statuses2["board_create"] == "already present"
            assert statuses2["door_principals"] == "already present"
            assert statuses2["policies"] == "already present"
            assert statuses2["fleet_clone"] == "already present"
            assert result2["doors"]["worker"].startswith("prs1.")
            assert result2["doors"]["reviewer"].startswith("prs1.")
            # Verify clone preparation was not repeated
            assert seats.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_guards_reject_cross_origin_and_non_admin(tmp_path: Path) -> None:
    keys_dir = tmp_path / "keys"
    jwks_path = tmp_path / "jwks.json"

    # 1. Cross-origin rejection
    admin_central = FakeDoorCentral(is_admin=True)
    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="admin-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=keys_dir,
        jwks_path=jwks_path,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=admin_central.client_factory)
    cache = dashboard.DashboardCache([fetcher], 60)
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache, seat_manager=SimpleNamespace())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    try:
        for path, body in (
            ("/api/doors/copy", {"board": "pursers", "role": "worker"}),
            ("/api/doors/rotate", {"board": "pursers", "role": "worker"}),
            (
                "/api/projects/add",
                {"name": "demo", "board_id": "demo", "work_dir": "/PATH/TO/DEMO"},
            ),
        ):
            req = urllib.request.Request(
                base + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req)
            assert exc_info.value.code == 403

        # Cross-origin GET /api/doors rejected
        req_get = urllib.request.Request(
            base + "/api/doors",
            headers={"Origin": "https://attacker.invalid"},
            method="GET",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_get)
        assert exc_info.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    # 2. Non-admin rejection
    non_admin_central = FakeDoorCentral(is_admin=False)
    config_non_admin = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="non-admin-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=keys_dir,
        jwks_path=jwks_path,
    )
    fetcher_non_admin = dashboard.FleetFetcher(
        config_non_admin, client_factory=non_admin_central.client_factory
    )
    member_client = non_admin_central.client_factory(
        config_non_admin.url,
        config_non_admin.token,
        "existing-board",
    )
    assert asyncio.run(member_client.board_status())["ok"] is True
    assert asyncio.run(member_client.board_members())["ok"] is True
    cache_non_admin = dashboard.DashboardCache([fetcher_non_admin], 60)
    server2 = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache_non_admin, seat_manager=SimpleNamespace())
    )
    thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
    thread2.start()
    base2 = f"http://127.0.0.1:{server2.server_port}"

    try:
        # Baseline key files and JWKS bytes before non-admin mutations
        initial_jwks = b'{"keys":[]}'
        jwks_path.write_bytes(initial_jwks)
        keys_dir.mkdir(parents=True, exist_ok=True)
        marker_file = keys_dir / "marker.txt"
        marker_file.write_text("untouched", encoding="utf-8")
        initial_keys_files = {p.name: p.read_bytes() for p in keys_dir.iterdir()}

        # GET /api/doors with non-admin fails with 403
        req_doors = urllib.request.Request(base2 + "/api/doors", headers={"Origin": base2})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_doors)
        assert exc_info.value.code == 403

        # POST /api/doors/copy with non-admin fails with 403
        req_copy = urllib.request.Request(
            base2 + "/api/doors/copy",
            data=json.dumps({"board": "existing-board", "role": "worker"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base2},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_copy)
        assert exc_info.value.code == 403

        # POST /api/doors/rotate with non-admin fails with 403
        req_rotate = urllib.request.Request(
            base2 + "/api/doors/rotate",
            data=json.dumps({"board": "existing-board", "role": "worker"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base2},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_rotate)
        assert exc_info.value.code == 403

        # Keys directory and JWKS remain byte-for-byte unchanged
        assert jwks_path.read_bytes() == initial_jwks
        assert {p.name: p.read_bytes() for p in keys_dir.iterdir()} == initial_keys_files

        # POST /api/projects/add with non-admin fails with 403
        req_add = urllib.request.Request(
            base2 + "/api/projects/add",
            data=json.dumps({"name": "demo", "board_id": "demo", "work_dir": "/PATH/TO/DEMO"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base2},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_add)
        assert exc_info.value.code == 403
    finally:
        server2.shutdown()
        server2.server_close()
        thread2.join()


def test_no_secret_assertions_in_state_logs_and_listings(tmp_path: Path) -> None:
    keys_dir = tmp_path / "keys"
    jwks_path = tmp_path / "jwks.json"
    state_dir = tmp_path / "dashboard_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fake_central = FakeDoorCentral()

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5,
        doors_keys_dir=keys_dir,
        jwks_path=jwks_path,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=fake_central.client_factory)
    cache = dashboard.DashboardCache([fetcher], 60)

    class FakeSecretSeatManager(dashboard.SeatConfigManager):
        def _clone_state(self, path: Path, ref: str = "main") -> dict:
            return {"status": "ready", "dirty": False}

        def prepare_fleet_clone(self, registry_payload: dict, project_name: str) -> dict:
            reg = copy.deepcopy(registry_payload["registry"])
            clone_path = self.state_dir / "fleet-clone"
            clone_path.mkdir(parents=True, exist_ok=True)
            reg["projects"][project_name]["fleet_clone_dir"] = str(clone_path)
            return {
                "project": project_name,
                "clone": {"path": str(clone_path), "status": "ready"},
                "registry": reg,
                "expected_sha256": registry_payload["expected_sha256"],
            }

    seat_mgr = FakeSecretSeatManager(state_dir=state_dir)

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache, seat_manager=seat_mgr)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    try:
        # Mint doors via copy, rotate, and add project
        req_copy = urllib.request.Request(
            base + "/api/doors/copy",
            data=json.dumps({"board": "existing-board", "role": "worker"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req_copy) as response:
            assert response.status == 200

        req_rotate = urllib.request.Request(
            base + "/api/doors/rotate",
            data=json.dumps({"board": "existing-board", "role": "worker"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req_rotate) as response:
            assert response.status == 200

        req_add = urllib.request.Request(
            base + "/api/projects/add",
            data=json.dumps({"name": "secret-test", "board_id": "secret-test", "work_dir": "/PATH/TO/SECRET"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(req_add) as response:
            assert response.status == 200

        # Verify listing endpoints NEVER contain door strings or JWT substrings
        for listing_path in ("/api/doors", "/api/fleet", "/api/config/registry"):
            with urllib.request.urlopen(base + listing_path) as response:
                content = response.read().decode("utf-8")
                assert not DOOR_SHAPE.search(content), f"Door string leak in {listing_path}"
                assert not JWT_SHAPE.search(content), f"JWT leak in {listing_path}"

        # Inspect all dashboard state files and audit logs under state_dir
        state_files = [p for p in state_dir.rglob("*") if p.is_file()]
        for sf in state_files:
            content = sf.read_text(encoding="utf-8", errors="replace")
            assert not DOOR_SHAPE.search(content), f"Door string leak in state file {sf.name}"
            assert not JWT_SHAPE.search(content), f"JWT leak in state file {sf.name}"

        # Inspect JWKS public key file
        jwks_content = jwks_path.read_text(encoding="utf-8")
        assert not DOOR_SHAPE.search(jwks_content), "Door string leak in JWKS"
        assert not JWT_SHAPE.search(jwks_content), "JWT leak in JWKS"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_doors_ui_rendering() -> None:
    html = dashboard.HTML
    assert "doors-panel" in html
    assert "add-project-panel" in html
    assert "add-project-form" in html
    assert 'data-door-action="copy"' in html
    assert 'data-door-action="rotate"' in html
    assert 'name="integration_ref"' in html


def test_clean_text_redaction_is_linear_time_and_behavior_preserved() -> None:
    """CodeQL py/polynomial-redos regression: the key/value redaction pass.

    The previous pattern nested stars around the keyword alternation and
    backtracked polynomially on repeated whitespace (seconds for ~30k
    spaces). The remediated split must stay linear and produce identical
    redaction output.
    """
    clean = dashboard.SeatConfigManager._clean_text

    adversarial = "token" + " " * 40_000
    started = time.monotonic()
    output = clean(adversarial)
    elapsed = time.monotonic() - started
    assert output == adversarial  # no separator on the line: nothing redacted
    assert elapsed < 5.0  # pre-fix pattern took ~9s at this size

    bigger = "token" + " " * 80_000
    started = time.monotonic()
    assert clean(bigger) == bigger
    elapsed_bigger = time.monotonic() - started
    assert elapsed_bigger < 5.0  # doubling input stays linear, not quadratic

    # Keyword fused into a longer key (no word boundary) is still redacted.
    assert clean("XTOKEN=abc") == "XTOKEN=[REDACTED]"
    # file/path/env_var keys remain visible.
    assert clean("XTOKEN_FILE=abc") == "XTOKEN_FILE=abc"
    assert clean("secret_path=/srv/x") == "secret_path=/srv/x"
    # Separator spacing preserved, value replaced.
    assert clean("  MY SECRET = s3kr1t") == "  MY SECRET = [REDACTED]"
    # Only the first separator splits key/value; the rest stays in the value.
    assert clean("mytoken=a=b") == "mytoken=[REDACTED]"
    # Lines without a sensitive keyword are untouched.
    assert clean("plain = value") == "plain = value"
    # Multi-line input redacts per line.
    assert clean("alpha=1\nmy bearer: x\nbeta=2") == (
        "alpha=1\nmy bearer: [REDACTED]\nbeta=2"
    )
