"""Bounded, project-scoped answers to ordinary technical coordinator questions.

This path is enabled only by the explicit technical_question_answer recovery
operation. It answers the exact current coordinator question as the resident
Butler; it never changes ticket work state or takes a worker/reviewer lease.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_CONTEXT_BYTES = 18_000
MAX_RELATED_TICKETS = 8
MAX_CANDIDATES_PER_SWEEP = 24
TERMINAL_TICKET_STATUSES = frozenset({"closed", "canceled", "cancelled", "terminated"})


def _terminal_ticket(ticket: Mapping[str, Any]) -> bool:
    return str(ticket.get("status", "")).casefold() in TERMINAL_TICKET_STATUSES


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()


def _state_doc(result: Any) -> tuple[str | None, dict[str, Any]]:
    state = result.get("state") if isinstance(result, Mapping) else None
    value = state.get("value") if isinstance(state, Mapping) else None
    if not isinstance(value, str):
        return None, {}
    document = json.loads(value)
    if not isinstance(document, dict):
        raise ValueError("technical question state is malformed")
    return value, document


async def _read_state(client: Any, key: str) -> tuple[str | None, dict[str, Any]]:
    try:
        return _state_doc(await client.board_state_get(key))
    except Exception as exc:
        if "state key not found" not in str(exc).casefold():
            raise
        return None, {}


async def _write_state(client: Any, key: str, previous: str | None,
                       document: Mapping[str, Any]) -> str:
    value = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    args: dict[str, Any] = {"agent_name": client.agent_name, "key": key, "value": value}
    if previous is None:
        args["expected_absent"] = True
    else:
        args["expected_sha256"] = hashlib.sha256(previous.encode()).hexdigest()
    await client._call("board_state_update", args)
    return value


def _question_key(policy: Any, ticket_id: str, question_id: str) -> str:
    return "technical_question_answer_" + _digest({
        "board": policy.board_id, "project": policy.project_id,
        "ticket": ticket_id, "question": question_id,
    })[:24]


def _candidate_sha(ticket: Mapping[str, Any]) -> str | None:
    submission = ticket.get("latest_submission")
    if not isinstance(submission, Mapping):
        history = ticket.get("submission_history")
        submission = history[-1] if isinstance(history, list) and history and isinstance(history[-1], Mapping) else {}
    proof = submission.get("submission_preflight") if isinstance(submission, Mapping) else None
    candidates: set[str] = set()
    if isinstance(proof, Mapping) and isinstance(proof.get("commit"), str):
        value = proof["commit"].lower()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            candidates.add(value)
    for key in ("candidate_sha", "commit_sha", "sha", "commit_hash"):
        value = submission.get(key) if isinstance(submission, Mapping) else None
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
            candidates.add(value.lower())
    notes = submission.get("notes") if isinstance(submission, Mapping) else None
    if isinstance(notes, str):
        matches = re.findall(r"(?m)^branch_and_commit:\s*[^\s@]+\s*@\s*([0-9a-fA-F]{40})\s*$", notes)
        candidates.update(value.lower() for value in matches)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _review(ticket: Mapping[str, Any]) -> Mapping[str, Any]:
    value = ticket.get("latest_verdict")
    if isinstance(value, Mapping):
        return value
    history = ticket.get("review_history")
    if isinstance(history, list) and history and isinstance(history[-1], Mapping):
        return history[-1]
    return {}


def _source_status(ticket: Mapping[str, Any]) -> dict[str, Any]:
    review = _review(ticket)
    submission = ticket.get("latest_submission")
    if not isinstance(submission, Mapping):
        history = ticket.get("submission_history")
        submission = history[-1] if isinstance(history, list) and history and isinstance(history[-1], Mapping) else {}
    candidate = _candidate_sha(ticket)
    submitter = submission.get("submitted_by_principal_id") if isinstance(submission, Mapping) else None
    reviewer_submitter = review.get("submitted_by_principal_id")
    reviewer = review.get("reviewed_by_principal_id")
    approved = (ticket.get("status") == "closed"
                and review.get("verdict") == "approve"
                and review.get("status_to") == "closed"
                and candidate is not None
                and review.get("submission_commit") == candidate
                and submitter and reviewer_submitter == submitter
                and reviewer and reviewer != submitter)
    # Project-list status, claims and leases are operational churn, not source
    # evidence. Retain only the stable candidate/independent-review facts.
    return {
        "candidate_sha": candidate,
        "accepted_sha": candidate if approved else None,
        "review_state": review.get("status_to"),
        "review_verdict": review.get("verdict"),
    }


def _workflow_dependency_ids(registry: Mapping[str, Any], policy: Any,
                             ticket_id: str) -> list[str] | None:
    """Prioritize prerequisites, then bounded same-workflow source references."""
    project = registry.get("projects", {}).get(policy.project_id)
    workflow = project.get("butler_workflow") if isinstance(project, Mapping) else None
    if workflow is None:
        if isinstance(project, Mapping) and project.get("butler_workflow_activation"):
            return None
        return []
    try:
        from pursers_client.project_workflow import parse_project_workflow
        if isinstance(workflow.get("nodes"), Mapping):
            # project_registry parsing returns the canonical workflow DAG in
            # this normalized shape; validate its binding before using it.
            if (workflow.get("project_id") != policy.project_id
                    or workflow.get("board_id") != policy.board_id
                    or workflow.get("sealed") is not True):
                return None
            parsed = workflow
        else:
            parsed = parse_project_workflow(workflow, project_id=policy.project_id,
                                            board_id=policy.board_id)
        by_ticket = {node["ticket_id"]: node for node in parsed["nodes"].values()}
        current = by_ticket.get(ticket_id)
        if current is None:
            return []
        by_id = parsed["nodes"]
        selected: list[str] = []
        seen: set[str] = set()

        def visit(node_id: str) -> None:
            for prerequisite in by_id[node_id]["requires"]:
                if prerequisite in seen:
                    continue
                seen.add(prerequisite)
                visit(prerequisite)
                selected.append(by_id[prerequisite]["ticket_id"])

        visit(current["node_id"])
        if len(selected) > MAX_RELATED_TICKETS:
            return None
        # A closed diagnostic harness may be a downstream node, rather than a
        # prerequisite. Include bounded registered cohort references too; never
        # turn the nonterminal work page into an all-board history query.
        selected.extend(sorted(set(by_ticket) - set(selected) - {ticket_id})[
            :MAX_RELATED_TICKETS - len(selected)])
        return selected
    except (ImportError, KeyError, TypeError, ValueError):
        return None


async def _load_workflow_dependencies(client: Any, registry: Mapping[str, Any],
                                      policy: Any, ticket: Mapping[str, Any],
                                      rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[str]] | None:
    identifiers = _workflow_dependency_ids(registry, policy, str(ticket.get("ticket_id", "")))
    if identifiers is None:
        return None
    available = {str(row.get("ticket_id")): row for row in rows if isinstance(row, Mapping)}
    for ticket_id in identifiers:
        # The bounded work page deliberately omits closed history and may omit
        # full submission/review evidence for nonterminal rows. Read each exact
        # registered prerequisite as full detail even when a page summary exists.
        try:
            result = await client.ticket_get(ticket_id, view="full")
        except Exception:
            return None
        dependency = result.get("ticket") if isinstance(result, Mapping) else None
        if not isinstance(dependency, Mapping) or dependency.get("ticket_id") != ticket_id:
            return None
        available[ticket_id] = dependency
    return list(available.values()), identifiers


def _evidence_projection(value: Any) -> Any:
    """Remove activity/telemetry metadata from source and review evidence."""
    if isinstance(value, Mapping):
        return {
            key: _evidence_projection(item) for key, item in value.items()
            if not key.endswith("_at")
            and key not in {"runtime_model_usage", "model_usage", "dispatch_state"}
        }
    if isinstance(value, list):
        return [_evidence_projection(item) for item in value]
    return value


def _question_projection(question: Mapping[str, Any]) -> dict[str, Any]:
    # No coordinator binding or unrelated question history enters the prompt.
    asker = question.get("asked_by")
    return {
        "question_id": question.get("question_id"),
        "message": question.get("message"),
        "kind": question.get("kind"),
        "state": question.get("state"),
        "asked_at": question.get("asked_at"),
        "asked_by": {key: asker.get(key) for key in ("agent_id", "agent_name", "principal_id")}
        if isinstance(asker, Mapping) else None,
        "accepted_by": question.get("accepted_by"),
        "in_reply_to": question.get("in_reply_to"),
    }


def _ticket_context(ticket: Mapping[str, Any]) -> dict[str, Any]:
    annotations = ticket.get("annotations")
    evidence_facts = []
    if isinstance(annotations, list):
        for row in reversed(annotations):
            if (not isinstance(row, Mapping)
                    or row.get("kind") not in {"authorization", "evidence", "decision"}
                    or not isinstance(row.get("text"), str)):
                continue
            fact = (row["kind"], row["text"])
            if fact not in evidence_facts:
                evidence_facts.append(fact)
            if len(evidence_facts) == 4:
                break
    # Annotation time/order is activity, not new source evidence. Keep a
    # deterministic bounded projection so replay and progress fingerprints
    # depend only on the actual scoped facts.
    evidence = [{"kind": kind, "text": text}
                for kind, text in sorted(evidence_facts)]
    return {
        "ticket_id": ticket.get("ticket_id"),
        "target_url": ticket.get("target_url"),
        "title": ticket.get("title"),
        "description": ticket.get("description"),
        "scope": ticket.get("scope"),
        "required_fields": ticket.get("required_fields", []),
        "forbidden": ticket.get("forbidden", []),
        "related_files": ticket.get("related_files", []),
        "status": ticket.get("status"),
        "claimed_by_agent_id": ticket.get("claimed_by_agent_id"),
        "claimed_by_principal_id": ticket.get("claimed_by_principal_id"),
        # Lease renewal and dispatch heartbeats are not new decision evidence.
        # The answer path never mutates these leases, so keep their timestamps
        # out of both the model input and its durable decision fingerprint.
        "review_in_progress": bool(ticket.get("review_lease")),
        "latest_submission": _evidence_projection(ticket.get("latest_submission")),
        "latest_verdict": _evidence_projection(ticket.get("latest_verdict")),
        "source_status": _source_status(ticket),
        "latest_scoped_evidence": evidence,
        "detail_ref": {"tool": "ticket_get", "ticket_id": ticket.get("ticket_id"), "view": "full"},
    }


def _related_summaries(rows: Sequence[Mapping[str, Any]], current: Mapping[str, Any],
                       registry: Mapping[str, Any], policy: Any,
                       dependency_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
    from pursers_client.project_registry import resolve_registry_target
    by_id = {str(row.get("ticket_id")): row for row in rows if isinstance(row, Mapping)}
    ordered = [by_id[ticket_id] for ticket_id in dependency_ids if ticket_id in by_id]
    ordered.extend(sorted((row for row in rows if isinstance(row, Mapping)
                   and str(row.get("ticket_id")) not in set(dependency_ids)),
                   key=lambda row: str(row.get("ticket_id", ""))))
    selected: list[dict[str, Any]] = []
    for row in ordered:
        # Terminal same-project tickets remain read-only source/review references.
        if row.get("ticket_id") == current.get("ticket_id"):
            continue
        try:
            route = resolve_registry_target(dict(registry), policy.board_id, str(row.get("target_url", "")))
        except (ValueError, TypeError):
            continue
        if route.get("project") != policy.project_id:
            continue
        selected.append({
            "ticket_id": row.get("ticket_id"),
            **_source_status(row),
            "detail_ref": {"tool": "ticket_get", "ticket_id": row.get("ticket_id"), "view": "full"},
        })
        if len(selected) >= MAX_RELATED_TICKETS:
            break
    return sorted(selected, key=lambda row: str(row.get("ticket_id", "")))


def build_context(*, registry: Mapping[str, Any], policy: Any,
                  question: Mapping[str, Any], ticket: Mapping[str, Any],
                  rows: Sequence[Mapping[str, Any]],
                  dependency_ids: Sequence[str] = (),
                  workspace_facts: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    from pursers_client.project_registry import resolve_registry_target
    try:
        route = resolve_registry_target(dict(registry), policy.board_id, str(ticket.get("target_url", "")))
    except (ValueError, TypeError):
        return None
    if (route.get("project") != policy.project_id
            or _terminal_ticket(ticket)
            or question.get("project") != policy.project_id
            or question.get("state") != "open"):
        return None
    context = {
        "operation": "technical_question_answer",
        **dict(workspace_facts or {}),
        "board_id": policy.board_id,
        "project_id": policy.project_id,
        "activation_id": policy.activation_id,
        "authorized_operations": list(policy.operations),
        "question": _question_projection(question),
        "ticket": _ticket_context(ticket),
        "related_ticket_summaries": _related_summaries(
            rows, ticket, registry, policy, dependency_ids),
        "selection": "One exact current pending coordinator question; at most eight same-project source summaries, prioritizing registered prerequisites and workflow members including closed accepted foundations or harnesses. Remaining history is not loaded. Candidate and independently accepted exact source revisions remain distinct. References identify additional detail required.",
    }
    if len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_CONTEXT_BYTES:
        return None
    return context


def _progress_identity(context: Mapping[str, Any]) -> tuple[str, str]:
    """Bound repeated questions independently of IDs, leases and model changes."""
    question = context["question"]
    meaning = {key: question.get(key) for key in ("kind", "message")}
    ticket = context["ticket"]
    evidence = {key: ticket.get(key) for key in (
        "ticket_id", "target_url", "title", "description", "scope",
        "required_fields", "forbidden", "related_files", "latest_submission",
        "latest_verdict", "latest_scoped_evidence",
    )}
    workspace = {key: context.get(key) for key in (
        "workspace_status", "workspace_branch", "workspace_head", "workspace_source_progress",
        "workspace_source_progress_status",
        "workspace_dirty_entries", "workspace_diagnostics_boundary",
        "workspace_path", "workspace_diagnostics_root", "workspace_allowed_actions",
        "runner_checkpoint_status", "runner_failure_class", "runner_checkpoint_ref",
        "workspace_error_class", "workspace_changed_paths",
    )}
    related = sorted(
        ({key: row.get(key) for key in (
            "ticket_id", "candidate_sha", "accepted_sha", "review_state", "review_verdict"
        )} for row in context.get("related_ticket_summaries", [])),
        key=lambda row: str(row.get("ticket_id", "")),
    )
    key = "technical_question_budget_" + _digest({
        "board": context["board_id"], "project": context["project_id"],
        "ticket": ticket["ticket_id"], "question": meaning,
    })[:24]
    return key, _digest({"ticket": evidence, "workspace": workspace, "related": related,
                         "activation_id": context["activation_id"],
                         "operations": context["authorized_operations"]})


class TechnicalQuestionAnswerer:
    """One fair, durable answer attempt using the resident coordinator identity."""
    def __init__(self, backend: Any, *, clock=time.time):
        self.backend, self.clock = backend, clock
        self.validator = None

    async def _workspace_facts(self, registry, policy, ticket):
        reader = getattr(self.backend, "_technical_recovery_workspace_evidence", None)
        if reader is None:
            return {}
        try:
            facts = await reader(registry, {
                "board_id": policy.board_id, "project_id": policy.project_id,
                "ticket": ticket,
            })
        except Exception as exc:
            return {"workspace_status": "unverified", "workspace_error_class": type(exc).__name__}
        if not isinstance(facts, Mapping):
            return {"workspace_status": "unverified", "workspace_error_class": "InvalidProjection"}
        return dict(facts)

    async def candidates(self, client: Any, registry: Mapping[str, Any], policy: Any,
                         rows: Sequence[Mapping[str, Any]], *, cursor: str | None = None) -> list[dict[str, Any]]:
        if "technical_question_answer" not in policy.operations:
            return []
        page_ids = {str(row.get("ticket_id")) for row in rows if isinstance(row, Mapping)}
        # The inbox has a bounded 100-row response and no global cursor. Query
        # only page tickets already projecting an open question; otherwise an
        # old unrelated backlog could permanently hide newer same-project CQs.
        question_tickets = sorted({
            str(row.get("ticket_id")) for row in rows if isinstance(row, Mapping)
            and not _terminal_ticket(row)
            and any(isinstance(q, Mapping) and q.get("state") == "open"
                    and q.get("project") == policy.project_id
                    for q in row.get("coordinator_questions", []))
        })
        inbox = []
        for ticket_id in question_tickets:
            result = await client.board_question_inbox(
                state="open", ticket_id=ticket_id, limit=100)
            items = result.get("questions") if isinstance(result, Mapping) else None
            if isinstance(items, list):
                inbox.extend(items)
        selected = [q for q in inbox if isinstance(q, Mapping)
                    and q.get("state") == "open"
                    and q.get("ticket_id") in page_ids
                    and q.get("project") == policy.project_id
                    and isinstance(q.get("question_id"), str)]
        selected.sort(key=lambda q: (str(q.get("asked_at", "")), str(q.get("question_id", ""))))
        if selected and cursor:
            cursor_key = tuple(str(v) for v in cursor.split("|", 1))
            keys = [(str(q.get("asked_at", "")), str(q.get("question_id", ""))) for q in selected]
            start = next((i for i, key in enumerate(keys) if key > cursor_key), 0)
            selected = selected[start:] + selected[:start]
        output = []
        seen_ids: set[tuple[str, str]] = set()
        for question in selected[:MAX_CANDIDATES_PER_SWEEP]:
            ticket_id = str(question["ticket_id"])
            payload = await client.ticket_get(ticket_id, view="full")
            ticket = payload.get("ticket") if isinstance(payload, Mapping) else None
            if not isinstance(ticket, Mapping):
                continue
            current_question = next((q for q in ticket.get("coordinator_questions", [])
                                     if isinstance(q, Mapping) and q.get("question_id") == question["question_id"]), None)
            if (_terminal_ticket(ticket)
                    or not isinstance(current_question, Mapping)
                    or current_question.get("state") != "open"
                    or current_question.get("project") != policy.project_id
                    or current_question.get("asked_by") != question.get("asked_by")):
                continue
            dependency_result = await _load_workflow_dependencies(
                client, registry, policy, ticket, rows)
            if dependency_result is None:
                continue
            context_rows, dependency_ids = dependency_result
            facts = await self._workspace_facts(registry, policy, ticket)
            context = build_context(registry=registry, policy=policy,
                                    question=current_question, ticket=ticket,
                                    rows=context_rows, dependency_ids=dependency_ids,
                                    workspace_facts=facts)
            key = _question_key(policy, ticket_id, str(question["question_id"]))
            previous, old = await _read_state(client, key)
            provider_contract: Mapping[str, Any] = {}
            contract_reader = getattr(self.backend, "_technical_recovery_provider_contract", None)
            if context is not None and contract_reader is not None:
                try:
                    provider_contract = await contract_reader(context)
                except Exception:
                    # No paid call or stale answer if semantic provider identity
                    # cannot be read from the configured backend.
                    continue
            digest = _digest({"context": context, "policy": policy.__dict__,
                              "provider_contract": provider_contract}) if context is not None else None
            # A question accepted by another live project coordinator belongs
            # to that owner. Do not race or replace its answer, and do not mark
            # this observation complete so it can be reconsidered if released.
            accepted_by = current_question.get("accepted_by")
            own_agent = getattr(getattr(client, "identity", None), "agent_id", None)
            if isinstance(client.identity, Mapping):
                own_agent = client.identity.get("agent_id")
            if isinstance(accepted_by, Mapping) and accepted_by.get("agent_id") != own_agent:
                continue
            if digest is not None and old.get("input_digest") == digest and old.get("status") in {"completed", "escalated"}:
                continue
            validator = self.validator
            if validator is None:
                import technical_recovery
                validator = technical_recovery.validate_recovery_recommendation
            output.append({"client": client, "registry": registry, "policy": policy,
                           "rows": rows, "workflow_reference_ids": dependency_ids,
                           "question": dict(current_question), "ticket": dict(ticket),
                           "context": context, "key": key, "previous": previous, "old": old,
                           "input_digest": digest, "provider_contract": dict(provider_contract),
                           "supervisor": self.supervisor,
                           "validate_recommendation": validator})
            seen_ids.add((ticket_id, str(current_question["question_id"])))

        # A response can be committed while both its RPC response and the first
        # receipt read are lost. Revisit only durable applying operations whose
        # exact question is now answered; this is read-only and never pays again.
        validator = self.validator
        if validator is None:
            import technical_recovery
            validator = technical_recovery.validate_recovery_recommendation
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            ticket_id = str(row.get("ticket_id", ""))
            if ticket_id not in page_ids:
                continue
            for question in row.get("coordinator_questions", []):
                if (not isinstance(question, Mapping) or question.get("state") != "answered"
                        or question.get("project") != policy.project_id
                        or not isinstance(question.get("question_id"), str)
                        or (ticket_id, question["question_id"]) in seen_ids):
                    continue
                key = _question_key(policy, ticket_id, question["question_id"])
                previous, old = await _read_state(client, key)
                if old.get("status") != "applying":
                    continue
                payload = await client.ticket_get(ticket_id, view="full")
                ticket = payload.get("ticket") if isinstance(payload, Mapping) else None
                exact = next((q for q in ticket.get("coordinator_questions", [])
                              if isinstance(q, Mapping) and q.get("question_id") == question["question_id"]), None) if isinstance(ticket, Mapping) else None
                if not isinstance(exact, Mapping) or exact.get("state") != "answered":
                    continue
                output.append({"client": client, "registry": registry, "policy": policy,
                               "rows": rows, "question": dict(exact), "ticket": dict(ticket),
                               "context": None, "key": key, "previous": previous, "old": old,
                               "input_digest": old.get("input_digest"), "supervisor": self.supervisor,
                               "validate_recommendation": validator,
                               "receipt_only": True})
        if output:
            last = output[-1]["question"]
            output[-1]["next_cursor"] = f"{last.get('asked_at', '')}|{last.get('question_id', '')}"
        return output

    async def answer(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        client, policy = candidate["client"], candidate["policy"]
        question, ticket_id = candidate["question"], candidate["ticket"].get("ticket_id")
        qid, key = question["question_id"], candidate["key"]
        old, previous = dict(candidate["old"]), candidate["previous"]
        digest = candidate["input_digest"]
        base = {"ticket_id": ticket_id, "question_id": qid}
        authorized = getattr(self.backend, "_technical_recovery_authorized", None)
        if candidate.get("receipt_only"):
            # An already answered exact question may still need read-only
            # own-principal receipt reconciliation after its ticket closes.
            proposal = old.get("proposal")
            receipt = (await client.ticket_get(str(ticket_id), view="full")).get("ticket")
            answered = next((q for q in receipt.get("coordinator_questions", [])
                             if isinstance(q, Mapping) and q.get("question_id") == qid), None) if isinstance(receipt, Mapping) else None
            actor = answered.get("answered_by") if isinstance(answered, Mapping) else None
            principal = _principal_id(client)
            if (not principal or not isinstance(proposal, Mapping)
                    or not isinstance(answered, Mapping) or answered.get("state") != "answered"
                    or answered.get("answer") != proposal.get("note")
                    or not isinstance(actor, Mapping) or actor.get("principal_id") != principal):
                return {**base, "status": "mutation_unknown"}
            reservation = dict(old)
            reservation.update(status="completed", completed_at=self.clock(),
                               answered_by_principal_id=principal)
            await _write_state(client, key, previous, reservation)
            return {**base, "status": "completed", "cached": True}
        if _terminal_ticket(candidate["ticket"]):
            return {**base, "status": "terminal_ticket"}
        if candidate["context"] is None:
            return {**base, "status": "detail_required"}
        if digest is None:
            return {**base, "status": "detail_required"}
        if old.get("input_digest") == digest and old.get("status") == "completed":
            return {**base, "status": "completed", "cached": True}
        if old.get("input_digest") == digest and old.get("status") == "escalated":
            return {**base, "status": "escalated", "cached": True}
        same_input = old.get("input_digest") == digest
        if same_input and old.get("status") in {"completed", "escalated"}:
            return {**base, "status": old["status"], "cached": True}
        if same_input and old.get("status") == "deciding":
            # A provider outcome without a persisted proposal is ambiguous; do
            # not pay again until evidence or policy changes.
            return {**base, "status": "reconciliation_required", "cached": True}
        if same_input and old.get("status") == "provider_failed":
            if old.get("attempts", 0) >= policy.max_no_progress_attempts:
                return {**base, "status": "no_progress_budget_exhausted", "cached": True}
            if old.get("next_attempt_at", 0) > self.clock():
                return {**base, "status": "backoff", "cached": True}
        if same_input and old.get("status") == "applying":
            # Never purchase a second recommendation when durable state already
            # contains the exact proposal. Unknown mutations stay read-only
            # until the same question, evidence, or policy changes.
            return {**base, "status": "reconciliation_required", "cached": True}
        same_question = old.get("question_asked_by") == question.get("asked_by")
        if same_question and old.get("status") in {"applying", "mutation_unknown"}:
            # A changed policy/evidence/provider fingerprint is not permission
            # to overwrite an ambiguous answer attempt for the same question.
            return {**base, "status": "reconciliation_required", "cached": True}
        if old.get("status") in {"applying", "mutation_unknown"}:
            # Exact question ID is never answered twice, even if a redelivery
            # has different projection data. Receipt reconciliation above is
            # the only safe route after a mutation may have been sent.
            return {**base, "status": "reconciliation_required", "cached": True}
        if same_input and old.get("status") in {"decided", "superseded"}:
            # The decision is durable but no mutation has been sent. Replay
            # that exact proposal through fresh authority/evidence fences.
            proposal = candidate["validate_recommendation"](
                old.get("proposal"), context=candidate["context"])
            reservation = dict(old)
        elif old.get("status") == "decided":
            return {**base, "status": "reconciliation_required", "cached": True}
        else:
            if authorized is not None and not await authorized(candidate["context"]):
                return {**base, "status": "kill_switch"}
            budget_key, progress = _progress_identity(candidate["context"])
            budget_previous, budget = await _read_state(client, budget_key)
            same_progress = budget.get("progress_digest") == progress
            used = budget.get("attempts", 0) if same_progress else 0
            if used >= policy.max_no_progress_attempts:
                return {**base, "status": "no_progress_budget_exhausted", "cached": True}
            try:
                await _write_state(client, budget_key, budget_previous, {
                    "progress_digest": progress, "attempts": used + 1,
                    "question_id": qid,
                })
            except Exception:
                return {**base, "status": "reservation_conflict"}
            reservation = {
                "schema_version": 1, "status": "deciding", "input_digest": digest,
                "attempts": (old.get("attempts", 0) if same_input else 0) + 1,
                "next_attempt_at": self.clock() + policy.retry_backoff_s,
                "operation_id": _digest({"key": key, "input": digest}),
                "board_id": policy.board_id, "project_id": policy.project_id,
                "ticket_id": ticket_id, "question_id": qid,
                "question_asked_by": question.get("asked_by"),
            }
            try:
                previous = await _write_state(client, key, previous, reservation)
            except Exception:
                return {**base, "status": "reservation_conflict"}
            try:
                recommendation = await self.backend._technical_recovery_choose(candidate["context"])
                proposal = candidate["validate_recommendation"](
                    recommendation, context=candidate["context"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reservation.update(status="provider_failed", error_class=type(exc).__name__)
                await _write_state(client, key, previous, reservation)
                return {**base, "status": "provider_failed"}
            reservation.update(status="decided", proposal=proposal)
            previous = await _write_state(client, key, previous, reservation)
        if proposal["decision"] == "escalate":
            reservation.update(status="escalated")
            await _write_state(client, key, previous, reservation)
            return {**base, "status": "escalated"}

        # Re-check route, explicit opt-in, kill switch and this one exact open
        # question. A concurrent different question is deliberately untouched.
        fresh_raw, fresh_registry = await candidate["supervisor"]._registry()
        fresh_policy = candidate["supervisor"]._policy(fresh_raw, policy.project_id)
        latest = (await client.ticket_get(str(ticket_id), view="full")).get("ticket")
        if not isinstance(latest, Mapping):
            reservation.update(status="superseded")
            await _write_state(client, key, previous, reservation)
            return {**base, "status": "superseded"}
        current = next((q for q in latest.get("coordinator_questions", [])
                        if isinstance(q, Mapping) and q.get("question_id") == qid), None)
        if (fresh_policy is None or _workflow_dependency_ids(fresh_registry, fresh_policy, str(ticket_id))
                != candidate.get("workflow_reference_ids", [])):
            reservation.update(status="superseded")
            await _write_state(client, key, previous, reservation)
            return {**base, "status": "superseded"}
        # Re-read each bounded same-project reference used by the proposal.
        # In particular, a candidate SHA becoming independently accepted while
        # the provider call is in flight must invalidate the stale answer.
        fresh_rows = []
        for summary in candidate["context"].get("related_ticket_summaries", []):
            reference = summary.get("ticket_id") if isinstance(summary, Mapping) else None
            if not isinstance(reference, str):
                continue
            detail = await client.ticket_get(reference, view="full")
            related = detail.get("ticket") if isinstance(detail, Mapping) else None
            if not isinstance(related, Mapping):
                fresh_rows = []
                break
            fresh_rows.append(related)
        fresh_context = build_context(
            registry=fresh_registry, policy=fresh_policy, question=current or {},
            ticket=latest, rows=fresh_rows,
            dependency_ids=[str(row["ticket_id"]) for row in
                            candidate["context"].get("related_ticket_summaries", [])
                            if isinstance(row, Mapping) and isinstance(row.get("ticket_id"), str)],
            workspace_facts=(await self._workspace_facts(fresh_registry, fresh_policy, latest)
                             if fresh_policy is not None else {})) if fresh_policy is not None else None
        try:
            fresh_authorized = (authorized is None or await authorized(candidate["context"]))
        except Exception:
            fresh_authorized = False
        if (fresh_policy is None or fresh_policy != policy
                or "technical_question_answer" not in policy.operations
                or not candidate["supervisor"].enabled()
                or not fresh_authorized
                or current is None or current.get("state") != "open"
                or current.get("asked_by") != question.get("asked_by")
                or current.get("accepted_by") != question.get("accepted_by")
                or fresh_context != candidate["context"]):
            reservation.update(status="superseded")
            await _write_state(client, key, previous, reservation)
            return {**base, "status": "superseded"}

        message = proposal["note"]
        reservation.update(status="applying", answer=message)
        previous = await _write_state(client, key, previous, reservation)
        principal = _principal_id(client)
        try:
            await client.ticket_question_answer(str(ticket_id), str(qid), action="answer", message=message)
        except Exception:
            # Reconcile a lost response only against the exact message and our
            # own authenticated principal; never send a second answer.
            receipt = (await client.ticket_get(str(ticket_id), view="full")).get("ticket")
            answered = next((q for q in receipt.get("coordinator_questions", [])
                             if isinstance(q, Mapping) and q.get("question_id") == qid), None) if isinstance(receipt, Mapping) else None
            actor = answered.get("answered_by") if isinstance(answered, Mapping) else None
            if (not principal or not isinstance(answered, Mapping)
                    or answered.get("state") != "answered" or answered.get("answer") != message
                    or not isinstance(actor, Mapping) or actor.get("principal_id") != principal):
                return {**base, "status": "mutation_unknown"}
        receipt = (await client.ticket_get(str(ticket_id), view="full")).get("ticket")
        answered = next((q for q in receipt.get("coordinator_questions", [])
                         if isinstance(q, Mapping) and q.get("question_id") == qid), None) if isinstance(receipt, Mapping) else None
        actor = answered.get("answered_by") if isinstance(answered, Mapping) else None
        if (not principal or not isinstance(answered, Mapping)
                or answered.get("state") != "answered" or answered.get("answer") != message
                or not isinstance(actor, Mapping) or actor.get("principal_id") != principal):
            raise ValueError("technical question answer receipt did not match this Butler operation")
        reservation.update(status="completed", completed_at=self.clock(),
                           answered_by_principal_id=principal)
        await _write_state(client, key, previous, reservation)
        return {**base, "status": "answered"}


def _principal_id(client: Any) -> str | None:
    identity = getattr(client, "identity", None)
    value = identity.get("principal_id") if isinstance(identity, Mapping) else getattr(identity, "principal_id", None)
    return value if isinstance(value, str) and value else None
