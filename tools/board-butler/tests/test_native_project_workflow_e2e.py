"""Disposable Central workflow-to-delivery exercise; no real credentials or PRs."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT / "tools" / "board-butler"))

import board_butler as butler
from pursers_client.project_workflow import (
    final_validation_ticket_id, parse_final_validation_packet_index,
    parse_project_workflow, policy_digest,
)
from workflow_delivery import workflow_delivery_attestation

for name in ("integration_delivery", "workflow_delivery"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "board-butler" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
import integration_delivery as delivery_runtime

BOARD = "pursers"
NOW = butler.datetime(2026, 10, 25, tzinfo=butler.timezone.utc)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


class _PrConnector:
    """Approved connector boundary fake; Git and delivery runtime remain real."""
    def __init__(self):
        self.prs = []
        self.create_calls = 0

    async def list_customer_prs(self, repository, target_branch, status):
        return [p for p in self.prs if p["target_branch"] == target_branch and p["status"] == status]

    async def create_customer_pr(self, repository, payload, body, operation_id):
        self.create_calls += 1
        pr = {"id": self.create_calls, "status": "active", "mutable": False,
              "source_sha": payload["snapshot_sha"], "source_branch": payload["source_branch"],
              "target_branch": payload["target_branch"], "correlation": payload["correlation"]}
        self.prs.append(pr)
        return {"status": "confirmed", "pr": pr}

    async def reconcile_operation(self, repository, reservation):
        return {"status": "unknown"}

    async def get_customer_pr(self, repository, pr_id):
        return next(row for row in self.prs if row["id"] == pr_id)


def _git_runtime(tmp_path: Path):
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--initial-branch=main")
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.name", "Disposable QA")
    _git(seed, "config", "user.email", "qa@example.invalid")
    (seed / "README").write_text("baseline\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "baseline")
    base = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin", "HEAD:refs/heads/main")
    branches, shas = {}, {}
    for ticket, filename in (("TK-workflow-foundation", "foundation.txt"),
                             ("TK-workflow-feature", "feature.txt")):
        branch = f"workflow/{ticket}"
        _git(seed, "switch", "-c", branch, "main")
        (seed / filename).write_text(f"accepted {ticket}\n")
        _git(seed, "add", filename)
        _git(seed, "commit", "-m", f"approved {ticket}")
        shas[ticket] = _git(seed, "rev-parse", "HEAD")
        branches[ticket] = branch
        _git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
    _git(seed, "switch", "main")
    _git(seed, "push", "origin", "main:refs/heads/customer/review")
    clone = tmp_path / "delivery-clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    connector = _PrConnector()
    adapter = delivery_runtime.VerifiedGitConnectorAdapter(
        clone, str(bare), connector, validation_commands=["git status --porcelain"])
    runtime = delivery_runtime.BatchDeliveryRuntime(
        delivery_runtime.BatchLedger(tmp_path / "batch-ledger.json"), adapter)
    policy = {"mode": "batch_pr", "repository": str(bare), "base_branch": "main",
        "integration_branch": "pursers/workflow-integration", "snapshot_prefix": "pursers/snapshots",
        "target_branch": "customer/review", "trigger": "ready", "snapshot_strategy": "frozen",
        "policy_revision": "f" * 40}
    return runtime, policy, connector, clone, base, branches, shas


def _native_ticket(ticket):
    """Build the explicit current-detail fields consumed by the client contract.

    Full Central evidence returns retained histories; this mirrors the native
    decision input without replacing those histories or inventing verdicts.
    """
    row = dict(ticket)
    submissions = row.get("submission_history", [])
    verdicts = row.get("review_history", [])
    if submissions:
        row["latest_submission"] = submissions[-1]
    if verdicts:
        row["latest_verdict"] = verdicts[-1]
    return row


def _workflow_delivery_from_ticket(ticket, packet_index):
    """Hydrate the exact paged QA identity from actual Central annotations."""
    pages = [row.get("text") for row in ticket.get("annotations", [])
             if isinstance(row, dict) and isinstance(row.get("text"), str)
             and row["text"].startswith("butler-workflow-packet:v1:")]
    found = {}
    for page in pages:
        match = re.fullmatch(r"butler-workflow-packet:v1:([0-9a-f]{64}):(\d+)/(\d+):(.*)", page, re.S)
        assert match, "Central retained a malformed native QA evidence page"
        digest, index, count, chunk = match.groups()
        assert digest == packet_index["evidence_packet"]["sha256"]
        assert int(count) == packet_index["evidence_packet"]["chunks"]
        assert int(index) not in found
        found[int(index)] = chunk
    assert set(found) == set(range(1, packet_index["evidence_packet"]["chunks"] + 1))
    encoded = "".join(found[index] for index in sorted(found))
    assert hashlib.sha256(encoded.encode()).hexdigest() == packet_index["evidence_packet"]["sha256"]
    payload = json.loads(encoded)
    assert isinstance(payload.get("workflow_delivery"), dict)
    return payload["workflow_delivery"]


def _workflow():
    raw = {"schema_version": 1, "workflow_id": "workflow:native-e2e",
        "project_id": "NativeE2E", "board_id": BOARD, "cohort_id": "cohort:native-e2e",
        "sealed": True, "nodes": [
            {"node_id": "foundation", "ticket_id": "TK-workflow-foundation", "requires": [],
             "acceptance_checks": ["independent exact-SHA review"]},
            {"node_id": "feature", "ticket_id": "TK-workflow-feature", "requires": ["foundation"],
             "acceptance_checks": ["independent exact-SHA review"]}],
        "final_validation": {"title": "Validate the complete native cohort",
            "acceptance_checks": ["Run actual checks on the exact combined integration head"]}}
    normalized = parse_project_workflow(raw, project_id="NativeE2E", board_id=BOARD)
    raw_activation = {"schema_version": 1, "state": "active",
        "policy_sha256": policy_digest(normalized), "activation_id": "operator:fixture-approval"}
    return raw, raw_activation


def test_disposable_central_coordinator_workflow_to_one_qa_gated_pr(tmp_path, monkeypatch):
    """Real Central mutations/projections and Git orchestration, fake only PR transport.

    The delivery factory is the agreed narrow test seam.  This deliberately
    asserts that the native reconciliation path actually calls it: merely
    patching an unused binding must not make the end-to-end gate pass.
    """
    import pursers_central.central as central

    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys":[]}')
    for key, value in {"CENTRAL_AUTH_MODE": "jwt", "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp", "CENTRAL_JWKS_PATH": str(jwks),
        "CENTRAL_ADMISSION": "invite", "STORE_BACKEND": "sqlite"}.items():
        monkeypatch.setenv(key, value)
    mcp, service = central.build_server("localhost", 8765, tmp_path / "central")
    principal = lambda name, caps: central.Principal("PR-" + name, name, frozenset(caps))
    admin = principal("admin", {"board:read", "board:write", "board:review", "board:coordinate", "board:intake"})
    coordinator = principal("coordinator", {"board:read", "board:write", "board:coordinate", "board:intake"})
    worker = principal("worker", {"board:read", "board:write"})
    reviewer = principal("reviewer", {"board:read", "board:write", "board:review"})
    current = {"principal": admin}
    monkeypatch.setattr(central, "current_principal", lambda: current["principal"])
    # A synthetic credential exists only in this disposable Central instance;
    # mimic the authenticated proof boundary for raw in-process tool calls.
    fixture_token = "disposable-central-test-token-never-reused"
    monkeypatch.setattr(central, "get_access_token", lambda: SimpleNamespace(token=fixture_token))
    now = NOW

    call_errors = []

    async def call(who, tool, **args):
        if tool == "ticket_create":
            call_errors.append(("raw_ticket_create", args))
        current["principal"] = who
        if tool == "ticket_submit" and "branch_and_commit" in args.get("notes", ""):
            match = __import__("re").search(r"(?m)^branch_and_commit: (.+?) @ ([0-9a-f]{40})$", args["notes"])
            assert match
            branch, sha = match.groups()
            preflight = {"kind": "git-ls-remote-exact-tip-v1", "branch": branch,
                "commit": sha, "remote_ref": f"origin/{branch}", "remote_tip": sha}
            proof_payload = json.dumps({"board_id": BOARD, "ticket_id": args["ticket_id"],
                "agent_name": args["agent_name"], **preflight}, sort_keys=True, separators=(",", ":")).encode()
            args["submission_preflight"] = {**preflight,
                "proof": hmac.new(fixture_token.encode(), proof_payload, hashlib.sha256).hexdigest()}
        try:
            result = await mcp.call_tool(tool, {"board_id": BOARD, **args})
        except Exception as exc:
            call_errors.append((tool, type(exc).__name__, str(exc)))
            raise
        if result.is_error:
            call_errors.append((tool, result.structured_content, result.content))
            raise AssertionError((tool, result.structured_content, result.content))
        return result.structured_content

    async def scenario():
        await call(admin, "board_join", agent_name="admin")
        for who, role in ((coordinator, "member"), (worker, "member"), (reviewer, "reviewer")):
            service.mutate(BOARD, lambda doc, who=who, role=role: doc["principal_memberships"].update({
                who.principal_id: {"principal_id": who.principal_id, "role": role,
                    "source": "disposable-test", "created_at": now.isoformat()}}))
        await call(coordinator, "board_join", agent_name="fixture-coordinator", role="coordinator",
                   capabilities={"can_work": False, "can_review": False})
        worker_join = await call(worker, "board_join", agent_name="fixture-worker", role="worker",
            capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        await call(reviewer, "board_join", agent_name="fixture-reviewer", role="reviewer",
            capabilities={"can_work": False, "can_review": True, "tier_max": 3, "max_parallel": 1})
        workflow, activation = _workflow()
        runtime, delivery_policy, connector, clone, base, branches, shas = _git_runtime(tmp_path)
        # Keep the protected clone, runtime adapter and registered repository
        # route pointed at the same disposable bare remote.
        repo_url = str((tmp_path / "origin.git").resolve())
        project = SimpleNamespace(name="NativeE2E", board_id=BOARD,
            butler_workflow=workflow, butler_workflow_activation=activation,
            fleet_clone_dir=str(clone), work_dir=str(clone), work_dir_owner="fleet",
            repository_url=repo_url)
        registry = {"schema_version": 1, "projects": {"NativeE2E": {
            "board_id": BOARD, "status": "active", "fleet": True,
            "work_dir": str(clone), "fleet_clone_dir": str(clone),
            "work_dir_owner": "fleet", "repository_url": repo_url}}}
        await call(admin, "board_state_update", agent_name="admin", key="project_registry",
            value=json.dumps(registry))
        backend = butler.CentralBackend(SimpleNamespace(runtime_mode="active"), "disposable")
        called = []

        async def delivery_factory(_project):
            called.append(True)
            return runtime, delivery_policy
        backend._workflow_delivery_runtime = delivery_factory

        @asynccontextmanager
        async def client_for_board(board):
            assert board == BOARD
            class Client:
                async def ticket_update(self, ticket_id, **kwargs):
                    return await call(coordinator, "ticket_update", agent_name="fixture-coordinator", ticket_id=ticket_id, **kwargs)
                async def ticket_create(self, ticket_id, title, **kwargs):
                    # Pass the actual runtime formatter output to Central's real
                    # SDK validator. In particular, do not conceal an accidental
                    # JSON-object description by stringifying it in this fixture.
                    assert isinstance(kwargs.get("description"), str)
                    return await call(coordinator, "ticket_create", agent_name="fixture-coordinator",
                        ticket_id=ticket_id, title=title, **kwargs)
                async def ticket_annotate(self, ticket_id, text, **kwargs):
                    return await call(admin, "ticket_annotate", ticket_id=ticket_id,
                        text=text, agent_name="admin", **kwargs)
                async def ticket_history_list(self, ticket_id, history, **kwargs):
                    return await call(admin, "ticket_history_list", ticket_id=ticket_id,
                        history=history, agent_name="admin", **kwargs)
            yield Client()
        backend._client_for_board = client_for_board

        tickets = {}
        for ticket_id in ("TK-workflow-foundation", "TK-workflow-feature"):
            await call(coordinator, "ticket_create", agent_name="fixture-coordinator",
                ticket_id=ticket_id, title=f"Implement {ticket_id}",
                description="Ordinary coordinator-created workflow member.", scope="interactive",
                target_url="NativeE2E", required_fields=["branch_and_commit", "test_output"], unassigned=True)
            tickets[ticket_id] = _native_ticket((await call(admin, "ticket_get", ticket_id=ticket_id, view="full"))["ticket"])
        async with backend._client_for_board(BOARD) as fixture_client:
            # Use native Central authorization; workflow release will unpark it again.
            await fixture_client.ticket_update("TK-workflow-feature", parked=True)
        # Reconciliation consumes the exact current hydrated projection, not the
        # stale pre-park create response.
        tickets["TK-workflow-feature"] = _native_ticket(
            (await call(admin, "ticket_get", ticket_id="TK-workflow-feature", view="full"))["ticket"]
        )
        foundation = "TK-workflow-foundation"
        await call(admin, "ticket_assign", agent_name="admin", ticket_id=foundation,
            assigned_to_agent_id=worker_join["agent_id"], expected_status="open",
            coordinator_op_key="assign-" + foundation, reason="Disposable workflow test")
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id=foundation)
        sha = shas[foundation]
        notes = f"branch_and_commit: {branches[foundation]} @ {sha}\ncommit_hash: {sha}\ntest_output: source checks passed"
        await call(worker, "ticket_submit", agent_name="fixture-worker", ticket_id=foundation,
            summary="Source validated", notes=notes, files_changed=["foundation.txt"], stay_active=False)
        await call(reviewer, "ticket_review", agent_name="fixture-reviewer", ticket_id=foundation,
            verdict="approve", review_notes="Independent exact-commit fixture review")
        tickets[foundation] = _native_ticket(
            (await call(admin, "ticket_get", ticket_id=foundation, view="full"))["ticket"]
        )

        from pursers_client.project_workflow import accepted_candidate
        assert accepted_candidate(tickets[foundation]) == (True, shas[foundation]), json.dumps({k: tickets[foundation].get(k) for k in ("status", "latest_submission", "latest_verdict", "review_verdict", "review_history")}, default=str)
        first = await backend._reconcile_project_workflows(BOARD, [project], tickets, registry=registry)
        feature = "TK-workflow-feature"
        tickets[feature] = _native_ticket(
            (await call(admin, "ticket_get", ticket_id=feature, view="full"))["ticket"]
        )
        assert tickets[feature].get("parked") is False, first
        await call(worker, "board_join", agent_name="fixture-worker", role="worker",
            capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id=feature)
        sha = shas[feature]
        notes = f"branch_and_commit: {branches[feature]} @ {sha}\ncommit_hash: {sha}\ntest_output: source checks passed"
        await call(worker, "ticket_submit", agent_name="fixture-worker", ticket_id=feature,
            summary="Source validated", notes=notes, files_changed=["feature.txt"], stay_active=False)
        await call(reviewer, "ticket_review", agent_name="fixture-reviewer", ticket_id=feature,
            verdict="approve", review_notes="Independent exact-commit fixture review")
        tickets[feature] = _native_ticket(
            (await call(admin, "ticket_get", ticket_id=feature, view="full"))["ticket"]
        )
        first = await backend._reconcile_project_workflows(BOARD, [project], tickets, registry=registry)
        assert called, "native reconciliation did not use the agreed delivery runtime factory"
        from pursers_client.project_workflow import final_validation_ticket_id
        qa_id = final_validation_ticket_id(parse_project_workflow(
            workflow, project_id=project.name, board_id=BOARD))
        assert qa_id in service.load(BOARD)["tickets"], json.dumps({"actions": first, "tool_errors": call_errors}, default=str)
        qa = _native_ticket((await call(admin, "ticket_get", ticket_id=qa_id, view="full"))["ticket"])
        assert qa["status"] == "open"
        qa_index = parse_final_validation_packet_index(qa["description"])

        # Carry the actual controller-created immutable QA packet through native
        # event preflight. Use a second verified protected clone whose advertised
        # origin is HTTPS, as in the registered fleet; the source clone and all
        # delivery refs remain disposable test fixtures.
        import runpy
        from pursers_client.project_workflow import final_validation_packet
        protected_root = tmp_path / "qa-fleet" / "clones"
        protected_root.mkdir(parents=True)
        protected_clone = protected_root / "NativeE2E"
        _git(tmp_path, "clone", str(clone), str(protected_clone))
        registered_origin = "https://example.invalid/NativeE2E.git"
        _git(protected_clone, "remote", "set-url", "origin", registered_origin)
        preflight_registry = {"schema_version": 1, "projects": {"NativeE2E": {
            **registry["projects"]["NativeE2E"],
            "work_dir": str(protected_clone), "fleet_clone_dir": str(protected_clone),
            "repository_url": registered_origin,
            "butler_workflow": workflow, "butler_workflow_activation": activation,
        }}}
        seat_api = runpy.run_path(str(ROOT / "tools" / "seat-kit" / "event_seat.py"))
        preflight = seat_api["resolve_event_context"](
            preflight_registry, BOARD, qa, str(protected_root), "fixture-worker")
        assert preflight["branch"] == f"pursers/{qa_id}"
        assert preflight["head"] == qa_index["combined_revision"]
        assert preflight["head"] != base
        assert _git(Path(preflight["worktree"]), "rev-parse", "HEAD") == qa_index["combined_revision"]

        prepared = _workflow_delivery_from_ticket(qa, qa_index)
        assert prepared["combined_revision"] == qa_index["combined_revision"]
        assert qa_index["project_id"] == project.name
        assert qa_index["board_id"] == BOARD
        assert len(runtime.ledger.document["batches"]) == 1
        batch = next(iter(runtime.ledger.document["batches"].values()))
        assert qa_index["combined_revision"] == batch["working_head"]
        combined = batch["working_head"]
        assert combined not in shas.values()
        assert _git(clone, "show", f"{combined}:foundation.txt").startswith("accepted")
        assert _git(clone, "show", f"{combined}:feature.txt").startswith("accepted")
        assert connector.prs == []

        # Source submissions close the worker's active lease; rejoin as the
        # same principal before assigning the independently gated QA ticket.
        await call(worker, "board_join", agent_name="fixture-worker", role="worker",
            capabilities={"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1})
        # A detached legacy QA hold must be classified as narrowly recoverable
        # by Butler's read-only evidence projection, then worker preflight alone
        # performs the safe ticket-branch attachment without changing its SHA.
        import importlib.util
        legacy_root = tmp_path / "legacy-fleet" / "clones"
        legacy_root.mkdir(parents=True)
        legacy_clone = legacy_root / "NativeE2E"
        _git(tmp_path, "clone", str(clone), str(legacy_clone))
        registered_origin = "https://example.invalid/NativeE2E.git"
        _git(legacy_clone, "remote", "set-url", "origin", registered_origin)
        legacy_registry = {"schema_version": 1, "projects": {"NativeE2E": {
            **registry["projects"]["NativeE2E"],
            "work_dir": str(legacy_clone), "fleet_clone_dir": str(legacy_clone),
            "repository_url": registered_origin,
            "butler_workflow": workflow, "butler_workflow_activation": activation,
        }}}
        legacy_ticket = {**qa, "target_url": registered_origin}
        legacy_workspace = tmp_path / "legacy-fleet" / "worktrees" / "NativeE2E" / qa_id
        # Reproduce the real legacy state: generic provisioning left the exact
        # deterministic final-QA branch at the protected old main ancestor.
        legacy_branch = f"pursers/{qa_id}"
        _git(legacy_clone, "branch", legacy_branch, base)
        _git(legacy_clone, "worktree", "add", "--detach", str(legacy_workspace), combined)
        evidence_spec = importlib.util.spec_from_file_location(
            "native_e2e_workspace_evidence", ROOT / "tools" / "board-butler" / "workspace_evidence.py")
        evidence_module = importlib.util.module_from_spec(evidence_spec)
        sys.modules[evidence_spec.name] = evidence_module
        evidence_spec.loader.exec_module(evidence_module)
        before_legacy = _git(legacy_clone, "worktree", "list", "--porcelain")
        evidence = evidence_module.project_workspace_evidence(
            registry=legacy_registry, board_id=BOARD, project_id="NativeE2E", ticket=legacy_ticket)
        assert evidence["status"] == "verified"
        assert evidence["branch"] == ""
        assert evidence["expected_branch"] == f"pursers/{qa_id}"
        assert evidence["final_validation_status"] == "recoverable_detached_exact_combined_sha"
        assert evidence["head"] == combined
        assert _git(legacy_clone, "worktree", "list", "--porcelain") == before_legacy
        assert _git(legacy_clone, "rev-parse", legacy_branch) == base
        assert _git(legacy_clone, "rev-parse", "refs/remotes/origin/main") == base
        legacy_api = runpy.run_path(str(ROOT / "tools" / "seat-kit" / "event_seat.py"))
        recovered = legacy_api["resolve_event_context"](
            legacy_registry, BOARD, legacy_ticket, str(legacy_root), "fixture-worker")
        assert recovered["branch"] == f"pursers/{qa_id}"
        assert recovered["head"] == combined
        assert _git(legacy_clone, "rev-parse", legacy_branch) == combined
        assert _git(legacy_clone, "rev-parse", "refs/remotes/origin/main") == base
        assert _git(Path(recovered["worktree"]), "rev-parse", "HEAD") == combined

        await call(admin, "ticket_assign", agent_name="admin", ticket_id=qa_id,
            assigned_to_agent_id=worker_join["agent_id"], expected_status="open",
            coordinator_op_key="assign-" + qa_id, reason="QA fixture assignment")
        await call(worker, "ticket_claim", agent_name="fixture-worker", ticket_id=qa_id)
        qa_commit = combined
        attestation = workflow_delivery_attestation(prepared)
        qa_notes = (f"branch_and_commit: pursers/native-qa @ {qa_commit}\n"
                    f"commit_hash: {qa_commit}\ntest_output: exact combined SHA {combined}; all checks passed\n"
                    f"{attestation}")
        await call(worker, "ticket_submit", agent_name="fixture-worker", ticket_id=qa_id,
            summary="Exact combined cohort checks passed", notes=qa_notes,
            files_changed=["validation-report.txt"], stay_active=False)
        await call(reviewer, "ticket_review", agent_name="fixture-reviewer", ticket_id=qa_id,
            verdict="approve", review_notes=f"validated_sha: {combined}\n"
                f"Independent review of the exact prepared combined SHA\n{attestation}")
        qa = _native_ticket((await call(admin, "ticket_get", ticket_id=qa_id, view="full"))["ticket"])
        assert qa["status"] == "closed"
        assert qa["submission_history"][-1]["submitted_by_principal_id"] == worker.principal_id
        assert qa["review_history"][-1]["reviewed_by_principal_id"] == reviewer.principal_id
        assert qa["review_history"][-1]["status_to"] == "closed"
        assert qa["review_history"][-1]["submission_commit"] == qa["submission_history"][-1]["commit_hash"]
        assert qa["latest_submission"] == qa["submission_history"][-1]
        assert qa["latest_verdict"] == qa["review_history"][-1]
        second = await backend._reconcile_project_workflows(BOARD, [project], {**tickets, qa_id: qa}, registry=registry)
        assert len(connector.prs) == 1, (first, second)
        assert connector.prs[0]["source_sha"] == combined
        assert connector.prs[0]["target_branch"] == "customer/review"
        refs_before = {ref: _git(clone, "rev-parse", f"refs/remotes/origin/{ref}")
            for ref in ("main", "customer/review")}
        await backend._reconcile_project_workflows(BOARD, [project], {**tickets, qa_id: qa}, registry=registry)
        assert len(connector.prs) == 1
        assert refs_before == {ref: _git(clone, "rev-parse", f"refs/remotes/origin/{ref}")
            for ref in refs_before}, "workflow delivery changed the environment/base refs"
        assert base == refs_before["main"] == refs_before["customer/review"]

    asyncio.run(scenario())


def test_resident_refresh_binds_real_registry_before_source_ownership(tmp_path, monkeypatch):
    """Exercise coordinator.read_cycle through native refresh, not injected registry state."""
    import pursers_central.central as central

    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys":[]}')
    for key, value in {"CENTRAL_AUTH_MODE": "jwt", "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp", "CENTRAL_JWKS_PATH": str(jwks),
        "CENTRAL_ADMISSION": "invite", "STORE_BACKEND": "sqlite"}.items():
        monkeypatch.setenv(key, value)
    mcp, _service = central.build_server("localhost", 8765, tmp_path / "central")
    admin = central.Principal("PR-refresh-admin", "refresh-admin",
        frozenset({"board:read", "board:write", "board:review", "board:coordinate", "board:intake"}))
    current = {"principal": admin}
    monkeypatch.setattr(central, "current_principal", lambda: current["principal"])
    fixture_token = "disposable-central-refresh-test-token-never-reused"
    monkeypatch.setattr(central, "get_access_token", lambda: SimpleNamespace(token=fixture_token))

    async def call(tool, **args):
        current["principal"] = admin
        result = await mcp.call_tool(tool, {"board_id": BOARD, **args})
        assert not result.is_error, (tool, result.structured_content, result.content)
        return result.structured_content

    async def scenario():
        await call("board_join", agent_name="refresh-admin")
        workflow = {"schema_version": 1, "workflow_id": "workflow:resident-refresh",
            "project_id": "NativeRefresh", "board_id": BOARD, "cohort_id": "cohort:resident-refresh",
            "sealed": True, "nodes": [{"node_id": "member", "ticket_id": "TK-refresh-member",
                "requires": [], "acceptance_checks": ["independent exact-SHA review"]}],
            "final_validation": {"title": "Validate resident workflow", "acceptance_checks": ["Run exact-head QA"]}}
        parsed = parse_project_workflow(workflow, project_id="NativeRefresh", board_id=BOARD)
        activation = {"schema_version": 1, "state": "active", "policy_sha256": policy_digest(parsed),
            "activation_id": "operator:resident-refresh-approval"}
        work_dir = tmp_path / "registered-worktree"
        work_dir.mkdir()
        registry = {"schema_version": 1, "projects": {"NativeRefresh": {
            "board_id": BOARD, "status": "active", "fleet": True,
            "work_dir": str(work_dir), "fleet_clone_dir": str(work_dir),
            "work_dir_owner": "fleet", "butler_workflow": workflow,
            "butler_workflow_activation": activation}}}
        await call("board_state_update", agent_name="refresh-admin", key="project_registry",
            value=json.dumps(registry))
        await call("ticket_create", agent_name="refresh-admin", ticket_id="TK-refresh-member",
            title="Actual registered resident workflow member",
            description="Disposable Central resident-refresh fixture.", scope="interactive",
            target_url="NativeRefresh/src/example.py",
            required_fields=["branch_and_commit", "test_output"], unassigned=True)
        actual_ticket = (await call("ticket_get", ticket_id="TK-refresh-member", view="full"))["ticket"]

        # These are ancillary read-only resident phases; the registry and board
        # snapshot themselves, the coordinator's read_cycle and native workflow
        # reconciliation remain real.
        args = SimpleNamespace(runtime_mode="active", url="http://localhost:8765/mcp", token_path=tmp_path / "token",
            home_board=BOARD, agent_name="refresh-test", dry_run=False, act_on_board=[BOARD],
            active_action=[], no_live_candidates_cycles=3, repo=ROOT)
        backend = butler.CentralBackend(args, "disposable")
        api = backend._coordinator_api()
        real_read_cycle = api["read_cycle"]

        class DisposableCentralRawReader:
            def __init__(self, *_args):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_exc):
                return None
            async def call(self, name, board_id, **arguments):
                return await call(name, board_id=board_id, **arguments)

        api["RawReader"] = DisposableCentralRawReader
        api["run"] = lambda _args: asyncio.sleep(0)
        assert api["read_cycle"] is real_read_cycle

        @asynccontextmanager
        async def client_for_board(board_id):
            assert board_id == BOARD
            class Client:
                async def ticket_get(self, ticket_id, **kwargs):
                    try:
                        return await call("ticket_get", ticket_id=ticket_id, **kwargs)
                    except Exception as exc:
                        if "ticket not found" in str(exc).casefold():
                            from pursers_client import BoardClientError
                            raise BoardClientError("ticket not found") from exc
                        raise
            yield Client()
        backend._client_for_board = client_for_board

        async def no_onboard(_previous, _now):
            return []
        async def no_fleet(_boards, _snapshots, _now):
            return {"status": "disabled"}
        async def empty_observation(board_id, _snapshot, now, **_kwargs):
            return butler.ObservationContext(board_id=board_id, tickets={}, questions=(), now=now)
        async def no_findings(*_args, **_kwargs):
            return None
        backend._auto_onboard_unknown_projects = no_onboard
        backend._reconcile_fleet = no_fleet
        backend._observation_context_for_board = empty_observation
        backend._write_observation_findings = no_findings
        backend._write_source_intake_findings = no_findings
        async def no_mechanical_holds(*_args, **_kwargs):
            return None
        backend._reconcile_mechanical_holds = no_mechanical_holds
        backend._schedule_source_intake_refresh = lambda _now: {"status": "fixture-disabled"}
        backend._schedule_approval_scan = lambda _boards, _now: {"status": "fixture-disabled"}
        backend.source_intake_poller = SimpleNamespace(workflow_owned=None)
        reconciled_registries = []
        real_reconcile = backend._reconcile_project_workflows
        async def observe_reconcile(board_id, projects, tickets, *, registry=None):
            reconciled_registries.append(registry)
            return await real_reconcile(board_id, projects, tickets, registry=registry)
        backend._reconcile_project_workflows = observe_reconcile

        result = await backend.refresh_registry_findings(NOW)
        expected_registry = __import__("pursers_client.project_registry", fromlist=["parse_project_registry"]).parse_project_registry(
            {"state": {"value": json.dumps(registry)}})
        # Registry validation normalizes node lists to dictionaries, but the
        # native sealed-workflow parser consumes the original list shape. The
        # resident must preserve those raw policy fields after full validation.
        for name, raw_project in registry["projects"].items():
            for field in ("butler_workflow", "butler_workflow_activation"):
                if field in raw_project:
                    expected_registry["projects"][name][field] = raw_project[field]
        assert backend._workflow_registry == expected_registry, (
            "resident refresh failed to retain the authoritative Central registry for native reconciliation",
            result, reconciled_registries)
        assert reconciled_registries and all(row == expected_registry for row in reconciled_registries)
        assert backend.source_intake_poller.workflow_owned == backend._source_ticket_workflow_owned
        assert await backend.source_intake_poller.workflow_owned(BOARD, actual_ticket) is True
        # Confirm read_cycle consumed the live Central project state and board snapshot.
        projects, snapshots, previous = await real_read_cycle(DisposableCentralRawReader(), BOARD)
        assert [project.name for project in projects] == ["NativeRefresh"]
        assert "TK-refresh-member" in {row["ticket_id"] for row in snapshots[BOARD]["coordination_tickets"]}
        assert "__registry__" not in previous, "fixture unexpectedly manufactured the native fix"

    asyncio.run(scenario())
