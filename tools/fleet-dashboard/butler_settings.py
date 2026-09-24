"""Secret-safe Board Butler settings for the loopback Fleet dashboard."""

from __future__ import annotations

import copy
import fcntl
from functools import partial
import http.client
import ipaddress
import json
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


MAX_KEY_BYTES = 8_192
MAX_ENDPOINT_CHARS = 300
MAX_MODEL_CHARS = 200
MAX_HEADER_COUNT = 16
MAX_HEADER_VALUE_CHARS = 1_000
DEFAULT_VALIDATION_PATH = "models"
DEFAULT_DRAFT_PATH = "draft"
DRAFT_PROTOCOL = "pursers_json_v1"
DRAFT_PROTOCOLS = frozenset({DRAFT_PROTOCOL, "openai_chat_completions_v1"})
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SECRET_HEADER = re.compile(r"(?:authorization|api[-_]?key|token|secret|cookie)", re.I)
_MANAGED_KEY_REFERENCE = re.compile(r"^file:([A-Za-z0-9._-]{1,160}\.key)$")
_AddressInfo = tuple[int, int, int, str, tuple[Any, ...]]
AUTONOMOUS_STATES = frozenset(
    {
        "shadow",
        "pending",
        "applying",
        "autonomous",
        "degraded",
        "auto_demoted",
        "killed",
    }
)
AUTONOMOUS_ROLES = ("worker", "reviewer", "acp_worker")
AUTONOMOUS_RUNNERS = frozenset({"direct_api", "acp"})
AUTONOMOUS_COMMANDS = frozenset(
    {"reconcile_now", "enable_connector", "disable_connector", "kill", "resume"}
)


class ButlerSettingsError(ValueError):
    """One bounded, key-free settings error safe for the local page."""


def _bounded_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ButlerSettingsError(
            f"{label} must be an integer between {minimum} and {maximum}"
        )
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", value
    ):
        raise ButlerSettingsError(f"{label} must be a bounded identifier")
    return value


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _state_identifier(value: Any) -> str | None:
    try:
        return _identifier(value, "state identifier")
    except ButlerSettingsError:
        return None


def _state_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        return None
    return value


def _project_health(value: Any, *, connector: bool = False) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    observed_at = _timestamp(value.get("observed_at"))
    if status not in {"healthy", "degraded", "unavailable", "unknown"} or observed_at is None:
        return None
    result: dict[str, Any] = {"status": status, "observed_at": observed_at}
    if connector:
        connector_id = _state_identifier(value.get("connector_id"))
        if connector_id is None:
            return None
        result["connector_id"] = connector_id
    reason_code = value.get("reason_code")
    if reason_code is not None:
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,79}", reason_code
        ):
            return None
        result["reason_code"] = reason_code
    if connector:
        for name in ("last_success_at", "last_failure_at"):
            if name in value:
                timestamp = _timestamp(value[name])
                if timestamp is None:
                    return None
                result[name] = timestamp
    return result


def _project_autonomous_state(value: Any, board_id: Any) -> dict[str, Any] | None:
    """Allowlist one strict public state projection and discard unknown fields."""
    if not isinstance(value, Mapping) or value.get("schema") != "autonomous_butler_state_v1":
        return None
    state_board = _state_identifier(value.get("board_id"))
    revision = value.get("config_revision")
    effective = value.get("effective_state")
    observed_at = _timestamp(value.get("observed_at"))
    stale_after = _timestamp(value.get("stale_after"))
    if (
        value.get("schema_version") != 1
        or state_board is None
        or state_board != board_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or effective not in AUTONOMOUS_STATES
        or observed_at is None
        or stale_after is None
        or not isinstance(value.get("kill_latched"), bool)
    ):
        return None
    capacity = value.get("capacity")
    if not isinstance(capacity, Mapping):
        return None
    projected_capacity: dict[str, Any] = {}
    for role in AUTONOMOUS_ROLES:
        row = capacity.get(role)
        if not isinstance(row, Mapping):
            return None
        counts = {
            name: _state_count(row.get(name))
            for name in (
                "desired",
                "ready",
                "busy",
                "starting",
                "draining",
                "unhealthy",
                "stopped",
            )
        }
        seat_ids = row.get("seat_ids")
        template_ids = row.get("template_ids")
        if (
            any(item is None for item in counts.values())
            or not isinstance(seat_ids, list)
            or not isinstance(template_ids, list)
            or len(seat_ids) > 1_000
            or len(template_ids) > 1_000
        ):
            return None
        clean_seats = [_state_identifier(item) for item in seat_ids]
        clean_templates = [_state_identifier(item) for item in template_ids]
        if any(item is None for item in clean_seats + clean_templates):
            return None
        projected_capacity[role] = {
            **counts,
            "seat_ids": clean_seats,
            "template_ids": clean_templates,
        }
    host = value.get("host_processes")
    if not isinstance(host, Mapping):
        return None
    host_counts = {
        name: _state_count(host.get(name))
        for name in (
            "role_agents",
            "control_plane",
            "agent_process_ceiling",
            "total_process_ceiling",
        )
    }
    host_observed_at = _timestamp(host.get("observed_at"))
    executor = _project_health(value.get("executor"))
    connectors = value.get("connectors")
    if (
        any(item is None for item in host_counts.values())
        or host_counts["agent_process_ceiling"] == 0
        or host_counts["total_process_ceiling"] == 0
        or host_observed_at is None
        or executor is None
        or not isinstance(connectors, list)
        or len(connectors) > 32
    ):
        return None
    projected_connectors = [
        _project_health(item, connector=True) for item in connectors
    ]
    if any(item is None for item in projected_connectors):
        return None
    result: dict[str, Any] = {
        "schema": "autonomous_butler_state_v1",
        "schema_version": 1,
        "board_id": state_board,
        "config_revision": revision,
        "effective_state": effective,
        "observed_at": observed_at,
        "stale_after": stale_after,
        "capacity": projected_capacity,
        "host_processes": {**host_counts, "observed_at": host_observed_at},
        "executor": executor,
        "connectors": projected_connectors,
        "kill_latched": value["kill_latched"],
    }
    reason_code = value.get("reason_code")
    if reason_code is not None:
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,79}", reason_code
        ):
            return None
        result["reason_code"] = reason_code
    for name in ("current_command_id", "current_operation_id"):
        if name in value:
            identifier = _state_identifier(value[name])
            if identifier is None:
                return None
            result[name] = identifier
    return result


