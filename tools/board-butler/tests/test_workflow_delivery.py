"""Contract tests for sealed-cohort, exact-QA delivery wrapper."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
for name, path in (("integration_delivery", ROOT / "integration_delivery.py"),
                   ("workflow_delivery", ROOT / "workflow_delivery.py")):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)

import integration_delivery as runtime_module
import workflow_delivery as delivery


def run(coro):
    return asyncio.run(coro)


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, text=True,
                          capture_output=True).stdout.strip()


class Connector:
    def __init__(self):
        self.prs = []
        self.calls = []

    async def list_customer_prs(self, repository, target_branch, status):
        self.calls.append(("list", target_branch, status))
        return [p for p in self.prs if p["target_branch"] == target_branch and p["status"] == status]

    async def create_customer_pr(self, repository, payload, body, operation_id):
        self.calls.append(("create", payload["snapshot_sha"], payload["target_branch"]))
        pr = {"id": len(self.prs) + 1, "status": "active", "mutable": False,
              "description": body,
              "is_draft": payload.get("is_draft", False),
              "source_sha": payload["snapshot_sha"], "source_branch": payload["source_branch"],
              "target_branch": payload["target_branch"], "correlation": payload["correlation"]}
        self.prs.append(pr)
        return {"status": "confirmed", "pr": pr}

    async def reconcile_operation(self, repository, reservation):
        return {"status": "unknown"}

    async def get_customer_pr(self, repository, pr_id):
        return next(pr for pr in self.prs if pr["id"] == pr_id)


@pytest.fixture
def repo(tmp_path):
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--bare", "--initial-branch=main")
    work = tmp_path / "seed"
    git(tmp_path, "clone", str(bare), str(work))
    git(work, "config", "user.name", "QA Fixture")
    git(work, "config", "user.email", "qa@example.invalid")
    (work / "README").write_text("baseline\n")
    git(work, "add", "README")
    git(work, "commit", "-m", "baseline")
    base = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "HEAD:refs/heads/main")
    # Two accepted source branches are parallel children of main. Both labels
    # survive a real merge into the owned integration branch.
    shas = {}
    for name, text in (("TK-one", "origin-one\n"), ("TK-two", "origin-two\n")):
        git(work, "checkout", "-b", f"workflow/{name}", "main")
        (work / f"{name}.txt").write_text(text)
        git(work, "add", f"{name}.txt")
        git(work, "commit", "-m", f"approved {name}")
        shas[name] = git(work, "rev-parse", "HEAD")
        git(work, "push", "origin", f"HEAD:refs/heads/workflow/{name}")
    git(work, "checkout", "main")
    # A customer target is read-only to the runtime; it is not the base.
    git(work, "push", "origin", "main:refs/heads/customer/review")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(bare), str(clone))
    connector = Connector()
    adapter = runtime_module.VerifiedGitConnectorAdapter(
        clone, str(bare), connector, validation_commands=["git status --porcelain"])
    ledger = runtime_module.BatchLedger(tmp_path / "batch-ledger.json")
    rt = runtime_module.BatchDeliveryRuntime(ledger, adapter)
    return {"bare": bare, "clone": clone, "base": base, "shas": shas,
            "connector": connector, "adapter": adapter, "runtime": rt,
            "ledger_path": tmp_path / "batch-ledger.json"}


def workflow_for(head, *, member_ids=("TK-one", "TK-two")):
    raw = {"schema_version": 1, "board_id": "pursers", "project_name": "Fixture",
           "cohort_id": "fixture-cohort", "member_ticket_ids": list(member_ids),
           "nodes": {ticket: {"prerequisites": []} for ticket in member_ids},
           "final_validation": {"enabled": True, "combined_revision": head,
               "title": "Validate complete cohort", "scope": "Run the actual combined checks.",
               "required_fields": ["branch_and_commit", "test_output"],
               "checks": ["Run tests on the exact combined Git revision."]}}
    raw["activation"] = {"schema_version": 1, "state": "active", "activation_id": "activation-1",
        "policy_revision": hashlib.sha256(delivery._canonical(raw)).hexdigest()}
    normalized = {"schema_version": 1, "board_id": raw["board_id"],
        "project_name": raw["project_name"], "cohort_id": raw["cohort_id"],
        "member_ticket_ids": tuple(raw["member_ticket_ids"]),
        "nodes": {ticket: {"prerequisites": tuple(node["prerequisites"])}
                  for ticket, node in raw["nodes"].items()},
        "final_validation": {**raw["final_validation"],
            "required_fields": tuple(raw["final_validation"]["required_fields"]),
            "checks": tuple(raw["final_validation"]["checks"])},
        "activation_id": "activation-1", "policy_revision": raw["activation"]["policy_revision"]}
    return normalized


def delivery_policy(repo, mode="batch_pr"):
    return {"mode": mode, "repository": str(repo), "base_branch": "main",
        "integration_branch": "pursers/workflow-integration", "snapshot_prefix": "pursers/snapshots",
        "target_branch": "customer/review", "trigger": "ready",
        "snapshot_strategy": "frozen", "policy_revision": "f" * 40}


def members(shas):
    return [{"ticket_id": ticket, "source_ref": f"workflow/{ticket}", "source_sha": shas[ticket],
        "issue_ids": [], "summary": f"Reviewed {ticket}", "tests": "independent source checks passed",
        "blockers": "none", "baseline_failures": "none",
        "origin": "managed-source" if ticket == "TK-one" else "coordinator",
        "evidence": {"approved_sha": shas[ticket], "independent_review": True,
                     "validation_passed": True,
                     "source_origin": "managed-source" if ticket == "TK-one" else "coordinator"}}
        for ticket in ("TK-one", "TK-two")]


def qa_ticket(prepared, *, submission_marker=None, review_marker=None, commit=None,
              submitter="qa-worker", reviewer="independent-reviewer"):
    attestation = delivery.workflow_delivery_attestation(prepared)
    commit = commit or prepared["combined_revision"]
    submission = {"branch": "pursers/qa", "commit_hash": commit,
        "notes": submission_marker if submission_marker is not None else (
            f"branch_and_commit: platform/qa @ {commit}\n{attestation}"),
        "submitted_by_principal_id": submitter, "submitted_at": "2026-10-25T12:00:00+00:00"}
    review = {"verdict": "approve", "status_to": "closed", "submission_commit": commit,
        "review_notes": review_marker if review_marker is not None else attestation,
        "submitted_by_principal_id": submitter, "reviewed_by_principal_id": reviewer,
        "reviewed_at": "2026-10-25T12:01:00+00:00"}
    # These are the actual persisted Central record shapes. Deliberately omit
    # fabricated validated_sha/workflow_delivery verdict fields and ticket payload.
    return {"project": "Fixture", "status": "closed",
        "latest_submission": submission, "latest_verdict": review,
        "submission_history": [submission], "review_history": [review]}


def test_actual_disposable_central_records_attest_before_one_publication(repo, tmp_path, monkeypatch):
    """Exercise real Central writes, histories, and exact QA markers end-to-end."""
    central_src = ROOT.parents[1] / "packages" / "central" / "src"
    client_src = ROOT.parents[1] / "packages" / "client" / "src"
    sys.path.insert(0, str(client_src))
    sys.path.insert(0, str(central_src))
    import pursers_central.central as central

    jwks = tmp_path / "central-jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    for key, value in {
        "CENTRAL_AUTH_MODE": "jwt", "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
        "CENTRAL_JWKS_PATH": str(jwks), "CENTRAL_ADMISSION": "invite",
        "STORE_BACKEND": "sqlite",
    }.items():
        monkeypatch.setenv(key, value)
    server, service = central.build_server("localhost", 8765, tmp_path / "central-data")
    admin = central.Principal("PR-qa-admin", "qa-admin",
        frozenset({"board:read", "board:write", "board:review"}))
    worker = central.Principal("PR-qa-worker", "qa-worker", frozenset({"board:read", "board:write"}))
    reviewer = central.Principal("PR-qa-reviewer", "qa-reviewer",
        frozenset({"board:read", "board:write", "board:review"}))
    active = {"principal": admin}
    monkeypatch.setattr(central, "current_principal", lambda: active["principal"])
    token = "disposable-central-qa-token"
    monkeypatch.setattr(central, "get_access_token", lambda: SimpleNamespace(token=token))

    async def call(principal, tool, **args):
        active["principal"] = principal
        result = await server.call_tool(tool, {"board_id": "pursers", **args})
        assert not result.is_error, (tool, result)
        return result.structured_content

    async def scenario():
        await call(admin, "board_join", agent_name="qa-admin")
        for principal, membership, name, role, capabilities in (
            (worker, "member", "qa-worker-seat", "worker", {"can_work": True, "can_review": False}),
            (reviewer, "reviewer", "qa-reviewer-seat", "reviewer", {"can_work": False, "can_review": True}),
        ):
            await call(admin, "board_member_add", agent_name="qa-admin",
                principal_id=principal.principal_id, role=membership)
            await call(principal, "board_join", agent_name=name, role=role, capabilities=capabilities)
        await call(admin, "board_state_update", agent_name="qa-admin", key="project_registry",
            value=json.dumps({"schema_version": 1, "projects": {"Fixture": {
                "board_id": "pursers", "status": "active", "fleet": True,
                "repository_url": "https://fixture.invalid/repo"}}}))
        rt, policy, rows = repo["runtime"], delivery_policy(repo["bare"]), members(repo["shas"])
        workflow = workflow_for(None)
        prepared = await delivery.prepare_workflow_delivery(rt, policy, workflow, rows)
        marker = delivery.workflow_delivery_attestation(prepared)
        ticket_id = "TK-central-qa-attest"
        await call(admin, "ticket_create", agent_name="qa-admin", ticket_id=ticket_id,
            title="Exact combined workflow QA", description="Run complete workflow checks",
            target_url="Fixture/control/qa", scope="interactive-no-send",
            required_fields=["test_output", "branch_and_commit"], assigned_to="qa-worker-seat")
        await call(worker, "ticket_claim", agent_name="qa-worker-seat", ticket_id=ticket_id)
        notes = (f"branch_and_commit: platform/workflow-qa @ {prepared['combined_revision']}\n"
                 f"test_output: disposable real Git integration checks passed\n{marker}")
        preflight = {"kind": "git-ls-remote-exact-tip-v1", "branch": "platform/workflow-qa",
            "commit": prepared["combined_revision"], "remote_ref": "origin/platform/workflow-qa",
            "remote_tip": prepared["combined_revision"]}
        payload = json.dumps({"board_id": "pursers", "ticket_id": ticket_id,
            "agent_name": "qa-worker-seat", **preflight}, sort_keys=True, separators=(",", ":")).encode()
        preflight["proof"] = hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()
        submitted = await call(worker, "ticket_submit", agent_name="qa-worker-seat",
            ticket_id=ticket_id, summary="All checks passed on exact combined head", notes=notes,
            files_changed=["validation-report.txt"], test_output="disposable real Git checks passed",
            submission_preflight=preflight)
        assert submitted["ticket"]["submission_history"][-1]["commit_hash"] == prepared["combined_revision"]
        await call(reviewer, "ticket_review_claim", agent_name="qa-reviewer-seat", ticket_id=ticket_id)
        await call(reviewer, "ticket_review", agent_name="qa-reviewer-seat", ticket_id=ticket_id,
            verdict="approve", review_notes=f"{marker}\nIndependently verified full QA packet and exact head.")
        actual = (await call(admin, "ticket_get", agent_name="qa-admin", ticket_id=ticket_id,
                             view="full"))["ticket"]
        submission, review = actual["submission_history"][-1], actual["review_history"][-1]
        assert set(submission) >= {"commit_hash", "notes", "submitted_at", "submitted_by_principal_id", "submission_preflight"}
        assert set(review) >= {"submission_commit", "review_notes", "reviewed_at", "submitted_by_principal_id", "reviewed_by_principal_id", "verdict", "status_to"}
        assert not ({"validated_sha", "workflow_delivery"} & set(review))
        assert not any(key in review for key in ("validated_sha", "workflow_delivery"))
        assert (await delivery.publish_workflow_delivery(
            rt, policy, workflow, actual, prepared))["state"] == "in_delivery"
        assert len(repo["connector"].prs) == 1
        assert (await call(admin, "ticket_get", agent_name="qa-admin", ticket_id=ticket_id,
                           view="full"))["ticket"]["status"] == "closed"

    run(scenario())


def test_report_held_rejected_qa_can_publish_exactly_one_truthful_draft(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    workflow = workflow_for(None)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, members(repo["shas"])))
    evidence = qa_ticket(prepared)
    evidence["status"] = "needs_human"
    evidence["human_request"] = {"request_id": "HR-held", "resolution": None}
    evidence["latest_verdict"].update(verdict="reject", status_to="open",
                                       review_notes="Independent review rejected the run.")
    evidence["review_history"][-1] = evidence["latest_verdict"]
    report = {"report_reference": "Central QA ticket", "test_build_summary": "29 suites, 90 tests passed; held final audit",
              "full_failure_baseline": "17 existing lint findings", "advisories": "9 accepted sources",
              "va_status": "UNKNOWN", "ra_status": "NOT APPROVED"}

    first = run(delivery.publish_workflow_delivery(rt, policy, workflow, evidence, prepared,
        final_validation_delivery="draft_with_report", report=report))
    assert first["state"] == "report_held_draft" and first["is_draft"] is True
    assert len(repo["connector"].prs) == 1
    pr = repo["connector"].prs[0]
    assert pr["is_draft"] is True and pr["source_sha"] == prepared["combined_revision"]
    batch = rt.ledger.document["batches"][prepared["batch_key"]]
    assert batch["state"] == "draft_open" and batch["qa_status"] == "report_held"
    assert batch["security_status"] == "pending" and batch["delivery_ready"] is False
    assert batch["draft_report"]["full_failure_baseline"] == report["full_failure_baseline"]
    assert batch["draft_report"]["test_build_summary"] == report["test_build_summary"]
    body = repo["connector"].prs[0]["description"]
    assert "Quality status: REJECTED" in body
    assert "VA status: UNKNOWN. RA status: NOT APPROVED" in body
    assert "Manual boundary" in body

    restarted = runtime_module.BatchDeliveryRuntime(
        runtime_module.BatchLedger(repo["ledger_path"]), repo["adapter"])
    replay = run(delivery.publish_workflow_delivery(restarted, policy, workflow, evidence, prepared,
        final_validation_delivery="draft_with_report", report=report))
    assert replay["state"] == "report_held_draft" and len(repo["connector"].prs) == 1
    assert run(restarted.release(policy, prepared["batch_key"], request={
        "cohort_id": "fixture-cohort", "members": ["TK-one", "TK-two"]
    }))["reason"] == "report_held_draft_is_not_ready_for_promotion"


def test_draft_create_unknown_outcome_is_reconciled_without_duplicate_create(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    workflow = workflow_for(None)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, members(repo["shas"])))
    evidence = qa_ticket(prepared)
    evidence["status"] = "needs_human"
    evidence["human_request"] = {"request_id": "HR-held", "resolution": None}
    evidence["latest_verdict"].update(verdict="reject", status_to="open")
    evidence["review_history"][-1] = evidence["latest_verdict"]

    async def unknown(*_args, **_kwargs):
        repo["connector"].calls.append(("create-unknown",))
        raise TimeoutError("provider outcome unknown")

    repo["connector"].create_customer_pr = unknown
    first = run(delivery.publish_workflow_delivery(rt, policy, workflow, evidence, prepared,
        final_validation_delivery="draft_with_report", report={"quality_status": "rejected"}))
    assert first["reason"] == "draft_pr_create_outcome_unknown"
    replay = run(delivery.publish_workflow_delivery(rt, policy, workflow, evidence, prepared,
        final_validation_delivery="draft_with_report", report={"quality_status": "rejected"}))
    assert replay["reason"] == "draft_pr_create_outcome_unknown"
    assert [call for call in repo["connector"].calls if call[0] == "create-unknown"] == [("create-unknown",)]


def test_resident_prepare_reconciles_exact_unknown_draft_and_persists_restart_state(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    workflow = workflow_for(None)
    rows = members(repo["shas"])
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, rows))
    evidence = qa_ticket(prepared)
    evidence["status"] = "needs_human"
    evidence["human_request"] = {"request_id": "HR-held", "resolution": None}
    evidence["latest_verdict"].update(verdict="reject", status_to="open")
    evidence["review_history"][-1] = evidence["latest_verdict"]
    report = {"quality_status": "rejected", "full_failure_baseline": "full baseline " + "B" * 8_000,
              "test_build_summary": "full submitted QA output " + "T" * 9_000}
    create = repo["connector"].create_customer_pr

    async def remote_created_then_timeout(repository, payload, body, operation_id):
        await create(repository, payload, body, operation_id)
        raise TimeoutError("provider acknowledgement was lost")

    repo["connector"].create_customer_pr = remote_created_then_timeout
    first = run(delivery.publish_workflow_delivery(
        rt, policy, workflow, evidence, prepared,
        final_validation_delivery="draft_with_report", report=report))
    assert first["reason"] == "draft_pr_create_outcome_unknown"
    assert len(repo["connector"].prs) == 1
    repo["connector"].create_customer_pr = create

    async def confirm_exact_draft(repository, reservation):
        assert reservation["kind"] == "create_draft_customer_pr"
        return {"status": "confirmed", "pr": dict(repo["connector"].prs[0])}

    repo["connector"].reconcile_operation = confirm_exact_draft
    restarted = runtime_module.BatchDeliveryRuntime(
        runtime_module.BatchLedger(repo["ledger_path"]), repo["adapter"])
    # Same producer path the resident uses: collect indexed members, reconcile
    # the exact draft intent read-only, then persist its derived semantic state.
    replay_prepared = run(delivery.prepare_workflow_delivery(restarted, policy, workflow, rows))
    assert replay_prepared == prepared
    durable = runtime_module.BatchLedger(repo["ledger_path"])
    batch = durable.document["batches"][prepared["batch_key"]]
    assert batch["state"] == "draft_open"
    assert batch["customer_pr"]["is_draft"] is True
    assert next(row for row in batch["reservations"].values()
                if row["kind"] == "create_draft_customer_pr")["status"] == "confirmed"
    recovered = runtime_module.BatchDeliveryRuntime(durable, repo["adapter"])
    result = run(delivery.publish_workflow_delivery(
        recovered, policy, workflow, evidence, replay_prepared,
        final_validation_delivery="draft_with_report", report=report))
    assert result["state"] == "report_held_draft" and len(repo["connector"].prs) == 1
    assert sum(row.get("kind") == "create_draft_customer_pr"
               for row in batch["reservations"].values()) == 1


def test_strict_default_and_draft_identity_failures_create_no_pr(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    workflow = workflow_for(None)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, members(repo["shas"])))
    evidence = qa_ticket(prepared)
    evidence["status"] = "needs_human"
    evidence["human_request"] = {"request_id": "HR-held", "resolution": None}
    evidence["latest_verdict"].update(verdict="reject", status_to="open")
    evidence["review_history"][-1] = evidence["latest_verdict"]
    with pytest.raises(ValueError, match="not closed and approved"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow, evidence, prepared))
    with pytest.raises(ValueError, match="bound to the current ticket revision"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow,
            {**evidence, "latest_verdict": {**evidence["latest_verdict"], "submission_commit": "8" * 40}},
            prepared, final_validation_delivery="draft_with_report", report={"quality_status": "rejected"}))
    assert repo["connector"].prs == []


def test_draft_independent_identity_and_missing_qa_history_fail_closed(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    workflow = workflow_for(None)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, members(repo["shas"])))
    rejected = qa_ticket(prepared)
    rejected["status"] = "needs_human"
    rejected["human_request"] = {"request_id": "HR-held", "resolution": None}
    rejected["latest_verdict"].update(verdict="reject", status_to="open")
    rejected["review_history"][-1] = rejected["latest_verdict"]

    same_principal = json.loads(json.dumps(rejected))
    same_principal["review_history"][-1]["reviewed_by_principal_id"] = "qa-worker"
    same_principal["latest_verdict"] = same_principal["review_history"][-1]
    with pytest.raises(ValueError, match="independent current review"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow, same_principal, prepared,
            final_validation_delivery="draft_with_report", report={"quality_status": "rejected"}))

    no_review_history = {**rejected, "review_history": []}
    with pytest.raises(ValueError, match="independent current review"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow, no_review_history, prepared,
            final_validation_delivery="draft_with_report", report={"quality_status": "rejected"}))
    assert repo["connector"].prs == []


def test_actual_history_attestation_rejects_missing_wrong_duplicate_and_stale_evidence(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    workflow = workflow_for(None)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, members(repo["shas"])))
    valid = qa_ticket(prepared)
    marker = delivery.workflow_delivery_attestation(prepared)

    cases = [
        (qa_ticket(prepared, submission_marker="ordinary QA notes"), "submission does not attest"),
        (qa_ticket(prepared, review_marker="ordinary review notes"), "review does not attest"),
        (qa_ticket(prepared, submission_marker=f"{marker}\n{marker}"), "submission does not attest"),
        (qa_ticket(prepared, review_marker=f"{marker}\n{marker}"), "review does not attest"),
        (qa_ticket(prepared, submission_marker=f"{marker}\nworkflow_delivery_sha256: {'0' * 64}"),
         "submission does not attest"),
        (qa_ticket(prepared, commit="8" * 40), "exact submitted combined SHA"),
    ]
    for evidence, message in cases:
        with pytest.raises(ValueError, match=message):
            run(delivery.publish_workflow_delivery(rt, policy, workflow, evidence, prepared))

    # Legacy synthetic fields do not substitute for actual record markers.
    synthetic = qa_ticket(prepared, submission_marker="", review_marker="")
    synthetic["workflow_delivery"] = dict(prepared)
    synthetic["latest_verdict"]["validated_sha"] = prepared["combined_revision"]
    synthetic["latest_verdict"]["workflow_delivery"] = dict(prepared)
    synthetic["review_history"][-1].update(synthetic["latest_verdict"])
    with pytest.raises(ValueError, match="submission does not attest"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow, synthetic, prepared))

    stale = {**valid, "latest_verdict": {**valid["latest_verdict"],
        "submission_commit": "8" * 40}}
    with pytest.raises(ValueError, match="current ticket revision"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow, stale, prepared))
    assert repo["connector"].prs == []


def test_parallel_sources_combine_and_publish_once_only_after_exact_independent_qa(repo):
    rt, connector, policy = repo["runtime"], repo["connector"], delivery_policy(repo["bare"])
    # Compute the actual expected merge head from the exact Git integration
    # runtime, then seal the normalized policy to that immutable revision.
    first = run(rt.collect(policy, members(repo["shas"])[0]))
    assert first["state"] == "integrated"
    assert connector.prs == []
    second = run(rt.collect(policy, members(repo["shas"])[1]))
    assert second["state"] == "integrated"
    batch = rt.ledger.document["batches"][first["batch_key"]]
    head = batch["working_head"]
    assert head not in repo["shas"].values()
    tree = git(repo["clone"], "show", f"{head}:TK-one.txt") + git(repo["clone"], "show", f"{head}:TK-two.txt")
    assert "origin-one" in tree and "origin-two" in tree
    workflow = workflow_for(head)
    prepared = run(delivery.prepare_workflow_delivery(
        # Replay with the full sealed members is idempotent after the runtime
        # previously collected each exact member; this also proves restart-safe reuse.
        runtime_module.BatchDeliveryRuntime(
            runtime_module.BatchLedger(repo["ledger_path"]), repo["adapter"]),
        policy, workflow, members(repo["shas"])))
    assert prepared["batch_key"] == first["batch_key"]
    assert prepared["state"] == "prepared"
    assert prepared["combined_revision"] == head
    assert prepared["manifest_sha256"]
    assert prepared["member_manifest"] == {"TK-one": repo["shas"]["TK-one"],
                                           "TK-two": repo["shas"]["TK-two"]}
    assert prepared["member_refs"] == {"TK-one": "workflow/TK-one", "TK-two": "workflow/TK-two"}
    assert set(prepared["member_evidence_digests"]) == {"TK-one", "TK-two"}
    assert connector.prs == []  # preparation never releases a customer snapshot
    with pytest.raises(ValueError, match="independent current approval"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow,
            qa_ticket(prepared, reviewer="qa-worker"), prepared))
    with pytest.raises(ValueError, match="exact submitted combined SHA"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow,
            qa_ticket(prepared, commit="8" * 40), prepared))
    assert connector.prs == []
    assert run(repo["adapter"].validate_cumulative(policy["repository"], "main", head,
        list(rt.ledger.document["batches"][prepared["batch_key"]]["members"].values())))["passed"] is True
    result = run(delivery.publish_workflow_delivery(rt, policy, workflow,
        qa_ticket(prepared), prepared))
    assert result["state"] == "in_delivery", result
    assert len(connector.prs) == 1
    assert connector.prs[0]["source_sha"] == head
    assert connector.prs[0]["target_branch"] == "customer/review"
    replay = run(delivery.publish_workflow_delivery(rt, policy, workflow,
        qa_ticket(prepared), prepared))
    assert replay["state"] == "delivery_open" and len(connector.prs) == 1


def test_single_pending_prepare_collects_real_parallel_merges_and_binds_actual_qa(repo):
    rt, policy, rows = repo["runtime"], delivery_policy(repo["bare"]), members(repo["shas"])
    workflow = workflow_for(None)

    # One public preparation call does both actual runtime merges. The caller
    # neither pre-collects nor predicts the metadata-bearing merge commit.
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, rows))
    assert prepared["state"] == "prepared"
    head = prepared["combined_revision"]
    assert head not in repo["shas"].values()
    batch = rt.ledger.document["batches"][prepared["batch_key"]]
    assert batch["working_head"] == head
    assert set(batch["members"]) == {"TK-one", "TK-two"}
    assert "origin-one" in git(repo["clone"], "show", f"{head}:TK-one.txt")
    assert "origin-two" in git(repo["clone"], "show", f"{head}:TK-two.txt")
    assert prepared["final_validation_policy_sha256"]
    assert repo["connector"].prs == []

    # QA is independent and names both the full returned identity and its head.
    result = run(delivery.publish_workflow_delivery(
        rt, policy, workflow, qa_ticket(prepared, commit=head), prepared))
    assert result["state"] == "in_delivery"
    assert len(repo["connector"].prs) == 1
    assert repo["connector"].prs[0]["source_sha"] == head
    replay = run(delivery.publish_workflow_delivery(
        rt, policy, workflow, qa_ticket(prepared, commit=head), prepared))
    assert replay["state"] == "delivery_open"
    assert len(repo["connector"].prs) == 1


def test_batch_pr_can_target_its_exact_base_without_writing_environment_ref(repo):
    rt = repo["runtime"]
    policy = delivery_policy(repo["bare"])
    policy["target_branch"] = "main"
    rows = members(repo["shas"])
    run(rt.collect(policy, rows[0])); run(rt.collect(policy, rows[1]))
    head = next(iter(rt.ledger.document["batches"].values()))["working_head"]
    workflow = workflow_for(head)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, rows))
    before = run(repo["adapter"].read_ref(policy["repository"], "main"))
    result = run(delivery.publish_workflow_delivery(rt, policy, workflow,
        qa_ticket(prepared), prepared))
    after = run(repo["adapter"].read_ref(policy["repository"], "main"))
    assert result["state"] == "in_delivery"
    assert len(repo["connector"].prs) == 1
    assert repo["connector"].prs[0]["target_branch"] == "main"
    assert repo["connector"].prs[0]["source_branch"].startswith("pursers/snapshots/")
    assert before == after == repo["base"]


def test_branch_only_publishes_one_owned_branch_and_never_customer_pr(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"], "branch_only")
    rows = members(repo["shas"])
    run(rt.collect(policy, rows[0])); run(rt.collect(policy, rows[1]))
    head = next(iter(rt.ledger.document["batches"].values()))["working_head"]
    workflow = workflow_for(head)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, rows))
    result = run(delivery.publish_workflow_delivery(rt, policy, workflow,
        qa_ticket(prepared), prepared))
    assert result["state"] == "in_delivery"
    assert result["branch"].startswith("pursers/snapshots/")
    assert repo["connector"].prs == []


def test_incomplete_malformed_and_wrong_trigger_cohorts_fail_closed(repo):
    policy = delivery_policy(repo["bare"])
    workflow = workflow_for("a" * 40)
    rows = members(repo["shas"])
    with pytest.raises(ValueError, match="exactly match"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], policy, workflow, rows[:1]))
    malformed = dict(workflow); malformed["member_ticket_ids"] = ("TK-one",)
    malformed["nodes"] = {"TK-one": {"prerequisites": ()}}
    with pytest.raises(ValueError, match="does not seal"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], policy, malformed, rows))
    with pytest.raises(ValueError, match="ready trigger"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], {**policy, "trigger": "manual"}, workflow, rows))
    with pytest.raises(ValueError, match="unsupported fields"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], policy, workflow,
            [{**rows[0], "unexpected": True}, rows[1]]))
    for field in ("source_ref", "source_sha", "summary", "tests", "blockers", "baseline_failures"):
        partial = dict(rows[0]); partial.pop(field)
        with pytest.raises(ValueError, match="incomplete"):
            run(delivery.prepare_workflow_delivery(repo["runtime"], policy, workflow,
                [partial, rows[1]]))
    unsealed = dict(workflow); unsealed["activation_id"] = ""
    with pytest.raises(ValueError, match="activation_id"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], policy, unsealed, rows))
    assert not repo["connector"].prs


def test_explicit_wrong_pre_pinned_head_still_fails_closed(repo):
    policy, rows = delivery_policy(repo["bare"]), members(repo["shas"])
    wrong = workflow_for("8" * 40)
    with pytest.raises(ValueError, match="differs from actual cumulative integration head"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], policy, wrong, rows))
    assert repo["connector"].prs == []


def test_pending_head_requires_explicit_none_and_is_not_publishable_by_itself(repo):
    policy, rows = delivery_policy(repo["bare"]), members(repo["shas"])
    workflow = workflow_for(None)
    malformed = dict(workflow)
    malformed["final_validation"] = {**workflow["final_validation"], "combined_revision": "pending"}
    with pytest.raises(ValueError, match="incomplete or unsealed"):
        run(delivery.prepare_workflow_delivery(repo["runtime"], policy, malformed, rows))

    # No independent QA, same-principal verdict, or QA bound to a different
    # actual commit can authorize publication of an explicitly pending policy.
    prepared = run(delivery.prepare_workflow_delivery(repo["runtime"], policy, workflow, rows))
    head = prepared["combined_revision"]
    with pytest.raises(ValueError, match="not closed and approved"):
        run(delivery.publish_workflow_delivery(repo["runtime"], policy, workflow,
            {"project": "Fixture", "status": "open", "workflow_delivery": prepared}, prepared))
    with pytest.raises(ValueError, match="independent current approval"):
        run(delivery.publish_workflow_delivery(repo["runtime"], policy, workflow,
            qa_ticket(prepared, reviewer="qa-worker"), prepared))
    with pytest.raises(ValueError, match="exact submitted combined SHA"):
        run(delivery.publish_workflow_delivery(repo["runtime"], policy, workflow,
            qa_ticket(prepared, commit="8" * 40), prepared))
    assert repo["connector"].prs == []


def test_prepared_actual_head_drift_is_rejected_even_with_matching_ledger_sha(repo):
    policy, rows = delivery_policy(repo["bare"]), members(repo["shas"])
    workflow = workflow_for(None)
    prepared = run(delivery.prepare_workflow_delivery(repo["runtime"], policy, workflow, rows))
    git(repo["clone"], "checkout", "-b", "drift", prepared["combined_revision"])
    (repo["clone"] / "drift.txt").write_text("integration ref moved after preparation\\n")
    git(repo["clone"], "add", "drift.txt")
    git(repo["clone"], "commit", "-m", "drift integration ref")
    drift = git(repo["clone"], "rev-parse", "HEAD")
    git(repo["clone"], "push", "origin", f"HEAD:refs/heads/{policy['integration_branch']}")
    assert drift != prepared["combined_revision"]
    assert repo["runtime"].ledger.document["batches"][prepared["batch_key"]]["working_head"] == prepared["combined_revision"]
    with pytest.raises(ValueError, match="prepared combined head changed"):
        run(delivery.publish_workflow_delivery(repo["runtime"], policy, workflow,
            qa_ticket(prepared), prepared))
    assert repo["connector"].prs == []


def test_unknown_remote_publish_outcome_stays_blocked_and_reconciles(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    rows = members(repo["shas"])
    run(rt.collect(policy, rows[0])); run(rt.collect(policy, rows[1]))
    head = next(iter(rt.ledger.document["batches"].values()))["working_head"]
    workflow = workflow_for(head)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, rows))
    async def unknown(*_args, **_kwargs):
        raise TimeoutError("remote create outcome unknown")
    repo["connector"].create_customer_pr = unknown
    result = run(delivery.publish_workflow_delivery(rt, policy, workflow,
        qa_ticket(prepared), prepared))
    assert result["state"] == "integration_blocked"
    assert result["reason"] == "customer_pr_create_outcome_unknown"
    assert rt.ledger.document["batches"][prepared["batch_key"]]["state"] == "blocked_unknown"


def test_changed_head_manifest_or_missing_source_object_blocks_without_release(repo):
    rt, policy = repo["runtime"], delivery_policy(repo["bare"])
    rows = members(repo["shas"])
    run(rt.collect(policy, rows[0])); run(rt.collect(policy, rows[1]))
    head = next(iter(rt.ledger.document["batches"].values()))["working_head"]
    workflow = workflow_for(head)
    prepared = run(delivery.prepare_workflow_delivery(rt, policy, workflow, rows))
    batch = rt.ledger.document["batches"][prepared["batch_key"]]
    batch["working_head"] = "7" * 40
    with pytest.raises(ValueError, match="revision differs"):
        run(delivery.publish_workflow_delivery(rt, policy, workflow, qa_ticket(prepared), prepared))
    assert repo["connector"].prs == []
    # A missing remote object is rejected by the underlying verified adapter;
    # no synthetic member or QA success can cover it.
    missing = dict(rows[1], source_sha="6" * 40,
        evidence={"approved_sha": "6" * 40, "independent_review": True,
                  "validation_passed": True, "source_origin": "coordinator"})
    rt2 = runtime_module.BatchDeliveryRuntime(runtime_module.BatchLedger(
        repo["ledger_path"].parent / "missing-ledger.json"), repo["adapter"])
    blocked = run(delivery.prepare_workflow_delivery(rt2, policy, workflow, [rows[0], missing]))
    assert blocked["state"] == "blocked"
    assert blocked["reason"] == "approved_source_ref_changed"
    assert repo["connector"].prs == []
