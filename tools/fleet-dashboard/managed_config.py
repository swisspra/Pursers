"""Typed, secret-safe contracts for Fleet managed configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


SCHEMA = "fleet_managed_config_v1"
PLAN_SCHEMA = "fleet_managed_config_plan_v1"
BOARD_POLICIES = frozenset({"strict", "workflow"})
MEMBERSHIP_ROLES = frozenset({"admin", "member", "reviewer"})
MIN_STALE_AFTER_DAYS = 1
MAX_STALE_AFTER_DAYS = 3_650
PRINCIPAL_ID = re.compile(r"^PR-[a-f0-9]{64}$")
BOARD_BUTLER_PRECEDENCE = ("safe_defaults", "global", "project", "board")
BOARD_BUTLER_DEFAULTS: dict[str, Any] = {
    "mode": "shadow",
    "answering_mode": "assist",
    "answer_scope": {
        name: "escalate"
        for name in (
            "ancestry",
            "ticket_status",
            "seat_capability",
            "waiver_applicability",
            "corpus_lookup",
            "coverage_check",
            "scope_change",
            "gate_waiver",
            "release",
            "membership",
            "registry",
        )
    },
    "required_evidence_kinds": [
        "git_ancestry",
        "ticket_status",
        "annotation",
        "seat_capability",
        "manifest_coverage",
        "corpus",
    ],
    "ceilings": {"per_hour": 5, "per_ticket": 2, "per_board": 20},
    "hold_before_post_s": 3_600,
    "active_windows": [],
    "kill_switch": True,
    "auto_demote": {"veto_count": 3, "failure_count": 3, "window_s": 3_600},
    "classification": {
        "model": None,
        "endpoint_ref": None,
        "key_ref": None,
        "extra_headers": {},
        "key_header": "Authorization",
        "key_prefix": "Bearer",
        "validation_path": "models",
        "draft_path": "draft",
        "draft_protocol": "pursers_json_v1",
    },
    "drafting": {
        "model": None,
        "endpoint_ref": None,
        "key_ref": None,
        "extra_headers": {},
        "key_header": "Authorization",
        "key_prefix": "Bearer",
        "validation_path": "models",
        "draft_path": "draft",
        "draft_protocol": "pursers_json_v1",
    },
}
_BOARD_BUTLER_MERGED_OBJECTS = frozenset(
    {"answer_scope", "ceilings", "auto_demote", "classification", "drafting"}
)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def board_policy_projection(status: Mapping[str, Any]) -> dict[str, Any]:
    """Project the two board settings with approved dynamic setters."""
    source = status.get("board") if isinstance(status.get("board"), Mapping) else status
    review_policy = source.get("review_policy", "strict")
    stale_after_days = source.get("stale_after_days", 3)
    if review_policy not in BOARD_POLICIES:
        raise ValueError("Central returned an unsupported review_policy")
    if (
        isinstance(stale_after_days, bool)
        or not isinstance(stale_after_days, int)
        or not MIN_STALE_AFTER_DAYS <= stale_after_days <= MAX_STALE_AFTER_DAYS
    ):
        raise ValueError("Central returned an unsupported stale_after_days")
    values = {
        "review_policy": review_policy,
        "stale_after_days": stale_after_days,
    }
    return {
        "values": values,
        "expected_sha256": _digest(values),
        "apply_mode": "hot-apply",
        "status": "configurable",
    }


def validate_board_policy_changes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value or not set(value) <= {
        "review_policy",
        "stale_after_days",
    }:
        raise ValueError("changes must contain only supported board policy fields")
    clean = dict(value)
    if "review_policy" in clean and clean["review_policy"] not in BOARD_POLICIES:
        raise ValueError("review_policy must be strict or workflow")
    stale = clean.get("stale_after_days")
    if "stale_after_days" in clean and (
        isinstance(stale, bool)
        or not isinstance(stale, int)
        or not MIN_STALE_AFTER_DAYS <= stale <= MAX_STALE_AFTER_DAYS
    ):
        raise ValueError("stale_after_days must be between 1 and 3650")
    return clean


def membership_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = payload.get("members", [])
    if not isinstance(rows, list):
        raise ValueError("Central returned malformed board memberships")
    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Central returned malformed board memberships")
        principal_id, role = row.get("principal_id"), row.get("role")
        if not isinstance(principal_id, str) or not PRINCIPAL_ID.fullmatch(principal_id):
            raise ValueError("Central returned an invalid principal_id")
        if role not in MEMBERSHIP_ROLES:
            raise ValueError("Central returned an invalid membership role")
        projected.append(
            {
                "principal_id": principal_id,
                "role": role,
                "source": row.get("source") if isinstance(row.get("source"), str) else None,
                "agent_names": sorted(
                    name
                    for name in row.get("agent_names", [])
                    if isinstance(name, str) and len(name) <= 160
                )[:100],
            }
        )
    projected.sort(key=lambda row: row["principal_id"])
    return {
        "members": projected,
        "expected_sha256": _digest(projected),
        "apply_mode": "hot-apply",
        "status": "configurable",
    }


def validate_membership_change(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("membership change must be an object")
    operation = value.get("operation")
    allowed = {"operation", "principal_id"} | ({"role"} if operation in {"add", "set_role"} else set())
    if operation not in {"add", "remove", "set_role"} or set(value) != allowed:
        raise ValueError("membership change fields are invalid")
    principal_id = value.get("principal_id")
    if not isinstance(principal_id, str) or not PRINCIPAL_ID.fullmatch(principal_id):
        raise ValueError("principal_id must be a full PR- identifier")
    result = {"operation": operation, "principal_id": principal_id}
    if operation in {"add", "set_role"}:
        role = value.get("role")
        if role not in MEMBERSHIP_ROLES:
            raise ValueError("role must be admin, member, or reviewer")
        result["role"] = role
    return result


def _provenance(value: Any, source: str) -> Any:
    if isinstance(value, Mapping):
        return {key: _provenance(item, source) for key, item in value.items()}
    return source


def _merge_board_butler_layer(
    effective: dict[str, Any],
    provenance: dict[str, Any],
    layer: Mapping[str, Any],
    source: str,
) -> None:
    for key, value in layer.items():
        if (
            key in _BOARD_BUTLER_MERGED_OBJECTS
            and isinstance(value, Mapping)
            and isinstance(effective.get(key), dict)
        ):
            for nested_key, nested_value in value.items():
                effective[key][nested_key] = copy.deepcopy(nested_value)
                provenance[key][nested_key] = _provenance(nested_value, source)
        else:
            effective[key] = copy.deepcopy(value)
            provenance[key] = _provenance(value, source)


def _redact_provider_references(
    effective: dict[str, Any], provenance: dict[str, Any]
) -> None:
    for task in ("classification", "drafting"):
        provider = effective.get(task)
        provider_sources = provenance.get(task)
        if not isinstance(provider, dict) or not isinstance(provider_sources, dict):
            continue
        endpoint_source = provider_sources.pop("endpoint_ref", "safe_defaults")
        key_source = provider_sources.pop("key_ref", "safe_defaults")
        provider["endpoint_configured"] = bool(provider.pop("endpoint_ref", None))
        provider["key_configured"] = bool(provider.pop("key_ref", None))
        provider_sources["endpoint_configured"] = endpoint_source
        provider_sources["key_configured"] = key_source
        for field in ("extra_headers", "key_header", "key_prefix"):
            provider.pop(field, None)
            provider_sources.pop(field, None)


def effective_board_butler(
    value: Any,
    *,
    board_id: str,
    project_name: str | None,
    default_per_hour: int = 5,
) -> dict[str, Any] | None:
    """Resolve safe defaults and selected layers with secret-safe provenance."""
    if not isinstance(value, Mapping):
        return None
    effective = copy.deepcopy(BOARD_BUTLER_DEFAULTS)
    if 1 <= default_per_hour <= 100:
        effective["ceilings"]["per_hour"] = default_per_hour
    provenance = _provenance(effective, "safe_defaults")
    selected: list[tuple[str, Mapping[str, Any]]] = []
    global_layer = value.get("global")
    if isinstance(global_layer, Mapping):
        selected.append(("global", global_layer))
    projects = value.get("projects")
    if project_name is not None and isinstance(projects, Mapping):
        project_layer = projects.get(project_name)
        if isinstance(project_layer, Mapping):
            selected.append((f"project:{project_name}", project_layer))
    boards = value.get("boards")
    if isinstance(boards, Mapping):
        board_layer = boards.get(board_id)
        if isinstance(board_layer, Mapping):
            selected.append((f"board:{board_id}", board_layer))
    for source, layer in selected:
        _merge_board_butler_layer(effective, provenance, layer, source)
    _redact_provider_references(effective, provenance)
    effective["provenance"] = provenance
    effective["inheritance"] = {
        "precedence": list(BOARD_BUTLER_PRECEDENCE),
        "applied_layers": ["safe_defaults", *(source for source, _ in selected)],
        "project": project_name,
        "board": board_id,
        "reset_semantics": "Remove an override field from its layer to inherit the next lower-precedence value.",
        "resettable_layers": [
            "global",
            *([f"project:{project_name}"] if project_name is not None else []),
            f"board:{board_id}",
        ],
    }
    return effective


def contract(
    *,
    board_id: str,
    board_policy: Mapping[str, Any],
    memberships: Mapping[str, Any],
    board_butler: Any,
    board_butler_project: str | None = None,
    board_butler_default_per_hour: int = 5,
) -> dict[str, Any]:
    """Return the stable backend contract consumed by Settings."""
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "board_id": board_id,
        "families": {
            "board_policy": dict(board_policy),
            "memberships": dict(memberships),
            "board_butler": {
                "status": "configurable" if board_butler is not None else "unavailable",
                "apply_mode": "hot-apply",
                "effective": effective_board_butler(
                    board_butler,
                    board_id=board_id,
                    project_name=board_butler_project,
                    default_per_hour=board_butler_default_per_hour,
                ),
                "endpoint": "/api/config",
                "reason": None if board_butler is not None else "No validated Board Butler policy is available.",
            },
            "managed_seats": {
                "status": "configurable",
                "apply_mode": "restart-required",
                "read_endpoint": "/api/config/seats",
                "plan_endpoint": "/api/config/plan",
                "apply_endpoint": "/api/config/apply",
            },
            "dispatch": {
                "status": "configurable",
                "apply_mode": "hot-apply",
                "endpoint": "/api/dispatch",
            },
            "delivery": {
                "status": "configurable",
                "apply_mode": "staged",
                "read_endpoint": "/api/projects/delivery",
                "plan_endpoint": "/api/lifecycle/plan",
                "apply_endpoint": "/api/lifecycle/apply",
            },
            "persistent_workers": {
                "status": "configurable",
                "apply_mode": "restart-required",
                "endpoint": "/api/workers",
                "secret_values_readable": False,
            },
            "source_connectors": {
                "status": "unavailable",
                "reason": "Authoritative connector/source schema is pending TK-dcc6d183eb1e15126e1f.",
                "dependency_ticket": "TK-dcc6d183eb1e15126e1f",
            },
            "central_retention": {
                "status": "unavailable",
                "reason": "Central retention setter contract is pending TK-eebab5f77b7a6b63eb37.",
                "dependency_ticket": "TK-eebab5f77b7a6b63eb37",
            },
            "acp_runners": {
                "status": "unavailable",
                "reason": "Pending ACP source is not part of this approved baseline.",
            },
        },
    }


def plan_digest(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key != "digest"})