def autonomous_butler_view(
    config_payload: Mapping[str, Any],
    command_payload: Mapping[str, Any],
    actual_state: Any = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project only public, typed Autonomous Butler control-plane fields."""
    config = config_payload.get("config")
    if config is not None and not isinstance(config, Mapping):
        raise ButlerSettingsError("Autonomous Butler config is malformed")
    clean_config = copy.deepcopy(dict(config)) if isinstance(config, Mapping) else None
    if clean_config is not None:
        for connector in clean_config.get("desired", {}).get("connectors", []):
            if isinstance(connector, dict):
                # Secret references are useful only to the transport adapter. Fleet
                # exposes presence, never the reference identifier or secret value.
                connector["secret_configured"] = connector.get("secret_ref") is not None
                connector.pop("secret_ref", None)
                connector.pop("endpoint_ref", None)
    commands = command_payload.get("commands", [])
    if not isinstance(commands, list):
        raise ButlerSettingsError("Autonomous Butler command history is malformed")
    projected_commands: list[dict[str, Any]] = []
    for row in commands[:50]:
        if not isinstance(row, Mapping):
            continue
        transition = row.get("transition")
        projected_commands.append(
            {
                "command_id": row.get("command_id"),
                "intent": row.get("intent"),
                "status": row.get("status"),
                "revision": row.get("revision"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "reason_code": (
                    transition.get("reason_code")
                    if isinstance(transition, Mapping)
                    else None
                ),
                "audit_id": (
                    transition.get("audit_id")
                    if isinstance(transition, Mapping)
                    else None
                ),
                "expired": row.get("expired") is True,
            }
        )
    state = _project_autonomous_state(actual_state, config_payload.get("board_id"))
    state_stale = False
    if state is not None:
        stale_after = state.get("stale_after")
        try:
            stale_at = datetime.fromisoformat(str(stale_after).replace("Z", "+00:00"))
            if stale_at.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError:
            state = None
        else:
            current_time = now or datetime.now(timezone.utc)
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            state_stale = stale_at.astimezone(timezone.utc) <= current_time.astimezone(
                timezone.utc
            )
            if state_stale:
                executor = state.get("executor")
                if isinstance(executor, dict):
                    executor["status"] = "unknown"
                    executor["reason_code"] = "stale_observation"
                for connector in state.get("connectors", []):
                    if isinstance(connector, dict):
                        connector["status"] = "unknown"
                        connector["reason_code"] = "stale_observation"
    effective = config_payload.get("effective_mode", "shadow")
    if state is not None and state.get("effective_state") in AUTONOMOUS_STATES:
        effective = state["effective_state"]
    return {
        "schema_version": 1,
        "board_id": config_payload.get("board_id"),
        "revision": config_payload.get("revision", 0),
        "config_digest_sha256": config_payload.get("config_digest_sha256"),
        "effective_state": effective if effective in AUTONOMOUS_STATES else "shadow",
        "config": clean_config,
        "actual_state": state,
        "actual_state_available": state is not None,
        "actual_state_stale": state_stale,
        "commands": projected_commands,
        "history_truncated": command_payload.get("truncated") is True,
    }


def prepare_autonomous_butler_config(
    current_payload: Mapping[str, Any], request: Any
) -> tuple[dict[str, Any], int, str]:
    """Validate a bounded Fleet edit while preserving human-owned authority."""
    required = {
        "board_id",
        "mutation_id",
        "expected_revision",
        "mode",
        "runner",
        "capacity",
        "host_concurrency",
        "board_concurrency",
        "cooldowns",
        "budget",
        "connectors",
    }
    if not isinstance(request, Mapping) or set(request) != required:
        raise ButlerSettingsError("Autonomous Butler config request fields are invalid")
    board_id = _identifier(request["board_id"], "board_id")
    mutation_id = _identifier(request["mutation_id"], "mutation_id")
    expected = _bounded_integer(
        request["expected_revision"], "expected_revision", 0, 2**63 - 1
    )
    current_revision = current_payload.get("revision", 0)
    if expected != current_revision:
        raise ButlerSettingsError("configuration changed; reload before saving")
    current = current_payload.get("config")
    if not isinstance(current, Mapping):
        raise ButlerSettingsError(
            "Autonomous Butler must be provisioned by an administrator first"
        )
    if current.get("board_id") != board_id:
        raise ButlerSettingsError("board_id does not match the current configuration")
    envelope = current.get("envelope")
    if not isinstance(envelope, Mapping):
        raise ButlerSettingsError("Autonomous Butler envelope is malformed")
    mode = request["mode"]
    if mode not in {"shadow", "autonomous"}:
        raise ButlerSettingsError("mode is invalid")
    if mode == "autonomous":
        raise ButlerSettingsError(
            "Fleet cannot activate autonomous mode; a separate active authorization is required"
        )
    runner = request["runner"]
    if runner not in AUTONOMOUS_RUNNERS:
        raise ButlerSettingsError("runner is invalid")
    capacity = request["capacity"]
    if not isinstance(capacity, Mapping) or set(capacity) != set(AUTONOMOUS_ROLES):
        raise ButlerSettingsError("capacity fields are invalid")
    clean_capacity: dict[str, dict[str, int]] = {}
    for role in AUTONOMOUS_ROLES:
        counts = capacity[role]
        if not isinstance(counts, Mapping) or set(counts) != {"min", "target", "max"}:
            raise ButlerSettingsError(f"capacity.{role} fields are invalid")
        clean = {
            name: _bounded_integer(counts[name], f"capacity.{role}.{name}", 0, 100)
            for name in ("min", "target", "max")
        }
        if not clean["min"] <= clean["target"] <= clean["max"]:
            raise ButlerSettingsError(
                f"capacity.{role} must satisfy min <= target <= max"
            )
        envelope_capacity = envelope.get("max_capacity")
        if (
            not isinstance(envelope_capacity, Mapping)
            or isinstance(envelope_capacity.get(role), bool)
            or not isinstance(envelope_capacity.get(role), int)
            or clean["max"] > envelope_capacity[role]
        ):
            raise ButlerSettingsError(f"capacity.{role} exceeds the immutable envelope")
        clean_capacity[role] = clean
    host_concurrency = _bounded_integer(
        request["host_concurrency"], "host_concurrency", 1, 256
    )
    board_concurrency = _bounded_integer(
        request["board_concurrency"], "board_concurrency", 1, 256
    )
    if host_concurrency > envelope.get("max_host_concurrency", 0):
        raise ButlerSettingsError("host_concurrency exceeds the immutable envelope")
    if board_concurrency > envelope.get("max_board_concurrency", 0):
        raise ButlerSettingsError("board_concurrency exceeds the immutable envelope")
    runtime = current.get("host_runtime")
    if not isinstance(runtime, Mapping) or host_concurrency > runtime.get(
        "agent_process_ceiling", 0
    ):
        raise ButlerSettingsError("host_concurrency exceeds the host runtime ceiling")
    if sum(role["target"] for role in clean_capacity.values()) > board_concurrency:
        raise ButlerSettingsError("capacity targets exceed board_concurrency")
    if sum(role["target"] for role in clean_capacity.values()) > host_concurrency:
        raise ButlerSettingsError("capacity targets exceed host_concurrency")
    cooldowns = request["cooldowns"]
    if not isinstance(cooldowns, Mapping) or set(cooldowns) != {
        "scale_up_s",
        "scale_down_s",
        "failure_backoff_s",
    }:
        raise ButlerSettingsError("cooldown fields are invalid")
    clean_cooldowns = {
        "scale_up_s": _bounded_integer(
            cooldowns["scale_up_s"], "cooldowns.scale_up_s", 0, 604_800
        ),
        "scale_down_s": _bounded_integer(
            cooldowns["scale_down_s"], "cooldowns.scale_down_s", 0, 604_800
        ),
        "failure_backoff_s": _bounded_integer(
            cooldowns["failure_backoff_s"], "cooldowns.failure_backoff_s", 1, 86_400
        ),
    }
    budget = request["budget"]
    if not isinstance(budget, Mapping) or set(budget) != {
        "period",
        "max_tokens",
        "max_cost_microunits",
        "max_external_calls",
    }:
        raise ButlerSettingsError("budget fields are invalid")
    if budget["period"] not in {"hour", "day", "month"}:
        raise ButlerSettingsError("budget.period is invalid")
    clean_budget = {
        "period": budget["period"],
        "max_tokens": _bounded_integer(
            budget["max_tokens"], "budget.max_tokens", 0, 1_000_000_000
        ),
        "max_cost_microunits": _bounded_integer(
            budget["max_cost_microunits"],
            "budget.max_cost_microunits",
            0,
            1_000_000_000_000,
        ),
        "max_external_calls": _bounded_integer(
            budget["max_external_calls"], "budget.max_external_calls", 0, 1_000_000
        ),
    }
    envelope_budget = envelope.get("max_budget")
    if not isinstance(envelope_budget, Mapping) or any(
        clean_budget[name] > envelope_budget.get(name, -1)
        for name in ("max_tokens", "max_cost_microunits", "max_external_calls")
    ):
        raise ButlerSettingsError("budget exceeds the immutable envelope")
    if clean_budget["period"] != envelope_budget.get("period"):
        raise ButlerSettingsError("budget period must match the immutable envelope")
    requested_connectors = request["connectors"]
    stored_connectors = current.get("desired", {}).get("connectors", [])
    if not isinstance(requested_connectors, list) or len(requested_connectors) > 32:
        raise ButlerSettingsError("connectors are invalid")
    enabled_by_id: dict[str, bool] = {}
    for connector in requested_connectors:
        if not isinstance(connector, Mapping) or set(connector) != {
            "connector_id",
            "enabled",
        }:
            raise ButlerSettingsError("connector fields are invalid")
        connector_id = _identifier(connector["connector_id"], "connector_id")
        if not isinstance(connector["enabled"], bool) or connector_id in enabled_by_id:
            raise ButlerSettingsError("connector enablement is invalid")
        enabled_by_id[connector_id] = connector["enabled"]
    stored_ids = {
        row.get("connector_id")
        for row in stored_connectors
        if isinstance(row, Mapping)
    }
    if set(enabled_by_id) != stored_ids:
        raise ButlerSettingsError("connector set changed; reload before saving")
    approved_ids = envelope.get("approved_connector_ids")
    if not isinstance(approved_ids, list) or not stored_ids <= set(approved_ids):
        raise ButlerSettingsError("connector set exceeds the immutable envelope")
    updated = copy.deepcopy(dict(current))
    updated["revision"] = expected + 1
    updated["enabled"] = False
    updated.pop("authorization", None)
    desired = updated["desired"]
    desired.update(
        {
            "mode": "shadow",
            "runner": runner,
            "capacity": clean_capacity,
            "host_concurrency": host_concurrency,
            "board_concurrency": board_concurrency,
            "cooldowns": clean_cooldowns,
            "budget": clean_budget,
        }
    )
    for connector in desired["connectors"]:
        connector["enabled"] = enabled_by_id[connector["connector_id"]]
    return updated, expected, mutation_id


def validate_autonomous_command_request(request: Any) -> dict[str, Any]:
    required = {"board_id", "request_id", "intent", "expected_config_revision"}
    optional = {"connector_id", "reason_code"}
    if (
        not isinstance(request, Mapping)
        or not required <= set(request)
        or not set(request) <= required | optional
    ):
        raise ButlerSettingsError("Autonomous Butler command request fields are invalid")
    board_id = _identifier(request["board_id"], "board_id")
    request_id = _identifier(request["request_id"], "request_id")
    intent = request["intent"]
    if intent not in AUTONOMOUS_COMMANDS:
        raise ButlerSettingsError("command intent is not allowlisted")
    revision = _bounded_integer(
        request["expected_config_revision"], "expected_config_revision", 0, 2**63 - 1
    )
    parameters: dict[str, Any]
    if intent in {"enable_connector", "disable_connector"}:
        parameters = {
            "connector_id": _identifier(request.get("connector_id"), "connector_id")
        }
        if "reason_code" in request:
            raise ButlerSettingsError("connector commands do not accept reason_code")
    elif intent in {"kill", "resume"}:
        reason = request.get("reason_code")
        if not isinstance(reason, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,79}", reason
        ):
            raise ButlerSettingsError("reason_code is invalid")
        parameters = {"reason_code": reason}
        if "connector_id" in request:
            raise ButlerSettingsError("kill and resume do not accept connector_id")
    else:
        if set(request) & optional:
            raise ButlerSettingsError("reconcile_now does not accept parameters")
        parameters = {}
    return {
        "board_id": board_id,
        "request_id": request_id,
        "intent": intent,
        "expected_config_revision": revision,
        "parameters": parameters,
    }


@dataclass(frozen=True)
class ValidationResult:
    outcome: str
    message: str
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "message": self.message,
            "http_status": self.http_status,
        }


def _text(value: Any, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ButlerSettingsError(f"{label} must be text")
    clean = value.strip()
    if (not clean and not allow_empty) or len(clean) > limit:
        raise ButlerSettingsError(f"{label} is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in clean):
        raise ButlerSettingsError(f"{label} contains control characters")
    return clean


def validate_endpoint(value: Any) -> str:
    endpoint = _text(value, "endpoint", MAX_ENDPOINT_CHARS).rstrip("/")
    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or "").casefold()
    try:
        parsed.port
    except ValueError as exc:
        raise ButlerSettingsError("endpoint contains an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ButlerSettingsError(
            "endpoint must be an http(s) URL without credentials, query, or fragment"
        )
    address_text = hostname.replace("%25", "%").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        address = None
    if address is None:
        try:
            numeric = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                0,
                socket.SOCK_STREAM,
                0,
                socket.AI_NUMERICHOST,
            )
        except socket.gaierror:
            numeric = []
        if numeric:
            address = ipaddress.ip_address(str(numeric[0][4][0]).split("%", 1)[0])
    effective_address = (
        address.ipv4_mapped
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped
        else address
    )
    if effective_address is not None and effective_address.is_link_local:
        raise ButlerSettingsError("endpoint must not use a link-local address")
    loopback = hostname == "localhost" or bool(
        effective_address and effective_address.is_loopback
    )
    if parsed.scheme == "http" and not loopback:
        raise ButlerSettingsError("endpoint must use https unless it is loopback")
    return endpoint


def _resolve_endpoint(
    endpoint: str,
    resolver: Callable[..., Sequence[_AddressInfo]] = socket.getaddrinfo,
) -> tuple[_AddressInfo, ...]:
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolved = tuple(resolver(hostname, port, 0, socket.SOCK_STREAM))
    except OSError as exc:
        raise ButlerSettingsError("endpoint host could not be resolved") from exc
    if not resolved:
        raise ButlerSettingsError("endpoint host could not be resolved")
    for family, _socktype, _proto, _canonname, sockaddr in resolved:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            raise ButlerSettingsError("endpoint host resolved unexpectedly")
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError as exc:
            raise ButlerSettingsError("endpoint host resolved unexpectedly") from exc
        effective = (
            address.ipv4_mapped
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped
            else address
        )
        if effective.is_link_local:
            raise ButlerSettingsError("endpoint must not use a link-local address")
        if parsed.scheme == "http" and not effective.is_loopback:
            raise ButlerSettingsError("endpoint must use https unless it is loopback")
    return resolved


def _connect_resolved(
    resolved: tuple[_AddressInfo, ...],
    timeout: float | object,
    source_address: tuple[str, int] | None,
) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in resolved:
        candidate = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                candidate.settimeout(timeout)
            if source_address:
                candidate.bind(source_address)
            candidate.connect(sockaddr)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    if last_error is not None:
        raise last_error
    raise OSError("no validated endpoint addresses")


class _PinnedConnectionMixin:
    def __init__(
        self,
        host: str,
        *,
        resolved: tuple[_AddressInfo, ...],
        **kwargs: Any,
    ) -> None:
        self._validated_addresses = resolved
        super().__init__(host, **kwargs)
        # HTTPConnection.__init__ deliberately installs socket.create_connection
        # on the instance.  Replace that callback after the stdlib initializer so
        # connect() cannot re-resolve the hostname and bypass the validated set.
        self._create_connection = self._create_validated_connection

    def _create_validated_connection(
        self,
        _address: tuple[str, int],
        timeout: float | object,
        source_address: tuple[str, int] | None,
    ) -> socket.socket:
        return _connect_resolved(
            self._validated_addresses, timeout, source_address
        )


class _PinnedHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(HTTPHandler):
    def __init__(self, resolved: tuple[_AddressInfo, ...]) -> None:
        super().__init__()
        self._connection = partial(_PinnedHTTPConnection, resolved=resolved)

    def http_open(self, request: Request) -> Any:
        return self.do_open(self._connection, request)


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, resolved: tuple[_AddressInfo, ...]) -> None:
        super().__init__()
        self._connection = partial(_PinnedHTTPSConnection, resolved=resolved)

    def https_open(self, request: Request) -> Any:
        return self.do_open(self._connection, request, context=self._context)


def _pinned_opener(
    validation_url: str, resolved: tuple[_AddressInfo, ...]
) -> Callable[..., Any]:
    return build_opener(
        ProxyHandler({}),
        _SameOriginRedirectHandler(validation_url),
        _PinnedHTTPHandler(resolved),
        _PinnedHTTPSHandler(resolved),
    ).open


def _url_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Follow validation redirects only while they stay on the original origin."""

    def __init__(self, validation_url: str) -> None:
        super().__init__()
        self._origin = _url_origin(validation_url)

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> Request | None:
        try:
            allowed = validate_endpoint(newurl)
        except ButlerSettingsError as exc:
            raise URLError("redirect refused") from exc
        if _url_origin(allowed) != self._origin:
            raise URLError("redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > MAX_HEADER_COUNT:
        raise ButlerSettingsError("extra_headers must be a bounded object")
    result: dict[str, str] = {}
    lowered: set[str] = set()
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not _HEADER_NAME.fullmatch(raw_name):
            raise ButlerSettingsError("extra_headers contains an invalid name")
        if _SECRET_HEADER.search(raw_name):
            raise ButlerSettingsError(
                "secret-bearing headers must use the write-only key field"
            )
        name = raw_name.strip()
        folded = name.casefold()
        if folded == "host":
            raise ButlerSettingsError("extra_headers must not set Host")
        if folded in lowered:
            raise ButlerSettingsError("extra_headers names must be unique")
        lowered.add(folded)
        result[name] = _text(
            raw_value, f"extra_headers.{name}", MAX_HEADER_VALUE_CHARS, allow_empty=True
        )
    return result


def validate_request(value: Any) -> dict[str, Any]:
    expected = {
        "endpoint",
        "model",
        "api_key",
        "extra_headers",
        "key_header",
        "key_prefix",
        "validation_path",
        "draft_path",
        "draft_protocol",
        "expected_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ButlerSettingsError("request fields are invalid")
    key_header = _text(value["key_header"], "key_header", 128)
    if not _HEADER_NAME.fullmatch(key_header):
        raise ButlerSettingsError("key_header is invalid")
    if key_header.casefold() == "host":
        raise ButlerSettingsError("key_header must not be Host")
    key_prefix = _text(value["key_prefix"], "key_prefix", 80, allow_empty=True)
    validation_path = _text(
        value["validation_path"], "validation_path", 500, allow_empty=True
    ) or DEFAULT_VALIDATION_PATH
    parsed_path = urlsplit(validation_path)
    if parsed_path.scheme or parsed_path.netloc or parsed_path.query or parsed_path.fragment:
        raise ButlerSettingsError("validation_path must be a relative URL path")
    draft_path = (
        _text(value["draft_path"], "draft_path", 500, allow_empty=True)
        or DEFAULT_DRAFT_PATH
    )
    parsed_draft_path = urlsplit(draft_path)
    if (
        parsed_draft_path.scheme
        or parsed_draft_path.netloc
        or parsed_draft_path.query
        or parsed_draft_path.fragment
    ):
        raise ButlerSettingsError("draft_path must be a relative URL path")
    if value["draft_protocol"] not in DRAFT_PROTOCOLS:
        raise ButlerSettingsError("draft_protocol is invalid")
    api_key = value["api_key"]
    if not isinstance(api_key, str) or len(api_key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ButlerSettingsError("api_key is invalid")
    if api_key != api_key.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in api_key
    ):
        raise ButlerSettingsError(
            "api_key must not contain surrounding whitespace or control characters"
        )
    expected_sha256 = value["expected_sha256"]
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise ButlerSettingsError("expected_sha256 is invalid")
    headers = validate_headers(value["extra_headers"])
    if key_header.casefold() in {name.casefold() for name in headers}:
        raise ButlerSettingsError("key_header must not duplicate extra_headers")
    return {
        "endpoint": validate_endpoint(value["endpoint"]),
        "model": _text(value["model"], "model", MAX_MODEL_CHARS),
        "api_key": api_key,
        "extra_headers": headers,
        "key_header": key_header,
        "key_prefix": key_prefix,
        "validation_path": validation_path,
        "draft_path": draft_path,
        "draft_protocol": value["draft_protocol"],
        "expected_sha256": expected_sha256,
    }


def reject_readable_credential(settings: Mapping[str, Any], api_key: str) -> None:
    """Reject a credential duplicated into any persisted or readable setting."""
    if not api_key:
        return
    readable = [
        str(settings[name])
        for name in (
            "endpoint",
            "model",
            "key_header",
            "key_prefix",
            "validation_path",
            "draft_path",
            "draft_protocol",
        )
    ]
    for name, value in dict(settings["extra_headers"]).items():
        readable.extend((str(name), str(value)))
    if any(api_key in value for value in readable):
        raise ButlerSettingsError(
            "credential must not appear in persisted or readable settings"
        )


def validate_board_butler_document(value: Any) -> dict[str, Any]:
    """Validate the documented Board Butler envelope before a dashboard write."""
    required = {"schema_version", "global"}
    allowed = required | {"projects", "boards"}
    if (
        not isinstance(value, Mapping)
        or not required <= set(value)
        or not set(value) <= allowed
    ):
        raise ButlerSettingsError("board_butler fields are invalid")
    if value.get("schema_version") != 1:
        raise ButlerSettingsError("board_butler.schema_version must be 1")
    setting_keys = {
        "mode",
        "answer_scope",
        "required_evidence_kinds",
        "ceilings",
        "hold_before_post_s",
        "active_windows",
        "kill_switch",
        "auto_demote",
        "classification",
        "drafting",
    }
    provider_keys = {
        "model",
        "endpoint_ref",
        "key_ref",
        "extra_headers",
        "key_header",
        "key_prefix",
        "validation_path",
        "draft_path",
        "draft_protocol",
    }

    def provider(candidate: Any, path: str) -> None:
        if not isinstance(candidate, Mapping) or not set(candidate) <= provider_keys:
            raise ButlerSettingsError(f"{path} fields are invalid")
        for name in ("model", "endpoint_ref", "key_ref"):
            selected = candidate.get(name)
            if selected is not None and (
                not isinstance(selected, str)
                or not selected.strip()
                or len(selected) > 300
            ):
                raise ButlerSettingsError(f"{path}.{name} is invalid")
        if "extra_headers" in candidate:
            validate_headers(candidate["extra_headers"])
        if "key_header" in candidate:
            selected = candidate["key_header"]
            if not isinstance(selected, str) or not _HEADER_NAME.fullmatch(selected):
                raise ButlerSettingsError(f"{path}.key_header is invalid")
        if "key_prefix" in candidate:
            _text(candidate["key_prefix"], f"{path}.key_prefix", 80, allow_empty=True)
        if "validation_path" in candidate:
            _text(candidate["validation_path"], f"{path}.validation_path", 500)
        if "draft_path" in candidate:
            _text(candidate["draft_path"], f"{path}.draft_path", 500)
        if (
            "draft_protocol" in candidate
            and candidate["draft_protocol"] not in DRAFT_PROTOCOLS
        ):
            raise ButlerSettingsError(f"{path}.draft_protocol is invalid")

    def settings(candidate: Any, path: str) -> None:
        if not isinstance(candidate, Mapping) or not set(candidate) <= setting_keys:
            raise ButlerSettingsError(f"{path} fields are invalid")
        if "mode" in candidate and candidate["mode"] not in {"shadow", "active"}:
            raise ButlerSettingsError(f"{path}.mode is invalid")
        for name in ("answer_scope", "ceilings", "auto_demote"):
            if name in candidate and not isinstance(candidate[name], Mapping):
                raise ButlerSettingsError(f"{path}.{name} must be an object")
        if "required_evidence_kinds" in candidate and not isinstance(
            candidate["required_evidence_kinds"], list
        ):
            raise ButlerSettingsError(f"{path}.required_evidence_kinds must be a list")
        if "hold_before_post_s" in candidate and (
            type(candidate["hold_before_post_s"]) is not int
            or not 0 <= candidate["hold_before_post_s"] <= 604_800
        ):
            raise ButlerSettingsError(f"{path}.hold_before_post_s is invalid")
        if "active_windows" in candidate and not isinstance(
            candidate["active_windows"], list
        ):
            raise ButlerSettingsError(f"{path}.active_windows must be a list")
        if "kill_switch" in candidate and type(candidate["kill_switch"]) is not bool:
            raise ButlerSettingsError(f"{path}.kill_switch must be boolean")
        for task in ("classification", "drafting"):
            if task in candidate:
                provider(candidate[task], f"{path}.{task}")

    settings(value["global"], "board_butler.global")
    for collection_name in ("projects", "boards"):
        collection = value.get(collection_name, {})
        if not isinstance(collection, Mapping) or len(collection) > 100:
            raise ButlerSettingsError(f"board_butler.{collection_name} is invalid")
        for name, candidate in collection.items():
            if not isinstance(name, str) or not name or len(name) > 200:
                raise ButlerSettingsError(
                    f"board_butler.{collection_name} name is invalid"
                )
            settings(candidate, f"board_butler.{collection_name}.{name}")
    return copy.deepcopy(dict(value))


def _model_ids(document: Any) -> set[str]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, Mapping):
        candidate = document.get("data", document.get("models", []))
        rows = candidate if isinstance(candidate, list) else []
    else:
        rows = []
    result: set[str] = set()
    for row in rows[:10_000]:
        if isinstance(row, str):
            result.add(row)
        elif isinstance(row, Mapping):
            selected = row.get("id", row.get("name", row.get("model")))
            if isinstance(selected, str):
                result.add(selected)
    return result


def validate_provider(
    settings: Mapping[str, Any],
    api_key: str,
    *,
    opener: Callable[..., Any] | None = None,
    resolver: Callable[..., Sequence[_AddressInfo]] | None = None,
    timeout_s: float = 5.0,
) -> ValidationResult:
    """Make one bounded call and return only a fixed, key-free outcome."""
    try:
        endpoint = validate_endpoint(settings["endpoint"])
        extra_headers = validate_headers(settings["extra_headers"])
        key_header = _text(settings["key_header"], "key_header", 128)
        if not _HEADER_NAME.fullmatch(key_header):
            raise ButlerSettingsError("key_header is invalid")
        if key_header.casefold() == "host":
            raise ButlerSettingsError("key_header must not be Host")
        if key_header.casefold() in {name.casefold() for name in extra_headers}:
            raise ButlerSettingsError("key_header must not duplicate extra_headers")
        key_prefix = _text(
            settings["key_prefix"], "key_prefix", 80, allow_empty=True
        )
        model = _text(settings["model"], "model", MAX_MODEL_CHARS)
        if not isinstance(api_key, str) or len(api_key.encode("utf-8")) > MAX_KEY_BYTES:
            raise ButlerSettingsError("api_key is invalid")
        if api_key != api_key.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in api_key
        ):
            raise ButlerSettingsError("api_key is invalid")
        validation_path = _text(
            settings["validation_path"],
            "validation_path",
            500,
            allow_empty=True,
        ) or DEFAULT_VALIDATION_PATH
        parsed_path = urlsplit(validation_path)
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
        ):
            raise ButlerSettingsError("validation_path must be a relative URL path")
        validation_url = urljoin(
            endpoint.rstrip("/") + "/",
            validation_path.lstrip("/"),
        )
        resolved: tuple[_AddressInfo, ...] | None = None
        if opener is None or resolver is not None:
            resolved = _resolve_endpoint(endpoint, resolver or socket.getaddrinfo)
        headers = {"Accept": "application/json", **extra_headers}
        if api_key:
            headers[key_header] = f"{key_prefix} {api_key}".strip()
        request = Request(validation_url, headers=headers, method="GET")
    except (ButlerSettingsError, KeyError, TypeError, ValueError):
        return ValidationResult("unreachable", "The endpoint is not permitted.")
    try:
        open_request = opener
        if open_request is None:
            assert resolved is not None
            open_request = _pinned_opener(validation_url, resolved)
        with open_request(request, timeout=timeout_s) as response:
            geturl = getattr(response, "geturl", None)
            final_url = geturl() if callable(geturl) else validation_url
            if _url_origin(final_url) != _url_origin(validation_url):
                return ValidationResult("unreachable", "The endpoint is not permitted.")
            status = int(getattr(response, "status", 200))
            payload = response.read(1_000_001)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            return ValidationResult(
                "rejected_credential", "The endpoint rejected the credential.", exc.code
            )
        return ValidationResult(
            "unreachable", "The endpoint returned an unusable response.", exc.code
        )
    except (URLError, TimeoutError, OSError, ValueError):
        return ValidationResult("unreachable", "The endpoint could not be reached.")
    if not 200 <= status < 300 or len(payload) > 1_000_000:
        return ValidationResult(
            "unreachable", "The endpoint returned an unusable response.", status
        )
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ValidationResult(
            "wrong_model", "The selected model was not present in the validation response.", status
        )
    if model not in _model_ids(document):
        return ValidationResult(
            "wrong_model", "The selected model was not present in the validation response.", status
        )
    return ValidationResult("reachable", "Endpoint, credential, and model validated.", status)


