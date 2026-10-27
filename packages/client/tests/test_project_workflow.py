from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "client" / "src"))
from pursers_client.project_workflow import (  # noqa: E402
    accepted_candidate, final_validation_packet, final_validation_ticket_id,
    final_validation_workspace_identity, parse_final_validation_packet_index,
    parse_project_workflow, parse_workflow_activation, policy_digest, reconcile,
    ticket_belongs_to_project,
)


def policy():
    return {
        "schema_version": 1, "workflow_id": "workflow:sample", "project_id": "sample",
        "board_id": "sample-board", "cohort_id": "cohort:one", "sealed": True,
        "nodes": [
            {"node_id": "foundation", "ticket_id": "TK-foundation", "requires": [],
             "acceptance_checks": ["exact candidate independently reviewed"]},
            {"node_id": "feature", "ticket_id": "TK-feature", "requires": ["foundation"],
             "acceptance_checks": ["target finding fixed", "no new owned regression"]},
        ],
        "final_validation": {"title": "Validate frozen cohort", "acceptance_checks": [
            "complete configured project checks on the exact combined revision"]},
    }


def accepted(sha="a" * 40, *, submitter="PR-worker", reviewer="PR-reviewer", review_sha=None):
    return {"status": "closed", "latest_submission": {
        "branch": "pursers/TK", "commit_hash": sha, "submitted_by_principal_id": submitter,
        "notes": "Required test_output: focused tests passed"},
        "latest_verdict": {"verdict": "approve", "status_to": "closed",
                            "submission_commit": review_sha or sha,
                            "reviewed_by_principal_id": reviewer}}


def test_final_validation_workspace_identity_requires_active_exact_packet_and_route():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    activation = {"schema_version": 1, "state": "active",
                  "policy_sha256": policy_digest(normalized), "activation_id": "activation-1"}
    registry = {"projects": {"sample": {
        "status": "active", "fleet": True, "board_id": "sample-board",
        "work_dir": "/repo/sample", "repository_url": "https://example.invalid/sample",
        "butler_workflow": normalized, "butler_workflow_activation": activation,
    }}}
    head = "c" * 40
    description, pages = final_validation_packet(normalized, {
        "ticket_id": final_validation_ticket_id(normalized), "combined_revision": head,
        "members": {"TK-foundation": "a" * 40, "TK-feature": "b" * 40},
        "acceptance_checks": ["full configured gate"],
    }, delivery={"cohort_id": normalized["cohort_id"], "combined_revision": head})
    ticket = {"ticket_id": final_validation_ticket_id(normalized),
              "target_url": "https://example.invalid/sample", "description": description,
              "annotations": [{"kind": "evidence", "text": page} for page in pages],
              "annotations_omitted_count": 0, "annotation_count": len(pages)}
    assert final_validation_workspace_identity(registry, "sample-board", ticket) == {
        "project_id": "sample", "combined_revision": head}
    # Packet lookalikes in ordinary annotations do not satisfy the evidence contract.
    wrong_kind = {**ticket, "annotations": [
        {"kind": "note", "text": page} for page in pages]}
    assert final_validation_workspace_identity(registry, "sample-board", wrong_kind) is None
    assert final_validation_workspace_identity(registry, "other-board", ticket) is None
    assert final_validation_workspace_identity(
        {"projects": {"sample": {**registry["projects"]["sample"],
                                  "butler_workflow_activation": None}}},
        "sample-board", ticket) is None
    assert final_validation_workspace_identity(registry, "sample-board", {
        **ticket, "ticket_id": "TK-forged", "tags": ["final-validation"]}) is None
    assert final_validation_workspace_identity(registry, "sample-board", {
        **ticket, "annotations_omitted_count": 1}) is None


def test_policy_seal_route_dag_and_activation_are_strict():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    activation = {"schema_version": 1, "state": "active", "policy_sha256": policy_digest(normalized),
                  "activation_id": "operator:activation-1"}
    assert parse_workflow_activation(activation, normalized) == activation
    for mutated in (
        {**policy(), "sealed": False},
        {**policy(), "board_id": "other-board"},
    ):
        with pytest.raises(ValueError):
            parse_project_workflow(mutated, project_id="sample", board_id="sample-board")
    # A shorter but otherwise valid DAG cannot be proven partial by shape alone;
    # the operator's activation digest is the seal on exact cohort membership.
    truncated = parse_project_workflow(
        {**policy(), "nodes": policy()["nodes"][:1]},
        project_id="sample", board_id="sample-board",
    )
    with pytest.raises(ValueError, match="does not pin"):
        parse_workflow_activation(activation, truncated)
    cyclic = policy()
    cyclic["nodes"][0]["requires"] = ["feature"]
    with pytest.raises(ValueError, match="cycle"):
        parse_project_workflow(cyclic, project_id="sample", board_id="sample-board")
    wrong_activation = {**activation, "policy_sha256": "0" * 64}
    with pytest.raises(ValueError, match="does not pin"):
        parse_workflow_activation(wrong_activation, normalized)
    malformed_digest = {**activation, "policy_sha256": "a" * 40}
    with pytest.raises(ValueError, match="does not pin"):
        parse_workflow_activation(malformed_digest, normalized)
    with pytest.raises(ValueError, match="unsupported or missing"):
        parse_workflow_activation({**activation, "operator_override": True}, normalized)



