from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

DASHBOARD_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(DASHBOARD_ROOT))
SPEC = importlib.util.spec_from_file_location(
    "runner_setup", DASHBOARD_ROOT / "runner_setup.py"
)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)
EXECUTOR_SPEC = importlib.util.spec_from_file_location(
    "runner_setup_test_executor",
    DASHBOARD_ROOT.parents[0] / "seat-kit" / "fleet_executor.py",
)
assert EXECUTOR_SPEC and EXECUTOR_SPEC.loader
executor = importlib.util.module_from_spec(EXECUTOR_SPEC)
sys.modules[EXECUTOR_SPEC.name] = executor
EXECUTOR_SPEC.loader.exec_module(executor)


def registry_payload() -> bytes:
    document = {
        "version": "1",
        "agents": [
            {
                "id": "safe-agent",
                "name": "Safe agent",
                "version": "1.2.3",
                "distribution": {
                    "npx": {"package": "safe-agent@1.2.3", "args": ["--acp"]}
                },
            }
        ],
    }
    return json.dumps(document).encode()


def manager(tmp_path: Path, **kwargs: object) -> setup.RunnerSetupManager:
    result = setup.RunnerSetupManager(tmp_path / "state", **kwargs)
    setup.refresh_catalog(
        result.cache_path,
        fetch=lambda _url, _timeout, _maximum: registry_payload(),
        now=100,
    )
    return result


def acp_request(tmp_path: Path, mgr: setup.RunnerSetupManager) -> dict[str, object]:
    private_root = tmp_path / "private"
    seat_root = private_root / "seats" / "worker-13"
    seat_root.mkdir(parents=True)
    token = seat_root / "seat.jwt"
    token.write_text("header.payload.signature", encoding="utf-8")
    os.chmod(token, 0o600)
    repository = tmp_path / "repositories" / "project"
    repository.mkdir(parents=True)
    view = setup.load_cached_catalog(mgr.cache_path, now=100)
    return {
        "preset": {
            "schema": "pursers_runner_preset_v1",
            "seat": {
                "agent_name": "worker-13",
                "board_id": "pursers",
                "role": "worker",
            },
            "runner": {
                "kind": "acp",
                "account_ref": "provider:dedicated",
                "catalog_pin": {
                    "agent_id": "safe-agent",
                    "agent_version": "1.2.3",
                    "platform": "linux-x86_64",
                    "registry_revision": view.registry_revision,
                    "distribution_kind": "npx",
                },
            },
            "session_options": {
                "model": "model-a",
                "mode": "plan",
                "reasoning": "high",
            },
        },
        "runtime": {
            "central_url": "https://central.example.invalid/mcp",
            "token_file": str(token),
            "expected_agent_id": "AI-worker-13",
            "expected_principal_id": "PR-worker",
            "repository": str(repository),
            "work_root": str(seat_root / "work"),
            "seat_root": str(seat_root),
            "credential_ref": "worker-13-token",
        },
    }


def runtime_bindings(
    request: dict[str, object],
) -> setup.RuntimePathBindings:
    runtime = request["runtime"]
    assert isinstance(runtime, dict)
    repository = Path(str(runtime["repository"]))
    seat_root = Path(str(runtime["seat_root"]))
    return setup.RuntimePathBindings(
        repository_root=repository.parent,
        private_root=seat_root.parents[1],
        repositories=(repository,),
        token_file=Path(str(runtime["token_file"])),
        work_root=Path(str(runtime["work_root"])),
        seat_root=seat_root,
        policy_file=(
            Path(str(runtime["policy_file"]))
            if runtime.get("policy_file") is not None
            else None
        ),
    )


