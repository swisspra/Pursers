"""Shared validation for explicit resident-Butler project workflow contracts."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any

_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def policy_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def final_validation_ticket_id(policy: Mapping[str, Any]) -> str:
    """Return an isolated deterministic QA ID for this exact project route."""
    identity = [policy["board_id"], policy["project_id"],
                policy["workflow_id"], policy["cohort_id"]]
    return "TK-wf-" + policy_digest(identity)[:32]


def final_validation_workspace_identity(
    registry: Mapping[str, Any], board_id: str, ticket: Mapping[str, Any]
) -> dict[str, str] | None:
    """Verify that one ticket is the exact final-QA task of an active workflow.

    Ticket prose and tags are not authority. The routed project must carry a
    successfully normalized sealed workflow and active matching activation;
    the deterministic ticket ID and immutable paged evidence packet must bind
    the same board, project, workflow, cohort, and combined Git revision.
    """
    if not isinstance(ticket, Mapping) or not isinstance(ticket.get("target_url"), str):
        return None
    from .project_registry import resolve_registry_target
    try:
        route = resolve_registry_target(dict(registry), board_id, ticket["target_url"])
    except (TypeError, ValueError):
        return None
    project_id = route.get("project")
    project = registry.get("projects", {}).get(project_id)
    if (not isinstance(project, Mapping) or project.get("board_id") != board_id
            or project.get("status") != "active" or project.get("fleet", True) is not True
            or project.get("butler_workflow_error") is not None):
        return None
    policy, activation = project.get("butler_workflow"), project.get("butler_workflow_activation")
    if not isinstance(policy, Mapping) or not isinstance(activation, Mapping):
        return None
    try:
        raw_policy = policy
        if isinstance(policy.get("nodes"), Mapping):
            raw_policy = {**dict(policy), "nodes": [
                dict(policy["nodes"][key]) for key in sorted(policy["nodes"])]}
        normalized = parse_project_workflow(raw_policy, project_id=project_id, board_id=board_id)
        if parse_workflow_activation(activation, normalized) is None:
            return None
        if ticket.get("ticket_id") != final_validation_ticket_id(normalized):
            return None
        index = parse_final_validation_packet_index(ticket.get("description"))
    except (KeyError, TypeError, ValueError):
        return None
    if any(index.get(key) != expected for key, expected in (
        ("workflow_id", normalized["workflow_id"]), ("cohort_id", normalized["cohort_id"]),
        ("project_id", project_id), ("board_id", board_id),
    )):
        return None

    annotations = ticket.get("annotations")
    if (not isinstance(annotations, list) or ticket.get("annotations_omitted_count", 0) != 0
            or ticket.get("annotation_count", len(annotations)) != len(annotations)):
        return None
    pages: dict[int, str] = {}
    packet_sha = index["evidence_packet"]["sha256"]
    page_count = index["evidence_packet"]["chunks"]
    for row in annotations:
        if not isinstance(row, Mapping) or row.get("kind") != "evidence":
            continue
        if not isinstance(row.get("text"), str):
            continue
        text = row["text"]
        if not text.startswith("butler-workflow-packet:v1:"):
            continue
        match = re.fullmatch(r"butler-workflow-packet:v1:([0-9a-f]{64}):(\d+)/(\d+):(.*)", text, re.S)
        if (match is None or match.group(1) != packet_sha
                or int(match.group(2)) in pages or int(match.group(3)) != page_count
                or not 1 <= int(match.group(2)) <= page_count):
            return None
        pages[int(match.group(2))] = match.group(4)
    if set(pages) != set(range(1, page_count + 1)):
        return None
    encoded = "".join(pages[number] for number in range(1, page_count + 1))
    if hashlib.sha256(encoded.encode()).hexdigest() != packet_sha:
        return None
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    expected_members = {node["ticket_id"] for node in normalized["nodes"].values()}
    delivery = payload.get("workflow_delivery") if isinstance(payload, Mapping) else None
    if (not isinstance(payload, Mapping) or payload.get("schema_version") != 1
            or payload.get("workflow_id") != normalized["workflow_id"]
            or payload.get("cohort_id") != normalized["cohort_id"]
            or payload.get("project_id") != project_id or payload.get("board_id") != board_id
            or payload.get("combined_revision") != index["combined_revision"]
            or set(payload.get("members", {})) != expected_members
            or not isinstance(delivery, Mapping)
            or delivery.get("cohort_id") != normalized["cohort_id"]
            or delivery.get("combined_revision") != index["combined_revision"]):
        return None
    return {"project_id": project_id, "combined_revision": index["combined_revision"]}


def ticket_belongs_to_project(ticket: Any, registry: Mapping[str, Any], *,
                              board_id: str, project_id: str) -> bool:
    """Resolve ticket routing against the authoritative registry, fail closed."""
    if not isinstance(ticket, Mapping) or not isinstance(ticket.get("target_url"), str):
        return False
    from .project_registry import RegistryRoutingError, resolve_registry_target
    try:
        route = resolve_registry_target(dict(registry), board_id, ticket["target_url"])
    except (RegistryRoutingError, TypeError, ValueError):
        return False
    return route.get("project") == project_id and route.get("board_id") == board_id


def parse_project_workflow(value: Any, *, project_id: str, board_id: str) -> dict[str, Any]:
    """Require a complete sealed DAG and exact project route; reject ambiguity."""
    fields = {"schema_version", "workflow_id", "project_id", "board_id", "cohort_id",
              "sealed", "nodes", "final_validation"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("butler_workflow is partial or contains unsupported fields")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("butler_workflow.schema_version is unsupported")
    for name in ("workflow_id", "project_id", "board_id", "cohort_id"):
        field = value.get(name)
        if not isinstance(field, str) or not field or field != field.strip() or len(field) > 200:
            raise ValueError(f"butler_workflow.{name} is invalid")
    if value["project_id"] != project_id or value["board_id"] != board_id:
        raise ValueError("butler_workflow project/board route mismatch")
    if value.get("sealed") is not True:
        raise ValueError("butler_workflow cohort is not explicitly sealed")
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes or len(raw_nodes) > 100:
        raise ValueError("butler_workflow.nodes must be a complete bounded non-empty list")
    nodes: dict[str, dict[str, Any]] = {}
    ticket_ids: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, Mapping) or set(raw) != {"node_id", "ticket_id", "requires", "acceptance_checks"}:
            raise ValueError("butler_workflow node is partial or contains unsupported fields")
        node_id, ticket_id = raw.get("node_id"), raw.get("ticket_id")
        if not isinstance(node_id, str) or _ID.fullmatch(node_id) is None:
            raise ValueError("butler_workflow node_id is invalid")
        if not isinstance(ticket_id, str) or _ID.fullmatch(ticket_id) is None:
            raise ValueError("butler_workflow ticket_id is invalid")
        if node_id in nodes or ticket_id in ticket_ids:
            raise ValueError("butler_workflow node and ticket membership must be unique")
        requires, checks = raw.get("requires"), raw.get("acceptance_checks")
        if (not isinstance(requires, list) or len(requires) > 100
                or any(not isinstance(dep, str) or _ID.fullmatch(dep) is None for dep in requires)
                or len(set(requires)) != len(requires) or node_id in requires):
            raise ValueError("butler_workflow prerequisites are invalid")
        if (not isinstance(checks, list) or not checks or len(checks) > 30
                or any(not isinstance(check, str) or not check.strip() or len(check) > 1000 for check in checks)):
            raise ValueError("butler_workflow acceptance_checks must be meaningful")
        nodes[node_id] = {"node_id": node_id, "ticket_id": ticket_id,
                          "requires": list(requires), "acceptance_checks": list(checks)}
        ticket_ids.add(ticket_id)
    for node in nodes.values():
        if any(dep not in nodes for dep in node["requires"]):
            raise ValueError("butler_workflow prerequisite is outside its sealed cohort")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("butler_workflow prerequisite graph contains a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in nodes[node_id]["requires"]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)
    for node_id in nodes:
        visit(node_id)
    final = value.get("final_validation")
    if (not isinstance(final, Mapping) or set(final) != {"title", "acceptance_checks"}
            or not isinstance(final.get("title"), str) or not final["title"].strip()
            or len(final["title"]) > 300
            or not isinstance(final.get("acceptance_checks"), list)
            or not final["acceptance_checks"] or len(final["acceptance_checks"]) > 30
            or any(not isinstance(check, str) or not check.strip() or len(check) > 1000
                   for check in final["acceptance_checks"])):
        raise ValueError("butler_workflow.final_validation is incomplete")
    return {"schema_version": 1, "workflow_id": value["workflow_id"],
            "project_id": project_id, "board_id": board_id, "cohort_id": value["cohort_id"],
            "sealed": True, "nodes": nodes,
            "final_validation": {"title": final["title"],
                                 "acceptance_checks": list(final["acceptance_checks"])}}


def parse_workflow_activation(value: Any, policy: Mapping[str, Any]) -> dict[str, Any] | None:
    if value is None:
        return None
    fields = {"schema_version", "state", "policy_sha256", "activation_id"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("butler_workflow_activation has unsupported or missing fields")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("butler_workflow_activation schema_version is unsupported")
    if value.get("state") != "active":
        raise ValueError("butler_workflow_activation must explicitly be active")
    digest = value.get("policy_sha256")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None or digest != policy_digest(policy):
        raise ValueError("butler_workflow_activation does not pin the effective workflow")
    activation_id = value.get("activation_id")
    if not isinstance(activation_id, str) or not activation_id.strip() or len(activation_id) > 128:
        raise ValueError("butler_workflow_activation.activation_id is invalid")
    return {"schema_version": 1, "state": "active", "policy_sha256": digest,
            "activation_id": activation_id}


_FINAL_PACKET_CHARS = 3_600
_FINAL_PACKET_MAX_ANNOTATIONS = 50
_FINAL_PACKET_FRAME = "Butler workflow final-validation evidence index v1\n"


def parse_final_validation_packet_index(description: Any) -> dict[str, Any]:
    """Parse the explicit plain-text frame around the QA evidence index.

    Central's SDK JSON-coerces string arguments that look like bare JSON before
    validating them. The human-readable prefix keeps descriptions a string;
    strict symmetric parsing prevents accepting malformed or ambiguous frames.
    """
    if not isinstance(description, str) or not description.startswith(_FINAL_PACKET_FRAME):
        raise ValueError("final-validation description has an unsupported evidence frame")
    try:
        value = json.loads(description[len(_FINAL_PACKET_FRAME):])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("final-validation evidence frame is malformed") from exc
    fields = {"workflow_id", "cohort_id", "project_id", "board_id", "combined_revision",
              "evidence_packet", "customer_final_merge"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("final-validation evidence index is incomplete or ambiguous")
    if (any(not isinstance(value.get(key), str) or not value[key]
            for key in ("workflow_id", "cohort_id", "project_id", "board_id"))
            or _SHA.fullmatch(value.get("combined_revision", "")) is None
            or value.get("customer_final_merge") != "manual"):
        raise ValueError("final-validation evidence index has invalid identity")
    packet = value.get("evidence_packet")
    if (not isinstance(packet, dict) or set(packet) != {"schema_version", "sha256", "chunks", "encoding"}
            or type(packet.get("schema_version")) is not int or packet["schema_version"] != 1
            or _DIGEST.fullmatch(packet.get("sha256", "")) is None
            or type(packet.get("chunks")) is not int or not 1 <= packet["chunks"] <= _FINAL_PACKET_MAX_ANNOTATIONS
            or packet.get("encoding") != "utf-8-json-text; ordered ticket evidence annotations"):
        raise ValueError("final-validation evidence packet index is invalid")
    return value


def final_validation_packet(
    policy: Mapping[str, Any], final: Mapping[str, Any], *,
    delivery: Mapping[str, Any] | None = None,
    member_evidence: Mapping[str, Any] | None = None,
    delivery_attestation: str | None = None,
) -> tuple[str, list[str]]:
    """Serialize the complete immutable QA contract into readable bounded pages.

    Central limits descriptions to 5,000 characters and annotation text to
    4,000. Keep a compact, verifiable index in description and page the entire
    member/evidence/check packet into annotations without truncation.
    """
    payload = {
        "schema_version": 1,
        "workflow_id": policy["workflow_id"],
        "cohort_id": policy["cohort_id"],
        "project_id": policy["project_id"],
        "board_id": policy["board_id"],
        "combined_revision": final["combined_revision"],
        "members": dict(sorted(final["members"].items())),
        "acceptance_checks": list(final["acceptance_checks"]),
        "customer_final_merge": "manual",
    }
    if delivery is not None:
        payload["workflow_delivery"] = dict(delivery)
    if delivery_attestation is not None:
        if not isinstance(delivery_attestation, str) or re.fullmatch(
                r"workflow_delivery_sha256: [0-9a-f]{64}", delivery_attestation) is None:
            raise ValueError("final-validation delivery attestation is malformed")
        payload["workflow_delivery_attestation"] = delivery_attestation
        payload["qa_instructions"] = {
            "worker": ("Run every listed check on the exact combined_revision. Submit the actual combined SHA "
                       f"and copy this line exactly once into submission notes: {delivery_attestation}"),
            "reviewer": ("Independently inspect the complete paged packet, member sources, and test evidence. "
                         "Approve only this exact combined SHA; copy this line exactly once into review_notes: "
                         f"{delivery_attestation}"),
        }
    if member_evidence is not None:
        payload["member_evidence"] = dict(sorted(member_evidence.items()))
    # Central's text cleaner strips surrounding Unicode whitespace from an
    # annotation. Escape spaces and non-ASCII characters in string values so a
    # chunk boundary can never put trim-sensitive content at a page edge; the
    # packet remains canonical JSON semantically and is reconstructed exactly.
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    encoded = encoded.replace(" ", r"\u0020")
    encoded_bytes = encoded.encode("utf-8")
    packet_sha = hashlib.sha256(encoded_bytes).hexdigest()
    # Central's limit is on annotation characters, while transport/proxy layers
    # may impose a byte cap. Bound UTF-8 bytes too and never split a code point.
    slices = []
    offset = 0
    while offset < len(encoded_bytes):
        end = min(offset + _FINAL_PACKET_CHARS, len(encoded_bytes))
        while end > offset:
            try:
                chunk = encoded_bytes[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == offset:
            raise ValueError("final-validation packet contains an invalid UTF-8 boundary")
        slices.append(chunk)
        offset = end
    if not slices:
        slices = [""]
    if len(slices) > _FINAL_PACKET_MAX_ANNOTATIONS:
        raise ValueError("complete final-validation evidence exceeds Central annotation capacity")
    description = _FINAL_PACKET_FRAME + json.dumps({
        "workflow_id": payload["workflow_id"],
        "cohort_id": payload["cohort_id"],
        "project_id": payload["project_id"],
        "board_id": payload["board_id"],
        "combined_revision": payload["combined_revision"],
        "evidence_packet": {
            "schema_version": 1,
            "sha256": packet_sha,
            "chunks": len(slices),
            "encoding": "utf-8-json-text; ordered ticket evidence annotations",
        },
        "customer_final_merge": "manual",
    }, sort_keys=True, separators=(",", ":"))
    pages = [f"butler-workflow-packet:v1:{packet_sha}:{index + 1}/{len(slices)}:{chunk}"
             for index, chunk in enumerate(slices)]
    if len(description) > 5_000 or any(len(page) > 4_000 for page in pages):
        raise AssertionError("workflow QA packet exceeds Central storage limits")
    return description, pages

def accepted_candidate(ticket: Any) -> tuple[bool, str | None]:
    """Only current exact-SHA independent review establishes node acceptance."""
    if not isinstance(ticket, Mapping) or ticket.get("status") != "closed":
        return False, None
    human = ticket.get("human_request")
    if isinstance(human, Mapping):
        resolution = human.get("resolution")
        if not isinstance(resolution, Mapping) or resolution.get("action") not in {"accept", "resolve", "dismiss"}:
            return False, None
    submission, review = ticket.get("latest_submission"), ticket.get("latest_verdict")
    if not isinstance(submission, Mapping) or not isinstance(review, Mapping):
        return False, None
    from .submission_evidence import submission_identity
    _branch, sha = submission_identity(submission)
    submitter = submission.get("submitted_by_principal_id")
    reviewer = review.get("reviewed_by_principal_id")
    if (not sha or _SHA.fullmatch(sha) is None
            or review.get("verdict", ticket.get("review_verdict")) != "approve"
            or review.get("status_to") != "closed" or review.get("submission_commit") != sha
            or not isinstance(submitter, str) or not submitter
            or not isinstance(reviewer, str) or not reviewer or reviewer == submitter):
        return False, None
    return True, sha


def _live_ticket_ownership(ticket: Mapping[str, Any], *, now: float) -> bool:
    """Fail closed on an active lease, not a retained historical holder."""
    status = ticket.get("status")
    holder = ticket.get("claimed_by_agent_id")
    expiry = ticket.get("lease_expires_at_epoch")
    active_work_status = status in {"claimed", "in_progress", "creating_report"}
    if active_work_status and expiry is None:
        # Active Central work states without expiry data are ambiguous even
        # when a partial projection omitted the holder identity.
        return True
    if holder and expiry is not None:
        try:
            if float(expiry) > now:
                return True
        except (TypeError, ValueError):
            # Malformed expiry paired with a retained holder must not
            # authorize an automated release.
            return True
    if active_work_status and expiry is not None:
        try:
            if float(expiry) > now:
                return True
        except (TypeError, ValueError):
            return True
    lease = ticket.get("review_lease")
    if isinstance(lease, Mapping):
        expiry = lease.get("expires_at_epoch")
        if expiry is None:
            return True
        try:
            if float(expiry) > now:
                return True
        except (TypeError, ValueError):
            return True
    return False


def reconcile(policy: Mapping[str, Any], tickets: Mapping[str, Mapping[str, Any]], *,
              combined_revision: str | None = None,
              final_task_exists: bool = False,
              now: float | None = None) -> dict[str, Any]:
    """Produce idempotent release/task intents without making workflow judgments."""
    accepted = {node_id: accepted_candidate(tickets.get(node["ticket_id"]))
                for node_id, node in policy["nodes"].items()}
    current_time = time.time() if now is None else float(now)
    releases = []
    for node_id, node in policy["nodes"].items():
        ticket = tickets.get(node["ticket_id"], {})
        if ticket.get("status") != "open" or ticket.get("parked") is not True:
            continue
        human = ticket.get("human_request")
        if isinstance(human, Mapping):
            resolution = human.get("resolution")
            if not isinstance(resolution, Mapping) or resolution.get("action") not in {"accept", "resolve", "dismiss"}:
                continue
        if _live_ticket_ownership(ticket, now=current_time):
            continue
        if node["requires"] and all(accepted.get(dep, (False, None))[0] for dep in node["requires"]):
            releases.append(node["ticket_id"])
    all_accepted = all(result[0] for result in accepted.values())
    members = {policy["nodes"][node_id]["ticket_id"]: sha
               for node_id, (ok, sha) in accepted.items() if ok and sha}
    final_task = None
    if (all_accepted and not final_task_exists and isinstance(combined_revision, str)
            and _SHA.fullmatch(combined_revision)):
        final_task = {"ticket_id": final_validation_ticket_id(policy),
            "title": policy["final_validation"]["title"],
            "combined_revision": combined_revision,
            "members": dict(sorted(members.items())),
            "acceptance_checks": list(policy["final_validation"]["acceptance_checks"])}
    return {"releases": sorted(releases), "all_accepted": all_accepted,
            "accepted_members": dict(sorted(members.items())), "final_task": final_task}