def test_final_qa_identity_is_project_and_board_scoped():
    raw = {
        "schema_version": 1, "workflow_id": "shared", "project_id": "sample",
        "board_id": "board-a", "cohort_id": "cohort", "sealed": True,
        "nodes": [{"node_id": "root", "ticket_id": "TK-root", "requires": [],
                   "acceptance_checks": ["independent review"]}],
        "final_validation": {"title": "Final QA", "acceptance_checks": ["full gate"]},
    }
    first = parse_project_workflow(raw, project_id="sample", board_id="board-a")
    second = parse_project_workflow({**raw, "board_id": "board-b"},
                                    project_id="sample", board_id="board-b")
    third = parse_project_workflow({**raw, "project_id": "other"},
                                   project_id="other", board_id="board-a")
    assert len({final_validation_ticket_id(first), final_validation_ticket_id(second),
                final_validation_ticket_id(third)}) == 3


def test_member_route_validation_rejects_other_project_on_shared_board():
    registry = {"schema_version": 1, "projects": {
        "sample": {"board_id": "board", "work_dir": "/sample", "status": "active"},
        "other": {"board_id": "board", "work_dir": "/other", "status": "active"},
        "remote": {"board_id": "elsewhere", "work_dir": "/remote", "status": "active"},
    }}
    assert ticket_belongs_to_project({"target_url": "sample/path"}, registry,
                                     board_id="board", project_id="sample")
    assert not ticket_belongs_to_project({"target_url": "other/path"}, registry,
                                         board_id="board", project_id="sample")
    assert not ticket_belongs_to_project({"target_url": "remote/path"}, registry,
                                         board_id="board", project_id="sample")
    assert not ticket_belongs_to_project({}, registry, board_id="board", project_id="sample")

def test_acceptance_requires_current_independent_exact_sha_and_no_human_hold():
    good = accepted()
    assert accepted_candidate(good) == (True, "a" * 40)
    assert accepted_candidate(accepted(submitter="PR-same", reviewer="PR-same")) == (False, None)
    assert accepted_candidate(accepted(review_sha="b" * 40)) == (False, None)
    changed = copy.deepcopy(good)
    changed["latest_submission"]["commit_hash"] = "b" * 40
    assert accepted_candidate(changed) == (False, None)
    held = copy.deepcopy(good)
    held["human_request"] = {"resolution": None}
    assert accepted_candidate(held) == (False, None)


def test_reconcile_releases_only_eligible_dependencies_and_one_final_task():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    rows = {
        "TK-foundation": accepted(),
        "TK-feature": {"status": "open", "parked": True},
    }
    first = reconcile(normalized, rows)
    assert first["releases"] == ["TK-feature"]
    assert first["final_task"] is None
    rows["TK-feature"] = accepted("b" * 40)
    final = reconcile(normalized, rows, combined_revision="b" * 40)
    assert final["all_accepted"] is True
    assert final["accepted_members"] == {"TK-feature": "b" * 40, "TK-foundation": "a" * 40}
    assert final["final_task"]["combined_revision"] == "b" * 40
    assert final["final_task"]["acceptance_checks"]
    assert reconcile(normalized, rows, combined_revision="b" * 40, final_task_exists=True)["final_task"] is None
    changed = copy.deepcopy(rows)
    changed["TK-foundation"] = accepted("c" * 40)
    assert reconcile(normalized, changed, combined_revision="b" * 40)["all_accepted"] is True
    assert reconcile(normalized, changed, combined_revision="not-a-sha")["final_task"] is None
    held_rows = copy.deepcopy(rows)
    held_rows["TK-feature"] = {"status": "open", "parked": True,
                                "human_request": {"resolution": None}}
    assert reconcile(normalized, held_rows)["releases"] == []