def test_acp_plan_needs_human_without_narrow_account_boundary(tmp_path: Path) -> None:
    mgr = manager(tmp_path)
    request = acp_request(tmp_path, mgr)
    plan = mgr.plan(request, runtime_bindings=runtime_bindings(request))

    assert plan["activation"]["state"] == "needs_human"
    assert plan["install"]["action"] == "use_pinned_package_launcher"
    result = mgr.apply(plan_id=plan["plan_id"], digest=plan["digest"])
    assert result == {
        "ok": False,
        "state": "needs_human",
        "decision": plan["activation"]["decision"],
        "changed": [],
    }
    assert not mgr.preset_root.exists()


def test_acp_apply_persists_session_options_and_defers_active_lease(
    tmp_path: Path,
) -> None:
    mgr = manager(
        tmp_path,
        account_probe=lambda _ref: {
            "status": "ready",
            "provider": "test",
            "network": "isolated",
            "auth_scope": "dedicated",
        },
        seat_probe=lambda _name: {"running": True, "active_lease": True},
    )
    request = acp_request(tmp_path, mgr)
    plan = mgr.plan(request, runtime_bindings=runtime_bindings(request))
    assert plan["activation"]["state"] == "deferred"

    result = mgr.apply(plan_id=plan["plan_id"], digest=plan["digest"])
    assert result["state"] == "deferred"
    template = json.loads(Path(result["template"]).read_text(encoding="utf-8"))
    assert template["acp"]["session_options"] == {
        "model": "model-a",
        "mode": "plan",
        "reasoning": "high",
    }
    assert template["seat"]["agent_name"] == "worker-13"
    assert template["seat"]["expected_principal_id"] == "PR-worker"
    executor_template = json.loads(
        Path(result["executor_template"]).read_text(encoding="utf-8")
    )
    assert executor_template["role"] == "acp_worker"
    assert executor_template["principal_id"] == "PR-worker"
    assert executor_template["credential_ref"] == "worker-13-token"
    assert executor_template["boards"] == "registry"
    assert executor_template["capabilities"] == {
        "can_work": True,
        "can_review": False,
        "tier_max": 2,
        "max_parallel": 1,
    }
    parsed = executor.SeatTemplate.from_record("worker-13-acp", executor_template)
    assert parsed.principal_id == "PR-worker"
    assert "header.payload.signature" not in json.dumps(plan)


def test_native_preset_remains_additive_without_acp_runtime(tmp_path: Path) -> None:
    mgr = setup.RunnerSetupManager(tmp_path / "state")
    request = {
        "preset": {
            "schema": "pursers_runner_preset_v1",
            "seat": {"agent_name": "codex-1", "board_id": "pursers", "role": "worker"},
            "runner": {
                "kind": "native",
                "provider": "codex",
                "account_ref": "codex:default",
                "config_ref": "codex:default",
                "codex_profile": "worker",
            },
            "session_options": {},
        },
        "runtime": {},
    }
    plan = mgr.plan(request)
    result = mgr.apply(plan_id=plan["plan_id"], digest=plan["digest"])
    assert result["state"] == "native_preserved"
    assert result["template"] is None


def test_apply_fails_closed_when_seat_state_changes(tmp_path: Path) -> None:
    state = {"running": False, "active_lease": False}
    mgr = manager(
        tmp_path,
        account_probe=lambda _ref: {"status": "ready"},
        seat_probe=lambda _name: dict(state),
    )
    request = acp_request(tmp_path, mgr)
    plan = mgr.plan(request, runtime_bindings=runtime_bindings(request))
    state["active_lease"] = True
    with pytest.raises(setup.RunnerSetupError, match="seat_state_changed_replan"):
        mgr.apply(plan_id=plan["plan_id"], digest=plan["digest"])


