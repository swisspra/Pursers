"""Validation primitives for the durable Autonomous Butler control surface.

This module is deliberately transport-free.  Central derives identity and
authority, then uses these helpers to validate bounded command/config data.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

COMMAND_SCHEMA = "autonomous_butler_command_v2"
COMMAND_INTENTS = frozenset(
    {
        "set_desired_state",
        "reconcile_now",
        "drain_seat",
        "retire_seat",
        "enable_connector",
        "disable_connector",
        "veto_action",
        "kill",
        "resume",
    }
)
COMMAND_STATES = frozenset(
    {
        "accepted",
        "validating",
        "pending",
        "applying",
        "succeeded",
        "rejected",
        "failed",
        "cancelled",
    }
)
TERMINAL_COMMAND_STATES = frozenset(
    {"succeeded", "rejected", "failed", "cancelled"}
)
COMMAND_TRANSITIONS = {
    "accepted": frozenset({"validating", "cancelled"}),
    "validating": frozenset({"pending", "rejected", "failed"}),
    "pending": frozenset({"applying", "rejected", "failed", "cancelled"}),
    "applying": frozenset({"succeeded", "failed", "cancelled"}),
}
COMMAND_PRIORITIES = {"low": 0, "normal": 1, "high": 2, "emergency": 3}
RESULT_COMMIT_STATES = frozenset(
    {"not_started", "not_reached", "reached", "unknown"}
)
PARAMETER_FIELDS = {
    "set_desired_state": frozenset({"desired_revision", "desired_digest_sha256"}),
    "reconcile_now": frozenset(),
    "drain_seat": frozenset({"seat_id", "seat_generation"}),
    "retire_seat": frozenset({"seat_id", "seat_generation"}),
    "enable_connector": frozenset({"connector_id"}),
    "disable_connector": frozenset({"connector_id"}),
    "veto_action": frozenset({"action_id", "reason_code"}),
    "kill": frozenset({"reason_code"}),
    "resume": frozenset({"reason_code"}),
}

CONFIG_TOP_LEVEL = frozenset(
    {
        "schema",
        "schema_version",
        "board_id",
        "revision",
        "enabled",
        "host_runtime",
        "desired",
        "envelope",
        "authorization",
    }
)
A2A_CONFIG_PATHS = (
    re.compile(r"^desired\.runner$"),
    re.compile(r"^desired\.capacity\.(worker|reviewer|acp_worker)\.target$"),
    re.compile(r"^desired\.(host_concurrency|board_concurrency)$"),
    re.compile(r"^desired\.cooldowns\.(scale_up_s|scale_down_s|failure_backoff_s)$"),
    re.compile(r"^desired\.budget\.(period|max_tokens|max_cost_microunits|max_external_calls)$"),
    re.compile(r"^desired\.connectors\.[0-9]+\.enabled$"),
)


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_identifier(field: str, value: Any) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded identifier")
    return value


def require_reason(field: str, value: Any) -> str:
    if not isinstance(value, str) or not REASON_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded reason code")
    return value


def parse_time(field: str, value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _strict_object(
    field: str,
    value: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    keys = frozenset(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{field} is missing {sorted(missing)[0]}")
    if unknown:
        raise ValueError(f"{field} contains unsupported field {sorted(unknown)[0]}")
    return value


def _bounded_int(field: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def validate_command_parameters(intent: str, parameters: Any) -> dict[str, Any]:
    if intent not in COMMAND_INTENTS:
        raise ValueError("intent is not allowlisted")
    expected = PARAMETER_FIELDS[intent]
    raw = _strict_object("parameters", parameters, required=expected)
    normalized = dict(raw)
    for field in ("seat_id", "connector_id", "action_id"):
        if field in normalized:
            normalized[field] = require_identifier(field, normalized[field])
    if "reason_code" in normalized:
        normalized["reason_code"] = require_reason(
            "parameters.reason_code", normalized["reason_code"]
        )
    if "seat_generation" in normalized:
        normalized["seat_generation"] = _bounded_int(
            "parameters.seat_generation", normalized["seat_generation"], 1, 2**63 - 1
        )
    if "desired_revision" in normalized:
        normalized["desired_revision"] = _bounded_int(
            "parameters.desired_revision", normalized["desired_revision"], 1, 2**63 - 1
        )
    if "desired_digest_sha256" in normalized and not (
        isinstance(normalized["desired_digest_sha256"], str)
        and SHA256_RE.fullmatch(normalized["desired_digest_sha256"])
    ):
        raise ValueError("parameters.desired_digest_sha256 must be lowercase SHA-256")
    return normalized


def validate_command_result(value: Any) -> dict[str, Any]:
    raw = _strict_object(
        "result",
        value,
        required=frozenset({"outcome", "reason_code", "commit_state"}),
        optional=frozenset(
            {"effect_observation_ref", "config_revision", "result_digest_sha256"}
        ),
    )
    outcome = raw["outcome"]
    if outcome not in TERMINAL_COMMAND_STATES:
        raise ValueError("result.outcome must be a terminal command state")
    require_reason("result.reason_code", raw["reason_code"])
    if raw["commit_state"] not in RESULT_COMMIT_STATES:
        raise ValueError("result.commit_state is invalid")
    result = dict(raw)
    if "effect_observation_ref" in result:
        result["effect_observation_ref"] = require_identifier(
            "result.effect_observation_ref", result["effect_observation_ref"]
        )
    if "config_revision" in result:
        _bounded_int("result.config_revision", result["config_revision"], 1, 2**63 - 1)
    if "result_digest_sha256" in result and not (
        isinstance(result["result_digest_sha256"], str)
        and SHA256_RE.fullmatch(result["result_digest_sha256"])
    ):
        raise ValueError("result.result_digest_sha256 must be lowercase SHA-256")
    return result


def command_sort_key(command: Mapping[str, Any]) -> tuple[int, int, int, str]:
    sender = command.get("sender")
    human_rank = 1 if isinstance(sender, Mapping) and sender.get("channel") == "human" else 0
    priority_rank = COMMAND_PRIORITIES.get(str(command.get("priority")), -1)
    kill_rank = 1 if command.get("intent") == "kill" else 0
    return (-human_rank, -priority_rank, -kill_rank, str(command.get("created_at", "")))


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(changed_paths(before[key], after[key], child))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        paths = []
        for index in range(max(len(before), len(after))):
            child = f"{prefix}.{index}"
            if index >= len(before) or index >= len(after):
                paths.append(child)
            else:
                paths.extend(changed_paths(before[index], after[index], child))
        return paths
    return [] if before == after else [prefix]


def _validate_role_count(field: str, value: Any) -> dict[str, int]:
    raw = _strict_object(
        field, value, required=frozenset({"min", "target", "max"})
    )
    result = {
        key: _bounded_int(f"{field}.{key}", raw[key], 0, 100)
        for key in ("min", "target", "max")
    }
    if not result["min"] <= result["target"] <= result["max"]:
        raise ValueError(f"{field} must satisfy min <= target <= max")
    return result


def _validate_budget(field: str, value: Any) -> dict[str, Any]:
    raw = _strict_object(
        field,
        value,
        required=frozenset(
            {"period", "max_tokens", "max_cost_microunits", "max_external_calls"}
        ),
    )
    if raw["period"] not in {"hour", "day", "month"}:
        raise ValueError(f"{field}.period is invalid")
    return {
        "period": raw["period"],
        "max_tokens": _bounded_int(f"{field}.max_tokens", raw["max_tokens"], 0, 1_000_000_000),
        "max_cost_microunits": _bounded_int(
            f"{field}.max_cost_microunits", raw["max_cost_microunits"], 0, 1_000_000_000_000
        ),
        "max_external_calls": _bounded_int(
            f"{field}.max_external_calls", raw["max_external_calls"], 0, 1_000_000
        ),
    }


def _validate_connector(field: str, value: Any) -> dict[str, Any]:
    raw = _strict_object(
        field,
        value,
        required=frozenset(
            {
                "connector_id",
                "enabled",
                "transport",
                "protocol_revision",
                "endpoint_ref",
                "secret_ref",
                "tools",
                "resources",
                "limits",
            }
        ),
        optional=frozenset({"risky_tools"}),
    )
    result = dict(raw)
    result["connector_id"] = require_identifier(f"{field}.connector_id", raw["connector_id"])
    if not isinstance(raw["enabled"], bool):
        raise ValueError(f"{field}.enabled must be boolean")
    if raw["transport"] not in {"stdio", "streamable_http"}:
        raise ValueError(f"{field}.transport is invalid")
    if not isinstance(raw["protocol_revision"], str) or not re.fullmatch(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", raw["protocol_revision"]
    ):
        raise ValueError(f"{field}.protocol_revision is invalid")
    require_identifier(f"{field}.endpoint_ref", raw["endpoint_ref"])
    if raw["secret_ref"] is not None:
        require_identifier(f"{field}.secret_ref", raw["secret_ref"])
    if not isinstance(raw["resources"], list) or len(raw["resources"]) > 100:
        raise ValueError(f"{field}.resources is invalid")
    for resource in raw["resources"]:
        if not isinstance(resource, str) or not 1 <= len(resource) <= 300:
            raise ValueError(f"{field}.resources is invalid")
    tools = raw["tools"]
    if not isinstance(tools, list) or len(tools) > 100:
        raise ValueError(f"{field}.tools is invalid")
    for index, tool in enumerate(tools):
        item = _strict_object(
            f"{field}.tools.{index}",
            tool,
            required=frozenset({"name", "effect", "replay", "stable_call_id_field"}),
        )
        require_identifier(f"{field}.tools.{index}.name", item["name"])
        if item["effect"] not in {"read_only", "mutating"}:
            raise ValueError(f"{field}.tools.{index}.effect is invalid")
        if item["replay"] not in {"safe_with_stable_call_id", "never"}:
            raise ValueError(f"{field}.tools.{index}.replay is invalid")
        stable_field = item["stable_call_id_field"]
        if item["replay"] == "safe_with_stable_call_id":
            require_identifier(f"{field}.tools.{index}.stable_call_id_field", stable_field)
        elif stable_field is not None:
            raise ValueError(f"{field}.tools.{index}.stable_call_id_field must be null")
        if item["effect"] == "read_only" and item["replay"] != "safe_with_stable_call_id":
            raise ValueError(f"{field}.tools.{index} read-only replay must be safe")
    limits = _strict_object(
        f"{field}.limits",
        raw["limits"],
        required=frozenset(
            {"timeout_ms", "max_input_bytes", "max_output_bytes", "max_concurrency", "calls_per_minute"}
        ),
    )
    for key, minimum, maximum in (
        ("timeout_ms", 100, 600_000),
        ("max_input_bytes", 1, 1_048_576),
        ("max_output_bytes", 1, 4_194_304),
        ("max_concurrency", 1, 32),
        ("calls_per_minute", 1, 1_000),
    ):
        _bounded_int(f"{field}.limits.{key}", limits[key], minimum, maximum)
    risky = raw.get("risky_tools", [])
    if not isinstance(risky, list) or len(risky) > 100:
        raise ValueError(f"{field}.risky_tools is invalid")
    for name in risky:
        require_identifier(f"{field}.risky_tools", name)
    return result


def validate_config(value: Any, board_id: str) -> dict[str, Any]:
    raw = _strict_object(
        "config",
        value,
        required=CONFIG_TOP_LEVEL - frozenset({"authorization"}),
        optional=frozenset({"authorization"}),
    )
    if raw["schema"] != "autonomous_butler_config_v1" or raw["schema_version"] != 1:
        raise ValueError("config schema/version is unsupported")
    if raw["board_id"] != board_id:
        raise ValueError("config board_id does not match the target board")
    _bounded_int("config.revision", raw["revision"], 1, 2**63 - 1)
    if not isinstance(raw["enabled"], bool):
        raise ValueError("config.enabled must be boolean")

    host = _strict_object(
        "config.host_runtime",
        raw["host_runtime"],
        required=frozenset(
            {
                "host_ref", "revision", "agent_process_ceiling",
                "control_plane_processes", "total_process_ceiling",
                "configured_by", "configured_at",
            }
        ),
    )
    require_identifier("config.host_runtime.host_ref", host["host_ref"])
    require_identifier("config.host_runtime.configured_by", host["configured_by"])
    parse_time("config.host_runtime.configured_at", host["configured_at"])
    _bounded_int("config.host_runtime.revision", host["revision"], 1, 2**63 - 1)
    agent_ceiling = _bounded_int(
        "config.host_runtime.agent_process_ceiling", host["agent_process_ceiling"], 1, 256
    )
    control_processes = _bounded_int(
        "config.host_runtime.control_plane_processes", host["control_plane_processes"], 0, 64
    )
    total_ceiling = _bounded_int(
        "config.host_runtime.total_process_ceiling", host["total_process_ceiling"], 1, 320
    )
    if agent_ceiling + control_processes > total_ceiling:
        raise ValueError("host runtime process ceilings are inconsistent")

    desired = _strict_object(
        "config.desired",
        raw["desired"],
        required=frozenset(
            {"mode", "runner", "capacity", "host_concurrency", "board_concurrency", "cooldowns", "budget", "connectors"}
        ),
    )
    if desired["mode"] not in {"shadow", "autonomous"}:
        raise ValueError("config.desired.mode is invalid")
    if desired["runner"] not in {"direct_api", "acp"}:
        raise ValueError("config.desired.runner is invalid")
    capacity = _strict_object(
        "config.desired.capacity",
        desired["capacity"],
        required=frozenset({"worker", "reviewer", "acp_worker"}),
    )
    counts = {
        role: _validate_role_count(f"config.desired.capacity.{role}", capacity[role])
        for role in ("worker", "reviewer", "acp_worker")
    }
    host_concurrency = _bounded_int(
        "config.desired.host_concurrency", desired["host_concurrency"], 1, 256
    )
    board_concurrency = _bounded_int(
        "config.desired.board_concurrency", desired["board_concurrency"], 1, 256
    )
    cooldowns = _strict_object(
        "config.desired.cooldowns",
        desired["cooldowns"],
        required=frozenset({"scale_up_s", "scale_down_s", "failure_backoff_s"}),
    )
    _bounded_int("config.desired.cooldowns.scale_up_s", cooldowns["scale_up_s"], 0, 604_800)
    _bounded_int("config.desired.cooldowns.scale_down_s", cooldowns["scale_down_s"], 0, 604_800)
    _bounded_int("config.desired.cooldowns.failure_backoff_s", cooldowns["failure_backoff_s"], 1, 86_400)
    desired_budget = _validate_budget("config.desired.budget", desired["budget"])
    connectors = desired["connectors"]
    if not isinstance(connectors, list) or len(connectors) > 32:
        raise ValueError("config.desired.connectors is invalid")
    validated_connectors = [
        _validate_connector(f"config.desired.connectors.{index}", item)
        for index, item in enumerate(connectors)
    ]
    connector_ids = [item["connector_id"] for item in validated_connectors]
    if len(connector_ids) != len(set(connector_ids)):
        raise ValueError("config.desired.connectors contains duplicate connector_id")

    envelope = _strict_object(
        "config.envelope",
        raw["envelope"],
        required=frozenset(
            {
                "fingerprint_sha256", "approved_template_ids", "approved_connector_ids",
                "max_capacity", "max_host_concurrency", "max_board_concurrency",
                "max_budget", "created_by", "created_at",
            }
        ),
    )
    if not isinstance(envelope["fingerprint_sha256"], str) or not SHA256_RE.fullmatch(envelope["fingerprint_sha256"]):
        raise ValueError("config.envelope.fingerprint_sha256 is invalid")
    fingerprint_input = dict(envelope)
    fingerprint_input.pop("fingerprint_sha256")
    if envelope["fingerprint_sha256"] != canonical_digest(fingerprint_input):
        raise ValueError("config.envelope.fingerprint_sha256 does not match the envelope")
    require_identifier("config.envelope.created_by", envelope["created_by"])
    parse_time("config.envelope.created_at", envelope["created_at"])
    for list_field, maximum in (("approved_template_ids", 100), ("approved_connector_ids", 32)):
        values = envelope[list_field]
        if not isinstance(values, list) or len(values) > maximum or len(values) != len(set(values)):
            raise ValueError(f"config.envelope.{list_field} is invalid")
        if list_field == "approved_template_ids" and not values:
            raise ValueError("config.envelope.approved_template_ids must not be empty")
        for item in values:
            require_identifier(f"config.envelope.{list_field}", item)
    max_capacity_raw = _strict_object(
        "config.envelope.max_capacity",
        envelope["max_capacity"],
        required=frozenset({"worker", "reviewer", "acp_worker"}),
    )
    max_capacity = {
        role: _bounded_int(f"config.envelope.max_capacity.{role}", max_capacity_raw[role], 0, 100)
        for role in ("worker", "reviewer", "acp_worker")
    }
    max_host = _bounded_int(
        "config.envelope.max_host_concurrency", envelope["max_host_concurrency"], 1, 256
    )
    max_board = _bounded_int(
        "config.envelope.max_board_concurrency", envelope["max_board_concurrency"], 1, 256
    )
    max_budget = _validate_budget("config.envelope.max_budget", envelope["max_budget"])
    if any(counts[role]["max"] > max_capacity[role] for role in counts):
        raise ValueError("desired capacity exceeds immutable envelope")
    if sum(counts[role]["max"] for role in counts) > agent_ceiling:
        raise ValueError("desired role maxima exceed the host agent-process ceiling")
    if host_concurrency > max_host or board_concurrency > max_board:
        raise ValueError("desired concurrency exceeds immutable envelope")
    if desired_budget["period"] != max_budget["period"] or any(
        desired_budget[key] > max_budget[key]
        for key in ("max_tokens", "max_cost_microunits", "max_external_calls")
    ):
        raise ValueError("desired budget exceeds immutable envelope")
    if not set(connector_ids) <= set(envelope["approved_connector_ids"]):
        raise ValueError("desired connector is outside the immutable envelope")

    authorization = raw.get("authorization")
    if desired["mode"] == "autonomous":
        if raw["enabled"] is not True or authorization is None:
            raise ValueError("autonomous mode requires enabled=true and authorization")
    if raw["enabled"] is False and desired["mode"] != "shadow":
        raise ValueError("enabled=false requires shadow mode")
    if authorization is not None:
        auth = _strict_object(
            "config.authorization",
            authorization,
            required=frozenset({"authorization_id", "config_revision", "envelope_fingerprint_sha256", "expires_at"}),
        )
        require_identifier("config.authorization.authorization_id", auth["authorization_id"])
        _bounded_int("config.authorization.config_revision", auth["config_revision"], 1, 2**63 - 1)
        if auth["config_revision"] > raw["revision"]:
            raise ValueError("authorization config_revision cannot be in the future")
        if auth["envelope_fingerprint_sha256"] != envelope["fingerprint_sha256"]:
            raise ValueError("authorization does not match envelope fingerprint")
        parse_time("config.authorization.expires_at", auth["expires_at"])
    return dict(raw)


def validate_config_authority(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    channel: str,
) -> list[str]:
    if channel not in {"human", "a2a"}:
        raise ValueError("sender_channel must be human or a2a")
    if before is None:
        if channel != "human":
            raise PermissionError("only a human-admin command may initialize Butler config")
        return sorted(after)
    paths = [path for path in changed_paths(before, after) if path != "revision"]
    if channel == "a2a":
        denied = [
            path for path in paths
            if not any(pattern.fullmatch(path) for pattern in A2A_CONFIG_PATHS)
        ]
        if denied:
            raise PermissionError(f"A2A authority cannot mutate {denied[0]}")
    return paths


def transition_allowed(current: str, target: str) -> bool:
    return target in COMMAND_TRANSITIONS.get(current, frozenset())