def test_reconcile_ignores_historical_holder_but_fences_live_work_and_review_leases():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    prerequisite = accepted()
    now = 1_800_000_000.0
    reopened = {
        "status": "open", "parked": True,
        "claimed_by_agent_id": "AI-historical-holder",
        "human_request": {"resolution": {"action": "accept"}},
    }
    rows = {"TK-foundation": prerequisite, "TK-feature": reopened}
    assert reconcile(normalized, rows, now=now)["releases"] == ["TK-feature"]

    # An extant work lease is authoritative even if a stale projection says
    # open; active pre-submission states without expiry fail closed as well.
    live_work = {**reopened, "status": "claimed",
                 "lease_expires_at_epoch": now + 60}
    rows["TK-feature"] = live_work
    assert reconcile(normalized, rows, now=now)["releases"] == []
    active_without_expiry = {**reopened, "status": "claimed"}
    rows["TK-feature"] = active_without_expiry
    assert reconcile(normalized, rows, now=now)["releases"] == []

    live_review = {**reopened, "review_lease": {"expires_at_epoch": now + 60}}
    rows["TK-feature"] = live_review
    assert reconcile(normalized, rows, now=now)["releases"] == []
    unresolved = {**reopened, "human_request": {"resolution": None}}
    rows["TK-feature"] = unresolved
    assert reconcile(normalized, rows, now=now)["releases"] == []


def test_policy_and_evidence_mismatch_block_without_guessing_acceptance():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    partial = accepted()
    del partial["latest_verdict"]["reviewed_by_principal_id"]
    assert accepted_candidate(partial) == (False, None)
    assert accepted_candidate({**accepted(), "status": "rejected"}) == (False, None)
    rows = {"TK-foundation": accepted(), "TK-feature": {"status": "open", "parked": True}}
    # An unknown or absent prerequisite must never release a node.
    rows.pop("TK-foundation")
    assert reconcile(normalized, rows)["releases"] == []
    # The sealed policy is bound to exact project and board routes.
    with pytest.raises(ValueError, match="route mismatch"):
        parse_project_workflow(policy(), project_id="other", board_id="sample-board")
    with pytest.raises(ValueError, match="outside its sealed cohort"):
        parse_project_workflow(
            {**policy(), "nodes": [{**policy()["nodes"][1], "requires": ["missing"]}]},
            project_id="sample", board_id="sample-board",
        )


def test_final_validation_packet_pages_complete_manifest_with_exact_digest():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    final = {
        "combined_revision": "c" * 40,
        "members": {"TK-foundation": "a" * 40, "TK-feature": "b" * 40},
        "acceptance_checks": ["run every configured project check on the exact candidate"],
    }
    member_evidence = {"TK-foundation": {
        "submission_notes": " \u2003" + ("candidate report " * 300) + "\u2003 "}}
    description, pages = final_validation_packet(
        normalized, final, member_evidence=member_evidence)
    import hashlib
    import json
    assert len(description) <= 5000
    assert all(len(page) <= 4000 for page in pages)
    index = parse_final_validation_packet_index(description)
    assert description.startswith("Butler workflow final-validation evidence index v1\n{")
    packet_hash = index["evidence_packet"]["sha256"]
    reconstructed = "".join(page.split(":", 4)[4] for page in pages)
    assert hashlib.sha256(reconstructed.encode()).hexdigest() == packet_hash
    packet = json.loads(reconstructed)
    assert packet["combined_revision"] == "c" * 40
    assert packet["members"] == final["members"]
    assert packet["acceptance_checks"] == final["acceptance_checks"]
    assert packet["member_evidence"] == member_evidence
    assert packet["customer_final_merge"] == "manual"
    # Central clean_text strips whitespace on each annotation. Framing must
    # make arbitrary exact evidence safe at page boundaries, including Unicode.
    assert len(pages) > 1
    for page in pages:
        chunk = page.split(":", 4)[4]
        assert chunk == chunk.strip()


def test_final_validation_packet_carries_bounded_prepared_identity_attestation_instructions():
    normalized = parse_project_workflow(policy(), project_id="sample", board_id="sample-board")
    final = {
        "combined_revision": "c" * 40,
        "members": {"TK-foundation": "a" * 40, "TK-feature": "b" * 40},
        "acceptance_checks": ["run all configured checks"],
    }
    prepared = {"schema_version": 1, "state": "prepared",
                "combined_revision": "c" * 40, "member_manifest": final["members"]}
    marker = "workflow_delivery_sha256: " + "d" * 64
    description, pages = final_validation_packet(
        normalized, final, delivery=prepared, delivery_attestation=marker)
    packet = json.loads("".join(page.split(":", 4)[4] for page in pages))
    assert packet["workflow_delivery"] == prepared
    assert packet["workflow_delivery_attestation"] == marker
    assert marker in packet["qa_instructions"]["worker"]
    assert marker in packet["qa_instructions"]["reviewer"]
    assert len(description) <= 5000 and all(len(page) <= 4000 for page in pages)
    with pytest.raises(ValueError, match="malformed"):
        final_validation_packet(normalized, final, delivery=prepared, delivery_attestation="not-a-marker")
