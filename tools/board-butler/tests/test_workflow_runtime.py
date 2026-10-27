"""Coordinator-only delivery binding tests; external ADO is a disposable fake."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT / "tools" / "board-butler"))

import board_butler as butler
from pursers_client.delivery_workflow import activate_delivery_policy

spec = importlib.util.spec_from_file_location("workflow_runtime", ROOT / "tools/board-butler/workflow_runtime.py")
assert spec and spec.loader
workflow_runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = workflow_runtime
spec.loader.exec_module(workflow_runtime)

BOARD = "shared-board"
REPOSITORY = "https://dev.azure.com/acme/Project/_git/repository"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def clone(tmp_path: Path, url: str = REPOSITORY) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "config", "user.name", "Disposable Test")
    git(seed, "config", "user.email", "test@example.invalid")
    (seed / "README").write_text("disposable\n")
    git(seed, "add", "README")
    git(seed, "commit", "-m", "baseline")
    git(seed, "push", "origin", "HEAD:refs/heads/main")
    path = tmp_path / "protected-clone"
    git(tmp_path, "clone", str(origin), str(path))
    git(path, "remote", "set-url", "origin", url)
    return path


def delivery_policy(*, target="customer/review"):
    return {
        "mode": "batch_pr", "mapped_base": "main",
        "integration_branch": "pursers-integration",
        "snapshot_branch_prefix": "pursers/delivery",
        "final_pr_target": target,
        "release_trigger": {"kind": "ready"}, "pr_update": "freeze_on_ready",
        "auto_integrate": False, "final_merge": "manual",
        "validation": {"test_commands": [], "required_reviewers": 1,
                       "independent_review": True, "require_upstream_policies": True},
        "conflict_policy": "pause", "collection_paused": False,
    }


class FakeConnector:
    """Existing approved connector surface; calls never leave this test."""
    def __init__(self):
        self.calls = []

    async def list_customer_prs(self, repository, target_branch, status):
        self.calls.append(("list", repository, target_branch, status))
        return []

    async def create_customer_pr(self, repository, payload, body, operation_id):
        self.calls.append(("create", repository, payload, operation_id))
        return {"status": "unknown"}

    async def get_customer_pr(self, repository, pr_id):
        self.calls.append(("get", repository, pr_id))
        return {}

    async def update_customer_pr(self, repository, payload, body, operation_id):
        self.calls.append(("update", repository, payload, operation_id))
        return {"status": "unknown"}

    async def reconcile_operation(self, repository, reservation):
        return {"status": "unknown"}


class Runtime:
    def __init__(self, board=BOARD, project="Alpha", connector_id="ado"):
        self.board_id = board
        self.project_id = project
        self.declaration = SimpleNamespace(
            connector_id=connector_id, enabled=True,
            tools=(SimpleNamespace(name="ado_pull_requests_list", effect="read_only"),
                   SimpleNamespace(name="ado_pull_request_get", effect="read_only"),
                   SimpleNamespace(name="ado_pull_request_create", effect="mutating"),
                   SimpleNamespace(name="ado_pull_request_update", effect="mutating")),
            risky_tools=frozenset({"ado_pull_request_create", "ado_pull_request_update"}),
        )
        self.policy_gate = None
        self.calls = []

    async def call_tool(self, operation, tool, arguments):
        self.calls.append((operation, tool, dict(arguments)))
        if tool in self.declaration.risky_tools:
            if self.policy_gate is None:
                raise butler.ConnectorDenied("policy gate missing")
            decision = await self.policy_gate(butler.ConnectorPolicyRequest(
                self.board_id, self.project_id, self.declaration.connector_id,
                operation, tool, "mutating", hashlib.sha256(
                    butler._canonical_json(arguments)).hexdigest()))
            if not decision.allowed:
                raise butler.ConnectorDenied("policy denied")
        document = ({"pullRequestId": 7, "status": "active",
                     "sourceRefName": "refs/heads/reviewed/x",
                     "targetRefName": "refs/heads/customer/y",
                     "repository": {"name": "repository",
                                    "project": {"name": "Project"}}}
                    if tool == "ado_pull_request_create" else {"value": []})
        return SimpleNamespace(payload={"structuredContent": document})


def project(name="Alpha", *, board=BOARD, url=REPOSITORY, clone_path=None,
            activation=True, policy=None):
    raw_policy = policy or delivery_policy()
    row = {"board_id": board, "status": "active", "work_dir": str(clone_path),
           "work_dir_owner": "fleet", "fleet_clone_dir": str(clone_path),
           "repository_url": url, "integration_ref": "main",
           "delivery_policy": raw_policy}
    if activation:
        row["delivery_policy_activation"] = activate_delivery_policy(raw_policy)
    registry = {"schema_version": 1, "projects": {name: row}}
    return {**row, "name": name, "__project_name__": name, "__registry__": registry}


def backend(tmp_path: Path, runtimes, sources=(), *, active=True, home_board=BOARD):
    state = tmp_path / "private" / "index.json"
    state.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(state.parent, 0o700)
    args = SimpleNamespace(runtime_mode="active" if active else "shadow",
                           home_board=home_board,
                           _connector_runtimes=tuple(runtimes),
                           _connector_sources=tuple(sources),
                           source_intake_index_file=state)
    poller = SimpleNamespace(active=active, _writeback_grants=set())
    poller._writeback_policy = lambda request: asyncio.sleep(0, result=butler.ConnectorPolicyDecision(
        active and request in poller._writeback_grants, "source-writeback-policy",
        "approved_source_writeback" if active and request in poller._writeback_grants else "unapproved_source_writeback"))
    return SimpleNamespace(args=args, source_intake_poller=poller)


def test_coordinator_only_no_source_binds_existing_runtime_and_policy(tmp_path):
    clone_path = clone(tmp_path)
    runtime = Runtime()
    backend_instance = backend(tmp_path, [runtime])
    actual, policy = asyncio.run(workflow_runtime.build_workflow_runtime(
        backend_instance, project(clone_path=clone_path)))
    assert isinstance(actual, butler._delivery_policy_api.__class__) is False
    assert isinstance(actual, importlib.import_module("integration_delivery").BatchDeliveryRuntime)
    assert policy["mode"] == "batch_pr"
    assert actual.adapter.repo_path == clone_path.resolve()
    assert isinstance(actual.adapter.connector, butler._ResidentBatchConnector)
    assert actual.adapter.connector.runtime is runtime
    assert actual.adapter.connector.fields["repository_project"] == "Project"
    assert actual.adapter.connector.fields["repository_name"] == "repository"


def test_exact_batch_base_equal_to_customer_target_is_preserved(tmp_path):
    clone_path = clone(tmp_path)
    configured = delivery_policy(target="main")
    row = project(clone_path=clone_path, policy=configured)
    actual, compiled = asyncio.run(workflow_runtime.build_workflow_runtime(
        backend(tmp_path, [Runtime()]), row))
    assert actual.adapter is not None
    assert compiled["base_branch"] == "main"
    assert compiled["target_branch"] == "main"


def test_same_board_projects_get_distinct_runtime_cache_and_durable_ledgers(tmp_path):
    a_clone, b_clone = clone(tmp_path / "a"), clone(tmp_path / "b", "https://dev.azure.com/acme/Other/_git/other")
    runtime_a, runtime_b = Runtime(project="Alpha", connector_id="ado-a"), Runtime(project="Beta", connector_id="ado-b")
    back = backend(tmp_path, [runtime_a, runtime_b])
    a = project("Alpha", clone_path=a_clone)
    b = project("Beta", url="https://dev.azure.com/acme/Other/_git/other", clone_path=b_clone)
    runtime1, _ = asyncio.run(workflow_runtime.build_workflow_runtime(back, a))
    runtime2, _ = asyncio.run(workflow_runtime.build_workflow_runtime(back, b))
    assert runtime1 is not runtime2
    assert runtime1.ledger.path != runtime2.ledger.path
    assert runtime1.adapter.repo_path != runtime2.adapter.repo_path
    assert asyncio.run(workflow_runtime.build_workflow_runtime(back, a))[0] is runtime1


def test_missing_or_foreign_connector_route_is_denied(tmp_path):
    clone_path = clone(tmp_path)
    with pytest.raises(butler.ConnectorDenied, match="route"):
        asyncio.run(workflow_runtime.build_workflow_runtime(
            backend(tmp_path, []), project(clone_path=clone_path)))
    foreign = Runtime(board="other-board", project="Alpha")
    with pytest.raises(butler.ConnectorDenied, match="route"):
        asyncio.run(workflow_runtime.build_workflow_runtime(
            backend(tmp_path, [foreign]), project(clone_path=clone_path)))


def test_source_project_map_binds_home_runtime_to_exact_nonhome_registry_board(tmp_path):
    clone_path = clone(tmp_path)
    runtime = Runtime(board="butler-home", project="registry-default")
    source = SimpleNamespace(enabled=True, connector_id="ado",
        routing=SimpleNamespace(project_hint_is_registry_key=False, project_map={"alpha": "Alpha"}))
    target = project(clone_path=clone_path, board="sonar-project-board")
    actual, _ = asyncio.run(workflow_runtime.build_workflow_runtime(
        backend(tmp_path, [runtime], [source], home_board="butler-home"), target))
    assert actual.adapter.connector.runtime is runtime
    assert runtime.board_id == "butler-home"  # bind; never mutate/recreate the authorized runtime


def test_source_project_map_is_exact_and_foreign_or_unmapped_routes_denied(tmp_path):
    clone_path = clone(tmp_path)
    runtime = Runtime(board="butler-home", project="registry-default")
    foreign = SimpleNamespace(enabled=True, connector_id="ado",
        routing=SimpleNamespace(project_hint_is_registry_key=False, project_map={"alpha": "Elsewhere"}))
    with pytest.raises(butler.ConnectorDenied, match="route"):
        asyncio.run(workflow_runtime.build_workflow_runtime(
            backend(tmp_path, [runtime], [foreign], home_board="butler-home"),
            project(clone_path=clone_path, board="sonar-project-board")))
    with pytest.raises(butler.ConnectorDenied, match="route"):
        asyncio.run(workflow_runtime.build_workflow_runtime(
            backend(tmp_path, [runtime], home_board="butler-home"),
            project(clone_path=clone_path, board="sonar-project-board")))
    source = SimpleNamespace(enabled=True, connector_id="ado",
        routing=SimpleNamespace(project_hint_is_registry_key=False, project_map={"alpha": "Alpha"}))
    foreign_board = Runtime(board="unrelated-board", project="registry-default")
    with pytest.raises(butler.ConnectorDenied, match="route"):
        asyncio.run(workflow_runtime.build_workflow_runtime(
            backend(tmp_path, [foreign_board], [source], home_board="butler-home"),
            project(clone_path=clone_path, board="sonar-project-board")))


def test_inactive_policy_or_unprotected_clone_is_blocked_without_connector_effect(tmp_path):
    clone_path = clone(tmp_path)
    runtime = Runtime()
    with pytest.raises(butler.ConnectorDenied, match="activated"):
        asyncio.run(workflow_runtime.build_workflow_runtime(
            backend(tmp_path, [runtime]), project(clone_path=clone_path, activation=False)))
    unprotected = project(clone_path=clone_path)
    unprotected["work_dir_owner"] = "operator"
    with pytest.raises(butler.ConnectorDenied, match="protected clone"):
        asyncio.run(workflow_runtime.build_workflow_runtime(backend(tmp_path, [runtime]), unprotected))
    assert runtime.calls == []


def test_draft_create_requires_live_discovered_boolean_capability(tmp_path):
    clone_path = clone(tmp_path)
    runtime = Runtime()
    back = backend(tmp_path, [runtime])
    runtime.policy_gate = back.source_intake_poller._writeback_policy
    bridge = workflow_runtime._DeliveryToolBridge(runtime, back)
    connector = butler._ResidentBatchConnector(
        bridge, runtime, {"repository_project": "Project", "repository_name": "repository"})

    async def unsupported(*_args, **_kwargs):
        return SimpleNamespace(tools=[{
            "name": "ado_pull_request_create",
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
        }])

    runtime.discover = unsupported
    runtime.calls.clear()
    with pytest.raises(butler.ConnectorDenied, match="unsupported by the discovered connector schema"):
        asyncio.run(connector.create_customer_pr("repo", {
            "source_branch": "reviewed/x", "target_branch": "customer/y",
            "snapshot_sha": "a" * 40, "correlation": "c" * 64, "is_draft": True,
        }, "report", "draft-operation"))
    assert runtime.calls == []
    assert not back.source_intake_poller._writeback_grants


def test_supported_draft_capability_sends_and_verifies_draft_true(tmp_path):
    runtime = Runtime()
    back = backend(tmp_path, [runtime])
    runtime.policy_gate = back.source_intake_poller._writeback_policy
    bridge = workflow_runtime._DeliveryToolBridge(runtime, back)
    connector = butler._ResidentBatchConnector(
        bridge, runtime, {"repository_project": "Project", "repository_name": "repository"})
    runtime.discover = lambda _operation: asyncio.sleep(0, result=SimpleNamespace(tools=[{
        "name": "ado_pull_request_create",
        "input_schema": {"type": "object", "properties": {"isDraft": {"type": "boolean"}}},
    }]))

    async def delivery_tool(_runtime, name, args, *, mutate=False):
        assert name == "ado_pull_request_create" and mutate is True
        assert args["isDraft"] is True
        return {"pullRequestId": 9, "status": "active", "isDraft": True,
                "sourceRefName": "refs/heads/reviewed/x", "targetRefName": "refs/heads/customer/y",
                "description": "Pursers-Batch-Correlation: " + "c" * 64,
                "repository": {"name": "repository", "project": {"name": "Project"}}}

    async def readback(_repository, _pr_id):
        return {"id": 9, "status": "active", "is_draft": True,
                "source_branch": "reviewed/x", "source_sha": "a" * 40,
                "target_branch": "customer/y", "correlation": "c" * 64}

    bridge._delivery_tool = delivery_tool
    connector.get_customer_pr = readback
    result = asyncio.run(connector.create_customer_pr("repo", {
        "source_branch": "reviewed/x", "target_branch": "customer/y",
        "snapshot_sha": "a" * 40, "correlation": "c" * 64, "is_draft": True,
    }, "report", "draft-operation"))
    assert result["status"] == "confirmed" and result["pr"]["is_draft"] is True


def test_existing_mutation_guard_requires_exact_active_grant_without_replacing_gate(tmp_path):
    runtime = Runtime()
    back = backend(tmp_path, [runtime])
    runtime.policy_gate = back.source_intake_poller._writeback_policy
    original_gate = runtime.policy_gate
    bridge = workflow_runtime._DeliveryToolBridge(runtime, back)
    asyncio.run(butler._ResidentBatchConnector(bridge, runtime, {
        "repository_project": "Project", "repository_name": "repository"}).create_customer_pr(
        "repo", {"source_branch": "reviewed/x", "target_branch": "customer/y"},
        "body", "operation"))
    assert [call[1] for call in runtime.calls] == ["ado_pull_request_create"]
    assert runtime.policy_gate is original_gate
    assert not back.source_intake_poller._writeback_grants

    denied_runtime = Runtime()
    denied_back = backend(tmp_path, [denied_runtime], active=False)
    denied_runtime.policy_gate = denied_back.source_intake_poller._writeback_policy
    original_denied_gate = denied_runtime.policy_gate
    denied_bridge = workflow_runtime._DeliveryToolBridge(denied_runtime, denied_back)
    with pytest.raises(butler.ConnectorDenied, match="requires active runtime"):
        asyncio.run(denied_bridge._delivery_tool(
            denied_runtime, "ado_pull_request_create", {"project": "Project"}, mutate=True))
    assert denied_runtime.policy_gate is original_denied_gate
    assert denied_runtime.calls == []
    assert not denied_back.source_intake_poller._writeback_grants


def test_mutation_is_denied_if_existing_policy_gate_is_absent(tmp_path):
    runtime = Runtime()
    back = backend(tmp_path, [runtime])
    bridge = workflow_runtime._DeliveryToolBridge(runtime, back)
    with pytest.raises(butler.ConnectorDenied, match="existing connector policy gate"):
        asyncio.run(bridge._delivery_tool(
            runtime, "ado_pull_request_create", {"project": "Project"}, mutate=True))
    assert runtime.calls == []
    assert not back.source_intake_poller._writeback_grants
