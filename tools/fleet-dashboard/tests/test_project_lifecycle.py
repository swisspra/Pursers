from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "project_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("project_lifecycle", MODULE_PATH)
assert SPEC and SPEC.loader
lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lifecycle
SPEC.loader.exec_module(lifecycle)


def _registry(status: str = "active") -> dict:
    return {
        "schema_version": 1,
        "projects": {
            "demo": {
                "board_id": "demo-board",
                "work_dir": "/PATH/TO/demo",
                "status": status,
            }
        },
    }


def test_non_git_discovery_never_invokes_git(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    calls = []

    def forbidden(*args: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        raise AssertionError("non-Git discovery must not run Git")

    observed = lifecycle.inspect_project_source(
        work_dir=str(folder), git_mode="none", runner=forbidden
    )

    assert observed["blocked"] is False
    assert observed["git_mode"] == "none"
    assert calls == []


def test_existing_git_discovery_blocks_dirty_checkout(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()

    def runner(_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        values = {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("status", "--porcelain"): (0, " M changed.txt\n"),
            ("remote", "get-url", "origin"): (0, "https://example.invalid/demo.git\n"),
            ("rev-parse", "--verify", "main^{commit}"): (0, "a" * 40 + "\n"),
        }
        code, output = values[args]
        return subprocess.CompletedProcess(["git", *args], code, output, "")

    observed = lifecycle.inspect_project_source(
        work_dir=str(folder),
        git_mode="existing",
        repository_url="https://example.invalid/demo.git",
        runner=runner,
    )

    assert observed["blocked"] is True
    assert observed["clean"] is False
    assert "dirty" in " ".join(observed["blockers"])


@pytest.mark.parametrize(
    "repository_url",
    [
        "https://user:secret@example.invalid/demo.git",
        "https://example.invalid/demo.git?token=secret",
    ],
)
def test_discovery_rejects_credential_bearing_repository_urls(
    tmp_path: Path, repository_url: str
) -> None:
    with pytest.raises(lifecycle.ProjectLifecycleError, match="must not contain"):
        lifecycle.inspect_project_source(
            work_dir=str(tmp_path / "new"),
            git_mode="clone",
            repository_url=repository_url,
        )


def test_existing_checkout_never_returns_credential_bearing_origin(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()

    def runner(_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        values = {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("status", "--porcelain"): (0, ""),
            ("remote", "get-url", "origin"): (
                0,
                "https://user:credential@example.invalid/demo.git\n",
            ),
            ("rev-parse", "--verify", "main^{commit}"): (0, "a" * 40 + "\n"),
        }
        code, output = values[args]
        return subprocess.CompletedProcess(["git", *args], code, output, "")

    observed = lifecycle.inspect_project_source(
        work_dir=str(folder), git_mode="existing", runner=runner
    )

    assert observed["blocked"] is True
    assert observed["observed_origin"] is None
    assert "credential" not in str(observed)


def test_add_plan_previews_registry_board_clone_and_permissions(tmp_path: Path) -> None:
    source = lifecycle.inspect_project_source(
        work_dir=str(tmp_path / "new"),
        git_mode="clone",
        repository_url="https://example.invalid/new.git",
        integration_ref="main",
    )
    plan = lifecycle.build_add_plan(
        request={
            "name": "new",
            "board_id": "new-board",
            "prepare_fleet_clone": True,
        },
        registry={"schema_version": 1, "projects": {}},
        registry_expected_sha256="a" * 64,
        source=source,
        board_exists=False,
        actor="dashboard",
        central="work",
    )

    assert plan["blocked"] is False
    assert [item["operation_id"] for item in plan["operations"]] == [
        "source-clone",
        "registry",
        "board",
        "door-principals-and-policy",
        "fleet-clone",
    ]
    assert all(item["required_permission"] for item in plan["operations"])
    assert plan["proposed_entry"]["repository_url"] == "https://example.invalid/new.git"


@pytest.mark.parametrize(
    ("source_changes", "request_changes", "changed_field"),
    [
        ({"path": "/PATH/TO/replacement"}, {}, "work_dir"),
        (
            {"repository_url": "https://example.invalid/replacement.git"},
            {},
            "repository_url",
        ),
        ({"integration_ref": "release"}, {}, "integration_ref"),
        ({}, {"board_id": "new-board"}, "board_id"),
    ],
)
def test_add_plan_blocks_existing_name_with_different_persisted_settings(
    source_changes: dict, request_changes: dict, changed_field: str
) -> None:
    source = {
        "path": "/PATH/TO/demo",
        "git_mode": "none",
        "repository_url": None,
        "integration_ref": "main",
        "blocked": False,
        "blockers": [],
    }
    source.update(source_changes)
    request = {
        "name": "demo",
        "board_id": "demo-board",
        "prepare_fleet_clone": False,
    }
    request.update(request_changes)

    plan = lifecycle.build_add_plan(
        request=request,
        registry=_registry(),
        registry_expected_sha256="d" * 64,
        source=source,
        board_exists=True,
        actor="dashboard",
        central="work",
    )

    assert plan["blocked"] is True
    assert "already registered" in " ".join(plan["blockers"])
    registry_operation = next(
        item for item in plan["operations"] if item["operation_id"] == "registry"
    )
    assert registry_operation["effect"] == "blocked_name_collision"
    assert changed_field in registry_operation["changed_fields"]
    store = lifecycle.ProjectLifecycleStore()
    stored = store.add(plan)
    with pytest.raises(lifecycle.ProjectLifecycleConflictError, match="blocked"):
        store.reserve(
            stored["plan_id"],
            actor="dashboard",
            central="work",
            plan_digest=stored["plan_digest"],
            confirmation="demo",
        )


def test_add_plan_allows_exact_idempotent_rerun() -> None:
    source = {
        "path": "/PATH/TO/demo",
        "git_mode": "none",
        "repository_url": None,
        "integration_ref": "main",
        "blocked": False,
        "blockers": [],
    }
    plan = lifecycle.build_add_plan(
        request={
            "name": "demo",
            "board_id": "demo-board",
            "prepare_fleet_clone": False,
        },
        registry=_registry(),
        registry_expected_sha256="e" * 64,
        source=source,
        board_exists=True,
        actor="dashboard",
        central="work",
    )

    assert plan["blocked"] is False
    registry_operation = next(
        item for item in plan["operations"] if item["operation_id"] == "registry"
    )
    assert registry_operation["effect"] == "already_present"
    assert "changed_fields" not in registry_operation


def test_browser_plan_projection_omits_server_held_registry_values() -> None:
    raw = lifecycle.build_add_plan(
        request={
            "name": "demo",
            "board_id": "new-board",
            "prepare_fleet_clone": False,
        },
        registry={
            "schema_version": 1,
            "projects": {
                "demo": {
                    "board_id": "old-board",
                    "status": "active",
                    "work_dir": "/PRIVATE/old",
                    "repository_url": "https://user:secret@example.invalid/private.git",
                    "fleet_clone_dir": "/PRIVATE/clone",
                    "api_token": "TOKEN-CANARY",
                    "private_note": "NOTE-CANARY",
                }
            },
        },
        registry_expected_sha256="f" * 64,
        source={
            "path": "/PRIVATE/new",
            "git_mode": "none",
            "repository_url": "https://example.invalid/new.git",
            "observed_origin": "https://example.invalid/observed.git",
            "integration_ref": "main",
            "blocked": False,
            "blockers": [],
        },
        board_exists=False,
        actor="dashboard-private-agent",
        central="work",
    )
    stored = lifecycle.ProjectLifecycleStore().add(raw)

    public = lifecycle.public_project_lifecycle_plan(stored)
    encoded = json.dumps(public, sort_keys=True)

    for canary in (
        "/PRIVATE/old",
        "/PRIVATE/new",
        "/PRIVATE/clone",
        "user:secret",
        "TOKEN-CANARY",
        "NOTE-CANARY",
        "dashboard-private-agent",
    ):
        assert canary not in encoded
    operation = next(
        item for item in public["operations"] if item["operation_id"] == "registry"
    )
    assert operation["before"] == {
        "board_id": "old-board",
        "status": "active",
        "work_dir": "[local folder configured]",
        "repository_url": "[Git source configured]",
        "fleet_clone_dir": "[Fleet clone configured]",
        "other_configured_fields": 2,
    }
    assert "other_configured_fields" in operation["changed_fields"]
    assert public["source"] == {
        "git_mode": "none",
        "integration_ref": "main",
        "blocked": False,
        "blockers": [],
        "path_configured": True,
        "repository_configured": True,
        "origin_observed": True,
    }
    receipt = lifecycle.public_project_lifecycle_receipt(
        {"ok": True, "removed_entry": raw["operations"][0]["before"]}
    )
    assert "TOKEN-CANARY" not in json.dumps(receipt, sort_keys=True)
    assert receipt["removed_entry"] == operation["before"]


def test_remove_plan_requires_paused_quiescent_complete_state() -> None:
    blocked = lifecycle.build_remove_plan(
        request={"action": "remove", "name": "demo"},
        registry=_registry("active"),
        registry_expected_sha256="b" * 64,
        board_observation={
            "complete": True,
            "active_tickets": [{"ticket_id": "TK-one", "status": "claimed"}],
            "pending_offers": [],
        },
        actor="dashboard",
        central="work",
    )
    assert blocked["blocked"] is True
    assert any("paused" in item for item in blocked["blockers"])
    assert any("active work" in item for item in blocked["blockers"])

    ready = lifecycle.build_remove_plan(
        request={"action": "remove", "name": "demo"},
        registry=_registry("paused"),
        registry_expected_sha256="b" * 64,
        board_observation={
            "complete": True,
            "active_tickets": [],
            "pending_offers": [],
        },
        actor="dashboard",
        central="work",
    )
    assert ready["blocked"] is False
    assert ready["operations"] == [
        {
            "operation_id": "registry-remove",
            "effect": "remove_reference",
            "target": "project_registry",
            "before": _registry("paused")["projects"]["demo"],
            "after": None,
            "required_permission": "registry board administrator",
        }
    ]
    assert any("shared credentials" in item for item in ready["preserved"])


def test_plan_store_binds_actor_digest_confirmation_expiry_and_replay() -> None:
    created = datetime(2030, 1, 2, tzinfo=timezone.utc)
    plan = lifecycle.build_remove_plan(
        request={"action": "remove", "name": "demo"},
        registry=_registry("paused"),
        registry_expected_sha256="c" * 64,
        board_observation={
            "complete": True,
            "active_tickets": [],
            "pending_offers": [],
        },
        actor="dashboard",
        central="work",
        created_at=created,
    )
    store = lifecycle.ProjectLifecycleStore()
    stored = store.add(plan)

    with pytest.raises(KeyError):
        store.get(stored["plan_id"], actor="other", central="work")
    with pytest.raises(lifecycle.ProjectLifecycleConflictError, match="digest"):
        store.reserve(
            stored["plan_id"],
            actor="dashboard",
            central="work",
            plan_digest="sha256:" + "0" * 64,
            confirmation="demo",
            now=created + timedelta(seconds=1),
        )
    with pytest.raises(lifecycle.ProjectLifecycleError, match="confirmation"):
        store.reserve(
            stored["plan_id"],
            actor="dashboard",
            central="work",
            plan_digest=stored["plan_digest"],
            confirmation="wrong",
            now=created + timedelta(seconds=1),
        )

    reserved, previous = store.reserve(
        stored["plan_id"],
        actor="dashboard",
        central="work",
        plan_digest=stored["plan_digest"],
        confirmation="demo",
        now=created + timedelta(seconds=1),
    )
    assert reserved["state"] == "applying"
    assert previous is None
    receipt = store.complete(stored["plan_id"], {"ok": True})
    assert receipt == {"ok": True}
    _reserved, previous = store.reserve(
        stored["plan_id"],
        actor="dashboard",
        central="work",
        plan_digest=stored["plan_digest"],
        confirmation="demo",
        now=created + timedelta(seconds=lifecycle.PLAN_TTL_SECONDS + 1),
    )
    assert previous == {"ok": True}

    expired = store.add(plan)
    with pytest.raises(lifecycle.ProjectLifecycleConflictError, match="expired"):
        store.reserve(
            expired["plan_id"],
            actor="dashboard",
            central="work",
            plan_digest=expired["plan_digest"],
            confirmation="demo",
            now=created + timedelta(seconds=lifecycle.PLAN_TTL_SECONDS),
        )


def test_failed_source_clone_preserves_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new-checkout"

    def fail_clone(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        created = Path(command[-1])
        created.mkdir()
        (created / "partial.data").write_text("inspect", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, "", "bounded failure")

    monkeypatch.setattr(lifecycle.subprocess, "run", fail_clone)
    plan = {
        "source": {
            "git_mode": "clone",
            "path": str(target),
            "repository_url": "https://example.invalid/demo.git",
            "integration_ref": "main",
        }
    }

    with pytest.raises(
        lifecycle.ProjectLifecycleConflictError, match="preserved for inspection"
    ):
        lifecycle.clone_project_source(plan)
    assert (target / "partial.data").read_text(encoding="utf-8") == "inspect"


@pytest.mark.parametrize(
    ("stderr", "reason_code"),
    [
        ("fatal: Authentication failed for private source TOKEN-CANARY", "repository_access_denied"),
        ("fatal: transport closed unexpectedly at /PRIVATE/repo", "clone_failed"),
    ],
)
def test_failed_source_clone_returns_only_bounded_reason_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    reason_code: str,
) -> None:
    target = tmp_path / "new-checkout"

    def fail_clone(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", stderr)

    monkeypatch.setattr(lifecycle.subprocess, "run", fail_clone)
    plan = {
        "source": {
            "git_mode": "clone",
            "path": str(target),
            "repository_url": "https://example.invalid/demo.git",
            "integration_ref": "main",
        }
    }

    with pytest.raises(lifecycle.ProjectLifecycleCloneError) as raised:
        lifecycle.clone_project_source(plan)

    assert raised.value.reason_code == reason_code
    assert stderr not in str(raised.value)
    assert "TOKEN-CANARY" not in str(raised.value)
    assert "/PRIVATE/repo" not in str(raised.value)
