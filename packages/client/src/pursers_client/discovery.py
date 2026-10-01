"""Small, versioned MCP discovery documents.

Static help is deliberately separate from authorized board data.  Callers read
one role or workflow document on demand instead of receiving the full manual in
the server instructions or every prompt.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

DISCOVERY_SCHEMA = "pursers.discovery.v1"
DISCOVERY_VERSION = "2026-10-02"
CANONICAL_GUIDE = "docs/guides/connecting-clients.md"

ROLE_HELP: dict[str, dict[str, Any]] = {
    "worker": {
        "purpose": "Claim offered work, keep its lease, validate it, and submit evidence.",
        "prerequisites": ["An active worker identity with can_work=true", "An explicit work offer"],
        "next_actions": [
            "Read the offered ticket",
            "Claim that exact ticket",
            "Renew its lease while working",
        ],
        "boundaries": ["Do not claim unoffered work", "Do not review your own submission"],
    },
    "reviewer": {
        "purpose": "Independently verify submitted work and record an evidence-based verdict.",
        "prerequisites": [
            "An active reviewer identity with can_review=true",
            "An explicit review offer",
        ],
        "next_actions": [
            "Read the submitted ticket",
            "Claim that exact review",
            "Verify the declared commit independently",
        ],
        "boundaries": [
            "Do not review work from the same principal",
            "A review does not merge or publish",
        ],
    },
    "coordinator": {
        "purpose": "Shape work, answer durable questions, and coordinate delivery boundaries.",
        "prerequisites": ["A coordinator identity authorized on the board"],
        "next_actions": [
            "Inspect bounded board state",
            "Create scoped work",
            "Answer pending ticket questions",
        ],
        "boundaries": [
            "Do not treat a bounded list as the whole board",
            "Do not bypass independent review",
        ],
    },
    "orchestrator": {
        "purpose": "Watch authorized boards and present compact, durable next actions.",
        "prerequisites": ["An orchestrator identity authorized on each selected board"],
        "next_actions": [
            "Read the digest",
            "Follow linked ticket details",
            "Acknowledge only processed cursors",
        ],
        "boundaries": [
            "Notifications are cues, not authority",
            "Do not infer success from missing events",
        ],
    },
}

WORKFLOW_HELP: dict[str, dict[str, Any]] = {
    "board": {
        "purpose": "Inspect board-wide counts and a bounded attention subset.",
        "safe_next_action": (
            "Open one exact ticket or request another bounded page before drawing "
            "conclusions."
        ),
        "mutates": False,
    },
    "create": {
        "purpose": "Prepare one scoped ticket after collecting required fields.",
        "safe_next_action": "Ask the caller to authorize the single create action.",
        "mutates": True,
    },
    "watch": {
        "purpose": "Wait for actionable changes from a returned positive cursor.",
        "safe_next_action": "Re-arm from the returned cursor; never reset it to zero to catch up.",
        "mutates": False,
    },
    "evidence": {
        "purpose": "Read bounded submission and independent-review evidence for one ticket.",
        "safe_next_action": "Verify the exact branch and commit before any delivery action.",
        "mutates": False,
    },
    "answer": {
        "purpose": "Prepare an answer for one pending ticket question.",
        "safe_next_action": "Ask the caller to authorize resolving that exact question once.",
        "mutates": True,
    },
    "setup": {
        "purpose": "Inspect local prerequisites before optional local provisioning.",
        "safe_next_action": (
            "Preview changes and request explicit consent before writing files or "
            "starting Central."
        ),
        "mutates": True,
    },
}


@lru_cache(maxsize=1)
def capability_index() -> dict[str, Any]:
    """Return a concise catalog; bodies remain lazy resources."""

    return {
        "schema": DISCOVERY_SCHEMA,
        "version": DISCOVERY_VERSION,
        "kind": "static_help_index",
        "resources": [
            {
                "title": "Role help",
                "purpose": "Role prerequisites, boundaries, and safe next actions.",
                "uri_template": "pursers://help/roles/{role}",
                "version": DISCOVERY_VERSION,
                "applicability": sorted(ROLE_HELP),
            },
            {
                "title": "Workflow help",
                "purpose": "Non-mutating preparation for one common workflow.",
                "uri_template": "pursers://help/workflows/{workflow}",
                "version": DISCOVERY_VERSION,
                "applicability": sorted(WORKFLOW_HELP),
            },
            {
                "title": "Authorized board summary",
                "purpose": "Fresh board summary using the same authorization as board_status.",
                "uri_template": "pursers://boards/{board_id}/summary",
                "version": DISCOVERY_VERSION,
                "applicability": ["configured board members"],
            },
            {
                "title": "Authorized ticket summary",
                "purpose": (
                    "Fresh compact ticket detail using the same authorization as "
                    "ticket_get."
                ),
                "uri_template": "pursers://boards/{board_id}/tickets/{ticket_id}",
                "version": DISCOVERY_VERSION,
                "applicability": ["configured board members"],
            },
            {
                "title": "Compatibility board digest",
                "purpose": "Wait-bridge digest when its orchestrator engine is available.",
                "uri_template": "board://{board_id}/digest",
                "version": 1,
                "applicability": ["pursers-wait-bridge orchestrator mode"],
            },
        ],
        "host_support": {
            "current_protocol": {"resources": True, "prompts": True},
            "legacy_zed_relay": {"resources": True, "prompts": True},
            "tool_only": {
                "resources": False,
                "prompts": False,
                "fallback": CANONICAL_GUIDE,
            },
        },
        "notes": [
            "Static help is public procedure; board resources are fresh authorized reads.",
            "Resources and prompts never grant permission or perform an implicit mutation.",
            "Resource update notifications are cues; clients must still read the resource.",
        ],
    }


@lru_cache(maxsize=len(ROLE_HELP) + 1)
def role_document(role: str) -> dict[str, Any]:
    normalized = role.strip().lower()
    body = ROLE_HELP.get(normalized)
    if body is None:
        return unavailable("unsupported_role", f"Supported roles: {', '.join(sorted(ROLE_HELP))}")
    return {
        "schema": DISCOVERY_SCHEMA,
        "version": DISCOVERY_VERSION,
        "kind": "static_role_help",
        "role": normalized,
        **body,
        "canonical_guide": CANONICAL_GUIDE,
    }


@lru_cache(maxsize=len(WORKFLOW_HELP) + 1)
def workflow_document(workflow: str) -> dict[str, Any]:
    normalized = workflow.strip().lower()
    body = WORKFLOW_HELP.get(normalized)
    if body is None:
        return unavailable(
            "unsupported_workflow",
            f"Supported workflows: {', '.join(sorted(WORKFLOW_HELP))}",
        )
    return {
        "schema": DISCOVERY_SCHEMA,
        "version": DISCOVERY_VERSION,
        "kind": "static_workflow_help",
        "workflow": normalized,
        **body,
        "canonical_guide": CANONICAL_GUIDE,
    }


def unavailable(code: str, detail: str) -> dict[str, Any]:
    return {
        "schema": DISCOVERY_SCHEMA,
        "version": DISCOVERY_VERSION,
        "ok": False,
        "status": "unavailable",
        "code": code,
        "detail": detail,
        "fallback": CANONICAL_GUIDE,
    }