class ButlerSettingsManager:
    """Manage one CAS-backed config slice and locally versioned key files."""

    def __init__(
        self,
        root: str | Path,
        *,
        opener: Callable[..., Any] | None = None,
        now: Callable[[], datetime] | None = None,
        runtime_path: str | Path | None = None,
        kill_path: str | Path | None = None,
        pid_path: str | Path | None = None,
        expected_process_path: str | Path | None = None,
        process_inspector: Callable[[int], bool] | None = None,
        process_probe: Callable[[int], bool] | None = None,
        signaler: Callable[[int, int], None] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.opener = opener
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.runtime_path = (
            Path(runtime_path).expanduser().resolve()
            if runtime_path
            else self.root.parent / "runtime.json"
        )
        self.kill_path = (
            Path(kill_path).expanduser().resolve()
            if kill_path
            else self.root.parent / "KILLED"
        )
        self.pid_path = (
            Path(pid_path).expanduser().resolve()
            if pid_path
            else self.runtime_path.parent / "board-butler.pid"
        )
        self.expected_process_path = (
            Path(expected_process_path).expanduser().resolve()
            if expected_process_path
            else Path(__file__).parents[1] / "board-butler" / "board_butler.py"
        ).resolve()
        if process_inspector is not None and process_probe is not None:
            raise ValueError("provide only one process identity verifier")
        # process_probe remains as a compatibility alias for focused tests. The
        # product path always uses the lock + OS process identity verifier.
        self.process_inspector = (
            process_inspector or process_probe or self._verified_butler_process
        )
        self.signaler = signaler or os.kill
        self._lock = threading.RLock()

    def _pid_lock_held_by(self, pid: int) -> bool:
        """Require the resident's private singleton lock to name this PID."""
        if not self._private_file(self.pid_path):
            return False
        try:
            if self.pid_path.read_text(encoding="utf-8")[:64].strip() != str(pid):
                return False
            descriptor = os.open(
                self.pid_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            )
        except (OSError, UnicodeError):
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _verified_butler_process(self, pid: int) -> bool:
        """Bind a runtime PID to the live, non-zombie Board Butler resident."""
        if not self._pid_lock_held_by(pid):
            return False
        try:
            result = subprocess.run(
                ["/bin/ps", "-ww", "-p", str(pid), "-o", "state=", "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        line = result.stdout.strip()
        if result.returncode != 0 or not line:
            return False
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or fields[0].upper().startswith("Z"):
            return False
        try:
            arguments = shlex.split(fields[1])
        except ValueError:
            return False
        expected = self.expected_process_path
        matches_entrypoint = False
        for argument in arguments[:5]:
            if not argument or argument.startswith("-"):
                continue
            try:
                matches_entrypoint = Path(argument).expanduser().resolve() == expected
            except (OSError, RuntimeError):
                matches_entrypoint = False
            if matches_entrypoint:
                break
        # Recheck the lock after reading process metadata. If the resident
        # exited or the PID was reused during inspection, fail closed.
        return matches_entrypoint and self._pid_lock_held_by(pid)

    @staticmethod
    def _private_file(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(info.st_mode)
            and not path.is_symlink()
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
        )

    def _runtime(self, configured: bool) -> dict[str, Any]:
        document: Mapping[str, Any] = {}
        if self._private_file(self.runtime_path):
            try:
                candidate = json.loads(
                    self.runtime_path.read_text(encoding="utf-8")[:16_384]
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                candidate = {}
            if isinstance(candidate, Mapping):
                document = candidate
        pid = document.get("pid")
        mode = document.get("mode")
        alive = bool(
            document.get("running") is True
            and isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 1
            and mode in {"shadow", "active"}
            and self.process_inspector(pid)
        )
        if alive:
            state = f"running_{mode}"
        elif configured:
            state = "configured_not_running"
        else:
            state = "not_configured"
        last_activity = document.get("last_activity")
        if last_activity not in {
            "startup",
            "registry_refresh",
            "question_processed",
            "stopped",
        }:
            last_activity = None

        def timestamp(name: str) -> str | None:
            value = document.get(name)
            if not isinstance(value, str) or len(value) > 64:
                return None
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return value

        return {
            "state": state,
            "configured": configured,
            "running": alive,
            "mode": mode if alive else None,
            "pid": pid if alive else None,
            "started_at": timestamp("started_at"),
            "last_activity_at": timestamp("last_activity_at"),
            "last_activity": last_activity,
            "kill_switch_engaged": self._private_file(self.kill_path),
        }

    @staticmethod
    def _global(config: Any) -> dict[str, Any]:
        if not isinstance(config, Mapping):
            return {}
        butler = config.get("board_butler")
        if not isinstance(butler, Mapping):
            return {}
        global_settings = butler.get("global")
        return dict(global_settings) if isinstance(global_settings, Mapping) else {}

    @staticmethod
    def _provider(global_settings: Mapping[str, Any]) -> dict[str, Any]:
        for name in ("drafting", "classification"):
            candidate = global_settings.get(name)
            if isinstance(candidate, Mapping):
                return dict(candidate)
        return {}

    def _managed_reference(self, value: Any) -> Path | None:
        if not isinstance(value, str) or (match := _MANAGED_KEY_REFERENCE.fullmatch(value)) is None:
            return None
        path = self.root / match.group(1)
        try:
            resolved = path.resolve()
            resolved.relative_to(self.root)
            info = resolved.stat()
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        return resolved

    @staticmethod
    def _reference(path: Path) -> str:
        return f"file:{path.name}"

    def _read_key(self, reference: Any) -> str:
        path = self._managed_reference(reference)
        if path is None:
            return ""
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        return value if len(value.encode("utf-8")) <= MAX_KEY_BYTES else ""

    def view(self, config_payload: Mapping[str, Any], central: str) -> dict[str, Any]:
        config = config_payload.get("config")
        global_settings = self._global(config)
        provider = self._provider(global_settings)
        key_ref = provider.get("key_ref")
        key_path = self._managed_reference(key_ref)
        mode = "off" if global_settings.get("kill_switch", True) else "shadow"
        configured = bool(provider.get("endpoint_ref") and provider.get("model"))
        return {
            "schema_version": 1,
            "central": central,
            "mode": mode,
            "endpoint": provider.get("endpoint_ref") or "",
            "model": provider.get("model") or "",
            "extra_headers": (
                dict(provider.get("extra_headers", {}))
                if isinstance(provider.get("extra_headers"), Mapping)
                else {}
            ),
            "key_header": provider.get("key_header") or "Authorization",
            "key_prefix": provider.get("key_prefix") or "Bearer",
            "validation_path": provider.get("validation_path") or DEFAULT_VALIDATION_PATH,
            "draft_path": provider.get("draft_path") or DEFAULT_DRAFT_PATH,
            "draft_protocol": provider.get("draft_protocol") or DRAFT_PROTOCOL,
            "key_present": key_path is not None,
            "key_location": key_ref if key_path is not None else None,
            "expected_sha256": config_payload.get("expected_sha256"),
            "runtime": self._runtime(configured),
        }

    def kill(self, config_payload: Mapping[str, Any], central: str) -> dict[str, Any]:
        """Engage the local fail-closed stop and signal a live resident once."""
        current = self.view(config_payload, central)
        self.kill_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.kill_path.parent, 0o700)
        marker = json.dumps(
            {
                "schema_version": 1,
                "engaged": True,
                "at": self.now().astimezone(timezone.utc).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor = os.open(
            self.kill_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(marker)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        configured = bool(current["runtime"]["configured"])
        # Re-read and re-verify immediately before signalling. This prevents a
        # stale projection or a PID reused after the first page render from
        # becoming a signal target.
        runtime = self._runtime(configured)
        signal_sent = False
        if runtime["running"] and isinstance(runtime["pid"], int):
            try:
                self.signaler(runtime["pid"], signal.SIGTERM)
                signal_sent = True
            except ProcessLookupError:
                pass
        return {
            "schema_version": 1,
            "central": central,
            "kill_switch_engaged": True,
            "signal_sent": signal_sent,
            "runtime": self._runtime(bool(runtime["configured"])),
        }

    def _write_key(self, central: str, value: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        name = re.sub(r"[^A-Za-z0-9._-]", "-", central)[:80] or "default"
        path = self.root / f"{name}-{uuid.uuid4().hex}.key"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def save(
        self,
        config_payload: Mapping[str, Any],
        request: Any,
        central: str,
        save_config: Callable[[dict[str, Any], str | None], Mapping[str, Any]],
    ) -> dict[str, Any]:
        clean = validate_request(request)
        if clean["expected_sha256"] != config_payload.get("expected_sha256"):
            raise ButlerSettingsError("configuration changed; reload before saving")
        current = self.view(config_payload, central)
        config = config_payload.get("config")
        if not isinstance(config, Mapping):
            raise ButlerSettingsError("coordinator config is unavailable")
        stored_key = self._read_key(current.get("key_location"))
        for credential in (clean["api_key"], stored_key):
            reject_readable_credential(clean, credential)
        same_endpoint = clean["endpoint"] == current.get("endpoint")
        api_key = clean["api_key"] or (stored_key if same_endpoint else "")
        validation = validate_provider(clean, api_key, opener=self.opener)
        if validation.outcome != "reachable":
            return {
                **current,
                "saved": False,
                "validation": validation.as_dict(),
            }
        with self._lock:
            new_key_path: Path | None = None
            old_key_path = self._managed_reference(current.get("key_location"))
            if clean["api_key"]:
                new_key_path = self._write_key(central, clean["api_key"])
                key_ref = self._reference(new_key_path)
            elif same_endpoint:
                key_ref = current.get("key_location") if old_key_path is not None else None
            else:
                key_ref = None
            updated = copy.deepcopy(dict(config))
            updated.pop("updated_at", None)
            updated.pop("updated_by", None)
            butler = updated.setdefault(
                "board_butler",
                {"schema_version": 1, "global": {}, "projects": {}, "boards": {}},
            )
            if not isinstance(butler, dict):
                if new_key_path is not None:
                    new_key_path.unlink(missing_ok=True)
                raise ButlerSettingsError("board_butler config is malformed")
            butler.setdefault("schema_version", 1)
            butler.setdefault("projects", {})
            butler.setdefault("boards", {})
            global_settings = butler.setdefault("global", {})
            if not isinstance(global_settings, dict):
                if new_key_path is not None:
                    new_key_path.unlink(missing_ok=True)
                raise ButlerSettingsError("board_butler.global is malformed")
            provider = {
                "model": clean["model"],
                "endpoint_ref": clean["endpoint"],
                "key_ref": key_ref,
                "extra_headers": clean["extra_headers"],
                "key_header": clean["key_header"],
                "key_prefix": clean["key_prefix"],
                "validation_path": clean["validation_path"],
                "draft_path": clean["draft_path"],
                "draft_protocol": clean["draft_protocol"],
            }
            global_settings["classification"] = copy.deepcopy(provider)
            global_settings["drafting"] = copy.deepcopy(provider)
            try:
                saved = save_config(updated, clean["expected_sha256"])
            except BaseException:
                if new_key_path is not None:
                    new_key_path.unlink(missing_ok=True)
                raise
            if old_key_path is not None and key_ref != current.get("key_location"):
                old_key_path.unlink(missing_ok=True)
            timestamp = self.now()
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return {
                "schema_version": 1,
                "central": central,
                "mode": "off" if global_settings.get("kill_switch", True) else "shadow",
                "endpoint": clean["endpoint"],
                "model": clean["model"],
                "extra_headers": clean["extra_headers"],
                "key_header": clean["key_header"],
                "key_prefix": clean["key_prefix"],
                "validation_path": clean["validation_path"],
                "draft_path": clean["draft_path"],
                "draft_protocol": clean["draft_protocol"],
                "key_present": key_ref is not None,
                "key_location": key_ref,
                "expected_sha256": saved.get("expected_sha256"),
                "saved": True,
                "reload": "next_cycle",
                "validated_at": timestamp.astimezone(timezone.utc).isoformat(),
                "validation": validation.as_dict(),
                "runtime": self._runtime(True),
            }
