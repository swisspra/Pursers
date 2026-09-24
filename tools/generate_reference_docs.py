#!/usr/bin/env python3
"""Generate the public MCP, CLI, and environment reference pages from source."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = {
    "mcp-tools.md": Path("docs/reference/mcp-tools.md"),
    "cli.md": Path("docs/reference/cli.md"),
    "environment.md": Path("docs/reference/environment.md"),
}

TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Onboarding & membership", (
        "board_join", "board_onboard", "board_snapshot", "board_invite",
        "board_member_add", "board_member_remove", "board_member_set_role",
        "board_members", "agent_retire", "agent_retire_inert",
        "agent_capabilities_set", "agent_readiness_set",
    )),
    ("Tickets", (
        "ticket_get", "ticket_create", "ticket_update", "ticket_annotate",
        "ticket_assign", "ticket_claim", "ticket_unclaim", "lease_renew",
        "board_reap", "ticket_submit", "ticket_cancel", "dispatch_my_offers",
        "ticket_list",
    )),
    ("Review", (
        "ticket_review_claim", "ticket_review_release", "ticket_review",
    )),
    ("Questions & human input", (
        "ticket_request_human", "ticket_human_resolve", "ticket_question_ask",
        "board_question_inbox", "ticket_question_answer",
    )),
    ("Memory", (
        "memory_write", "memory_read", "memory_unpin", "memory_search",
        "memory_links", "memory_checkpoint", "memory_handoff",
    )),
    ("State", ("board_status", "board_state_update", "board_state_get")),
    ("Butler control", (
        "butler_config_get", "butler_config_set", "butler_command_submit",
        "butler_command_inspect", "butler_command_wait",
        "butler_command_acknowledge", "butler_command_cancel",
        "butler_command_result",
    )),
    ("Events & wait", ("board_dispatch_events", "board_catchup")),
    ("Board admin & policy", (
        "board_scrub_profile_set", "board_response_view_set",
        "board_review_policy_set", "board_stale_after_set",
        "board_journal_retention_set", "board_dispatch_policy_set",
        "board_claim_ttl_set",
    )),
    ("Retention & archive", ("journal_compact", "board_archive_run")),
    ("Registry", ("board_list",)),
)

TOOL_SCOPES: dict[str, str] = {
    "board_join": "`board:read`; a valid invite is also required for a new principal",
    "board_onboard": "`board:read`; a valid invite is also required for a new principal",
    "board_snapshot": "`board:read`",
    "board_invite": "`board:write` and board-admin membership",
    "board_member_add": "`board:write` and board-admin membership",
    "board_member_remove": "`board:write` and board-admin membership",
    "board_member_set_role": "`board:write` and board-admin membership",
    "board_members": "`board:read`",
    "agent_retire": "`board:read`; retiring another seat also needs admin or `board:coordinate`",
    "agent_retire_inert": "`board:coordinate`",
    "agent_capabilities_set": "`board:write` for the authenticated seat",
    "agent_readiness_set": (
        "`board:write` for the authenticated worker seat, or `board:review` "
        "for the authenticated reviewer seat"
    ),
    "ticket_get": "`board:read` and visibility of the ticket",
    "ticket_create": "`board:write`, or restricted `board:intake`",
    "ticket_update": "`board:write` or `board:coordinate`; creator/admin checks also apply",
    "ticket_annotate": "`board:write`, `board:review`, or `board:coordinate`; role limits apply",
    "ticket_assign": "`board:coordinate` (deprecated compatibility tool)",
    "ticket_claim": "`board:write` and a live offer or assignment for this seat",
    "ticket_unclaim": "`board:write` and the current work lease",
    "lease_renew": "`board:write` for work or `board:review` for review, with the current lease",
    "board_reap": "`board:write`",
    "ticket_submit": "`board:write` and the current work lease",
    "ticket_cancel": "`board:write`; creator/executor/reviewer checks apply",
    "dispatch_my_offers": "`board:read` for the caller-owned seat",
    "ticket_list": "`board:read`; server-side visibility filters still apply",
    "ticket_review_claim": "`board:review` and a live review offer or assignment",
    "ticket_review_release": "`board:review` and the current review lease",
    "ticket_review": "`board:review` and the current review lease",
    "ticket_request_human": "`board:write` or `board:coordinate`; ticket authority checks apply",
    "ticket_human_resolve": "`board:write` as admin, or `board:coordinate`",
    "ticket_question_ask": "`board:write` with ticket dispatch context",
    "board_question_inbox": "`board:coordinate` and project-coordinator binding",
    "ticket_question_answer": "`board:coordinate` and project-coordinator binding",
    "memory_write": "`board:write`",
    "memory_read": "`board:read` and memory visibility",
    "memory_unpin": "`board:write` plus author/admin/coordinator authority",
    "memory_search": "`board:read` and memory visibility",
    "memory_links": "`board:read` and memory visibility",
    "memory_checkpoint": "`board:write`",
    "memory_handoff": "`board:write` for the exact seat identity",
    "board_status": "`board:read`",
    "board_state_update": "`board:write`, or restricted `board:coordinate`/`board:intake`",
    "board_state_get": "`board:read`, or restricted `board:intake`",
    "butler_config_get": "`board:read`",
    "butler_config_set": "`board:write` or `board:coordinate`; human initialization and per-field authority checks apply",
    "butler_command_submit": "`board:write` or `board:coordinate`; sender authority and command policy checks apply",
    "butler_command_inspect": "`board:read`; non-admin callers see only commands from their principal",
    "butler_command_wait": "`board:read`; command visibility checks apply",
    "butler_command_acknowledge": "`board:write` or `board:coordinate`; board-owned non-working coordinator only",
    "butler_command_cancel": "`board:write` or `board:coordinate`; A2A cannot cancel human commands",
    "butler_command_result": "`board:write` or `board:coordinate`; board-owned non-working coordinator only",
    "board_dispatch_events": "`board:read`",
    "board_catchup": "`board:read`; `board:write` enables touch/lease renewal side effects",
    "board_scrub_profile_set": "`board:write` and board-admin membership",
    "board_response_view_set": "`board:write` and board-admin membership",
    "board_review_policy_set": "`board:write` and board-admin membership",
    "board_stale_after_set": "`board:coordinate`",
    "board_journal_retention_set": "`board:coordinate`",
    "board_dispatch_policy_set": "`board:write` as admin, or `board:coordinate`",
    "board_claim_ttl_set": "`board:write` as admin, or `board:coordinate`",
    "journal_compact": "`board:write` and board-admin membership",
    "board_archive_run": "`board:write` and admin membership, or `board:coordinate`",
    "board_list": "`board:read`",
}

SPECIAL_RESPONSE_FIELDS = {
    "board_join": (
        "ok, board_id, agent_id, agent_name, principal_id, role, membership_role, "
        "lifecycle_status, capabilities, generation_token, generation_revision, "
        "claim_ttl_s, rejoined, release_events"
    ),
    "board_snapshot": (
        "ok, board, agents, tickets, memories, latest_seq, total_counts, "
        "returned_counts, omitted_counts, truncated"
    ),
    "board_catchup": (
        "ok, board_id, bounds, events, next_cursor, latest_cursor, has_more, "
        "resync_required, compacted_through, reset_cursor, new_seq, scan_count, "
        "visible_count, acknowledged_cursor, touched, release_events, "
        "total_counts, returned_counts, omitted_counts, truncated"
    ),
    "journal_compact": (
        "ok, board_id, compacted_through, removed, retained, latest_cursor, "
        "durable_records_untouched"
    ),
    "board_archive_run": (
        "ok, board_id, archived_ticket_count, bounded_history_count, "
        "tombstoned_member_count, pruned_invite_count, journal_compaction, "
        "durable_records_untouched"
    ),
}

COMPACT_RESPONSE_FIELDS = {
    "ticket_update": "ok, ticket_id, status, parked, generation, dispatch_state, revoked_offer, at",
    "ticket_annotate": "ok, ticket_id, status, parked, generation, dispatch_state, revoked_offer, at, annotation_id",
    "ticket_claim": "ok, ticket_id, status, parked, generation, dispatch_state, revoked_offer, at",
    "ticket_unclaim": "ok, ticket_id, status, parked, generation, dispatch_state, revoked_offer, at",
    "lease_renew": "ok, ticket_id, lease_expires_at, at",
    "memory_write": "ok, memory_id, scope, generation, at",
    "memory_checkpoint": "ok, memory_id, scope, generation, at",
}

CLI_SPECS: tuple[tuple[str, str, str, tuple[tuple[str, ...], ...]], ...] = (
    ("pursers-central", "packages/central/src:packages/client/src", "pursers_central.pursers_central_runtime", (
        (), ("init",), ("run",), ("rotate-key",), ("retire-key",),
    )),
    ("pursers-personal", "packages/personal/src:packages/client/src:packages/central/src", "pursers_personal.cli", (
        (), ("setup",), ("profiles",), ("profiles", "list"), ("profiles", "prune"),
        ("doctor",), ("central",), ("mcp",), ("rotate",), ("restart",),
        ("rollback",), ("uninstall",),
    )),
    ("pursers-personal-import", "packages/import", "personal_import", (
        (), ("import",), ("retry",), ("rollback",), ("review",), ("decide",),
        ("status",), ("archive-backfill",),
    )),
    ("pursers-wait-bridge", "tools/wait-bridge:packages/client/src", "pursers_wait_server", (
        (), ("join",), ("status",), ("forget",), ("ticket-lifecycle",),
        ("seat-lifecycle",), ("team-lifecycle",),
    )),
    ("pursers-door", "tools/wait-bridge:packages/client/src", "door_admin", (
        (), ("issue",), ("rotate",), ("list",), ("revoke-kid",), ("decode",),
    )),
    ("pursers-acp", "tools/acp-agent/src:packages/client/src:packages/central/src", "pursers_acp.agent", ((),)),
    ("Fleet dashboard launcher", "tools/fleet-dashboard:packages/client/src", "fleet_dashboard", ((),)),
    ("Board Butler", "tools/board-butler:packages/client/src", "board_butler", ((),)),
)

CLI_ENTRY_POINTS = {
    "pursers-central": ("packages/central/pyproject.toml", "pursers_central.pursers_central_runtime:main"),
    "pursers-personal": ("packages/personal/pyproject.toml", "pursers_personal.cli:main"),
    "pursers-personal-import": ("packages/import/pyproject.toml", "pursers_personal_import.personal_import:main"),
    "pursers-wait-bridge": ("tools/wait-bridge/pyproject.toml", "pursers_wait_server:main"),
    "pursers-door": ("tools/wait-bridge/pyproject.toml", "door_admin:main"),
    "pursers-acp": ("tools/acp-agent/pyproject.toml", "pursers_acp.agent:main"),
}

ENV_COMPONENT_PATHS = {
    "Central": (Path("packages/central/src/pursers_central"),),
    "Client": (Path("packages/client/src/pursers_client"),),
    "Wait bridge": (Path("tools/wait-bridge"),),
    "Personal": (Path("packages/personal/src/pursers_personal"),),
    "ACP": (Path("tools/acp-agent/src/pursers_acp"),),
}

# Dynamic-name reads are listed explicitly because the source loops over these names.
ENV_DYNAMIC_READS = {
    "Client": {"ONBOARD_CENTRAL_TOKEN", "ONBOARD_CENTRAL_URL", "ONBOARD_BOARD_ID", "ONBOARD_AGENT_NAME"},
    "Wait bridge": {"PURSERS_CAN_REVIEW", "PURSERS_CAN_WORK", "PURSERS_MODEL", "PURSERS_PROVIDER"},
    "ACP": {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "SYSTEMROOT", "TMPDIR", "USER", "PURSERS_MODEL", "PURSERS_PROVIDER"},
}

ENV_DEFAULTS: dict[str, str] = {
    "CENTRAL_ADMISSION": "`invite`", "CENTRAL_AUTH_MODE": "`jwt`",
    "CENTRAL_JWKS_PATH": "none; required", "CENTRAL_JWT_AUDIENCE": "Central MCP URL",
    "CENTRAL_JWT_CLOCK_SKEW": "`30` seconds", "CENTRAL_JWT_ISSUER": "none; required",
    "CENTRAL_PRINCIPAL_STREAM_CAP": "`32`", "CENTRAL_REAPER_INTERVAL_S": "`30.0` seconds",
    "CENTRAL_REQUEST_STATE_KEY_FILE": "Central data directory/request-state.keys",
    "CENTRAL_ALLOWED_HOSTS": "unset", "ONBOARD_CENTRAL_ALLOWED_HOSTS": "unset",
    "ONBOARD_CENTRAL_DATA_DIR": "unset; required unless supplied by CLI",
    "ONBOARD_CENTRAL_HOST": "`127.0.0.1`", "ONBOARD_CENTRAL_LOG_LEVEL": "`info`",
    "ONBOARD_CENTRAL_PORT": "`8766`", "ONBOARD_CENTRAL_SSL_CERTFILE": "unset",
    "ONBOARD_CENTRAL_SSL_KEYFILE": "unset", "ONBOARD_CENTRAL_TLS_CERTFILE": "unset",
    "ONBOARD_CENTRAL_TLS_KEYFILE": "unset", "PURSERS_LEGACY_TOOLS": "`0` / unset",
    "STORE_BACKEND": "`sqlite`", "ONBOARD_PERSONAL_PROFILE": "unset",
    "ONBOARD_AGENT_INSTANCE": "unset", "ONBOARD_AGENT_NAME": "`pursers-wait-bridge`",
    "ONBOARD_BOARD_ID": "`pursers`", "ONBOARD_CENTRAL_TOKEN": "unset",
    "ONBOARD_CENTRAL_TOKEN_FILE": "unset", "ONBOARD_CENTRAL_URL": "`http://127.0.0.1:8766/mcp`",
    "ONBOARD_TOKEN_FILE": "unset", "PROJECT_REGISTRY_FILE": "unset",
    "PURSERS_BACKLOG_RESURFACE_INTERVAL_S": "`600` seconds",
    "PURSERS_BACKLOG_SCAN_INTERVAL_S": "`30` seconds", "PURSERS_BOARDS": "unset",
    "PURSERS_BOARD_CONNECTOR_TOKEN": "unset", "PURSERS_BOARD_CONNECTOR_TOKEN_SHA256": "unset",
    "PURSERS_BRIDGE_STATE": "derived from state directory", "PURSERS_BRIDGE_STATE_DIR": "platform state directory",
    "PURSERS_BRIDGE_STATS": "state directory/bridge-stats.json", "PURSERS_CENTRAL_CONNECTION_CAP": "`4`",
    "PURSERS_DOCTOR_TOKEN_PATH": "unset", "PURSERS_HOST": "`codex`",
    "PURSERS_HOST_TIMEOUT_S": "named-host timeout", "PURSERS_KEEPALIVE_IDLE_LIMIT_S": "three claim-TTL periods",
    "PURSERS_REQUEST_STATE_KEY_FILE": "state directory/request-state.keys",
    "PURSERS_REQUIRE_TOKEN_MATCH": "unset", "PURSERS_ROLE": "door role or unset",
    "PURSERS_SKILLS": "empty list", "PURSERS_STALE_SECONDS": "`300` seconds",
    "PURSERS_TIER_MAX": "unset", "PURSERS_WAIT_MODE": "`push`",
    "PURSERS_WAIT_RECONCILE_INTERVAL_S": "`30` seconds",
    "PURSERS_CAN_REVIEW": "unset", "PURSERS_CAN_WORK": "unset",
    "PURSERS_MODEL": "unset", "PURSERS_PROVIDER": "unset",
    "PATH": "operating-system default", "PURSERS_WAIT_BRIDGE_COMMAND": "`pursers-wait-bridge`",
    "HOME": "inherited", "LANG": "inherited", "LC_ALL": "inherited",
    "LC_CTYPE": "inherited", "SHELL": "inherited", "SYSTEMROOT": "inherited",
    "TMPDIR": "inherited", "USER": "inherited",
}

ENV_MEANINGS: dict[str, str] = {
    "CENTRAL_ADMISSION": "Admission policy. The shipped runtime requires invite-only admission.",
    "CENTRAL_AUTH_MODE": "Authentication mode. The shipped runtime requires JWT.",
    "CENTRAL_JWKS_PATH": "Public JWKS file used to verify seat credentials.",
    "CENTRAL_JWT_AUDIENCE": "Expected JWT audience and resource URL.",
    "CENTRAL_JWT_CLOCK_SKEW": "Clock-skew allowance when validating JWT timestamps.",
    "CENTRAL_JWT_ISSUER": "Exact issuer URL accepted in JWTs.",
    "CENTRAL_PRINCIPAL_STREAM_CAP": "Maximum concurrent event streams for one principal.",
    "CENTRAL_REAPER_INTERVAL_S": "Interval between automatic expired-lease sweeps.",
    "CENTRAL_REQUEST_STATE_KEY_FILE": "Restart-safe MCP request-state keyring path.",
    "CENTRAL_ALLOWED_HOSTS": "Legacy alias for extra accepted bare HTTP Host names.",
    "ONBOARD_CENTRAL_ALLOWED_HOSTS": "Comma-separated extra accepted bare HTTP Host names.",
    "ONBOARD_CENTRAL_DATA_DIR": "Private Central SQLite data directory.",
    "ONBOARD_CENTRAL_HOST": "Loopback address used by the packaged Central runtime.",
    "ONBOARD_CENTRAL_LOG_LEVEL": "Uvicorn log level.",
    "ONBOARD_CENTRAL_PORT": "Loopback port used by Central.",
    "ONBOARD_CENTRAL_SSL_CERTFILE": "Legacy alias for the TLS certificate path.",
    "ONBOARD_CENTRAL_SSL_KEYFILE": "Legacy alias for the TLS private-key path.",
    "ONBOARD_CENTRAL_TLS_CERTFILE": "TLS certificate path; requires the matching key setting.",
    "ONBOARD_CENTRAL_TLS_KEYFILE": "TLS private-key path; keep this file private.",
    "PURSERS_LEGACY_TOOLS": "Set to `1` to expose deprecated compatibility tools.",
    "STORE_BACKEND": "Central storage backend; only SQLite is supported.",
    "ONBOARD_PERSONAL_PROFILE": "Explicit Personal profile JSON selected ahead of project discovery.",
    "ONBOARD_AGENT_INSTANCE": "Optional suffix used to distinguish repeated instances of one seat name.",
    "ONBOARD_AGENT_NAME": "Seat identity advertised to Central.",
    "ONBOARD_BOARD_ID": "Home board used when a command does not receive an explicit board.",
    "ONBOARD_CENTRAL_TOKEN": "Bearer credential value. Prefer the file setting for persistent configuration.",
    "ONBOARD_CENTRAL_TOKEN_FILE": "Private file containing the Central bearer credential.",
    "ONBOARD_CENTRAL_URL": "Central MCP endpoint.",
    "ONBOARD_TOKEN_FILE": "Compatibility token-file setting used by seat and registry administration commands.",
    "PROJECT_REGISTRY_FILE": "Project-registry JSON file used by the registry seeder.",
    "PURSERS_BACKLOG_RESURFACE_INTERVAL_S": "Minimum interval before an unchanged backlog cue can resurface.",
    "PURSERS_BACKLOG_SCAN_INTERVAL_S": "Interval between claimable-backlog reconciliation scans while a push wait remains open.",
    "PURSERS_BOARDS": "Comma-separated board allowlist for a multi-board wait bridge.",
    "PURSERS_BOARD_CONNECTOR_TOKEN": "Connector token used only by the optional token-match guard.",
    "PURSERS_BOARD_CONNECTOR_TOKEN_SHA256": "Expected connector-token fingerprint for the token-match guard.",
    "PURSERS_BRIDGE_STATE": "Exact wait-bridge cursor state file.",
    "PURSERS_BRIDGE_STATE_DIR": "Directory for cursor, request-state, and bridge runtime files.",
    "PURSERS_BRIDGE_STATS": "JSON statistics file written by the wait bridge.",
    "PURSERS_CENTRAL_CONNECTION_CAP": "Maximum pooled Central connections used by the bridge.",
    "PURSERS_DOCTOR_TOKEN_PATH": "Credential file inspected by registry doctor.",
    "PURSERS_HOST": "Named host profile used for wait timeouts and capability provenance.",
    "PURSERS_HOST_TIMEOUT_S": "Explicit host-response timeout override in seconds.",
    "PURSERS_KEEPALIVE_IDLE_LIMIT_S": "Maximum idle duration for automatic lease keepalive.",
    "PURSERS_REQUEST_STATE_KEY_FILE": "Restart-safe MCP request-state keyring for the bridge.",
    "PURSERS_REQUIRE_TOKEN_MATCH": "Set to `1` to require connector/bridge credential matching.",
    "PURSERS_ROLE": "Seat role advertised during onboarding.",
    "PURSERS_SKILLS": "Comma-separated dispatch skills advertised by the seat.",
    "PURSERS_STALE_SECONDS": "Inactive-seat threshold used by seat administration.",
    "PURSERS_TIER_MAX": "Maximum dispatch tier accepted by the seat.",
    "PURSERS_WAIT_MODE": "Wait transport preference (`push` or compatibility mode).",
    "PURSERS_WAIT_RECONCILE_INTERVAL_S": "Interval between current-state claimability scans while a Client wait remains open.",
    "PURSERS_CAN_REVIEW": "Boolean review capability advertised by the seat.",
    "PURSERS_CAN_WORK": "Boolean work capability advertised by the seat.",
    "PURSERS_MODEL": "Model name recorded with seat capabilities and usage.",
    "PURSERS_PROVIDER": "Model provider recorded with seat capabilities and usage.",
    "PATH": "Executable search path passed to local ACP child processes.",
    "PURSERS_WAIT_BRIDGE_COMMAND": "Wait-bridge executable started by ACP.",
    "HOME": "Home directory passed to Personal setup subprocesses.",
    "LANG": "Locale passed to Personal setup subprocesses.",
    "LC_ALL": "Locale override passed to Personal setup subprocesses.",
    "LC_CTYPE": "Character-type locale passed to Personal setup subprocesses.",
    "SHELL": "User shell passed to Personal setup subprocesses.",
    "SYSTEMROOT": "Windows system root passed to Personal setup subprocesses.",
    "TMPDIR": "Temporary-directory root passed to Personal setup subprocesses.",
    "USER": "User name passed to Personal setup subprocesses.",
}

# The Client reads these names only to report ignored legacy overrides while a
# profile supplies its identity. Other components still use the variables as
# runtime configuration, so their rows must not share the Client wording.
ENV_COMPONENT_OVERRIDES: dict[tuple[str, str], tuple[str, str]] = {
    ("Client", "ONBOARD_AGENT_NAME"): (
        "ignored",
        "Legacy override detection only; profile-backed Client identity wins and doctor reports the variable as ignored.",
    ),
    ("Client", "ONBOARD_BOARD_ID"): (
        "ignored",
        "Legacy override detection only; the profile-backed board wins and doctor reports the variable as ignored.",
    ),
    ("Client", "ONBOARD_CENTRAL_TOKEN"): (
        "ignored",
        "Legacy override detection only; the profile-backed credential wins and doctor reports the variable as ignored.",
    ),
    ("Client", "ONBOARD_CENTRAL_URL"): (
        "ignored",
        "Legacy override detection only; the profile-backed Central endpoint wins and doctor reports the variable as ignored.",
    ),
}


def _tool_names() -> set[str]:
    return {name for _heading, names in TOOL_GROUPS for name in names}


def _load_central_tools() -> list[Any]:
    sys.path[:0] = [str(ROOT / "packages/central/src"), str(ROOT / "packages/client/src")]
    from pursers_central.central import build_server

    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir()))
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pursers-reference-", dir=temp_parent) as raw:
        directory = Path(raw)
        jwks = directory / "jwks.json"
        jwks.write_text('{"keys": []}\n', encoding="utf-8")
        environment = {
            "CENTRAL_AUTH_MODE": "jwt",
            "CENTRAL_JWT_ISSUER": "https://issuer.example",
            "CENTRAL_JWT_AUDIENCE": "http://127.0.0.1:19443/mcp",
            "CENTRAL_JWKS_PATH": str(jwks),
            "CENTRAL_ADMISSION": "invite",
            "STORE_BACKEND": "sqlite",
        }
        with patch.dict(os.environ, environment, clear=False):
            server, _service = build_server("127.0.0.1", 19443, directory / "data")
            return asyncio.run(server.list_tools(include_legacy=True))


def _type_name(schema: Mapping[str, Any]) -> str:
    if "anyOf" in schema:
        return " | ".join(dict.fromkeys(_type_name(item) for item in schema["anyOf"]))
    kind = schema.get("type", "any")
    if kind == "array":
        return f"array[{_type_name(schema.get('items', {}))}]"
    if kind == "object" and isinstance(schema.get("additionalProperties"), Mapping):
        return f"object[string, {_type_name(schema['additionalProperties'])}]"
    return str(kind)


def _markdown_default(schema: Mapping[str, Any], required: bool) -> str:
    if "default" in schema:
        return f"`{json.dumps(schema['default'], ensure_ascii=False, sort_keys=True)}`"
    return "—" if required else "not set"


def _direct_response_fields() -> dict[str, str]:
    source = ROOT / "packages/central/src/pursers_central/central.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    fields: dict[str, str] = {}

    class ReturnVisitor(ast.NodeVisitor):
        def __init__(self, root: ast.AsyncFunctionDef) -> None:
            self.root = root
            self.keys: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is self.root:
                self.generic_visit(node)

        def visit_Return(self, node: ast.Return) -> None:
            if isinstance(node.value, ast.Dict):
                self.keys.extend(
                    key.value
                    for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        decorated = any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "tool"
            for item in node.decorator_list
        )
        if not decorated:
            continue
        visitor = ReturnVisitor(node)
        visitor.visit(node)
        if visitor.keys:
            fields[node.name] = ", ".join(dict.fromkeys(visitor.keys))
    return fields


def _direct_required_scopes() -> dict[str, tuple[str, ...]]:
    """Return unconditional literal ``require_scope`` calls in tool bodies."""
    source = ROOT / "packages/central/src/pursers_central/central.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    scopes: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        decorated = any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "tool"
            for item in node.decorator_list
        )
        if not decorated:
            continue
        direct: list[str] = []
        for statement in node.body:
            if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
                continue
            call = statement.value
            if (
                isinstance(call.func, ast.Name)
                and call.func.id == "require_scope"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and isinstance(call.args[1].value, str)
            ):
                direct.append(call.args[1].value)
        if direct:
            scopes[node.name] = tuple(dict.fromkeys(direct))
    return scopes


def render_mcp_tools() -> str:
    tools = _load_central_tools()
    by_name = {tool.name: tool for tool in tools}
    expected = _tool_names()
    if set(by_name) != expected:
        raise ValueError(
            "Central tool registry changed; update grouping/scope metadata: "
            f"missing={sorted(set(by_name) - expected)}, stale={sorted(expected - set(by_name))}"
        )
    if set(TOOL_SCOPES) != expected:
        raise ValueError("scope metadata does not exactly cover the Central registry")
    for name, scopes in _direct_required_scopes().items():
        if name not in TOOL_SCOPES:
            continue
        missing = [scope for scope in scopes if f"`{scope}`" not in TOOL_SCOPES[name]]
        if missing:
            raise ValueError(
                f"scope metadata for {name} omits unconditional authorization: {missing}"
            )
    source_fields = _direct_response_fields()
    lines = [
        "# Central MCP tools",
        "",
        "Generated by `python3 tools/generate_reference_docs.py`. Do not edit this page by hand.",
        "",
        f"Central currently registers **{len(tools)} tools**. `ticket_assign` is deprecated and hidden unless `PURSERS_LEGACY_TOOLS=1`; the default `tools/list` therefore returns {len(tools) - 1} tools.",
        "",
        "All calls require a verified bearer credential and board authorization. Scope notes below list additional tool-level checks. Role, ticket ownership, assignment, lease, and visibility checks can narrow access further.",
        "",
    ]
    for heading, names in TOOL_GROUPS:
        lines.extend([f"## {heading}", ""])
        for name in names:
            tool = by_name[name]
            description = " ".join((tool.description or "No description.").split())
            if name == "ticket_assign":
                description = f"**Deprecated; hidden by default.** {description}"
            lines.extend([f"### `{name}`", "", description, "", f"Required authorization: {TOOL_SCOPES[name]}.", ""])
            schema = tool.input_schema
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            lines.extend([
                "| Argument | Type | Presence | Default |",
                "|---|---|---|---|",
            ])
            for argument, definition in properties.items():
                is_required = argument in required
                lines.append(
                    f"| `{argument}` | `{_type_name(definition)}` | "
                    f"{'required' if is_required else 'optional'} | "
                    f"{_markdown_default(definition, is_required)} |"
                )
            if not properties:
                lines.append("| — | — | — | — |")
            response = COMPACT_RESPONSE_FIELDS.get(name)
            if response is not None:
                lines.extend(["", f"Compact response fields: `{response}`."])
            else:
                response = SPECIAL_RESPONSE_FIELDS.get(name, source_fields.get(name, "JSON object"))
                lines.extend(["", f"Response fields: `{response}`."])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _check_entry_points() -> None:
    for command, (relative, expected) in CLI_ENTRY_POINTS.items():
        data = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
        actual = data.get("project", {}).get("scripts", {}).get(command)
        if actual != expected:
            raise ValueError(f"{command} entry point changed: expected {expected!r}, found {actual!r}")


def _capture_help(program: str, pythonpath: str, module: str, args: tuple[str, ...]) -> str:
    environment = os.environ.copy()
    paths = [str(ROOT / item) for item in pythonpath.split(":")]
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(paths + ([inherited] if inherited else []))
    environment["COLUMNS"] = "120"
    environment["PYTHONHASHSEED"] = "0"
    runner = (
        "import importlib,sys; "
        f"sys.argv={[program, *args, '--help']!r}; "
        f"importlib.import_module({module!r}).main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", runner], cwd=ROOT, env=environment,
        text=True, capture_output=True, check=False, timeout=30,
    )
    output = result.stdout
    if result.returncode != 0 or not output.lstrip().startswith("usage:"):
        detail = (result.stderr or output).strip()
        raise ValueError(f"could not capture {' '.join((program, *args))} --help: {detail}")
    return output.rstrip()


def render_cli() -> str:
    _check_entry_points()
    lines = [
        "# Command-line reference",
        "",
        "Generated by `python3 tools/generate_reference_docs.py`. The blocks below are literal `--help` output captured from this source tree.",
        "",
        "The Fleet dashboard and Board Butler are repository launchers rather than installed console scripts. Their sections use `python3 tools/fleet-dashboard/fleet_dashboard.py` and `python3 tools/board-butler/board_butler.py` respectively.",
        "",
    ]
    for title, pythonpath, module, commands in CLI_SPECS:
        lines.extend([f"## {title}", ""])
        for command in commands:
            display_program = {
                "Fleet dashboard launcher": "python3 tools/fleet-dashboard/fleet_dashboard.py",
                "Board Butler": "python3 tools/board-butler/board_butler.py",
            }.get(title, title)
            invocation = " ".join((display_program, *command, "--help"))
            capture_program = display_program if title in {"Fleet dashboard launcher", "Board Butler"} else title
            help_text = _capture_help(capture_program, pythonpath, module, command)
            lines.extend([f"### `{invocation}`", "", "```text", help_text, "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _module_constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    constants: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        values: tuple[str, ...] = ()
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values = (value.value,)
        elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            values = tuple(
                item.value for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = values
    return constants


def _resolved_strings(node: ast.AST, constants: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Name):
        return constants.get(node.id, ())
    return ()


def _environment_reads(root: Path) -> set[str]:
    result: set[str] = set()
    for source in root.rglob("*.py"):
        if "tests" in source.parts:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            candidates: Iterable[str] = ()
            if isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Attribute) and function.attr in {"get", "getenv"} and node.args:
                    receiver = ast.unparse(function.value)
                    if receiver in {"os.environ", "os", "environment", "env", "selected"}:
                        candidates = _resolved_strings(node.args[0], constants)
                elif isinstance(function, ast.Name) and function.id == "_env" and node.args:
                    candidates = _resolved_strings(node.args[0], constants)
                elif isinstance(function, ast.Name) and function.id in {"_env_first", "_env_hosts"}:
                    candidates = (
                        value for argument in node.args
                        for value in _resolved_strings(argument, constants)
                    )
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Load)
                and ast.unparse(node.value) == "os.environ"
            ):
                candidates = _resolved_strings(node.slice, constants)
            for name in candidates:
                if name and name.upper() == name and any(character.isalpha() for character in name):
                    result.add(name)
    return result


def _environment_inventory() -> dict[str, set[str]]:
    inventory: dict[str, set[str]] = {}
    for component, roots in ENV_COMPONENT_PATHS.items():
        names: set[str] = set()
        for root in roots:
            names.update(_environment_reads(ROOT / root))
        names.update(ENV_DYNAMIC_READS.get(component, set()))
        inventory[component] = names
    # Personal selects profiles through pursers-client, so document that public input for both.
    inventory["Personal"].add("ONBOARD_PERSONAL_PROFILE")
    return inventory


def _server_json_environment() -> set[str]:
    document = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    packages = [item for item in document["packages"] if item.get("identifier") == "pursers-central"]
    if len(packages) != 1:
        raise ValueError("server.json must contain exactly one pursers-central package")
    return {item["name"] for item in packages[0]["environmentVariables"]}


def _environment_rows(
    inventory: Mapping[str, set[str]],
) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for name in sorted(set().union(*inventory.values())):
        for component, names in inventory.items():
            if name not in names:
                continue
            default, meaning = ENV_COMPONENT_OVERRIDES.get(
                (component, name), (ENV_DEFAULTS[name], ENV_MEANINGS[name])
            )
            rows.append((name, component, default, meaning))
    return rows


def render_environment() -> str:
    inventory = _environment_inventory()
    all_names = set().union(*inventory.values())
    missing = all_names - set(ENV_DEFAULTS) | all_names - set(ENV_MEANINGS)
    if missing:
        raise ValueError(f"environment metadata missing for: {sorted(missing)}")
    server_names = _server_json_environment()
    central_names = inventory["Central"]
    if not server_names <= central_names:
        raise ValueError(f"server.json Central environment not found in source inventory: {sorted(server_names - central_names)}")
    lines = [
        "# Environment variables",
        "",
        "Generated by `python3 tools/generate_reference_docs.py` from runtime environment reads, with Central's canonical public settings cross-checked against `server.json`.",
        "",
        "Unset means the component applies its documented default or requires an explicit CLI argument. Credential values are intentionally not shown; persistent setups should use private credential files.",
        "",
        "| Variable | Components | Default | Meaning |",
        "|---|---|---|---|",
    ]
    for name, component, default, meaning in _environment_rows(inventory):
        lines.append(f"| `{name}` | {component} | {default} | {meaning} |")
    lines.extend([
        "",
        "## Central registry cross-check",
        "",
        f"`server.json` declares {len(server_names)} canonical Central environment variables. All are present in the source-derived inventory above. Compatibility aliases read by Central are documented too, even when they are not advertised in registry metadata.",
        "",
    ])
    return "\n".join(lines)


# argparse changed its --help layout in Python 3.13, so cli.md is literal only
# for one interpreter. It is pinned to the version CI runs (.github/workflows/ci.yml).
CLI_REFERENCE_PYTHON = (3, 12)


def cli_reference_python_matches() -> bool:
    return sys.version_info[:2] == CLI_REFERENCE_PYTHON


def generated_documents() -> dict[Path, str]:
    documents = {OUTPUTS["mcp-tools.md"]: render_mcp_tools()}
    if cli_reference_python_matches():
        documents[OUTPUTS["cli.md"]] = render_cli()
    documents[OUTPUTS["environment.md"]] = render_environment()
    return documents


def write_documents(*, check: bool = False) -> list[Path]:
    changed: list[Path] = []
    for relative, content in generated_documents().items():
        destination = ROOT / relative
        current = destination.read_text(encoding="utf-8") if destination.exists() else None
        if current == content:
            continue
        changed.append(relative)
        if not check:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when committed pages differ")
    args = parser.parse_args(argv)
    changed = write_documents(check=args.check)
    if args.check and changed:
        print("reference documentation is stale: " + ", ".join(map(str, changed)))
        return 1
    if not cli_reference_python_matches():
        wanted = ".".join(map(str, CLI_REFERENCE_PYTHON))
        print(f"skipped {OUTPUTS['cli.md']}: regenerate it with python{wanted}")
    state = "current" if not changed else "generated"
    print(f"reference documentation {state}: {len(OUTPUTS)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