def test_ready_plan_hands_exact_template_to_existing_executor(tmp_path: Path) -> None:
    handed_off: list[Path] = []

    def execute(path: Path) -> dict[str, object]:
        handed_off.append(path)
        executor.SeatTemplate.from_record(
            "worker-13-acp", json.loads(path.read_text(encoding="utf-8"))
        )
        return {"state": "signed_executor_queued", "committed": False}

    mgr = manager(
        tmp_path,
        account_probe=lambda _ref: {"status": "ready"},
        seat_probe=lambda _name: {"running": False, "active_lease": False},
        executor=execute,
    )
    request = acp_request(tmp_path, mgr)
    plan = mgr.plan(request, runtime_bindings=runtime_bindings(request))
    assert plan["activation"]["state"] == "ready"

    result = mgr.apply(plan_id=plan["plan_id"], digest=plan["digest"])
    assert result["state"] == "signed_executor_queued"
    assert handed_off == [Path(result["executor_template"])]


@pytest.mark.parametrize(
    "field",
    ["repository", "token_file", "work_root", "seat_root", "policy_file"],
)
def test_acp_runtime_rejects_request_path_outside_server_bindings(
    tmp_path: Path, field: str
) -> None:
    mgr = manager(tmp_path)
    request = acp_request(tmp_path, mgr)
    runtime = request["runtime"]
    assert isinstance(runtime, dict)
    if field == "policy_file":
        policy = Path(str(runtime["seat_root"])) / "acp-policy.json"
        policy.write_text("{}\n", encoding="utf-8")
        policy.chmod(0o600)
        runtime[field] = str(policy)
    bindings = runtime_bindings(request)
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime[field] = str(outside / ".." / "escaped")

    with pytest.raises(ValueError, match=rf"runtime.{field} is not authorized"):
        mgr.plan(request, runtime_bindings=bindings)


def test_runtime_bindings_reject_symlink_escape(tmp_path: Path) -> None:
    repository_root = tmp_path / "repositories"
    private_root = tmp_path / "private"
    seat_root = private_root / "seats" / "worker-13"
    outside = tmp_path / "outside"
    repository_root.mkdir()
    seat_root.mkdir(parents=True)
    outside.mkdir()
    link = repository_root / "escaped"
    link.symlink_to(outside, target_is_directory=True)
    token = seat_root / "seat.jwt"
    token.write_text("header.payload.signature", encoding="utf-8")
    token.chmod(0o600)

    with pytest.raises(setup.RunnerSetupError, match="repository_binding_symlink"):
        setup.RuntimePathBindings(
            repository_root=repository_root,
            private_root=private_root,
            repositories=(link,),
            token_file=token,
            work_root=seat_root / "work",
            seat_root=seat_root,
        )


def test_runtime_bindings_reject_configured_repository_outside_root(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repositories"
    private_root = tmp_path / "private"
    seat_root = private_root / "seats" / "worker-13"
    outside = tmp_path / "outside"
    repository_root.mkdir()
    seat_root.mkdir(parents=True)
    outside.mkdir()
    token = seat_root / "seat.jwt"
    token.write_text("header.payload.signature", encoding="utf-8")
    token.chmod(0o600)

    with pytest.raises(
        setup.RunnerSetupError,
        match="repository_binding_outside_approved_root",
    ):
        setup.RuntimePathBindings(
            repository_root=repository_root,
            private_root=private_root,
            repositories=(repository_root / ".." / "outside",),
            token_file=token,
            work_root=seat_root / "work",
            seat_root=seat_root,
        )


def test_apply_revalidates_repository_binding_after_symlink_swap(
    tmp_path: Path,
) -> None:
    mgr = manager(
        tmp_path,
        account_probe=lambda _ref: {"status": "ready"},
        seat_probe=lambda _name: {"running": False, "active_lease": False},
    )
    request = acp_request(tmp_path, mgr)
    bindings = runtime_bindings(request)
    plan = mgr.plan(request, runtime_bindings=bindings)
    repository = bindings.repositories[0]
    outside = tmp_path / "outside-after-plan"
    outside.mkdir()
    repository.rmdir()
    repository.symlink_to(outside, target_is_directory=True)

    with pytest.raises(setup.RunnerSetupError, match="repository_binding_symlink"):
        mgr.apply(plan_id=plan["plan_id"], digest=plan["digest"])
