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


def public_board_butler(value: Any) -> dict[str, Any] | None:
    """Remove secret/path references while retaining effective policy values."""
    if not isinstance(value, Mapping):
        return None
    result = copy.deepcopy(dict(value))
    layers = [result.get("global")]
    for collection_name in ("projects", "boards"):
        collection = result.get(collection_name)
        if isinstance(collection, Mapping):
            layers.extend(collection.values())
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for task in ("classification", "drafting"):
            provider = layer.get(task)
            if not isinstance(provider, dict):
                continue
            provider["endpoint_configured"] = bool(provider.pop("endpoint_ref", None))
            provider["key_configured"] = bool(provider.pop("key_ref", None))
            provider.pop("extra_headers", None)
            provider.pop("key_header", None)
            provider.pop("key_prefix", None)
    return result


def contract(
    *,
    board_id: str,
    board_policy: Mapping[str, Any],
    memberships: Mapping[str, Any],
    board_butler: Any,
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
                "effective": public_board_butler(board_butler),
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
