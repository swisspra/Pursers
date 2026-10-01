from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT / "tools" / "fleet-dashboard"))
sys.path.insert(0, str(ROOT / "tools" / "wait-bridge"))
sys.path.insert(0, str(ROOT / "tools" / "board-butler"))

import board_butler
import fleet_dashboard as dashboard
import registry_admin
import registry_doctor
from pursers_client.project_registry import parse_project_registry
from pursers_client.delivery_workflow import activate_delivery_policy


RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "delivery_activation_runtime", ROOT / "tools" / "board-butler" / "integration_delivery.py"
)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
runtime_api = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime_api)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit(repo: Path, branch: str, name: str, content: str, base: str) -> str:
    git(repo, "switch", "-C", branch, base)
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", branch)
    sha = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    git(repo, "push", "origin", f"{sha}:refs/heads/{branch}")
    return sha


class FakePrTransport:
    def __init__(self) -> None:
        self.prs: list[dict[str, object]] = []
        self.create_calls = 0
        self.update_calls = 0

    async def list_customer_prs(self, repository, target_branch, status):
        return [
            dict(row)
            for row in self.prs
            if row["target_branch"] == target_branch and row["status"] == status
        ]

    async def create_customer_pr(self, repository, payload, body, operation_id):
        self.create_calls += 1
        pr = {
            "id": self.create_calls,
            "status": "active",
            "mutable": bool(payload["mutable"]),
            "source_sha": payload["snapshot_sha"],
            "source_branch": payload["source_branch"],
            "target_branch": payload["target_branch"],
            "correlation": payload["correlation"],
            "body": body,
        }
        self.prs.append(pr)
        return {"status": "confirmed", "pr": dict(pr)}

    async def update_customer_pr(self, repository, payload, body, operation_id):
        self.update_calls += 1
        raise AssertionError("frozen delivery must not update a customer PR")


def test_resident_connector_reads_full_ado_detail_before_matching_correlation():
    marker = "f" * 64
    full_description = "x" * 450 + f"\nPursers-Batch-Correlation: {marker}\n"
    raw = {
        "pullRequestId": 17,
        "status": "active",
        "description": full_description,
        "sourceRefName": "refs/heads/pursers/delivery/one",
        "targetRefName": "refs/heads/customer-review",
        "lastMergeSourceCommit": {"commitId": "a" * 40},
        "lastMergeTargetCommit": {"commitId": "b" * 40},
        "lastMergeCommit": {"commitId": "c" * 40},
        "isDraft": False,
        "mergeStatus": "succeeded",
        "reviewers": [],
        "repository": {
            "id": "repo-id", "name": "repo", "project": {"id": "project-id"},
        },
    }
    calls: list[str] = []

    async def delivery_tool(_runtime, name, _args, **_kwargs):
        calls.append(name)
        if name == "ado_pull_requests_list":
            return {"value": [{**raw, "description": full_description[:400]}], "count": 1}
        if name == "ado_pull_request_get":
            return raw
        raise AssertionError(name)

    poller = SimpleNamespace(_delivery_tool=delivery_tool)
    connector = board_butler._ResidentBatchConnector(
        poller, object(), {"repository_project": "sample", "repository_name": "repo"}
    )
    rows = asyncio.run(connector.list_customer_prs("ignored", "customer-review", "active"))
    assert calls == ["ado_pull_requests_list", "ado_pull_request_get"]
    assert rows[0]["correlation"] == marker
    reconciled = asyncio.run(connector.reconcile_operation("ignored", {
        "kind": "create_customer_pr",
        "payload": {"target_branch": "customer-review", "correlation": marker},
    }))
    assert reconciled["status"] == "confirmed"


def reviewed_member(ticket_id: str, branch: str, sha: str) -> dict[str, object]:
    return {
        "ticket_id": ticket_id,
        "source_ref": branch,
        "source_sha": sha,
        "issue_ids": ["ISSUE-" + ticket_id.rsplit("-", 1)[-1]],
        "summary": "Reviewed change " + ticket_id,
        "tests": "plain pytest passed",
        "blockers": "none",
        "baseline_failures": "none",
        "evidence": {
            "approved_sha": sha,
            "independent_review": True,
            "validation_passed": True,
        },
    }


