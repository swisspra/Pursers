"""QA-gated delivery for one sealed resident workflow cohort.

This is a thin policy/evidence adapter over ``BatchDeliveryRuntime``. It owns
no scheduler, Git operations, connector calls, or delivery ledger of its own.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from integration_delivery import parse_batch_policy

SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _workflow(policy: Any) -> dict[str, Any]:
    """Validate the normalized, activated output of parse_workflow_contract."""
    required = {"schema_version", "board_id", "project_name", "cohort_id",
                "member_ticket_ids", "nodes", "final_validation", "activation_id",
                "policy_revision"}
    if not isinstance(policy, Mapping) or set(policy) != required or policy.get("schema_version") != 1:
        raise ValueError("workflow policy must be a normalized sealed v1 contract")
    for key in ("board_id", "project_name", "cohort_id", "activation_id"):
        if not isinstance(policy.get(key), str) or not policy[key].strip():
            raise ValueError(f"workflow policy {key} is required")
    if not SHA256.fullmatch(str(policy.get("policy_revision", ""))):
        raise ValueError("workflow policy revision is not sealed")
    members = policy.get("member_ticket_ids")
    nodes = policy.get("nodes")
    if (not isinstance(members, (tuple, list)) or not members
            or any(not isinstance(ticket, str) or not ticket for ticket in members)
            or len(set(members)) != len(members) or not isinstance(nodes, Mapping)
            or set(nodes) != set(members)):
        raise ValueError("workflow cohort membership is incomplete or unsealed")
    graph = {}
    for ticket, node in nodes.items():
        if not isinstance(node, Mapping) or set(node) != {"prerequisites"}:
            raise ValueError("workflow node is malformed")
        prerequisites = node["prerequisites"]
        if (not isinstance(prerequisites, (tuple, list))
                or any(not isinstance(item, str) or item not in nodes for item in prerequisites)
                or len(set(prerequisites)) != len(prerequisites) or ticket in prerequisites):
            raise ValueError("workflow node prerequisites are invalid")
        graph[ticket] = tuple(prerequisites)
    visiting, visited = set(), set()
    def visit(ticket):
        if ticket in visiting:
            raise ValueError("workflow prerequisite graph contains a cycle")
        if ticket in visited:
            return
        visiting.add(ticket)
        for dependency in graph[ticket]:
            visit(dependency)
        visiting.remove(ticket)
        visited.add(ticket)
    for ticket in members:
        visit(ticket)
    final = policy.get("final_validation")
    if (not isinstance(final, Mapping)
            or set(final) != {"enabled", "combined_revision", "title", "scope", "required_fields", "checks"}
            or final.get("enabled") is not True
            or (final.get("combined_revision") is not None
                and (not isinstance(final.get("combined_revision"), str)
                     or not SHA.fullmatch(final["combined_revision"])))
            or not isinstance(final.get("title"), str) or not final["title"].strip()
            or not isinstance(final.get("scope"), str) or not final["scope"].strip()
            or not isinstance(final.get("required_fields"), (tuple, list))
            or not final["required_fields"]
            or any(not isinstance(item, str) or not item for item in final["required_fields"])
            or len(set(final["required_fields"])) != len(final["required_fields"])
            or not isinstance(final.get("checks"), (tuple, list)) or not final["checks"]
            or any(not isinstance(item, str) or not item.strip() for item in final["checks"])):
        raise ValueError("workflow final-validation policy is incomplete or unsealed")
    final_body = final
    policy_body = {
        "schema_version": 1, "board_id": policy["board_id"],
        "project_name": policy["project_name"], "cohort_id": policy["cohort_id"],
        "member_ticket_ids": list(members),
        "nodes": {ticket: {"prerequisites": list(nodes[ticket]["prerequisites"])}
                  for ticket in members},
        "final_validation": {"enabled": final_body["enabled"],
            "combined_revision": final_body["combined_revision"], "title": final_body["title"],
            "scope": final_body["scope"], "required_fields": list(final_body["required_fields"]),
            "checks": list(final_body["checks"])},
    }
    if _digest(policy_body) != policy["policy_revision"]:
        raise ValueError("workflow policy revision does not seal the normalized cohort")
    return {**dict(policy), "member_ticket_ids": tuple(members)}


def _delivery_policy(runtime: Any, policy: Any) -> dict[str, Any] | None:
    parsed = parse_batch_policy(policy)
    if parsed is None:
        raise ValueError("delivery policy mode is unavailable")
    if parsed["mode"] == "per_ticket_pr":
        return parsed
    if not isinstance(policy, Mapping) or policy.get("trigger") != "ready" or parsed.get("trigger") != "ready":
        raise ValueError("workflow cohort delivery requires the configured ready trigger")
    if parsed.get("snapshot_strategy") != "frozen":
        raise ValueError("workflow cohort delivery requires immutable frozen snapshots")
    if not all(callable(getattr(runtime, name, None)) for name in ("collect", "release")):
        raise ValueError("an existing BatchDeliveryRuntime is required")
    return parsed


def _validate_members(runtime: Any, workflow: Mapping[str, Any], members: Any) -> list[dict[str, Any]]:
    if not isinstance(members, (tuple, list)):
        raise ValueError("workflow members must be a complete member list")
    declared = workflow["member_ticket_ids"]
    if len(members) != len(declared) or {m.get("ticket_id") for m in members if isinstance(m, Mapping)} != set(declared):
        raise ValueError("workflow members do not exactly match sealed cohort membership")
    normalized = []
    required = {"ticket_id", "source_ref", "source_sha", "issue_ids", "summary", "tests",
                "evidence", "blockers", "baseline_failures"}
    allowed = required | {"origin"}
    for member in members:
        if not isinstance(member, Mapping) or set(member) - allowed or not required <= set(member):
            raise ValueError("workflow member is malformed, incomplete, or contains unsupported fields")
        if (not isinstance(member["ticket_id"], str) or not member["ticket_id"]
                or not isinstance(member["source_ref"], str) or not member["source_ref"]
                or not isinstance(member["source_sha"], str) or not SHA.fullmatch(member["source_sha"])):
            raise ValueError("member ticket, source ref, and full source SHA are required")
        if (not isinstance(member["issue_ids"], list)
                or any(not isinstance(item, str) for item in member["issue_ids"])):
            raise ValueError("member issue_ids must be a list of strings")
        if (not isinstance(member["summary"], str) or not member["summary"].strip()
                or not isinstance(member["tests"], str) or not member["tests"].strip()):
            raise ValueError("member summary and test evidence must be nonempty strings")
        if (not isinstance(member["blockers"], str) or not member["blockers"].strip()
                or not isinstance(member["baseline_failures"], str)
                or not member["baseline_failures"].strip()):
            raise ValueError("blockers and baseline failures must be explicit truthful evidence")
        if "origin" in member and (not isinstance(member["origin"], str) or not member["origin"].strip()):
            raise ValueError("member origin must be truthful nonempty audit context")
        evidence = member["evidence"]
        if not isinstance(evidence, Mapping):
            raise ValueError("member independent approval evidence is required")
        if "origin" in member and evidence.get("source_origin") != member["origin"]:
            raise ValueError("member origin is not bound to its independently approved evidence")
        normalized.append(dict(member))
    by_id = {m["ticket_id"]: m for m in normalized}
    return [by_id[ticket] for ticket in declared]


def _ledger_batch(runtime: Any, batch_key: str) -> dict[str, Any]:
    ledger = getattr(runtime, "ledger", None)
    batches = getattr(getattr(ledger, "document", None), "get", lambda *_: None)("batches")
    batch = batches.get(batch_key) if isinstance(batches, Mapping) else None
    if not isinstance(batch, dict):
        raise ValueError("prepared batch is missing from the durable runtime ledger")
    return batch


def _member_identity(batch: Mapping[str, Any], workflow: Mapping[str, Any]
                     ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    stored = batch.get("members")
    if not isinstance(stored, Mapping) or set(stored) != set(workflow["member_ticket_ids"]):
        raise ValueError("batch membership differs from the complete sealed cohort")
    manifest, refs, digests = {}, {}, {}
    for ticket in workflow["member_ticket_ids"]:
        member = stored[ticket]
        evidence = member.get("evidence") if isinstance(member, Mapping) else None
        sha = member.get("source_sha") if isinstance(member, Mapping) else None
        source_ref = member.get("source_ref") if isinstance(member, Mapping) else None
        if (not isinstance(member, Mapping)
                or member.get("status") not in {"integrated", "in_delivery", "report_held"}
                or not isinstance(sha, str) or not SHA.fullmatch(sha)
                or not isinstance(source_ref, str) or not source_ref
                or not isinstance(evidence, Mapping) or evidence.get("approved_sha") != sha
                or evidence.get("independent_review") is not True
                or evidence.get("validation_passed") is not True
                or not SHA256.fullmatch(str(member.get("evidence_digest", "")))
                or member.get("evidence_digest") != _digest(evidence)):
            raise ValueError("batch member lacks exact durable independent approval evidence")
        manifest[ticket], refs[ticket] = sha, source_ref
        digests[ticket] = member["evidence_digest"]
    return manifest, refs, digests


def _prepared(runtime: Any, delivery: Mapping[str, Any], workflow: Mapping[str, Any], batch_key: str,
              *, allow_pending_head: bool = False,
              final_validation_ticket_id: str | None = None,
              allow_report_held_unknown: bool = False) -> dict[str, Any]:
    batch = _ledger_batch(runtime, batch_key)
    if (batch.get("effective_policy_revision") != delivery.get("policy_revision")
            or batch.get("repository") != delivery.get("repository")
            or batch.get("target_branch") != delivery.get("target_branch")
            or batch.get("base_branch") != delivery.get("base_branch")
            or batch.get("integration_branch") != delivery.get("integration_branch")
            or batch.get("mode") != delivery.get("mode")
            or batch.get("trigger") != delivery.get("trigger")
            or batch.get("snapshot_strategy") != delivery.get("snapshot_strategy")
            or not str(batch.get("snapshot_branch", "")).startswith(delivery["snapshot_prefix"].rstrip("/") + "/")):
        raise ValueError("runtime batch policy or route differs from the approved policy")
    released = batch.get("state") in {"delivery_open", "in_delivery", "draft_open"}
    uncertain_report_held = (allow_report_held_unknown and batch.get("state") == "blocked_unknown"
                              and any(isinstance(row, Mapping)
                                      and row.get("kind") == "create_draft_customer_pr"
                                      and row.get("status") == "unknown"
                                      for row in batch.get("reservations", {}).values()))
    if (batch.get("state") != "collecting" and not released and not uncertain_report_held
            or (batch.get("snapshot_sha") is not None
                and batch.get("snapshot_sha") != batch.get("working_head"))
            or batch.get("customer_pr") and batch["customer_pr"].get("source_sha") != batch.get("working_head")):
        raise ValueError("batch is already released or has a changed delivery snapshot")
    manifest, refs, evidence_digests = _member_identity(batch, workflow)
    combined_sha = batch.get("working_head")
    if not isinstance(combined_sha, str) or not SHA.fullmatch(combined_sha):
        raise ValueError("actual cumulative integration head is unavailable")
    pinned_head = workflow["final_validation"]["combined_revision"]
    if pinned_head is None:
        if not allow_pending_head:
            raise ValueError("a pending combined revision is valid only during preparation")
    elif pinned_head != combined_sha:
        raise ValueError("sealed final QA revision differs from actual cumulative integration head")
    final_policy = dict(workflow["final_validation"])
    final_policy["combined_revision"] = combined_sha
    binding = {"cohort_id": workflow["cohort_id"],
               "board_id": workflow["board_id"],
               "project_name": workflow["project_name"],
               "workflow_policy_sha256": workflow["policy_revision"],
               "final_validation_policy_sha256": _digest(final_policy),
               "member_manifest": manifest, "member_refs": refs,
               "member_evidence_digests": evidence_digests,
               "combined_revision": combined_sha,
               "final_validation_ticket_id": final_validation_ticket_id}
    prior_binding = batch.get("workflow_delivery")
    if prior_binding is not None and prior_binding != binding:
        raise ValueError("runtime batch is already bound to a different workflow cohort, head, or policy")
    if prior_binding is None:
        batch["workflow_delivery"] = binding
        runtime.ledger.save()
    return {"schema_version": 1, "state": "prepared", "batch_key": batch_key,
            **binding, "delivery_policy_revision": delivery["policy_revision"],
            "manifest_sha256": _digest(manifest)}


async def _revalidate_member_refs(runtime: Any, delivery: Mapping[str, Any], prepared: Mapping[str, Any]) -> None:
    reader = getattr(getattr(runtime, "adapter", None), "read_ref", None)
    if not callable(reader):
        raise ValueError("runtime cannot independently revalidate prepared source refs")
    actual_head = await reader(delivery["repository"], delivery["integration_branch"])
    if actual_head != prepared["combined_revision"]:
        raise ValueError("prepared combined head changed on the runtime integration ref")
    for ticket, source_ref in prepared["member_refs"].items():
        current = await reader(delivery["repository"], source_ref)
        if current != prepared["member_manifest"][ticket]:
            raise ValueError("prepared member source ref changed after independent approval")


def workflow_delivery_attestation(prepared: Mapping[str, Any]) -> str:
    """Return the one canonical Central-note marker for this prepared identity."""
    if not isinstance(prepared, Mapping):
        raise ValueError("prepared workflow delivery record is malformed")
    return f"workflow_delivery_sha256: {_digest(dict(prepared))}"


async def prepare_workflow_delivery(runtime, delivery_policy, workflow_policy, members,
                                    *, final_validation_ticket_id: str | None = None):
    """Collect exactly a sealed cohort and return its actual cumulative head.

    No release is requested here: ready-trigger batches remain unpublished until
    ``publish_workflow_delivery`` validates independent final QA.
    """
    workflow = _workflow(workflow_policy)
    delivery = _delivery_policy(runtime, delivery_policy)
    if delivery["mode"] == "per_ticket_pr":
        return {"state": "legacy_per_ticket_pr", "changed": False}
    normalized = _validate_members(runtime, workflow, members)
    pending_head = workflow["final_validation"]["combined_revision"] is None
    expected = set(workflow["member_ticket_ids"])
    if not hasattr(runtime, "ledger") or not isinstance(getattr(runtime.ledger, "document", None), Mapping):
        raise ValueError("runtime has no durable BatchLedger state")
    if not callable(getattr(runtime, "_route", None)):
        raise ValueError("runtime is not an existing BatchDeliveryRuntime")
    route = runtime._route(delivery)
    candidates = [batch for batch in runtime.ledger.document["batches"].values()
                  if batch.get("route_key") == route
                  and batch.get("effective_policy_revision") == delivery["policy_revision"]
                  and (batch.get("state") == "collecting"
                       or (batch.get("state") == "blocked_unknown"
                           and any(isinstance(row, Mapping)
                                   and row.get("kind") == "create_draft_customer_pr"
                                   and row.get("status") in {"reserved", "unknown"}
                                   for row in batch.get("reservations", {}).values())
                           and all(row.get("kind") == "create_draft_customer_pr"
                                   for row in batch.get("reservations", {}).values()
                                   if row.get("status") in {"reserved", "unknown"})))]
    if any(set(batch.get("members", {})) - expected for batch in candidates):
        raise ValueError("runtime batch contains members outside the sealed cohort")
    keys = set()
    reader = getattr(getattr(runtime, "adapter", None), "read_ref", None)
    if not callable(reader):
        raise ValueError("runtime cannot verify independently approved source refs")
    for member in normalized:
        try:
            source_head = await reader(delivery["repository"], member["source_ref"])
        except Exception as exc:
            return {"state": "blocked", "reason": "approved_source_ref_unavailable",
                    "ticket_id": member["ticket_id"], "error_class": type(exc).__name__}
        if source_head != member["source_sha"]:
            return {"state": "blocked", "reason": "approved_source_ref_changed",
                    "ticket_id": member["ticket_id"]}
        persisted = dict(member)
        result = await runtime.collect(delivery, persisted)
        if result.get("state") not in {"integrated", "in_delivery"}:
            return {"state": "blocked", "reason": result.get("reason", result.get("state", "unknown")),
                    "ticket_id": member["ticket_id"], "batch_key": result.get("batch_key")}
        key = result.get("batch_key")
        if not isinstance(key, str) or not key:
            return {"state": "blocked", "reason": "durable_batch_key_unavailable",
                    "ticket_id": member["ticket_id"]}
        keys.add(key)
    if len(keys) != 1:
        raise ValueError("sealed workflow members were split across runtime batches")
    actual = _prepared(runtime, delivery, workflow, keys.pop(), allow_pending_head=pending_head,
                        final_validation_ticket_id=final_validation_ticket_id,
                        allow_report_held_unknown=True)
    await _revalidate_member_refs(runtime, delivery, actual)
    if pending_head:
        final_policy = dict(workflow["final_validation"])
        final_policy["combined_revision"] = actual["combined_revision"]
        actual["final_validation_policy_sha256"] = _digest(final_policy)
    return actual


def _history_records(ticket: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    """Return complete actual Central records, rejecting omitted/malformed pages."""
    if ticket.get(f"{name}_omitted_count", 0) != 0:
        raise ValueError("final QA ticket evidence is incomplete")
    records = ticket.get(name)
    if not isinstance(records, list) or any(not isinstance(row, Mapping) for row in records):
        raise ValueError("actual final QA submission and review histories are unavailable")
    return records


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _marker_matches(notes: Any, expected: str) -> bool:
    if not isinstance(notes, str):
        return False
    matches = re.findall(r"(?m)^workflow_delivery_sha256: ([0-9a-f]{64})$", notes)
    labels = re.findall(r"(?im)^[ \t]*workflow_delivery_sha256[ \t]*:", notes)
    return len(matches) == 1 and len(labels) == 1 and f"workflow_delivery_sha256: {matches[0]}" == expected


def _qa_record(ticket: Any, prepared: Mapping[str, Any], *, allow_rejected: bool = False) -> Mapping[str, Any]:
    if not isinstance(ticket, Mapping):
        raise ValueError("final QA ticket is unavailable")
    status = ticket.get("status")
    if not allow_rejected and status != "closed":
        # Preserve the strict ready-publication error contract verbatim.
        raise ValueError("final QA ticket is not closed and approved")
    human_held = (allow_rejected and status == "needs_human"
                  and isinstance(ticket.get("human_request"), Mapping)
                  and ticket["human_request"].get("resolution") is None)
    if allow_rejected and status not in {"open", "rejected", "closed"} and not human_held:
        raise ValueError("final QA ticket has no completed report for draft delivery")
    submissions = _history_records(ticket, "submission_history")
    reviews = _history_records(ticket, "review_history")
    if not submissions or not reviews:
        raise ValueError(
            "final QA verdict is not an independent current review" if allow_rejected
            else "final QA verdict is not an independent current approval"
        )
    submission, review = submissions[-1], reviews[-1]
    # Bounded full-ticket projections and ticket history pages expose the same
    # actual Central records. If latest aliases are present, they must agree.
    if (isinstance(ticket.get("latest_submission"), Mapping)
            and ticket["latest_submission"] != submission):
        raise ValueError("final QA verdict is not bound to the current ticket revision")
    if (isinstance(ticket.get("latest_verdict"), Mapping)
            and ticket["latest_verdict"] != review):
        raise ValueError("final QA verdict is not bound to the current ticket revision")
    if not _marker_matches(submission.get("notes"), workflow_delivery_attestation(prepared)):
        raise ValueError("actual QA submission does not attest the exact prepared identity")
    expected_marker = workflow_delivery_attestation(prepared)
    review_notes = review.get("review_notes")
    if (not allow_rejected and not _marker_matches(review_notes, expected_marker)):
        raise ValueError("actual independent review does not attest the exact prepared identity")
    if allow_rejected and isinstance(review_notes, str):
        labels = re.findall(r"(?im)^[ \\t]*workflow_delivery_sha256[ \\t]*:", review_notes)
        if labels and not _marker_matches(review_notes, expected_marker):
            raise ValueError("actual independent review binds a different prepared identity")
    submitter = submission.get("submitted_by_principal_id")
    reviewer = review.get("reviewed_by_principal_id")
    submitted_at, reviewed_at = _timestamp(submission.get("submitted_at")), _timestamp(review.get("reviewed_at"))
    verdict = review.get("verdict")
    permitted_status = ({"closed"} if verdict == "approve" else {"open", "rejected"})
    if (not isinstance(submitter, str) or not submitter
            or not isinstance(reviewer, str) or not reviewer or submitter == reviewer
            or review.get("submitted_by_principal_id") != submitter
            or verdict not in ({"approve", "reject"} if allow_rejected else {"approve"})
            or review.get("status_to") not in (permitted_status if allow_rejected else {"closed"})
            or (allow_rejected and not human_held and ticket.get("status") != review.get("status_to"))
            or submitted_at is None or reviewed_at is None or reviewed_at < submitted_at):
        raise ValueError(
            "final QA verdict is not an independent current review" if allow_rejected
            else "final QA verdict is not an independent current approval"
        )
    submission_sha = submission.get("commit_hash")
    if (not isinstance(submission_sha, str) or not SHA.fullmatch(submission_sha)
            or submission_sha != prepared.get("combined_revision")
            or review.get("submission_commit") != submission_sha):
        raise ValueError("final QA verdict is not bound to the exact submitted combined SHA")
    return {"submission": submission, "review": review}


async def publish_workflow_delivery(runtime, delivery_policy, workflow_policy, qa_ticket, prepared,
                                     *, final_validation_delivery="require_pass", report=None):
    """Release a prepared batch only on exact, independent final QA evidence.

    The native QA ticket body must preserve the full prepared identity for
    human-readable review. Actual Central submission notes and independent
    review_notes each attest its canonical SHA-256 marker; Central history
    identity, chronology, project, and combined commit are checked directly.
    """
    workflow = _workflow(workflow_policy)
    if final_validation_delivery not in {"require_pass", "draft_with_report"}:
        raise ValueError("final-validation delivery mode is unsupported")
    delivery = _delivery_policy(runtime, delivery_policy)
    if delivery["mode"] == "per_ticket_pr":
        return {"state": "legacy_per_ticket_pr", "changed": False}
    if (not isinstance(prepared, Mapping) or prepared.get("schema_version") != 1
            or prepared.get("state") != "prepared"):
        raise ValueError("prepared workflow delivery record is malformed")
    current = _prepared(
        runtime, delivery, workflow, prepared.get("batch_key", ""),
        allow_pending_head=workflow["final_validation"]["combined_revision"] is None,
        final_validation_ticket_id=prepared.get("final_validation_ticket_id"),
        allow_report_held_unknown=final_validation_delivery == "draft_with_report",
    )
    expected = dict(prepared)
    expected.pop("member_shas", None)
    if current != expected:
        raise ValueError("prepared batch head, membership, or policy has changed")
    if (workflow["final_validation"]["combined_revision"] is None
            and prepared.get("final_validation_policy_sha256") != current.get("final_validation_policy_sha256")):
        raise ValueError("prepared final QA policy does not bind the actual combined head")
    if (not isinstance(qa_ticket, Mapping)
            or qa_ticket.get("project") != workflow["project_name"]):
        raise ValueError("final QA ticket does not match the prepared project")
    evidence = _qa_record(
        qa_ticket, prepared, allow_rejected=final_validation_delivery == "draft_with_report")
    if not isinstance(getattr(runtime, "_locks", None), dict):
        raise ValueError("runtime does not expose the existing route lock")
    await _revalidate_member_refs(runtime, delivery, prepared)
    lock = runtime._locks.setdefault(runtime._route(delivery), asyncio.Lock())
    async with lock:
        if _prepared(
            runtime, delivery, workflow, prepared["batch_key"],
            allow_pending_head=workflow["final_validation"]["combined_revision"] is None,
            final_validation_ticket_id=prepared.get("final_validation_ticket_id"),
            allow_report_held_unknown=final_validation_delivery == "draft_with_report",
        ) != expected:
            raise ValueError("prepared batch changed before QA-gated release")
        await _revalidate_member_refs(runtime, delivery, prepared)
        request = {"cohort_id": workflow["cohort_id"],
                   "members": list(workflow["member_ticket_ids"])}
        batch = _ledger_batch(runtime, prepared["batch_key"])
        if final_validation_delivery == "draft_with_report":
            validated_report = {
                "quality_status": "approved" if evidence["review"].get("verdict") == "approve" else "rejected",
                "combined_revision": prepared["combined_revision"],
                "submission": dict(evidence["submission"]),
                "review": dict(evidence["review"]),
                "submission_principal": evidence["submission"].get("submitted_by_principal_id"),
                "reviewer_principal": evidence["review"].get("reviewed_by_principal_id"),
            }
            if isinstance(report, Mapping):
                for field in ("report_reference", "test_build_summary", "full_failure_baseline", "advisories"):
                    value = report.get(field)
                    if isinstance(value, str):
                        validated_report[field] = value
            if batch.get("state") == "draft_open":
                return await runtime.release_report_held_draft(
                    delivery, prepared["batch_key"], request=request, report=validated_report)
            if batch.get("state") in {"delivery_open", "in_delivery"}:
                raise ValueError("draft-output mode cannot adopt an existing ready publication")
            return await runtime.release_report_held_draft(
                delivery, prepared["batch_key"], request=request, report=validated_report)
        if batch.get("state") in {"delivery_open", "in_delivery"}:
            # A retry re-enters the existing runtime, whose correlated PR and
            # frozen snapshot reconciliation makes this an idempotent read.
            return await runtime.release(delivery, prepared["batch_key"], request=request)
        return await runtime.release(delivery, prepared["batch_key"], request=request)