def test_dashboard_activation_roundtrip_drives_one_frozen_customer_pr(tmp_path):
    origin = tmp_path / "origin.git"
    clone = tmp_path / "fleet-clone"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "test@example.invalid")
    git(clone, "config", "user.name", "Delivery E2E")
    (clone / "base.txt").write_text("base\n", encoding="utf-8")
    git(clone, "add", "base.txt")
    git(clone, "commit", "-m", "base")
    base = git(clone, "rev-parse", "--verify", "HEAD^{commit}")
    git(clone, "branch", "-M", "main")
    git(clone, "push", "origin", "main")
    git(clone, "push", "origin", f"{base}:refs/heads/customer-review")

    repository_url = "https://example.invalid/acme/repo.git"
    git(clone, "remote", "set-url", "origin", repository_url)
    git(clone, "config", f"url.file://{origin}.insteadOf", repository_url)
    first_sha = commit(clone, "reviewed/one", "one.txt", "one\n", base)
    second_sha = commit(clone, "reviewed/two", "two.txt", "two\n", base)
    third_sha = commit(clone, "reviewed/three", "three.txt", "three\n", base)

    registry = {
        "schema_version": 1,
        "projects": {
            "sample": {
                "board_id": "sample-board",
                "work_dir": str(tmp_path / "operator-checkout"),
                "work_dir_owner": "fleet",
                "fleet_clone_dir": str(clone),
                "fleet": True,
                "repository_url": repository_url,
                "integration_ref": "main",
                "status": "active",
                "domain": "work",
            }
        },
    }
    request = {
        "action": "delivery",
        "scope": "repository",
        "name": "sample",
        "activate": True,
        "delivery_policy": {
            "mode": "batch_pr",
            "mapped_base": "main",
            "integration_branch": "pursers-integration",
            "snapshot_branch_prefix": "pursers/delivery",
            "final_pr_target": "customer-review",
            "release_trigger": {"kind": "ready"},
            "pr_update": "freeze_on_ready",
            "auto_integrate": False,
            "final_merge": "manual",
            "validation": {
                "test_commands": [],
                "required_reviewers": 1,
                "independent_review": True,
                "require_upstream_policies": True,
            },
            "conflict_policy": "pause",
            "collection_paused": False,
        },
    }

    fetcher = object.__new__(dashboard.FleetFetcher)
    fetcher.config = SimpleNamespace(
        home_board="pursers", label="test", agent_name="operator"
    )
    fetcher._require_board_admin = AsyncMock()
    fetcher.fetch_project_registry = AsyncMock(
        return_value={"registry": registry, "expected_sha256": "a" * 64}
    )
    fetcher._delivery_observation = AsyncMock(
        return_value={"complete": True, "active_tickets": [], "pending_offers": []}
    )
    saved: list[dict[str, object]] = []

    async def save_registry(document, expected_sha256):
        assert expected_sha256 == "a" * 64
        saved.append(json.loads(json.dumps(document)))

    fetcher.save_project_registry = AsyncMock(side_effect=save_registry)
    plan = asyncio.run(fetcher.build_project_lifecycle_plan(request))
    assert not plan["blocked"]
    asyncio.run(fetcher.apply_project_lifecycle_plan(plan, None))
    assert len(saved) == 1

    serialized = json.dumps(saved[0], sort_keys=True)
    central_result = {"state": {"key": "project_registry", "value": serialized}}
    admin_registry = registry_admin.validate_registry(json.loads(serialized))
    doctor_registry = registry_doctor.parse_registry(central_result)
    client_registry = parse_project_registry(central_result)
    for reloaded in (admin_registry, doctor_registry, client_registry):
        row = reloaded["projects"]["sample"]
        assert row["delivery_policy"]["mode"] == "batch_pr"
        assert row["delivery_policy_activation"]["policy_revision"] == plan[
            "proposed_registry"
        ]["projects"]["sample"]["delivery_policy_activation"]["policy_revision"]

    class StateClient:
        async def board_state_get(self, key):
            assert key == "project_registry"
            return central_result

    @asynccontextmanager
    async def connection(board_id):
        assert board_id == "pursers"
        yield StateClient()

    backend = object.__new__(board_butler.CentralBackend)
    backend.args = SimpleNamespace(home_board="pursers")
    backend._client_for_board = connection
    project = asyncio.run(backend._source_project_reader("sample-board"))
    assert project is not None
    poller = object.__new__(board_butler.SourceIntakePoller)
    resolved_batch = poller._resolved_batch_policy(project)
    assert resolved_batch is not None
    resident_policy, resolved = resolved_batch
    assert project["delivery_policy_activation"]["policy_revision"] == resident_policy[
        "policy_revision"
    ]

    transport = FakePrTransport()
    adapter = runtime_api.VerifiedGitConnectorAdapter(clone, repository_url, transport)
    batch = runtime_api.BatchDeliveryRuntime(
        runtime_api.BatchLedger(tmp_path / "batch-ledger.json"), adapter
    )
    first = asyncio.run(
        batch.collect(resident_policy, reviewed_member("TK-one", "reviewed/one", first_sha))
    )
    second = asyncio.run(
        batch.collect(resident_policy, reviewed_member("TK-two", "reviewed/two", second_sha))
    )
    members = ["TK-one", "TK-two"]
    cohort = hashlib.sha256(
        json.dumps([second["batch_key"], members], separators=(",", ":")).encode()
    ).hexdigest()
    released = asyncio.run(
        batch.release(
            resident_policy,
            second["batch_key"],
            request={"cohort_id": cohort, "members": members},
        )
    )
    assert first["state"] == second["state"] == "integrated"
    assert released["state"] == "in_delivery"
    assert transport.create_calls == 1
    frozen = dict(transport.prs[0])

    third = asyncio.run(
        batch.collect(
            resident_policy, reviewed_member("TK-three", "reviewed/three", third_sha)
        )
    )
    next_release = asyncio.run(
        batch.release(
            resident_policy,
            third["batch_key"],
            request={"cohort_id": "ready-next", "members": ["TK-three"]},
        )
    )
    assert third["batch_key"] != second["batch_key"]
    assert next_release["reason"] == "customer_pr_slot_busy"
    assert transport.create_calls == 1 and transport.update_calls == 0
    assert transport.prs[0] == frozen
    assert git(clone, "ls-remote", "--heads", "origin", "refs/heads/main").split()[0] == base


def test_real_poller_releases_same_file_fence_after_integration_while_pr_is_open(tmp_path):
    from test_grouped_intake import Board, NOW, grouped_poller, issue

    origin = tmp_path / "origin.git"
    clone = tmp_path / "fleet-clone"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "test@example.invalid")
    git(clone, "config", "user.name", "Delivery Poller E2E")
    (clone / "base.txt").write_text("base\n", encoding="utf-8")
    git(clone, "add", "base.txt")
    git(clone, "commit", "-m", "base")
    base = git(clone, "rev-parse", "--verify", "HEAD^{commit}")
    git(clone, "branch", "-M", "main")
    git(clone, "push", "origin", "main")
    git(clone, "push", "origin", f"{base}:refs/heads/customer-review")
    git(clone, "push", "origin", f"{base}:refs/heads/pursers-integration")
    repository_url = "https://dev.azure.com/acme/sample/_git/repo"
    git(clone, "remote", "set-url", "origin", repository_url)
    git(clone, "config", f"url.file://{origin}.insteadOf", repository_url)
    git(clone, "switch", "-C", "reviewed/one", base)
    changed = clone / "src" / "module" / "file.ts"
    changed.parent.mkdir(parents=True)
    changed.write_text("reviewed\n", encoding="utf-8")
    git(clone, "add", "src/module/file.ts")
    git(clone, "commit", "-m", "reviewed one")
    reviewed = git(clone, "rev-parse", "--verify", "HEAD^{commit}")
    git(clone, "push", "origin", f"{reviewed}:refs/heads/reviewed/one")

    effective = {
        "mode": "batch_pr", "mapped_base": "main",
        "integration_branch": "pursers-integration",
        "snapshot_branch_prefix": "pursers/delivery",
        "final_pr_target": "customer-review",
        "release_trigger": {"kind": "ready"}, "pr_update": "freeze_on_ready",
        "auto_integrate": False, "final_merge": "manual",
        "validation": {"test_commands": [], "required_reviewers": 1,
                       "independent_review": True, "require_upstream_policies": True},
        "conflict_policy": "pause", "collection_paused": False,
    }
    project_row = {
        "board_id": "board-a", "integration_ref": "main", "status": "active",
        "domain": "work", "repository_url": repository_url,
        "fleet": True, "fleet_clone_dir": str(clone), "work_dir_owner": "fleet",
        "delivery_policy": effective,
        "delivery_policy_activation": activate_delivery_policy(effective),
    }
    registry = {"schema_version": 1, "projects": {"Alpha": project_row}}
    project = {
        **project_row, "__registry__": registry, "__project_name__": "Alpha",
    }

    board = Board()
    rows = [
        issue(0, project="alpha", component="alpha:src/module/file.ts"),
        issue(1, project="alpha", component="alpha:src/module/file.ts"),
    ]
    poller, _calls = grouped_poller(tmp_path, board, rows, [[0], [1]])
    poller.sources = (
        replace(poller.sources[0], writeback=SimpleNamespace(tool="ado_pull_request_create")),
    )
    first = asyncio.run(poller.run_cycle(NOW))
    assert first["new_asks"] == 1
    entry_key, entry = next(iter(poller.index.entries.items()))
    board.tickets[entry["ticket_id"]] = {
        "status": "closed", "latest_verdict": {"verdict": "approve"},
        "title": "Reviewed same-file repair",
        "latest_submission": {
            "branch": "reviewed/one", "commit_hash": reviewed,
            "test_output": "plain pytest passed",
        },
    }
    poller.project_reader = AsyncMock(return_value=project)
    prs: list[dict[str, object]] = []
    delivery_calls: list[str] = []

    async def delivery_tool(_runtime, name, args, **_kwargs):
        delivery_calls.append(name)
        if name == "ado_pull_requests_list":
            return {"value": [], "count": 0}
        if name == "ado_pull_request_create":
            snapshot = git(clone, "ls-remote", "--heads", "origin", args["sourceRefName"]).split()[0]
            row = {
                "pullRequestId": 41, "status": "active", "description": args["description"],
                "sourceRefName": args["sourceRefName"], "targetRefName": args["targetRefName"],
                "lastMergeSourceCommit": {"commitId": snapshot},
                "lastMergeTargetCommit": {"commitId": base}, "lastMergeCommit": {},
                "isDraft": False, "mergeStatus": "succeeded", "reviewers": [],
                "repository": {"id": "repo-id", "name": "repo",
                               "project": {"id": "project-id"}},
            }
            prs.append(row)
            return row
        if name == "ado_pull_request_get":
            return prs[0]
        raise AssertionError(name)

    poller._delivery_tool = delivery_tool
    second = asyncio.run(poller.run_cycle(NOW.replace(minute=NOW.minute + 1)))
    assert second["writebacks"] == 1
    assert second["new_asks"] == 1
    assert poller.index.entries[entry_key]["status"] == "delivered"
    assert poller.index.entries[entry_key]["delivery_state"] == "in_delivery"
    assert len(board.asks()) == 2
    assert len(prs) == 1 and prs[0]["status"] == "active"
    integration = git(
        clone, "ls-remote", "--heads", "origin", "refs/heads/pursers-integration"
    ).split()[0]
    assert integration != base
    assert git(clone, "merge-base", "--is-ancestor", reviewed, integration) == ""
    assert git(clone, "ls-remote", "--heads", "origin", "refs/heads/main").split()[0] == base
    assert "ado_pull_request_create" in delivery_calls
