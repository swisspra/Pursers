#!/usr/bin/env python3
"""Verifier-owned typed evidence recorders and closed evaluators.

The module is deliberately independent of the acceptance runner.  Install it
outside the candidate checkout, pin its digest in verifier-owned trust config,
then call :func:`record_evidence` and :func:`evaluate_evidence` or the matching
CLI subcommands.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SCHEMA_VERSION = 1
KINDS = frozenset({
    "http_response", "mcp_tool_response", "receipt_field", "log_assertion",
    "state_transition",
})
SURFACES = frozenset({"aionui", "fleet", "personal"})
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MAX_CONFIG_BYTES = 1_000_000
MAX_HTTP_BODY_BYTES = 65_536
MAX_LOG_BYTES = 262_144
MAX_SELECTED_VALUE_BYTES = 16_384
SENSITIVE_KEY = re.compile(r"(?:authorization|bearer|cookie|password|secret|token|private_key)", re.I)
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\Users\\\\)")
SECRET_VALUE = re.compile(
    r"(?:Bearer\s+\S+|prs1\.[A-Za-z0-9._-]{12,}|eyJ[A-Za-z0-9_-]+\."
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)",
    re.I,
)
PYTHON_EXECUTABLE_NAME = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.I)
CONTEXT_KEYS = {
    "observation_id", "run_id", "action_id", "entity", "surface",
    "board_id", "candidate_commit", "issued_at", "causal_index",
}
FLEET_TRACE_KEYS = frozenset({
    "schema_version", "emitter", "timestamp", "runtime_id", "pid",
    "candidate_commit", "entrypoint_sha256", "board_id", "surface",
    "observation_id", "run_id", "action_id", "entity", "method", "path",
    "status", "outcome", "effect", "changed", "before_sha256",
    "after_sha256", "result_sha256", "action_sha256",
})
FLEET_RESPONSE_POINTERS = frozenset(
    {f"/_evidence/{field}" for field in FLEET_TRACE_KEYS}
    | {"/_evidence/log_emitted"}
)
FLEET_PROJECT_STEP_POINTERS = frozenset(
    f"/steps/{index}/{field}"
    for index in (0, 2, 3, 4, 5)
    for field in ("step", "status")
)
FLEET_PROJECT_CREDENTIAL_ABSENCE_POINTERS = frozenset({"/doors"})
FLEET_PROJECT_STATE_DOMAINS = (
    "registry", "board", "credentials", "keys", "clone",
)
FLEET_PROJECT_PREFLIGHT_POINTERS = frozenset({
    "/error",
    "/_project_preflight/schema_version",
    "/_project_preflight/request/project",
    "/_project_preflight/request/board_id",
    "/_project_preflight/request/action_sha256",
    "/_project_preflight/result/status",
    "/_project_preflight/result/decision",
    "/_evidence/action_sha256",
    "/_evidence/entity",
    "/_evidence/board_id",
    "/_evidence/status",
    "/_evidence/outcome",
    "/_evidence/effect",
    "/_evidence/changed",
} | {
    f"/_project_preflight/domains/{domain}/{phase}_sha256"
    for domain in FLEET_PROJECT_STATE_DOMAINS
    for phase in ("before", "after")
})
FLEET_ACTION_RESULT_POINTERS = {
    "/api/attention": frozenset({"/items"}),
    "/api/projects/add": (
        FLEET_PROJECT_STEP_POINTERS | FLEET_PROJECT_CREDENTIAL_ABSENCE_POINTERS
    ),
}
FLEET_ACTION_RESPONSE_POINTERS = {
    action_path: FLEET_RESPONSE_POINTERS | result_pointers
    for action_path, result_pointers in FLEET_ACTION_RESULT_POINTERS.items()
}
LOG_COMMON_KEYS = frozenset({
    "adapter", "provenance", "runtime_id", "path",
    "timestamp_pointer", "max_age_seconds", "required_bindings", "emitter",
    "runtime_pointer", "max_bytes", "document_keys", "action_input_path",
    "action_input_sha256", "action_digest_pointer",
})
FLEET_LOG_SOURCE_KEYS = LOG_COMMON_KEYS | {
    "http_source_id", "http_source_config_sha256", "schema_version_pointer",
    "pid_pointer", "entrypoint_digest_pointer", "status_pointer",
    "changed_pointer", "outcome_pointer", "effect_pointer", "sha256_pointers",
    "action_path",
}
TRUST_KEYS = {
    "schema_version", "verifier_id", "trusted_module_path", "module_sha256",
    "candidate_checkout_root", "candidate_commit", "board_id", "max_age_seconds", "active_evidence_key",
    "evidence_keys", "http_sources", "mcp_sources", "receipt_sources", "log_sources",
    "state_sources", "replay_guard",
}
MCP_SOURCE_KEYS = {
    "adapter", "provenance", "runtime_id", "surface", "board_id",
    "candidate_commit", "command", "command_sha256", "args", "env", "cwd",
    "candidate_source", "candidate_source_sha256", "challenge_key", "tool",
    "arguments", "select_allowlist", "timeout_seconds",
}
MCP_ATTESTATION_KEYS = {
    "schema_version", "server_name", "version", "build", "candidate_commit",
    "candidate_source", "board_id", "pid", "transport", "nonce", "signature",
}
MCP_PUBLIC_ATTESTATION_KEYS = (
    MCP_ATTESTATION_KEYS - {"candidate_source"}
) | {"candidate_source_sha256"}
BROWSER_STATE_SOURCE_KEYS = {
    "adapter", "provenance", "runtime_id", "surface", "board_id",
    "candidate_commit", "command", "command_sha256", "config_path",
    "config_sha256", "base_url", "page_url", "recipe", "env",
    "timeout_seconds", "select_allowlist",
}
ASSISTANT_BINDING_SOURCE_KEYS = BROWSER_STATE_SOURCE_KEYS | {
    "candidate_manifest", "candidate_manifest_sha256", "installed_manifest",
}
BROWSER_PROPERTIES = frozenset({
    "text", "value", "checked", "disabled", "count", "class", "hidden",
})
BROWSER_ACTION_KEYS = {
    "observe": {"kind", "path"},
    "click": {"kind", "selector", "path"},
    "set_value": {"kind", "selector", "value", "path"},
    "select": {"kind", "selector", "value", "path"},
    "submit": {"kind", "selector", "path"},
    "press_key": {"kind", "selector", "key", "path"},
    "wait": {"kind", "milliseconds", "path"},
    "resource_delta": {"kind", "endpoint", "milliseconds", "path"},
    "fetch": {"kind", "method", "endpoint", "body", "path"},
    "fetch_json": {
        "kind", "method", "endpoint", "body", "pointer", "path",
    },
    "assistant_binding": {"kind", "endpoint", "assistant_id", "path"},
    "click_pending_state": {
        "kind", "selector", "method", "endpoint", "property",
        "hold_milliseconds", "path", "pending_path", "settled_path",
        "status_path", "response_sha256_path", "error_path",
    },
    "click_response_json": {
        "kind", "selector", "method", "endpoint", "pointer", "path",
    },
}


class TypedEvidenceError(ValueError):
    """Fail-closed typed evidence contract error."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, file_pointer: Any, code: int, message: str,
        headers: Any, new_url: str,
    ) -> None:
        return None


def _closed(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TypedEvidenceError(f"{label} fields do not match schema")
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _module_digest() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def _read_json(path: Path, label: str) -> Any:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise TypedEvidenceError(f"{label} is too large")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TypedEvidenceError(f"{label} is not readable JSON") from exc


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise TypedEvidenceError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TypedEvidenceError(f"{label} must be an RFC3339 timestamp") from None
    if parsed.tzinfo is None:
        raise TypedEvidenceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh(value: Any, max_age: int, label: str) -> None:
    age = (datetime.now(timezone.utc) - _timestamp(value, label)).total_seconds()
    if age < -30 or age > max_age:
        raise TypedEvidenceError(f"{label} is stale")


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise TypedEvidenceError(f"{label} is invalid")
    return value


def _safe_public(value: Any, label: str, *, allow_path: bool = False) -> Any:
    encoded = _json_bytes(value)
    if len(encoded) > MAX_SELECTED_VALUE_BYTES:
        raise TypedEvidenceError(f"{label} is too large")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE_KEY.search(key):
                raise TypedEvidenceError(f"{label} contains a sensitive key")
            _safe_public(item, label, allow_path=allow_path)
    elif isinstance(value, list):
        for item in value:
            _safe_public(item, label, allow_path=allow_path)
    elif isinstance(value, str):
        if SECRET_VALUE.search(value) or (not allow_path and PRIVATE_PATH.search(value)):
            raise TypedEvidenceError(f"{label} contains sensitive data")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise TypedEvidenceError(f"{label} contains an unsupported value")
    return value


def _pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 512:
        raise TypedEvidenceError("JSON pointer is invalid")
    current = document
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise TypedEvidenceError(f"JSON pointer is absent: {pointer}")
    return current


def _leaf_pointers(document: Any, prefix: str = "") -> set[str]:
    if isinstance(document, dict):
        result: set[str] = set()
        for key, value in document.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            result.update(_leaf_pointers(value, f"{prefix}/{escaped}"))
        return result
    if isinstance(document, list):
        result = set()
        for index, value in enumerate(document):
            result.update(_leaf_pointers(value, f"{prefix}/{index}"))
        return result
    return {prefix}


def _without_member(document: dict[str, Any], member: str) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != member}


def _same_json_type(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        )
    return type(left) is type(right)


def _context(request: dict[str, Any], trust: dict[str, Any]) -> dict[str, Any]:
    context = _closed(request.get("context"), CONTEXT_KEYS, "request context")
    for field in ("observation_id", "run_id", "action_id", "entity"):
        _safe_id(context[field], field)
    if context["surface"] not in SURFACES:
        raise TypedEvidenceError("surface is invalid")
    if context["board_id"] != trust["board_id"]:
        raise TypedEvidenceError("request board does not match verifier trust")
    if context["candidate_commit"] != trust["candidate_commit"]:
        raise TypedEvidenceError("request candidate does not match verifier trust")
    if not FULL_SHA.fullmatch(str(context["candidate_commit"])):
        raise TypedEvidenceError("candidate_commit must be a full SHA")
    if not isinstance(context["causal_index"], int) or isinstance(context["causal_index"], bool) or context["causal_index"] < 0:
        raise TypedEvidenceError("causal_index is invalid")
    _fresh(context["issued_at"], trust["max_age_seconds"], "request issued_at")
    return context


def _trust(trust: Any) -> dict[str, Any]:
    trust = _closed(trust, TRUST_KEYS, "trust config")
    if trust["schema_version"] != SCHEMA_VERSION:
        raise TypedEvidenceError("trust schema_version is unsupported")
    _safe_id(trust["verifier_id"], "verifier_id")
    path = Path(str(trust["trusted_module_path"])).resolve()
    if path != Path(__file__).resolve() or not path.is_file():
        raise TypedEvidenceError("module path is not verifier-pinned")
    checkout = Path(str(trust["candidate_checkout_root"])).resolve()
    if not checkout.is_absolute() or not checkout.is_dir() or path.is_relative_to(checkout):
        raise TypedEvidenceError("module must be installed outside the candidate checkout")
    digest = _module_digest()
    if trust["module_sha256"] != digest or not SHA256.fullmatch(str(trust["module_sha256"])):
        raise TypedEvidenceError("module digest is not verifier-pinned")
    if not FULL_SHA.fullmatch(str(trust["candidate_commit"])):
        raise TypedEvidenceError("trusted candidate_commit must be a full SHA")
    _safe_id(trust["board_id"], "trusted board_id")
    if not isinstance(trust["max_age_seconds"], int) or not 1 <= trust["max_age_seconds"] <= 86_400:
        raise TypedEvidenceError("max_age_seconds is invalid")
    for field in (
        "evidence_keys", "http_sources", "mcp_sources", "receipt_sources",
        "log_sources", "state_sources",
    ):
        if not isinstance(trust[field], dict):
            raise TypedEvidenceError(f"{field} must be an object")
    key_id = _safe_id(trust["active_evidence_key"], "active_evidence_key")
    if key_id not in trust["evidence_keys"]:
        raise TypedEvidenceError("active evidence key is unavailable")
    _key_bytes(trust["evidence_keys"][key_id], "evidence key")
    replay = _closed(trust["replay_guard"], {"path", "consume"}, "replay_guard")
    if not isinstance(replay["consume"], bool):
        raise TypedEvidenceError("replay_guard consume must be boolean")
    if replay["consume"] and not Path(str(replay["path"])).is_absolute():
        raise TypedEvidenceError("replay_guard path must be absolute")
    return trust


def _key_bytes(value: Any, label: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64,128}", value):
        raise TypedEvidenceError(f"{label} must be 32-64 bytes of lowercase hex")
    return bytes.fromhex(value)


def _source(trust: dict[str, Any], collection: str, source_id: Any) -> tuple[str, dict[str, Any]]:
    source_id = _safe_id(source_id, "source_id")
    source = trust[collection].get(source_id)
    if not isinstance(source, dict):
        raise TypedEvidenceError("source is not verifier-trusted")
    return source_id, source


def _python_execution_selector(argv: list[str]) -> tuple[str, str | None, int]:
    """Return CPython's first effective execution selector.

    Later ``-m`` text is inert after ``-c`` or a script path, so source trust
    cannot be satisfied by merely embedding the desired module tuple anywhere
    in argv.
    """
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "-":
            return "stdin", None, index + 1
        if not argument.startswith("-"):
            return "script", argument, index + 1
        if argument.startswith("--"):
            index += 2 if argument == "--check-hash-based-pycs" else 1
            continue
        position = 1
        while position < len(argument):
            letter = argument[position]
            if letter in {"c", "m"}:
                kind = "command" if letter == "c" else "module"
                attached = argument[position + 1 :]
                if attached:
                    return kind, attached, index + 1
                value = argv[index + 1] if index + 1 < len(argv) else None
                return kind, value, index + 2
            if letter in {"W", "X"}:
                if not argument[position + 1 :]:
                    index += 1
                break
            position += 1
        index += 1
    return "repl", None, len(argv)


def _clean_candidate_source(source: dict[str, Any], trust: dict[str, Any]) -> Path:
    checkout = Path(str(trust["candidate_checkout_root"])).resolve()
    candidate = Path(str(source["candidate_source"])).resolve()
    if (
        not candidate.is_file()
        or not candidate.is_relative_to(checkout)
        or not SHA256.fullmatch(str(source["candidate_source_sha256"]))
        or hashlib.sha256(candidate.read_bytes()).hexdigest()
        != source["candidate_source_sha256"]
    ):
        raise TypedEvidenceError("MCP candidate source is not verifier-pinned")
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    dirty = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if (
        head.returncode or head.stdout.strip() != trust["candidate_commit"]
        or dirty.returncode or dirty.stdout
    ):
        raise TypedEvidenceError("MCP candidate checkout is not the trusted clean commit")
    return candidate


def _mcp_source(
    source_id: Any, trust: dict[str, Any], context: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    source_id, source = _source(trust, "mcp_sources", source_id)
    _closed(source, MCP_SOURCE_KEYS, "MCP source")
    if source["adapter"] != "trusted_mcp_stdio_v1":
        raise TypedEvidenceError("MCP source adapter is unsupported")
    if (
        source["surface"] != context["surface"]
        or source["board_id"] != context["board_id"]
        or source["candidate_commit"] != context["candidate_commit"]
    ):
        raise TypedEvidenceError("MCP source binding does not match request")
    configured_command = Path(str(source["command"])).expanduser()
    command = configured_command.resolve()
    if (
        not configured_command.is_absolute() or not command.is_file()
        or not os.access(command, os.X_OK)
        or not SHA256.fullmatch(str(source["command_sha256"]))
        or hashlib.sha256(command.read_bytes()).hexdigest() != source["command_sha256"]
    ):
        raise TypedEvidenceError("MCP executable is not verifier-pinned")
    checkout = Path(str(trust["candidate_checkout_root"])).resolve()
    if Path(str(source["cwd"])).resolve() != checkout:
        raise TypedEvidenceError("MCP working directory is not the candidate checkout")
    args = source["args"]
    if not isinstance(args, list) or not args or any(not isinstance(item, str) for item in args):
        raise TypedEvidenceError("MCP argv is invalid")
    selector, selected, after = _python_execution_selector([str(command), *args])
    if (
        selector != "module" or selected != "pursers_personal.cli"
        or args[after - 1 : after] != ["mcp"]
    ):
        raise TypedEvidenceError("MCP argv does not execute pursers_personal.cli mcp")
    env = source["env"]
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise TypedEvidenceError("MCP environment is invalid")
    _clean_candidate_source(source, trust)
    challenge = Path(str(source["challenge_key"])).resolve()
    if (
        not challenge.is_absolute() or challenge.is_relative_to(checkout)
        or challenge.is_symlink() or not challenge.is_file()
        or challenge.stat().st_uid != os.getuid() or challenge.stat().st_mode & 0o077
        or len(challenge.read_bytes()) < 32
    ):
        raise TypedEvidenceError("MCP challenge key is not private verifier material")
    _safe_id(source["tool"], "MCP tool")
    if not isinstance(source["arguments"], dict):
        raise TypedEvidenceError("MCP tool arguments are invalid")
    _safe_public(source["arguments"], "MCP tool arguments")
    selectors = source["select_allowlist"]
    if (
        not isinstance(selectors, list) or not selectors
        or len(selectors) != len(set(selectors))
        or any(not isinstance(pointer, str) or not pointer.startswith("/") for pointer in selectors)
    ):
        raise TypedEvidenceError("MCP selector allowlist is invalid")
    if not isinstance(source["timeout_seconds"], (int, float)) or not 0.1 <= source["timeout_seconds"] <= 30:
        raise TypedEvidenceError("MCP timeout is invalid")
    return source_id, source


def _browser_selectors(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise TypedEvidenceError(f"{label} selectors are empty or unbounded")
    paths: set[str] = set()
    for selector in value:
        _closed(selector, {"path", "selector", "property"}, f"{label} selector")
        path = selector["path"]
        css = selector["selector"]
        if (
            not isinstance(path, str) or not path.startswith("/") or path in paths
            or not isinstance(css, str) or not css or len(css) > 512
            or selector["property"] not in BROWSER_PROPERTIES
        ):
            raise TypedEvidenceError(f"{label} selector is invalid")
        paths.add(path)
    return value


def _browser_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 32:
        raise TypedEvidenceError("browser actions are empty or unbounded")
    paths: set[str] = set()
    for action in value:
        if not isinstance(action, dict) or action.get("kind") not in BROWSER_ACTION_KEYS:
            raise TypedEvidenceError("browser action kind is unsupported")
        kind = action["kind"]
        if set(action) != BROWSER_ACTION_KEYS[kind]:
            raise TypedEvidenceError("browser action fields do not match schema")
        result_paths = _browser_action_result_paths(action)
        if any(
            not isinstance(path, str) or not path.startswith("/") or path in paths
            for path in result_paths
        ) or len(result_paths) != (6 if kind == "click_pending_state" else 1):
            raise TypedEvidenceError("browser action result path is invalid")
        paths.update(result_paths)
        if kind in {
            "click", "set_value", "select", "submit", "press_key",
            "click_response_json",
            "click_pending_state",
        }:
            selector = action["selector"]
            if not isinstance(selector, str) or not selector or len(selector) > 512:
                raise TypedEvidenceError("browser action selector is invalid")
        if kind in {"set_value", "select"} and not isinstance(action["value"], str):
            raise TypedEvidenceError("browser action value is invalid")
        if kind == "press_key" and action["key"] not in {
            "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", " ",
            "Escape",
        }:
            raise TypedEvidenceError("browser key action is invalid")
        if kind in {"wait", "resource_delta"} and (
            not isinstance(action["milliseconds"], int)
            or isinstance(action["milliseconds"], bool)
            or not 0 <= action["milliseconds"] <= 10_000
        ):
            raise TypedEvidenceError("browser wait is invalid")
        if kind == "resource_delta" and (
            not isinstance(action["endpoint"], str)
            or not action["endpoint"].startswith("/")
            or action["endpoint"].startswith("//")
            or "?" in action["endpoint"]
            or "#" in action["endpoint"]
        ):
            raise TypedEvidenceError("browser resource endpoint is invalid")
        if kind in {"fetch", "fetch_json"}:
            endpoint = action["endpoint"]
            if (
                action["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                or not isinstance(endpoint, str) or not endpoint.startswith("/")
                or endpoint.startswith("//") or "#" in endpoint
                or action["body"] is not None and not isinstance(action["body"], dict)
            ):
                raise TypedEvidenceError("browser fetch action is invalid")
        if kind == "assistant_binding" and (
            action["endpoint"] != "/api/extensions/assistants"
            or not isinstance(action["assistant_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", action["assistant_id"])
        ):
            raise TypedEvidenceError("browser assistant binding action is invalid")
        if kind == "click_pending_state" and (
            action["method"] != "POST"
            or action["endpoint"] not in {"/api/doors/copy", "/api/doors/rotate"}
            or action["property"] != "disabled"
            or not isinstance(action["hold_milliseconds"], int)
            or isinstance(action["hold_milliseconds"], bool)
            or not 100 <= action["hold_milliseconds"] <= 2_000
        ):
            raise TypedEvidenceError("browser pending-state action is invalid")
        if kind == "click_response_json" and (
            action["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(action["endpoint"], str)
            or not action["endpoint"].startswith("/")
            or action["endpoint"].startswith("//")
            or "#" in action["endpoint"]
        ):
            raise TypedEvidenceError("browser response capture is invalid")
        if kind in {"fetch_json", "click_response_json"} and (
            not isinstance(action["pointer"], str)
            or len(action["pointer"]) > 512
            or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)+", action["pointer"])
            is None
        ):
            raise TypedEvidenceError("browser fetch JSON pointer is invalid")
    if sum(action["kind"] == "click_response_json" for action in value) > 1:
        raise TypedEvidenceError("browser response capture must be unique")
    if (
        sum(action["kind"] == "click_pending_state" for action in value) > 1
        or any(action["kind"] == "click_pending_state" for action in value)
        and any(action["kind"] == "click_response_json" for action in value)
    ):
        raise TypedEvidenceError("browser pending-state capture must be unique")
    return value


def _browser_action_result_paths(action: dict[str, Any]) -> set[str]:
    paths = {action["path"]}
    if action["kind"] == "click_pending_state":
        paths.update(action[field] for field in (
            "pending_path", "settled_path", "status_path",
            "response_sha256_path", "error_path",
        ))
    return paths


def _browser_state_source(
    source_id: Any, trust: dict[str, Any], context: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    source_id, source = _source(trust, "state_sources", source_id)
    adapter = source.get("adapter")
    _closed(
        source,
        (
            ASSISTANT_BINDING_SOURCE_KEYS
            if adapter == "aionui_assistant_binding_v1"
            else BROWSER_STATE_SOURCE_KEYS
        ),
        "browser state source",
    )
    if adapter not in {"trusted_browser_state_v1", "aionui_assistant_binding_v1"}:
        raise TypedEvidenceError("browser state adapter is unsupported")
    if (
        source["surface"] != context["surface"]
        or source["board_id"] != context["board_id"]
        or source["candidate_commit"] != context["candidate_commit"]
    ):
        raise TypedEvidenceError("browser state source binding does not match request")
    checkout = Path(str(trust["candidate_checkout_root"])).resolve()
    command = Path(str(source["command"])).resolve()
    config = Path(str(source["config_path"])).resolve()
    if (
        not command.is_absolute() or not command.is_file() or not os.access(command, os.X_OK)
        or command.is_relative_to(checkout) or command.stat().st_uid != os.getuid()
        or command.stat().st_mode & 0o022
        or hashlib.sha256(command.read_bytes()).hexdigest() != source["command_sha256"]
        or not SHA256.fullmatch(str(source["command_sha256"]))
    ):
        raise TypedEvidenceError("browser state command is not verifier-pinned")
    if (
        not config.is_file() or config.is_relative_to(checkout)
        or config.stat().st_uid != os.getuid() or config.stat().st_mode & 0o077
        or config != command.parent / "observer.json"
        or hashlib.sha256(config.read_bytes()).hexdigest() != source["config_sha256"]
        or not SHA256.fullmatch(str(source["config_sha256"]))
    ):
        raise TypedEvidenceError("browser state config is not verifier-pinned")
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    dirty = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if (
        head.returncode or head.stdout.strip() != trust["candidate_commit"]
        or dirty.returncode or dirty.stdout
    ):
        raise TypedEvidenceError("browser candidate checkout is not the trusted clean commit")
    parsed = urlsplit(str(source["base_url"]))
    page = urlsplit(str(source["page_url"]))
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if (
        parsed.scheme not in {"http", "https"} or not loopback
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or (page.scheme, page.hostname, page.port) != (parsed.scheme, parsed.hostname, parsed.port)
        or page.username or page.password or page.query or page.fragment
    ):
        raise TypedEvidenceError("browser state origin or page is unsafe")
    recipe = _closed(
        source["recipe"], {"before", "actions", "after", "settle_milliseconds"},
        "browser state recipe",
    )
    _browser_selectors(recipe["before"], "browser before")
    _browser_actions(recipe["actions"])
    _browser_selectors(recipe["after"], "browser after")
    if (
        not isinstance(recipe["settle_milliseconds"], int)
        or isinstance(recipe["settle_milliseconds"], bool)
        or not 0 <= recipe["settle_milliseconds"] <= 10_000
    ):
        raise TypedEvidenceError("browser settle delay is invalid")
    pending = [item for item in recipe["actions"] if item["kind"] == "click_pending_state"]
    if (
        len(pending) > 1
        or pending and recipe["settle_milliseconds"] < pending[0]["hold_milliseconds"]
    ):
        raise TypedEvidenceError("browser pending-state scheduling is invalid")
    expected_selectors = {
        selector["path"] for selector in (*recipe["before"], *recipe["after"])
    } | set().union(*(
        _browser_action_result_paths(action) for action in recipe["actions"]
    ))
    allowlist = source["select_allowlist"]
    if (
        not isinstance(allowlist, list) or set(allowlist) != expected_selectors
        or len(allowlist) != len(set(allowlist))
    ):
        raise TypedEvidenceError("browser state selector allowlist differs from recipe")
    env = source["env"]
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise TypedEvidenceError("browser state environment is invalid")
    if not isinstance(source["timeout_seconds"], (int, float)) or not 1 <= source["timeout_seconds"] <= 180:
        raise TypedEvidenceError("browser state timeout is invalid")
    if adapter == "aionui_assistant_binding_v1":
        actions = [item for item in recipe["actions"] if item["kind"] == "assistant_binding"]
        if len(actions) != 1 or len(recipe["actions"]) != 1:
            raise TypedEvidenceError("assistant binding recipe must contain one exact action")
        _assistant_manifest_binding(source, trust, actions[0]["assistant_id"])
    return source_id, source


def _assistant_manifest_binding(
    source: dict[str, Any], trust: dict[str, Any], assistant_id: str,
) -> tuple[dict[str, Any], bytes, str]:
    """Bind an installed manifest/context byte-for-byte to the clean candidate."""
    checkout = Path(str(trust["candidate_checkout_root"])).resolve()
    candidate_value = Path(str(source["candidate_manifest"]))
    installed_value = Path(str(source["installed_manifest"]))
    candidate = candidate_value.resolve()
    installed = installed_value.resolve()
    if (
        not candidate_value.is_absolute() or not candidate.is_file()
        or candidate != checkout / "tools/aionui-extension/aion-extension.json"
        or not SHA256.fullmatch(str(source["candidate_manifest_sha256"]))
        or hashlib.sha256(candidate.read_bytes()).hexdigest()
        != source["candidate_manifest_sha256"]
    ):
        raise TypedEvidenceError("candidate assistant manifest is not verifier-pinned")
    candidate_bytes = candidate.read_bytes()
    if (
        not installed_value.is_absolute() or installed_value.is_symlink()
        or not installed.is_file() or installed.is_relative_to(checkout)
        or installed.read_bytes() != candidate_bytes
    ):
        raise TypedEvidenceError("installed assistant manifest differs from candidate")
    try:
        manifest = json.loads(candidate_bytes)
    except json.JSONDecodeError:
        raise TypedEvidenceError("candidate assistant manifest is not JSON") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
        raise TypedEvidenceError("candidate assistant manifest has no extension identity")
    contributes = manifest.get("contributes")
    assistants = contributes.get("assistants") if isinstance(contributes, dict) else None
    matches = [
        item for item in assistants or []
        if isinstance(item, dict) and item.get("id") == assistant_id
    ]
    if len(matches) != 1:
        raise TypedEvidenceError("candidate assistant manifest match is not unique")
    assistant = _closed(
        matches[0],
        {"id", "name", "description", "agentId", "presetAgentType", "contextFile"},
        "candidate assistant manifest entry",
    )
    if (
        not all(isinstance(assistant[field], str) and assistant[field]
                for field in assistant)
        or assistant["agentId"] != assistant["presetAgentType"]
        or re.fullmatch(r"contexts/[^/]+\.md", assistant["contextFile"]) is None
    ):
        raise TypedEvidenceError("candidate assistant manifest entry is invalid")
    candidate_context_value = candidate.parent / assistant["contextFile"]
    installed_context_value = installed.parent / assistant["contextFile"]
    candidate_context = candidate_context_value.resolve()
    installed_context = installed_context_value.resolve()
    if (
        candidate_context_value.is_symlink() or not candidate_context.is_file()
        or not candidate_context.is_relative_to(candidate.parent)
        or installed_context_value.is_symlink() or not installed_context.is_file()
        or not installed_context.is_relative_to(installed.parent)
        or installed_context.is_relative_to(checkout)
        or installed_context.read_bytes() != candidate_context.read_bytes()
    ):
        raise TypedEvidenceError("installed assistant context differs from candidate")
    context_bytes = candidate_context.read_bytes()
    try:
        context_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise TypedEvidenceError("candidate assistant context is not UTF-8") from None
    return assistant, context_bytes, manifest["name"]


def _assistant_public_binding(
    source: dict[str, Any], trust: dict[str, Any], assistant_id: str,
) -> dict[str, Any]:
    assistant, context_bytes, extension_name = _assistant_manifest_binding(
        source, trust, assistant_id
    )
    return {
        "manifest_id": assistant["id"],
        "runtime_id": f"ext-{assistant['id']}",
        "agent_id": assistant["agentId"],
        "preset_agent_type": assistant["presetAgentType"],
        "context_file": assistant["contextFile"],
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "manifest_sha256": source["candidate_manifest_sha256"],
        "extension_name": extension_name,
        "endpoint": "/api/extensions/assistants",
        "transport": "same-origin-http",
    }


def _binding_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        field = value[1:]
        if field not in CONTEXT_KEYS:
            raise TypedEvidenceError("source binding references an unsupported context field")
        return context[field]
    return value


def _check_bindings(document: Any, bindings: Any, context: dict[str, Any], label: str) -> None:
    if not isinstance(bindings, dict) or not bindings:
        raise TypedEvidenceError(f"{label} needs verifier-owned bindings")
    for pointer, expected in bindings.items():
        if _pointer(document, pointer) != _binding_value(expected, context):
            raise TypedEvidenceError(f"{label} binding mismatch: {pointer}")


def _require_context_bindings(bindings: dict[str, Any], fields: set[str], label: str) -> None:
    present = {value[1:] for value in bindings.values() if isinstance(value, str) and value.startswith("$")}
    missing = fields - present
    if missing:
        raise TypedEvidenceError(f"{label} lacks required context bindings")


def _process_cwd(pid: int, label: str) -> Path:
    completed = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    paths = [line[1:] for line in completed.stdout.splitlines() if line.startswith("n")]
    if completed.returncode or len(paths) != 1:
        raise TypedEvidenceError(f"{label} working directory is unavailable")
    return Path(paths[0]).resolve()


def _process_check(process: Any, receipt: Any | None = None) -> dict[str, Any] | None:
    if process is None:
        return None
    process = _closed(
        process, {
            "pid_file", "argv0_names", "argv_prefix", "argv_contains",
            "required_arguments", "cwd", "artifact_path", "artifact_sha256",
            "receipt_pid_pointer", "executable", "executable_sha256",
            "entrypoint",
        },
        "process trust",
    )
    pid_path = Path(str(process["pid_file"])).resolve()
    if (
        not pid_path.is_absolute() or not pid_path.is_file()
        or pid_path.stat().st_mode & 0o077 or pid_path.stat().st_uid != os.getuid()
    ):
        raise TypedEvidenceError("trusted process PID file is unavailable or not private")
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        raise TypedEvidenceError("trusted process PID is invalid") from None
    if (
        pid <= 1 or not isinstance(process["argv_prefix"], list) or not process["argv_prefix"]
        or not isinstance(process["argv0_names"], list) or not process["argv0_names"]
        or not isinstance(process["argv_contains"], list)
        or not isinstance(process["required_arguments"], dict)
        or any(
            not isinstance(part, str)
            for part in process["argv0_names"] + process["argv_prefix"] + process["argv_contains"]
        )
        or any(
            not isinstance(flag, str) or not flag.startswith("--")
            or not isinstance(value, str)
            for flag, value in process["required_arguments"].items()
        )
    ):
        raise TypedEvidenceError("trusted process contract is invalid")
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="], text=True,
        capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    command = completed.stdout.strip()
    try:
        arguments = shlex.split(command)
    except ValueError:
        arguments = []
    executable = Path(str(process["executable"])).resolve()
    prefix = process["argv_prefix"]
    required_arguments = process["required_arguments"]
    if (
        completed.returncode or not arguments
        or Path(arguments[0]).name not in process["argv0_names"]
        or Path(arguments[0]).resolve() != executable
        or not executable.is_file()
        or not SHA256.fullmatch(str(process["executable_sha256"]))
        or hashlib.sha256(executable.read_bytes()).hexdigest()
        != process["executable_sha256"]
        or arguments[1:1 + len(prefix)] != prefix
        or any(part not in arguments for part in process["argv_contains"])
    ):
        raise TypedEvidenceError("trusted process identity does not match")
    for flag, expected in required_arguments.items():
        positions = [index for index, value in enumerate(arguments) if value == flag]
        if len(positions) != 1 or positions[0] + 1 >= len(arguments):
            raise TypedEvidenceError(f"trusted process lacks exact {flag}")
        if arguments[positions[0] + 1] != expected:
            raise TypedEvidenceError(f"trusted process {flag} changed")
    entrypoint = _closed(
        process["entrypoint"],
        {"kind", "module", "path", "sha256", "resolver", "resolver_sha256"},
        "process entrypoint trust",
    )
    entrypoint_path = Path(str(entrypoint["path"])).resolve()
    if (
        entrypoint["kind"] not in {"isolated_module", "script"}
        or not isinstance(entrypoint["module"], str)
        or not entrypoint_path.is_file()
        or not SHA256.fullmatch(str(entrypoint["sha256"]))
        or hashlib.sha256(entrypoint_path.read_bytes()).hexdigest()
        != entrypoint["sha256"]
    ):
        raise TypedEvidenceError("trusted process entrypoint is invalid")
    if entrypoint["kind"] == "script":
        if (
            entrypoint["module"] or entrypoint["resolver"]
            or entrypoint["resolver_sha256"]
            or len(arguments) < 2 or Path(arguments[1]).resolve() != entrypoint_path
        ):
            raise TypedEvidenceError("trusted process script is not the executed entrypoint")
    else:
        module = entrypoint["module"]
        resolver = Path(str(entrypoint["resolver"])).resolve()
        if (
            not module or arguments[1:4] != ["-I", "-m", module]
            or not resolver.is_file()
            or not SHA256.fullmatch(str(entrypoint["resolver_sha256"]))
            or hashlib.sha256(resolver.read_bytes()).hexdigest()
            != entrypoint["resolver_sha256"]
        ):
            raise TypedEvidenceError("trusted process module invocation is not isolated")
        probe = subprocess.run(
            [
                str(entrypoint["resolver"]), "-I", "-c",
                (
                    "import importlib.util,sys; s=importlib.util.find_spec(sys.argv[1]); "
                    "print('' if s is None else s.origin)"
                ),
                module,
            ],
            text=True, capture_output=True, check=False, timeout=5,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        )
        try:
            resolved_module = Path(probe.stdout.strip()).resolve()
        except (OSError, RuntimeError):
            resolved_module = Path("/")
        if probe.returncode or resolved_module != entrypoint_path:
            raise TypedEvidenceError("trusted process module resolution changed")
    expected_cwd = Path(str(process["cwd"])).resolve()
    if not expected_cwd.is_absolute() or _process_cwd(pid, "trusted process") != expected_cwd:
        raise TypedEvidenceError("trusted process working directory changed")
    artifact = Path(str(process["artifact_path"])).resolve()
    if (
        not artifact.is_file()
        or not SHA256.fullmatch(str(process["artifact_sha256"]))
        or hashlib.sha256(artifact.read_bytes()).hexdigest() != process["artifact_sha256"]
        or str(artifact) not in arguments
    ):
        raise TypedEvidenceError("trusted process artifact is not bound to its command")
    pointer = process["receipt_pid_pointer"]
    if receipt is not None and _pointer(receipt, pointer) != pid:
        raise TypedEvidenceError("receipt PID does not match the trusted process")
    return {
        "pid": pid,
        "argv_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "cwd_sha256": hashlib.sha256(str(expected_cwd).encode()).hexdigest(),
        "artifact_sha256": process["artifact_sha256"],
        "entrypoint_sha256": entrypoint["sha256"],
        "executable_sha256": process["executable_sha256"],
    }


def _runtime_check(runtime: Any, trust: dict[str, Any], base_url: str) -> dict[str, Any]:
    runtime = _closed(
        runtime,
        {
            "pid_file", "command_sha256", "start_time", "executable",
            "cwd", "artifact_path", "artifact_sha256", "listener_port",
        },
        "HTTP runtime trust",
    )
    pid_path = Path(str(runtime["pid_file"])).resolve()
    if (
        not pid_path.is_absolute() or not pid_path.is_file()
        or pid_path.stat().st_mode & 0o077 or pid_path.stat().st_uid != os.getuid()
    ):
        raise TypedEvidenceError("HTTP runtime PID file is unavailable or not private")
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        raise TypedEvidenceError("HTTP runtime PID is invalid") from None
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    command = completed.stdout.strip()
    started = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode or started.returncode or not command:
        raise TypedEvidenceError("HTTP runtime process is unavailable")
    start_time = str(runtime["start_time"])
    if not start_time or started.stdout.strip() != start_time:
        raise TypedEvidenceError("HTTP runtime start time changed")
    try:
        arguments = shlex.split(command)
    except ValueError:
        arguments = []
    if (
        not arguments
        or str(Path(arguments[0]).resolve()) != str(Path(runtime["executable"]).resolve())
        or hashlib.sha256(command.encode()).hexdigest() != runtime["command_sha256"]
    ):
        raise TypedEvidenceError("HTTP runtime executable or command changed")
    artifact = Path(str(runtime["artifact_path"])).resolve()
    checkout = Path(str(trust["candidate_checkout_root"])).resolve()
    if (
        not artifact.is_file() or not artifact.is_relative_to(checkout)
        or hashlib.sha256(artifact.read_bytes()).hexdigest() != runtime["artifact_sha256"]
    ):
        raise TypedEvidenceError("HTTP runtime artifact is not verifier-pinned")
    if not SHA256.fullmatch(str(runtime["artifact_sha256"])):
        raise TypedEvidenceError("HTTP runtime artifact digest is invalid")
    expected_cwd = Path(str(runtime["cwd"])).resolve()
    if expected_cwd != checkout or _process_cwd(pid, "HTTP runtime") != checkout:
        raise TypedEvidenceError("HTTP runtime working directory is not the candidate checkout")
    if len(arguments) < 2 or Path(arguments[1]).resolve() != artifact:
        raise TypedEvidenceError("HTTP runtime artifact is not the executed script")
    port = urlsplit(base_url).port
    if runtime["listener_port"] != port or not isinstance(port, int):
        raise TypedEvidenceError("HTTP runtime listener port changed")
    listener = subprocess.run(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    listener_pids = {
        int(line[1:]) for line in listener.stdout.splitlines()
        if line.startswith("p") and line[1:].isdigit()
    }
    if listener.returncode or listener_pids != {pid}:
        raise TypedEvidenceError("HTTP runtime listener is not owned by the trusted process")
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if head.returncode or head.stdout.strip() != trust["candidate_commit"]:
        raise TypedEvidenceError("HTTP runtime checkout HEAD is not the trusted candidate")
    dirty = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if dirty.returncode or dirty.stdout:
        raise TypedEvidenceError("HTTP runtime candidate checkout is not clean")
    return {
        "pid": pid,
        "start_time": start_time,
        "command_sha256": runtime["command_sha256"],
        "cwd_sha256": hashlib.sha256(str(checkout).encode()).hexdigest(),
        "artifact_sha256": runtime["artifact_sha256"],
        "listener_port": port,
    }


def _sign_evidence(evidence: dict[str, Any], trust: dict[str, Any]) -> dict[str, Any]:
    key_id = trust["active_evidence_key"]
    key = _key_bytes(trust["evidence_keys"][key_id], "evidence key")
    signature = hmac.new(key, _json_bytes(evidence), hashlib.sha256).hexdigest()
    return {**evidence, "auth": {"key_id": key_id, "hmac_sha256": signature}}


def _validate_runtime_record(value: Any, label: str) -> dict[str, Any]:
    value = _closed(
        value,
        {
            "pid", "start_time", "command_sha256", "cwd_sha256", "artifact_sha256",
            "listener_port",
        },
        label,
    )
    if (
        not isinstance(value["pid"], int) or isinstance(value["pid"], bool)
        or value["pid"] <= 1
        or not isinstance(value["start_time"], str) or not value["start_time"]
        or not SHA256.fullmatch(str(value["command_sha256"]))
        or not SHA256.fullmatch(str(value["cwd_sha256"]))
        or not SHA256.fullmatch(str(value["artifact_sha256"]))
        or not isinstance(value["listener_port"], int)
        or isinstance(value["listener_port"], bool)
        or not 1 <= value["listener_port"] <= 65535
    ):
        raise TypedEvidenceError(f"{label} has invalid types")
    return value


def _validate_fleet_project_projection(
    path: str, status: int, selected: dict[str, Any], label: str,
) -> None:
    """Validate the only credential-safe projection of Add project output."""
    preflight_unique = {
        pointer for pointer in FLEET_PROJECT_PREFLIGHT_POINTERS
        if pointer.startswith("/_project_preflight/")
    }
    project_pointers = (
        FLEET_PROJECT_STEP_POINTERS | FLEET_PROJECT_CREDENTIAL_ABSENCE_POINTERS
        | preflight_unique
    )
    if path != "/api/projects/add":
        if set(selected) & project_pointers:
            raise TypedEvidenceError(
                f"{label} uses project selectors on another route"
            )
        return
    preflight = set(selected) & preflight_unique
    if preflight:
        if not FLEET_PROJECT_PREFLIGHT_POINTERS <= set(selected):
            raise TypedEvidenceError(
                f"{label} project preflight projection is incomplete"
            )
        if (
            status != 403
            or selected["/_project_preflight/schema_version"] != SCHEMA_VERSION
            or selected["/_project_preflight/result/status"] != status
            or selected["/_project_preflight/result/decision"] != "denied"
            or selected["/_evidence/status"] != status
            or selected["/_evidence/outcome"] != "failed"
            or selected["/_evidence/effect"] != "project_state_unchanged"
            or selected["/_evidence/changed"] is not False
            or selected["/_project_preflight/request/project"]
            != selected["/_evidence/entity"]
            or selected["/_project_preflight/request/board_id"]
            != selected["/_evidence/board_id"]
            or selected["/_project_preflight/request/action_sha256"]
            != selected["/_evidence/action_sha256"]
            or not isinstance(selected["/error"], str)
            or not selected["/error"].startswith(
                "board access denied: admin membership required for "
            )
        ):
            raise TypedEvidenceError(
                f"{label} project preflight denial is inconsistent"
            )
        for domain in FLEET_PROJECT_STATE_DOMAINS:
            before = selected[
                f"/_project_preflight/domains/{domain}/before_sha256"
            ]
            after = selected[
                f"/_project_preflight/domains/{domain}/after_sha256"
            ]
            if (
                not SHA256.fullmatch(str(before))
                or not SHA256.fullmatch(str(after))
                or not hmac.compare_digest(before, after)
            ):
                raise TypedEvidenceError(
                    f"{label} project {domain} state changed before denial"
                )
    if "/doors" in selected and selected["/doors"] is not None:
        raise TypedEvidenceError(f"{label} retained door credential material")
    if (
        "/steps/5/step" in selected
        and selected["/steps/5/step"] != "door_credentials"
    ):
        raise TypedEvidenceError(f"{label} door credential step changed")
    if (
        "/steps/5/status" in selected
        and selected["/steps/5/status"] not in {"created", "already present"}
    ):
        raise TypedEvidenceError(f"{label} door credential status is invalid")


def _validate_http_result(value: Any, source: dict[str, Any], label: str) -> dict[str, Any]:
    value = _closed(
        value,
        {
            "method", "path", "status", "selected", "response_sha256",
            "correlation",
        },
        label,
    )
    correlation = _closed(
        value["correlation"],
        {"observation_id", "run_id", "action_id", "entity"},
        f"{label} correlation",
    )
    if (
        not isinstance(value["method"], str)
        or value["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        or not isinstance(value["path"], str) or not value["path"].startswith("/")
        or not isinstance(value["status"], int) or isinstance(value["status"], bool)
        or not 100 <= value["status"] <= 599
        or not isinstance(value["selected"], dict) or not value["selected"]
        or any(not isinstance(key, str) for key in value["selected"])
        or not set(value["selected"]) <= set(source["select_allowlist"])
        or not SHA256.fullmatch(str(value["response_sha256"]))
        or any(not isinstance(item, str) for item in correlation.values())
    ):
        raise TypedEvidenceError(f"{label} has invalid types or fields")
    _validate_fleet_project_projection(
        value["path"], value["status"], value["selected"], label
    )
    return value


def _verify_evidence(evidence: Any, trust: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "kind", "context", "captured_at", "source",
        "record", "payload_sha256", "auth",
    }
    evidence = _closed(evidence, keys, "evidence")
    auth = _closed(evidence["auth"], {"key_id", "hmac_sha256"}, "evidence auth")
    key_id = _safe_id(auth["key_id"], "evidence key_id")
    if key_id not in trust["evidence_keys"] or not SHA256.fullmatch(str(auth["hmac_sha256"])):
        raise TypedEvidenceError("evidence authentication is invalid")
    unsigned = {key: value for key, value in evidence.items() if key != "auth"}
    expected = hmac.new(
        _key_bytes(trust["evidence_keys"][key_id], "evidence key"),
        _json_bytes(unsigned), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, auth["hmac_sha256"]):
        raise TypedEvidenceError("evidence authentication failed")
    if evidence["schema_version"] != SCHEMA_VERSION or evidence["kind"] not in KINDS:
        raise TypedEvidenceError("evidence schema is unsupported")
    context = _context({"context": evidence["context"]}, trust)
    if evidence["payload_sha256"] != _digest(evidence["record"]):
        raise TypedEvidenceError("evidence payload digest changed")
    source = _closed(
        evidence["source"], {
            "source_id", "adapter", "provenance", "runtime_id", "module_sha256",
            "source_config_sha256",
        },
        "evidence source",
    )
    if source["module_sha256"] != trust["module_sha256"]:
        raise TypedEvidenceError("evidence module digest changed")
    collection = {
        "http_response": "http_sources", "mcp_tool_response": "mcp_sources",
        "receipt_field": "receipt_sources",
        "log_assertion": "log_sources", "state_transition": "state_sources",
    }[evidence["kind"]]
    source_id, trusted_source = _source(trust, collection, source["source_id"])
    expected_source = _base_source(source_id, trusted_source, trust)
    if source != expected_source:
        raise TypedEvidenceError("evidence source no longer matches verifier trust")
    _fresh(evidence["captured_at"], trust["max_age_seconds"], "evidence captured_at")
    _safe_public(evidence["record"], "evidence record")
    if evidence["kind"] == "http_response":
        record = _closed(
            evidence["record"], {"action_origin", "runtime", "response"},
            "HTTP evidence record",
        )
        if record["action_origin"] != "verifier_api":
            raise TypedEvidenceError("HTTP evidence action origin is unsupported")
        runtime = _validate_runtime_record(record["runtime"], "HTTP runtime record")
        if runtime != _runtime_check(
            trusted_source["runtime"], trust, trusted_source["base_url"]
        ):
            raise TypedEvidenceError("HTTP runtime evidence changed")
        _validate_http_result(record["response"], trusted_source, "HTTP response record")
    elif evidence["kind"] == "mcp_tool_response":
        _, trusted_source = _mcp_source(source_id, trust, context)
        record = _closed(
            evidence["record"], {"process", "transport", "attestation", "result"},
            "MCP evidence record",
        )
        if record["transport"] != "stdio":
            raise TypedEvidenceError("MCP evidence transport is unsupported")
        process = _closed(
            record["process"],
            {"pid", "argv_sha256", "executable_sha256", "candidate_source_sha256"},
            "MCP process record",
        )
        if (
            not isinstance(process["pid"], int) or isinstance(process["pid"], bool)
            or process["pid"] <= 1
            or any(
                not SHA256.fullmatch(str(process[field]))
                for field in ("argv_sha256", "executable_sha256", "candidate_source_sha256")
            )
            or process["executable_sha256"] != trusted_source["command_sha256"]
            or process["candidate_source_sha256"] != trusted_source["candidate_source_sha256"]
        ):
            raise TypedEvidenceError("MCP process record changed")
        attestation = _closed(
            record["attestation"], MCP_PUBLIC_ATTESTATION_KEYS,
            "MCP public attestation",
        )
        nonce = attestation.get("nonce")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
            raise TypedEvidenceError("MCP attestation nonce is invalid")
        full_attestation = {
            **attestation,
            "candidate_source": str(Path(str(trusted_source["candidate_source"])).resolve()),
        }
        full_attestation.pop("candidate_source_sha256", None)
        _validate_mcp_attestation(full_attestation, trusted_source, context, nonce)
        result = _closed(
            record["result"],
            {"tool", "arguments_sha256", "selected", "result_sha256", "correlation"},
            "MCP tool result record",
        )
        correlation = _closed(
            result["correlation"], {"observation_id", "run_id", "action_id", "entity"},
            "MCP result correlation",
        )
        if (
            attestation["candidate_source_sha256"] != trusted_source["candidate_source_sha256"]
            or attestation["pid"] != process["pid"]
            or result["tool"] != trusted_source["tool"]
            or result["arguments_sha256"] != _digest(trusted_source["arguments"])
            or not isinstance(result["selected"], dict) or not result["selected"]
            or not set(result["selected"]) <= set(trusted_source["select_allowlist"])
            or not SHA256.fullmatch(str(result["result_sha256"]))
            or correlation != {
                key: context[key]
                for key in ("observation_id", "run_id", "action_id", "entity")
            }
        ):
            raise TypedEvidenceError("MCP tool result binding changed")
    elif evidence["kind"] == "receipt_field":
        record = _closed(
            evidence["record"],
            {"fields", "receipt_sha256", "authenticity", "process"},
            "receipt evidence record",
        )
        if (
            not isinstance(record["fields"], dict) or not record["fields"]
            or any(not isinstance(key, str) or not key.startswith("/") for key in record["fields"])
            or not SHA256.fullmatch(str(record["receipt_sha256"]))
            or record["authenticity"]
            not in {"canonical_hmac_sha256", "verifier_bound_personal_runtime"}
        ):
            raise TypedEvidenceError("receipt evidence record has invalid types")
        if record["process"] is not None:
            process = _closed(
                record["process"],
                {
                    "pid", "argv_sha256", "cwd_sha256", "artifact_sha256",
                    "entrypoint_sha256", "executable_sha256",
                },
                "receipt process record",
            )
            if (
                not isinstance(process["pid"], int) or isinstance(process["pid"], bool)
                or not SHA256.fullmatch(str(process["argv_sha256"]))
                or not SHA256.fullmatch(str(process["cwd_sha256"]))
                or not SHA256.fullmatch(str(process["artifact_sha256"]))
                or not SHA256.fullmatch(str(process["entrypoint_sha256"]))
                or not SHA256.fullmatch(str(process["executable_sha256"]))
            ):
                raise TypedEvidenceError("receipt process record has invalid types")
    elif evidence["kind"] == "log_assertion":
        adapter = trusted_source["adapter"]
        record = _closed(
            evidence["record"],
            (
                {
                    "entry", "entry_sha256", "authenticity", "runtime",
                    "action_response",
                }
                if adapter == "fleet_evidence_trace_v1"
                else {"entry", "entry_sha256", "authenticity", "process"}
            ),
            "log evidence record",
        )
        if (
            not isinstance(record["entry"], dict)
            or set(record["entry"]) != set(trusted_source["document_keys"])
            or not SHA256.fullmatch(str(record["entry_sha256"]))
        ):
            raise TypedEvidenceError("log evidence record has invalid fields")
        if adapter == "fleet_evidence_trace_v1":
            _fleet_source_contract(trusted_source)
            if record["authenticity"] != "fleet_runtime_trace_bound":
                raise TypedEvidenceError("log evidence record has invalid fields")
            _, http_source = _http_source(
                trusted_source["http_source_id"], trust, context
            )
            if (
                trusted_source["runtime_id"] != http_source["runtime_id"]
                or trusted_source["http_source_config_sha256"]
                != _digest(http_source)
            ):
                raise TypedEvidenceError(
                    "Fleet trace source does not match its trusted HTTP runtime"
                )
            runtime = _validate_runtime_record(
                record["runtime"], "log runtime record"
            )
            if runtime != _runtime_check(
                http_source["runtime"], trust, http_source["base_url"]
            ):
                raise TypedEvidenceError("log runtime evidence changed")
            _validate_fleet_entry(
                record["entry"], trusted_source, runtime, context
            )
            action_response = _validate_http_result(
                record["action_response"], http_source,
                "Fleet trace action response",
            )
            expected_selected = {
                f"/_evidence/{key}": value
                for key, value in record["entry"].items()
            }
            expected_selected["/_evidence/log_emitted"] = True
            result_values = {
                pointer: action_response["selected"][pointer]
                for pointer in FLEET_ACTION_RESULT_POINTERS[
                    trusted_source["action_path"]
                ]
                if pointer in action_response["selected"]
            }
            expected_selected.update(result_values)
            if (
                action_response["method"] != "POST"
                or action_response["path"] != trusted_source["action_path"]
                or action_response["status"] != record["entry"]["status"]
                or action_response["selected"] != expected_selected
            ):
                raise TypedEvidenceError(
                    "Fleet trace action response does not match its log entry"
                )
            if trusted_source["action_path"] == "/api/attention" and (
                _digest({"items": result_values["/items"]})
                != record["entry"]["after_sha256"]
                or _digest({"items": result_values["/items"]})
                != record["entry"]["result_sha256"]
            ):
                raise TypedEvidenceError(
                    "Fleet attention response does not match its state digests"
                )
        else:
            if record["authenticity"] != "verifier_captured_process_bound":
                raise TypedEvidenceError("log evidence record has invalid fields")
            process = _closed(
                record["process"],
                {
                    "pid", "argv_sha256", "cwd_sha256", "artifact_sha256",
                    "entrypoint_sha256", "executable_sha256",
                },
                "log process record",
            )
            if (
                not isinstance(process["pid"], int)
                or isinstance(process["pid"], bool)
                or not SHA256.fullmatch(str(process["argv_sha256"]))
                or not SHA256.fullmatch(str(process["cwd_sha256"]))
                or not SHA256.fullmatch(str(process["artifact_sha256"]))
                or not SHA256.fullmatch(str(process["entrypoint_sha256"]))
                or not SHA256.fullmatch(str(process["executable_sha256"]))
            ):
                raise TypedEvidenceError("log process record has invalid types")
    else:
        record = evidence["record"]
        if trusted_source.get("adapter") in {
            "trusted_browser_state_v1", "aionui_assistant_binding_v1",
        }:
            _, trusted_source = _browser_state_source(source_id, trust, context)
            record = _closed(
                record, {"before", "action", "after", "order", "observer"},
                "browser state transition record",
            )
            order = _closed(
                record["order"], {"before_at", "action_at", "after_at"},
                "browser state order",
            )
            moments = [
                _timestamp(order[field], f"browser state {field}")
                for field in ("before_at", "action_at", "after_at")
            ]
            if moments != sorted(moments):
                raise TypedEvidenceError("browser state causal order is invalid")
            observer = _closed(
                record["observer"],
                {"command_sha256", "config_sha256", "runtime", "page_url"},
                "browser observer record",
            )
            runtime = _closed(
                observer["runtime"], {"product", "version", "build", "source"},
                "browser observer runtime",
            )
            if (
                observer["command_sha256"] != trusted_source["command_sha256"]
                or observer["config_sha256"] != trusted_source["config_sha256"]
                or observer["page_url"] != trusted_source["page_url"]
                or any(not isinstance(value, str) or not value for value in runtime.values())
            ):
                raise TypedEvidenceError("browser observer binding changed")
            for phase in ("before", "action", "after"):
                _browser_phase(record[phase], trusted_source, context, phase)
            if trusted_source["adapter"] == "aionui_assistant_binding_v1":
                action_spec = trusted_source["recipe"]["actions"][0]
                if record["action"]["selected"].get(action_spec["path"]) != (
                    _assistant_public_binding(
                        trusted_source, trust, action_spec["assistant_id"]
                    )
                ):
                    raise TypedEvidenceError("assistant binding evidence changed")
        else:
            record = _closed(
                record,
                {
                    "before", "action", "after", "order", "http_source_id",
                    "http_source_config_sha256", "runtime",
                },
                "state transition record",
            )
            http_id, http_source = _source(
                trust, "http_sources", trusted_source["http_source_id"]
            )
            if (
                trusted_source["runtime_id"] != http_source["runtime_id"]
                or record["http_source_id"] != http_id
                or record["http_source_config_sha256"] != _digest(http_source)
                or trusted_source["http_source_config_sha256"] != _digest(http_source)
            ):
                raise TypedEvidenceError("state transition HTTP source binding changed")
            runtime = _validate_runtime_record(record["runtime"], "state runtime record")
            if runtime != _runtime_check(http_source["runtime"], trust, http_source["base_url"]):
                raise TypedEvidenceError("state runtime evidence changed")
            order = _closed(record["order"], {"before_at", "action_at", "after_at"}, "state transition order")
            moments = [_timestamp(order[field], f"state transition {field}") for field in ("before_at", "action_at", "after_at")]
            if moments != sorted(moments):
                raise TypedEvidenceError("state transition causal order is invalid")
            expected_correlation = {
                "observation_id": context["observation_id"], "run_id": context["run_id"],
                "action_id": context["action_id"], "entity": context["entity"],
            }
            for phase in ("before", "action", "after"):
                phase_record = _validate_http_result(
                    record[phase], http_source, f"state transition {phase}"
                )
                if phase_record["correlation"] != expected_correlation:
                    raise TypedEvidenceError("state transition correlation changed")
    return evidence


def _base_source(source_id: str, source: dict[str, Any], trust: dict[str, Any]) -> dict[str, Any]:
    for field in ("adapter", "provenance", "runtime_id"):
        _safe_id(source.get(field), f"source {field}")
    return {
        "source_id": source_id,
        "adapter": source["adapter"],
        "provenance": source["provenance"],
        "runtime_id": source["runtime_id"],
        "module_sha256": trust["module_sha256"],
        "source_config_sha256": _digest(source),
    }


def _http_source(source_id: Any, trust: dict[str, Any], context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    source_id, source = _source(trust, "http_sources", source_id)
    keys = {
        "adapter", "provenance", "runtime_id", "base_url", "surface", "board_id",
        "candidate_commit", "methods", "headers", "timeout_seconds",
        "response_bindings", "runtime", "select_allowlist",
    }
    _closed(source, keys, "HTTP source")
    if source["adapter"] != "trusted_http_v1":
        raise TypedEvidenceError("HTTP source adapter is unsupported")
    parsed = urlsplit(str(source["base_url"]))
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TypedEvidenceError("HTTP source base_url is unsafe")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise TypedEvidenceError("HTTP source requires loopback or verified TLS")
    if source["surface"] != context["surface"] or source["board_id"] != context["board_id"] or source["candidate_commit"] != context["candidate_commit"]:
        raise TypedEvidenceError("HTTP source binding does not match request")
    _runtime_check(source["runtime"], trust, source["base_url"])
    if not isinstance(source["methods"], list) or not source["methods"]:
        raise TypedEvidenceError("HTTP source methods are invalid")
    if not isinstance(source["headers"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in source["headers"].items()):
        raise TypedEvidenceError("HTTP source headers are invalid")
    if (
        not isinstance(source["select_allowlist"], list)
        or not source["select_allowlist"]
        or len(set(source["select_allowlist"])) != len(source["select_allowlist"])
        or any(not isinstance(pointer, str) or not pointer.startswith("/") for pointer in source["select_allowlist"])
    ):
        raise TypedEvidenceError("HTTP source selector allowlist is invalid")
    if not isinstance(source["timeout_seconds"], (int, float)) or not 0.1 <= source["timeout_seconds"] <= 30:
        raise TypedEvidenceError("HTTP source timeout is invalid")
    _require_context_bindings(
        source["response_bindings"],
        {"candidate_commit", "board_id", "surface", "entity", "run_id", "action_id"},
        "HTTP source",
    )
    if source["runtime_id"] not in source["response_bindings"].values():
        raise TypedEvidenceError("HTTP source lacks a runtime binding")
    return source_id, source


def _select_http_projection(
    document: Any, select: list[str], path: str, label: str, status: int = 200,
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for pointer in select:
        value = _pointer(document, pointer)
        # A first-run response contains one-time door credentials.  Do not put
        # that mapping in evidence; retain the exact pointer only when the
        # product returned null on an idempotent rerun.
        if (
            path == "/api/projects/add"
            and pointer == "/doors"
            and value is not None
        ):
            if not isinstance(value, dict):
                raise TypedEvidenceError(f"{label} doors result has an invalid shape")
            continue
        selected[pointer] = _safe_public(value, f"{label} selected value")
    _validate_fleet_project_projection(path, status, selected, label)
    return selected


def _http_call(
    source: dict[str, Any], context: dict[str, Any], spec: Any, label: str,
) -> dict[str, Any]:
    spec = _closed(spec, {"method", "path", "body", "select"}, f"{label} request")
    method = str(spec["method"]).upper()
    if method not in source["methods"] or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise TypedEvidenceError(f"{label} method is not allowlisted")
    path = spec["path"]
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "#" in path:
        raise TypedEvidenceError(f"{label} path is invalid")
    target = urljoin(str(source["base_url"]).rstrip("/") + "/", path.lstrip("/"))
    base = urlsplit(str(source["base_url"]))
    actual = urlsplit(target)
    if (actual.scheme, actual.hostname, actual.port) != (base.scheme, base.hostname, base.port):
        raise TypedEvidenceError(f"{label} target escapes trusted origin")
    body = spec["body"]
    data = None
    headers = dict(source["headers"])
    headers.update({
        "X-Pursers-Observation-Id": context["observation_id"],
        "X-Pursers-Run-Id": context["run_id"],
        "X-Pursers-Action-Id": context["action_id"],
        "X-Pursers-Entity-Id": context["entity"],
    })
    if body is not None:
        _safe_public(body, f"{label} request body")
        data = _json_bytes(body)
        headers["Content-Type"] = "application/json"
        headers["X-Pursers-Action-SHA256"] = hashlib.sha256(data).hexdigest()
    request = Request(target, data=data, headers=headers, method=method)
    status: int
    response_headers: Any
    raw: bytes
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=source["timeout_seconds"]) as response:
            status = response.status
            response_headers = response.headers
            raw = response.read(MAX_HTTP_BODY_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        response_headers = exc.headers
        raw = exc.read(MAX_HTTP_BODY_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise TypedEvidenceError(f"{label} trusted request failed") from exc
    if len(raw) > MAX_HTTP_BODY_BYTES:
        raise TypedEvidenceError(f"{label} response is too large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TypedEvidenceError(f"{label} response is not bounded JSON") from None
    _check_bindings(document, source["response_bindings"], context, f"{label} response")
    correlation_headers = {
        "observation_id": response_headers.get("X-Pursers-Observation-Id"),
        "run_id": response_headers.get("X-Pursers-Run-Id"),
        "action_id": response_headers.get("X-Pursers-Action-Id"),
        "entity": response_headers.get("X-Pursers-Entity-Id"),
    }
    expected_correlation = {key: context[key] for key in correlation_headers}
    if correlation_headers != expected_correlation:
        raise TypedEvidenceError(f"{label} response is not correlated to the request")
    select = spec["select"]
    if (
        not isinstance(select, list)
        or not select
        or len(select) > 64
        or len(set(select)) != len(select)
    ):
        raise TypedEvidenceError(f"{label} selectors are invalid")
    if not set(select) <= set(source["select_allowlist"]):
        raise TypedEvidenceError(f"{label} selector is not verifier-allowlisted")
    selected = _select_http_projection(document, select, path, label, status)
    if (
        data is not None
        and "/_evidence/action_sha256" in selected
        and selected["/_evidence/action_sha256"]
        != hashlib.sha256(data).hexdigest()
    ):
        raise TypedEvidenceError(f"{label} action digest does not match request")
    return {
        "method": method, "path": path, "status": status, "selected": selected,
        "response_sha256": hashlib.sha256(raw).hexdigest(), "correlation": correlation_headers,
    }


def _mcp_structured(result: Any, label: str) -> Any:
    if bool(getattr(result, "is_error", False)):
        raise TypedEvidenceError(f"{label} returned an MCP error")
    value = getattr(result, "structured_content", None)
    if value is None:
        raise TypedEvidenceError(f"{label} returned no structured content")
    return value


def _validate_mcp_attestation(
    attestation: Any,
    source: dict[str, Any],
    context: dict[str, Any],
    nonce: str,
) -> dict[str, Any]:
    attestation = _closed(attestation, MCP_ATTESTATION_KEYS, "MCP attestation")
    candidate = Path(str(source["candidate_source"])).resolve()
    expected = {
        "schema_version": SCHEMA_VERSION,
        "server_name": "On Board Personal",
        "version": attestation.get("version"),
        "build": source["candidate_source_sha256"],
        "candidate_commit": context["candidate_commit"],
        "candidate_source": str(candidate),
        "board_id": context["board_id"],
        "pid": attestation.get("pid"),
        "transport": "stdio",
        "nonce": nonce,
    }
    if (
        not isinstance(attestation["version"], str) or not attestation["version"]
        or not isinstance(attestation["pid"], int) or isinstance(attestation["pid"], bool)
        or attestation["pid"] <= 1
        or {key: attestation[key] for key in expected} != expected
        or not SHA256.fullmatch(str(attestation["signature"]))
    ):
        raise TypedEvidenceError("MCP attestation claims do not match verifier trust")
    payload = _json_bytes(expected)
    signature = hmac.new(
        Path(str(source["challenge_key"])).read_bytes(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, attestation["signature"]):
        raise TypedEvidenceError("MCP attestation authentication failed")
    return attestation


def _mcp_process_record(
    pid: int, source: dict[str, Any]
) -> dict[str, Any]:
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        text=True, capture_output=True, check=False, timeout=5,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    command_text = completed.stdout.strip()
    try:
        argv = shlex.split(command_text)
    except ValueError:
        argv = []
    executable = Path(str(source["command"])).resolve()
    if completed.returncode or not argv:
        raise TypedEvidenceError("MCP attested process command is unavailable")
    actual_executable = Path(argv[0]).resolve()
    allowed_executables = {executable}
    # Framework Python on macOS re-execs its Python.app binary. Derive that
    # launch target from the already digest-pinned interpreter instead of
    # weakening identity to a basename such as ``Python``.
    if PYTHON_EXECUTABLE_NAME.fullmatch(executable.name):
        identity = subprocess.run(
            [str(Path(str(source["command"])).absolute()), "-I", "-c", "import sys; print(sys.base_prefix)"],
            text=True, capture_output=True, check=False, timeout=5,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        )
        lines = identity.stdout.splitlines()
        if identity.returncode or len(lines) != 1 or not Path(lines[0]).is_absolute():
            raise TypedEvidenceError("MCP pinned Python runtime identity is unavailable")
        framework_executable = (
            Path(lines[0]) / "Resources/Python.app/Contents/MacOS/Python"
        ).resolve()
        if framework_executable.is_file():
            allowed_executables.add(framework_executable)
    if actual_executable not in allowed_executables:
        raise TypedEvidenceError("MCP attested process executable does not match source trust")
    if argv[1:] != source["args"]:
        raise TypedEvidenceError("MCP attested process argv does not match source trust")
    if _process_cwd(pid, "MCP process") != Path(str(source["cwd"])).resolve():
        raise TypedEvidenceError("MCP attested process cwd does not match source trust")
    return {
        "pid": pid,
        "argv_sha256": hashlib.sha256(command_text.encode()).hexdigest(),
        "executable_sha256": source["command_sha256"],
        "candidate_source_sha256": source["candidate_source_sha256"],
    }


async def _execute_mcp_tool(
    source: dict[str, Any], context: dict[str, Any], select: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
    except ImportError as exc:
        raise TypedEvidenceError("MCP client runtime is unavailable") from exc
    params = StdioServerParameters(
        # Preserve a verifier-pinned virtual-environment launcher path. Resolving
        # its interpreter symlink before exec drops Python's venv identity and
        # can load an unrelated system environment; process verification below
        # still binds argv[0] to the pinned resolved executable and digest.
        command=str(Path(str(source["command"])).expanduser().absolute()),
        args=list(source["args"]),
        env=dict(source["env"]),
        cwd=str(Path(str(source["cwd"])).resolve()),
    )
    nonce = secrets.token_hex(32)
    try:
        async with Client(
            params,
            mode="2026-07-28",
            read_timeout_seconds=float(source["timeout_seconds"]),
        ) as client:
            attestation_result = await client.call_tool(
                "acceptance_runtime_attest", {"nonce": nonce}
            )
            attestation = _validate_mcp_attestation(
                _mcp_structured(attestation_result, "MCP attestation"),
                source, context, nonce,
            )
            process = _mcp_process_record(attestation["pid"], source)
            result = await client.call_tool(source["tool"], dict(source["arguments"]))
            document = _mcp_structured(result, f"MCP tool {source['tool']}")
    except TypedEvidenceError:
        raise
    except Exception as exc:
        raise TypedEvidenceError("trusted MCP stdio request failed") from exc
    selected = {
        pointer: _safe_public(_pointer(document, pointer), "MCP selected value")
        for pointer in select
    }
    public_attestation = {
        key: value
        for key, value in attestation.items()
        if key != "candidate_source"
    }
    public_attestation["candidate_source_sha256"] = source["candidate_source_sha256"]
    return process, public_attestation, {
        "tool": source["tool"],
        "arguments_sha256": _digest(source["arguments"]),
        "selected": selected,
        "result_sha256": _digest(document),
        "correlation": {
            key: context[key]
            for key in ("observation_id", "run_id", "action_id", "entity")
        },
    }


def _mcp_call(
    source: dict[str, Any], context: dict[str, Any], spec: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    spec = _closed(spec, {"tool", "arguments", "select"}, "MCP request")
    if (
        spec["tool"] != source["tool"]
        or spec["arguments"] != source["arguments"]
        or not isinstance(spec["select"], list) or not spec["select"]
        or len(spec["select"]) != len(set(spec["select"]))
        or not set(spec["select"]) <= set(source["select_allowlist"])
    ):
        raise TypedEvidenceError("MCP request differs from verifier-owned tool recipe")
    try:
        return asyncio.run(_execute_mcp_tool(source, context, spec["select"]))
    except RuntimeError as exc:
        raise TypedEvidenceError("MCP recorder requires a synchronous verifier process") from exc


def _record_http(request: dict[str, Any], trust: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    recorder = _closed(request["recorder"], {"source_id", "request", "action_origin"}, "http_response recorder")
    if recorder["action_origin"] != "verifier_api":
        raise TypedEvidenceError("HTTP action_origin is invalid")
    source_id, source = _http_source(recorder["source_id"], trust, context)
    record = {
        "action_origin": recorder["action_origin"],
        "runtime": _runtime_check(source["runtime"], trust, source["base_url"]),
        "response": _http_call(source, context, recorder["request"], "HTTP"),
    }
    return _base_source(source_id, source, trust), record


def _record_mcp(
    request: dict[str, Any], trust: dict[str, Any], context: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    recorder = _closed(
        request["recorder"], {"source_id", "tool", "arguments", "select"},
        "mcp_tool_response recorder",
    )
    source_id, source = _mcp_source(recorder["source_id"], trust, context)
    process, attestation, result = _mcp_call(
        source, context,
        {
            "tool": recorder["tool"],
            "arguments": recorder["arguments"],
            "select": recorder["select"],
        },
    )
    return _base_source(source_id, source, trust), {
        "process": process,
        "transport": "stdio",
        "attestation": attestation,
        "result": result,
    }


def _record_receipt(request: dict[str, Any], trust: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    recorder = _closed(request["recorder"], {"source_id", "fields"}, "receipt_field recorder")
    source_id, source = _source(trust, "receipt_sources", recorder["source_id"])
    adapter = source.get("adapter")
    common_keys = {
        "adapter", "provenance", "runtime_id", "path", "max_age_seconds",
        "required_bindings", "transport_pointer", "transport", "process",
        "document_keys",
    }
    if adapter == "hmac_json_v1":
        _closed(
            source,
            common_keys
            | {
                "hmac_key_hex", "signature_field", "timestamp_pointer",
                "issuer_pointer", "issuer", "runtime_pointer",
            },
            "receipt source",
        )
    elif adapter == "personal_runtime_receipt_v1":
        _closed(
            source,
            common_keys
            | {
                "candidate_source", "candidate_source_sha256",
                "product", "server_name", "version",
            },
            "receipt source",
        )
    else:
        raise TypedEvidenceError("receipt adapter is unsupported")
    path = Path(str(source["path"])).resolve()
    if (
        not path.is_absolute() or not path.is_file()
        or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid()
    ):
        raise TypedEvidenceError("receipt source is unavailable or not private")
    try:
        raw_receipt = path.read_bytes()
        if len(raw_receipt) > MAX_CONFIG_BYTES:
            raise TypedEvidenceError("receipt source is too large")
        receipt = json.loads(raw_receipt)
    except (OSError, json.JSONDecodeError) as exc:
        raise TypedEvidenceError("receipt source is not readable JSON") from exc
    if not isinstance(receipt, dict):
        raise TypedEvidenceError("receipt source must be an object")
    document_keys = source["document_keys"]
    if (
        not isinstance(document_keys, list) or not document_keys
        or len(set(document_keys)) != len(document_keys)
        or any(not isinstance(field, str) for field in document_keys)
    ):
        raise TypedEvidenceError("receipt document fields do not match schema")
    if adapter == "hmac_json_v1":
        signature_field = source["signature_field"]
        if (
            not isinstance(signature_field, str)
            or set(receipt) != set(document_keys) | {signature_field}
        ):
            raise TypedEvidenceError("receipt document fields do not match schema")
        expected = hmac.new(
            _key_bytes(source["hmac_key_hex"], "receipt HMAC key"),
            _json_bytes(_without_member(receipt, signature_field)),
            hashlib.sha256,
        ).hexdigest()
        if not isinstance(receipt[signature_field], str) or not hmac.compare_digest(expected, receipt[signature_field]):
            raise TypedEvidenceError("receipt authenticity verification failed")
        _fresh(_pointer(receipt, source["timestamp_pointer"]), source["max_age_seconds"], "receipt timestamp")
        if _pointer(receipt, source["issuer_pointer"]) != source["issuer"]:
            raise TypedEvidenceError("receipt issuer does not match verifier trust")
        if _pointer(receipt, source["runtime_pointer"]) != source["runtime_id"]:
            raise TypedEvidenceError("receipt runtime does not match verifier trust")
        authenticity = "canonical_hmac_sha256"
    else:
        if set(receipt) != set(document_keys):
            raise TypedEvidenceError("receipt document fields do not match schema")
        candidate_source = Path(str(source["candidate_source"])).resolve()
        checkout = Path(str(trust["candidate_checkout_root"])).resolve()
        head = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=5,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        )
        expected_receipt = {
            "schema_version": SCHEMA_VERSION,
            "product": source["product"],
            "server_name": source["server_name"],
            "version": source["version"],
            "build": source["candidate_source_sha256"],
            "candidate_commit": context["candidate_commit"],
            "candidate_source": str(candidate_source),
            "board_id": context["board_id"],
            "pid": receipt.get("pid"),
            "transport": source["transport"],
        }
        if (
            head.returncode or head.stdout.strip() != trust["candidate_commit"]
            or not candidate_source.is_file()
            or not candidate_source.is_relative_to(checkout)
            or hashlib.sha256(candidate_source.read_bytes()).hexdigest()
            != source["candidate_source_sha256"]
            or not isinstance(receipt.get("pid"), int)
            or isinstance(receipt.get("pid"), bool)
            or receipt != expected_receipt
        ):
            raise TypedEvidenceError("Personal runtime receipt binding mismatch")
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        if age < -30 or age > source["max_age_seconds"]:
            raise TypedEvidenceError("Personal runtime receipt is stale")
        authenticity = "verifier_bound_personal_runtime"
    if _pointer(receipt, source["transport_pointer"]) != source["transport"]:
        raise TypedEvidenceError("receipt transport does not match verifier trust")
    _require_context_bindings(
        source["required_bindings"],
        (
            {"candidate_commit", "board_id", "surface", "entity", "run_id", "action_id"}
            if adapter == "hmac_json_v1"
            else {"candidate_commit", "board_id"}
        ),
        "receipt source",
    )
    _check_bindings(receipt, source["required_bindings"], context, "receipt")
    process = _process_check(source["process"], receipt)
    requested = recorder["fields"]
    if not isinstance(requested, list) or not requested or len(requested) > 32 or len(set(requested)) != len(requested):
        raise TypedEvidenceError("receipt fields are invalid")
    selected = {pointer: _safe_public(_pointer(receipt, pointer), "receipt selected value") for pointer in requested}
    record = {
        "fields": selected, "receipt_sha256": hashlib.sha256(raw_receipt).hexdigest(),
        "authenticity": authenticity, "process": process,
    }
    return _base_source(source_id, source, trust), record


def _fleet_source_contract(source: Any) -> list[str]:
    source = _closed(source, set(FLEET_LOG_SOURCE_KEYS), "Fleet trace source")
    if (
        source["adapter"] != "fleet_evidence_trace_v1"
        or source["emitter"] != "fleet-dashboard-runtime"
        or not isinstance(source["document_keys"], list)
        or set(source["document_keys"]) != FLEET_TRACE_KEYS
        or len(source["document_keys"]) != len(FLEET_TRACE_KEYS)
    ):
        raise TypedEvidenceError("Fleet trace document schema is invalid")
    if source["action_path"] not in FLEET_ACTION_RESPONSE_POINTERS:
        raise TypedEvidenceError("Fleet trace action path is not allowlisted")
    fixed_pointers = {
        "timestamp_pointer": "/timestamp",
        "runtime_pointer": "/runtime_id",
        "action_digest_pointer": "/action_sha256",
        "schema_version_pointer": "/schema_version",
        "pid_pointer": "/pid",
        "entrypoint_digest_pointer": "/entrypoint_sha256",
        "status_pointer": "/status",
        "changed_pointer": "/changed",
        "outcome_pointer": "/outcome",
        "effect_pointer": "/effect",
    }
    sha256_pointers = source["sha256_pointers"]
    if (
        {field: source[field] for field in fixed_pointers} != fixed_pointers
        or not isinstance(sha256_pointers, list)
        or len(sha256_pointers) != 5
        or len(set(sha256_pointers)) != len(sha256_pointers)
        or set(sha256_pointers) != {
            "/before_sha256", "/after_sha256", "/result_sha256",
            "/action_sha256", "/entrypoint_sha256",
        }
    ):
        raise TypedEvidenceError("Fleet trace pointer contract is invalid")
    return sha256_pointers


def _validate_fleet_entry(
    entry: Any,
    source: dict[str, Any],
    runtime: dict[str, Any],
    context: dict[str, Any],
) -> None:
    if not isinstance(entry, dict) or set(entry) != FLEET_TRACE_KEYS:
        raise TypedEvidenceError("Fleet trace entry schema is invalid")
    status = entry["status"]
    changed = entry["changed"]
    outcome = entry["outcome"]
    effect = entry["effect"]
    if (
        entry["schema_version"] != SCHEMA_VERSION
        or entry["emitter"] != "fleet-dashboard-runtime"
        or entry["runtime_id"] != source["runtime_id"]
        or entry["pid"] != runtime["pid"]
        or entry["entrypoint_sha256"] != runtime["artifact_sha256"]
        or entry["action_sha256"] != source["action_input_sha256"]
        or entry["method"] != "POST"
        or entry["path"] != source["action_path"]
        or not isinstance(status, int) or isinstance(status, bool)
        or not 100 <= status <= 599
        or not isinstance(changed, bool)
    ):
        raise TypedEvidenceError("Fleet trace entry provenance is invalid")
    _safe_id(outcome, "Fleet trace outcome")
    _safe_id(effect, "Fleet trace effect")
    expected_effect = {
        "/api/attention": (
            "attention_state_changed" if changed else "attention_state_unchanged"
        ),
        "/api/projects/add": (
            "project_state_changed" if changed else "project_state_unchanged"
        ),
    }[source["action_path"]]
    if (
        outcome != ("succeeded" if 200 <= status < 300 else "failed")
        or effect != expected_effect
        or changed != (entry["before_sha256"] != entry["after_sha256"])
        or any(
            not SHA256.fullmatch(str(_pointer(entry, pointer)))
            for pointer in source["sha256_pointers"]
        )
    ):
        raise TypedEvidenceError("Fleet trace entry outcome is inconsistent")
    _fresh(entry["timestamp"], source["max_age_seconds"], "log timestamp")
    _check_bindings(entry, source["required_bindings"], context, "log entry")


def _record_log(request: dict[str, Any], trust: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    recorder = _closed(request["recorder"], {"source_id", "field_equals"}, "log_assertion recorder")
    source_id, source = _source(trust, "log_sources", recorder["source_id"])
    adapter = source.get("adapter")
    if adapter == "process_captured_jsonl_v1":
        keys = set(LOG_COMMON_KEYS) | {"process"}
    elif adapter == "fleet_evidence_trace_v1":
        keys = set(FLEET_LOG_SOURCE_KEYS)
    else:
        raise TypedEvidenceError("log adapter is unsupported")
    _closed(source, keys, "log source")
    _require_context_bindings(
        source["required_bindings"],
        (
            {
                "candidate_commit", "board_id", "surface", "observation_id",
                "entity", "run_id", "action_id",
            }
            if adapter == "fleet_evidence_trace_v1"
            else {
                "candidate_commit", "board_id", "surface", "entity",
                "run_id", "action_id",
            }
        ),
        "log source",
    )
    configured_path = Path(str(source["path"])).expanduser()
    if not configured_path.is_absolute():
        raise TypedEvidenceError("log source path is not absolute")
    path = configured_path.resolve()
    configured_action_path = Path(str(source["action_input_path"])).expanduser()
    if not configured_action_path.is_absolute():
        raise TypedEvidenceError("log action input path is not absolute")
    action_path = configured_action_path.resolve()
    if (
        not action_path.is_absolute() or not action_path.is_file()
        or action_path.stat().st_mode & 0o077
        or action_path.stat().st_uid != os.getuid()
        or not SHA256.fullmatch(str(source["action_input_sha256"]))
        or hashlib.sha256(action_path.read_bytes()).hexdigest()
        != source["action_input_sha256"]
    ):
        raise TypedEvidenceError("log action input is not verifier-pinned")
    process = None
    runtime = None
    action_response = None
    if adapter == "process_captured_jsonl_v1":
        process = _process_check(source["process"])
        if process is None:
            raise TypedEvidenceError("log capture requires a bound emitter process")
    else:
        try:
            action_raw = action_path.read_bytes()
            action_document = json.loads(action_raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise TypedEvidenceError("Fleet trace action input is not JSON") from exc
        _fleet_source_contract(source)
        document_keys = source["document_keys"]
        if (
            not isinstance(action_document, dict)
            or action_raw != _json_bytes(action_document)
            or set(action_document) & set(document_keys)
        ):
            raise TypedEvidenceError(
                "Fleet trace action input is non-canonical or contains producer-owned fields"
            )
        _, http_source = _http_source(
            source["http_source_id"], trust, context
        )
        if (
            source["runtime_id"] != http_source["runtime_id"]
            or source["http_source_config_sha256"] != _digest(http_source)
        ):
            raise TypedEvidenceError(
                "Fleet trace source does not match its trusted HTTP runtime"
            )
        runtime = _runtime_check(
            http_source["runtime"], trust, http_source["base_url"]
        )
    limit = source["max_bytes"]
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LOG_BYTES:
        raise TypedEvidenceError("log max_bytes is invalid")
    before = b""
    if path.exists():
        info = path.stat()
        if (
            not path.is_file() or info.st_mode & 0o077
            or info.st_uid != os.getuid()
        ):
            raise TypedEvidenceError("log source is unavailable or not private")
        if adapter == "fleet_evidence_trace_v1":
            before = path.read_bytes()
            if len(before) > limit or (before and not before.endswith(b"\n")):
                raise TypedEvidenceError("Fleet trace baseline is invalid")
    elif adapter == "process_captured_jsonl_v1":
        raise TypedEvidenceError("log source is unavailable or not private")
    elif (
        not path.parent.is_dir()
        or path.parent.stat().st_uid != os.getuid()
        or path.parent.stat().st_mode & 0o077
    ):
        raise TypedEvidenceError("Fleet trace directory is unavailable or not private")
    if adapter == "fleet_evidence_trace_v1":
        response_selectors = FLEET_ACTION_RESPONSE_POINTERS[source["action_path"]]
        if not response_selectors <= set(
            http_source["select_allowlist"]
        ):
            raise TypedEvidenceError(
                "Fleet trace HTTP source lacks exact response selectors"
            )
        action_response = _http_call(
            http_source,
            context,
            {
                "method": "POST", "path": source["action_path"],
                "body": action_document,
                "select": sorted(response_selectors),
            },
            "Fleet trace action",
        )
        if (
            not path.is_file() or path.stat().st_mode & 0o077
            or path.stat().st_uid != os.getuid()
        ):
            raise TypedEvidenceError("Fleet trace action produced no private log")
        after = path.read_bytes()
        if (
            len(after) > limit or not after.startswith(before)
            or len(after) == len(before) or not after.endswith(b"\n")
        ):
            raise TypedEvidenceError("Fleet trace append is invalid")
        raw_lines = after[len(before):].splitlines()
    else:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > limit:
                stream.seek(size - limit)
                stream.readline()
            raw_lines = stream.read(limit + 1).splitlines()
    if sum(len(line) for line in raw_lines) > limit:
        raise TypedEvidenceError("bounded log capture exceeded")
    filters = recorder["field_equals"]
    if not isinstance(filters, dict) or len(filters) > 16:
        raise TypedEvidenceError("log field_equals is invalid")
    matches: list[tuple[dict[str, Any], bytes]] = []
    for raw in raw_lines:
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("emitter") != source["emitter"]:
            continue
        document_keys = source["document_keys"]
        if (
            not isinstance(document_keys, list)
            or not document_keys or len(set(document_keys)) != len(document_keys)
            or any(not isinstance(field, str) for field in document_keys)
            or set(entry) != set(document_keys)
        ):
            continue
        try:
            if _pointer(entry, source["runtime_pointer"]) != source["runtime_id"]:
                continue
            if (
                _pointer(entry, source["action_digest_pointer"])
                != source["action_input_sha256"]
            ):
                continue
            if adapter == "fleet_evidence_trace_v1":
                _validate_fleet_entry(entry, source, runtime, context)
            else:
                _fresh(_pointer(entry, source["timestamp_pointer"]), source["max_age_seconds"], "log timestamp")
                _check_bindings(entry, source["required_bindings"], context, "log entry")
            if all(_pointer(entry, pointer) == expected_value for pointer, expected_value in filters.items()):
                matches.append((entry, raw))
        except TypedEvidenceError:
            continue
    if len(matches) != 1:
        raise TypedEvidenceError("log capture needs exactly one authentic correlated entry")
    entry, raw = matches[0]
    if adapter == "fleet_evidence_trace_v1":
        expected_selected = {
            f"/_evidence/{key}": value for key, value in entry.items()
        }
        expected_selected["/_evidence/log_emitted"] = True
        result_values = {
            pointer: action_response["selected"][pointer]
            for pointer in FLEET_ACTION_RESULT_POINTERS[source["action_path"]]
            if pointer in action_response["selected"]
        }
        expected_selected.update(result_values)
        if (
            action_response["status"] != entry["status"]
            or action_response["selected"] != expected_selected
        ):
            raise TypedEvidenceError(
                "Fleet trace action response does not match its log entry"
            )
        if source["action_path"] == "/api/attention" and (
            _digest({"items": result_values["/items"]}) != entry["after_sha256"]
            or _digest({"items": result_values["/items"]})
            != entry["result_sha256"]
        ):
            raise TypedEvidenceError(
                "Fleet attention response does not match its state digests"
            )
    _safe_public(entry, "log entry")
    record = {
        "entry": entry,
        "entry_sha256": hashlib.sha256(raw).hexdigest(),
        "authenticity": (
            "fleet_runtime_trace_bound"
            if adapter == "fleet_evidence_trace_v1"
            else "verifier_captured_process_bound"
        ),
    }
    if adapter == "fleet_evidence_trace_v1":
        record["runtime"] = runtime
        record["action_response"] = action_response
    else:
        record["process"] = process
    return _base_source(source_id, source, trust), record


def _browser_phase(
    value: Any,
    source: dict[str, Any],
    context: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    result = _validate_http_result(value, source, f"browser {phase} record")
    expected_method = "POST" if phase == "action" else "GET"
    expected_correlation = {
        key: context[key]
        for key in ("observation_id", "run_id", "action_id", "entity")
    }
    expected_paths = {
        item["path"]
        for item in (
            source["recipe"][phase]
            if phase != "action" else []
        )
    }
    if phase == "action":
        expected_paths = set().union(*(
            _browser_action_result_paths(item)
            for item in source["recipe"]["actions"]
        ))
    if (
        result["method"] != expected_method
        or result["path"] != f"/browser/state/{phase}"
        or result["status"] != 200
        or result["correlation"] != expected_correlation
        or set(result["selected"]) != expected_paths
    ):
        raise TypedEvidenceError(f"browser {phase} result differs from verifier recipe")
    if phase == "action":
        pending = [
            item for item in source["recipe"]["actions"]
            if item["kind"] == "click_pending_state"
        ]
        if pending:
            item = pending[0]
            selected = result["selected"]
            if (
                selected[item["path"]] != "clicked"
                or selected[item["pending_path"]] is not True
                or selected[item["settled_path"]] is not True
                or not isinstance(selected[item["status_path"]], int)
                or isinstance(selected[item["status_path"]], bool)
                or not 100 <= selected[item["status_path"]] <= 599
                or not SHA256.fullmatch(str(selected[item["response_sha256_path"]]))
                or selected[item["error_path"]] is not None
            ):
                raise TypedEvidenceError("browser pending-state result is invalid")
    return result


def _browser_transition_call(
    source: dict[str, Any], context: dict[str, Any], trust: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "context": context,
        "surface_id": source["surface"],
        "target": {"base_url": source["base_url"], "board_id": source["board_id"]},
        "candidate_commit": source["candidate_commit"],
        "page_url": source["page_url"],
        "recipe": source["recipe"],
    }
    environment = {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        **source["env"],
    }
    try:
        completed = subprocess.run(
            [str(Path(str(source["command"])).resolve()), "transition"],
            input=json.dumps(payload, sort_keys=True),
            text=True, capture_output=True, check=False,
            timeout=float(source["timeout_seconds"]),
            cwd=Path(str(source["command"])).resolve().parent,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TypedEvidenceError("trusted browser state command failed") from exc
    if completed.returncode or len(completed.stdout.encode()) > MAX_CONFIG_BYTES:
        raise TypedEvidenceError("trusted browser state command returned no valid result")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise TypedEvidenceError("trusted browser state command returned invalid JSON") from None
    expected_keys = {
        "schema_version", "context", "surface_id", "target", "candidate_commit",
        "page_url", "runtime", "before", "action", "after", "order",
    }
    if not isinstance(result, dict) or set(result) != expected_keys:
        raise TypedEvidenceError("trusted browser state result fields do not match schema")
    if (
        result["schema_version"] != SCHEMA_VERSION
        or result["context"] != context
        or result["surface_id"] != source["surface"]
        or result["target"] != payload["target"]
        or result["candidate_commit"] != source["candidate_commit"]
        or result["page_url"] != source["page_url"]
    ):
        raise TypedEvidenceError("trusted browser state result binding changed")
    runtime = _closed(
        result["runtime"], {"product", "version", "build", "source"},
        "browser runtime binding",
    )
    for field in runtime.values():
        if not isinstance(field, str) or not field:
            raise TypedEvidenceError("browser runtime binding is invalid")
    order = _closed(
        result["order"], {"before_at", "action_at", "after_at"},
        "browser state order",
    )
    moments = [
        _timestamp(order[field], f"browser state {field}")
        for field in ("before_at", "action_at", "after_at")
    ]
    if moments != sorted(moments):
        raise TypedEvidenceError("browser state causal order is invalid")
    action = _browser_phase(result["action"], source, context, "action")
    if source["adapter"] == "aionui_assistant_binding_v1":
        action_spec = source["recipe"]["actions"][0]
        raw = action["selected"][action_spec["path"]]
        raw = _closed(
            raw, {"transport", "endpoint", "status", "assistant"},
            "runtime assistant binding",
        )
        runtime_assistant = _closed(
            raw["assistant"], {
                "id", "name", "description", "avatar", "agentId", "context",
                "models", "enabledSkills", "prompts", "isPreset", "isBuiltin",
                "enabled", "_source", "_extensionName", "_kind",
            }, "runtime assistant",
        )
        manifest_assistant, context_bytes, extension_name = _assistant_manifest_binding(
            source, trust, action_spec["assistant_id"]
        )
        expected_runtime = {
            "id": f"ext-{manifest_assistant['id']}",
            "name": manifest_assistant["name"],
            "description": manifest_assistant["description"],
            "avatar": None,
            "agentId": manifest_assistant["agentId"],
            "context": context_bytes.decode("utf-8"),
            "models": [], "enabledSkills": [], "prompts": [],
            "isPreset": True, "isBuiltin": False, "enabled": True,
            "_source": "extension", "_extensionName": extension_name,
            "_kind": "assistant",
        }
        if (
            raw["transport"] != "same-origin-http"
            or raw["endpoint"] != "/api/extensions/assistants"
            or raw["status"] != 200
            or runtime_assistant != expected_runtime
        ):
            raise TypedEvidenceError("runtime assistant differs from installed candidate")
        compact = _assistant_public_binding(
            source, trust, action_spec["assistant_id"]
        )
        action["selected"] = {action_spec["path"]: compact}
        action["response_sha256"] = _digest(action["selected"])
    return {
        "before": _browser_phase(result["before"], source, context, "before"),
        "action": action,
        "after": _browser_phase(result["after"], source, context, "after"),
        "order": order,
        "observer": {
            "command_sha256": source["command_sha256"],
            "config_sha256": source["config_sha256"],
            "runtime": runtime,
            "page_url": result["page_url"],
        },
    }


def _record_transition(request: dict[str, Any], trust: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    recorder = _closed(request["recorder"], {"source_id", "before", "action", "after"}, "state_transition recorder")
    raw_source = trust["state_sources"].get(recorder["source_id"])
    adapter = raw_source.get("adapter") if isinstance(raw_source, dict) else None
    if adapter in {"trusted_browser_state_v1", "aionui_assistant_binding_v1"}:
        source_id, source = _browser_state_source(
            recorder["source_id"], trust, context
        )
        recipe = source["recipe"]
        if (
            recorder["before"] != recipe["before"]
            or recorder["action"] != recipe["actions"]
            or recorder["after"] != recipe["after"]
        ):
            raise TypedEvidenceError(
                "browser state recorder differs from verifier-owned recipe"
            )
        return _base_source(source_id, source, trust), _browser_transition_call(
            source, context, trust
        )
    source_id, source = _source(trust, "state_sources", recorder["source_id"])
    _closed(
        source,
        {
            "adapter", "provenance", "runtime_id", "http_source_id",
            "http_source_config_sha256",
        },
        "state source",
    )
    if source["adapter"] != "trusted_http_state_v1":
        raise TypedEvidenceError("state adapter is unsupported")
    http_id, http_source = _http_source(source["http_source_id"], trust, context)
    if (
        source["runtime_id"] != http_source["runtime_id"]
        or source["http_source_config_sha256"] != _digest(http_source)
    ):
        raise TypedEvidenceError("state source does not match its trusted HTTP runtime")
    before_at = _now_text()
    before = _http_call(http_source, context, recorder["before"], "state before")
    action_at = _now_text()
    action = _http_call(http_source, context, recorder["action"], "state action")
    after_at = _now_text()
    after = _http_call(http_source, context, recorder["after"], "state after")
    record = {
        "before": before, "action": action, "after": after,
        "order": {"before_at": before_at, "action_at": action_at, "after_at": after_at},
        "http_source_id": http_id,
        "http_source_config_sha256": _digest(http_source),
        "runtime": _runtime_check(http_source["runtime"], trust, http_source["base_url"]),
    }
    return _base_source(source_id, source, trust), record


def record_evidence(request: Any, trust_config: Any) -> dict[str, Any]:
    """Execute one trusted recorder and return authenticated evidence."""
    trust = _trust(trust_config)
    request = _closed(request, {"schema_version", "kind", "context", "recorder"}, "record request")
    if request["schema_version"] != SCHEMA_VERSION or request["kind"] not in KINDS:
        raise TypedEvidenceError("record request schema is unsupported")
    context = _context(request, trust)
    functions = {
        "http_response": _record_http,
        "mcp_tool_response": _record_mcp,
        "receipt_field": _record_receipt,
        "log_assertion": _record_log,
        "state_transition": _record_transition,
    }
    source, record = functions[request["kind"]](request, trust, context)
    _safe_public(record, "evidence record")
    unsigned = {
        "schema_version": SCHEMA_VERSION, "kind": request["kind"], "context": context,
        "captured_at": _now_text(), "source": source, "record": record,
        "payload_sha256": _digest(record),
    }
    return _sign_evidence(unsigned, trust)


def _compare(actual: Any, assertion: Any) -> bool:
    assertion = _closed(assertion, {"op", "value"}, "typed assertion")
    op = assertion["op"]
    expected = assertion["value"]
    if op == "eq":
        return _same_json_type(actual, expected) and actual == expected
    if op == "ne":
        return not (_same_json_type(actual, expected) and actual == expected)
    if op == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return any(
                _same_json_type(item, expected) and item == expected
                for item in actual
            )
        if isinstance(actual, dict) and isinstance(expected, str):
            return expected in actual
        raise TypedEvidenceError("contains operands have incompatible JSON types")
    if op == "in":
        if not isinstance(expected, list):
            raise TypedEvidenceError("in expected value must be an array")
        return any(
            _same_json_type(actual, item) and actual == item
            for item in expected
        )
    if op in {"gt", "gte", "lt", "lte"} and isinstance(actual, (int, float)) and not isinstance(actual, bool) and isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return {"gt": actual > expected, "gte": actual >= expected, "lt": actual < expected, "lte": actual <= expected}[op]
    raise TypedEvidenceError("typed assertion operator is unsupported")


def _field_check(container: Any, conjunct: Any, label: str) -> dict[str, Any]:
    conjunct = _closed(conjunct, {"path", "op", "value"}, f"{label} conjunct")
    actual = _pointer(container, conjunct["path"])
    assertion = {"op": conjunct["op"], "value": conjunct["value"]}
    return {"path": conjunct["path"], "op": conjunct["op"], "passed": _compare(actual, assertion)}


def _consume_replay(evidence_id: str, replay: dict[str, Any]) -> None:
    if not replay["consume"]:
        return
    path = Path(str(replay["path"])).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise TypedEvidenceError("replay journal is unavailable") from exc
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        os.close(descriptor)
        raise TypedEvidenceError("replay journal is not a private verifier file")
    try:
        with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            seen = {line.strip() for line in stream if line.strip()}
            if evidence_id in seen:
                raise TypedEvidenceError("evidence replay detected")
            stream.write(evidence_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        raise


def evaluate_evidence(evidence: Any, expected: Any, trust_config: Any) -> dict[str, Any]:
    """Evaluate an authenticated record against an exact closed all_of list."""
    trust = _trust(trust_config)
    evidence = _verify_evidence(evidence, trust)
    expected = _closed(expected, {"schema_version", "kind", "context", "all_of"}, "expected predicate")
    if expected["schema_version"] != SCHEMA_VERSION or expected["kind"] != evidence["kind"]:
        raise TypedEvidenceError("expected predicate kind does not match evidence")
    expected_context = _closed(expected["context"], CONTEXT_KEYS, "expected context")
    if expected_context != evidence["context"]:
        raise TypedEvidenceError("expected context does not match evidence")
    conjuncts = expected["all_of"]
    if not isinstance(conjuncts, list) or not conjuncts or len(conjuncts) > 64:
        raise TypedEvidenceError("all_of must contain 1-64 conjuncts")
    checks: list[dict[str, Any]] = []
    kind = evidence["kind"]
    record = evidence["record"]
    for conjunct in conjuncts:
        if kind == "http_response":
            conjunct = _closed(conjunct, {"target", "path", "op", "value"}, "http_response conjunct")
            if conjunct["target"] == "status":
                actual = record["response"]["status"]
            elif conjunct["target"] == "action_origin":
                actual = record["action_origin"]
            elif conjunct["target"] == "field":
                actual = record["response"]["selected"].get(conjunct["path"], object())
            else:
                raise TypedEvidenceError("http_response conjunct target is unsupported")
            passed = _compare(actual, {"op": conjunct["op"], "value": conjunct["value"]})
            checks.append({"target": conjunct["target"], "path": conjunct["path"], "op": conjunct["op"], "passed": passed})
        elif kind == "mcp_tool_response":
            conjunct = _closed(
                conjunct, {"path", "op", "value"},
                "mcp_tool_response conjunct",
            )
            actual = record["result"]["selected"].get(conjunct["path"], object())
            passed = _compare(
                actual, {"op": conjunct["op"], "value": conjunct["value"]}
            )
            checks.append({
                "path": conjunct["path"], "op": conjunct["op"],
                "passed": passed,
            })
        elif kind == "receipt_field":
            conjunct = _closed(conjunct, {"path", "op", "value"}, "receipt_field conjunct")
            actual = record["fields"].get(conjunct["path"], object())
            passed = _compare(actual, {"op": conjunct["op"], "value": conjunct["value"]})
            checks.append({"path": conjunct["path"], "op": conjunct["op"], "passed": passed})
        elif kind == "log_assertion":
            checks.append(_field_check(record["entry"], conjunct, "log_assertion"))
        else:
            conjunct = _closed(conjunct, {"phase", "path", "op", "value"}, "state_transition conjunct")
            if conjunct["phase"] not in {"before", "action", "after"}:
                raise TypedEvidenceError("state_transition phase is unsupported")
            phase = record[conjunct["phase"]]
            actual = phase["status"] if conjunct["path"] == "/status" else phase["selected"].get(conjunct["path"], object())
            passed = _compare(actual, {"op": conjunct["op"], "value": conjunct["value"]})
            checks.append({"phase": conjunct["phase"], "path": conjunct["path"], "op": conjunct["op"], "passed": passed})
    passed = all(check["passed"] for check in checks)
    evidence_id = _digest({"payload_sha256": evidence["payload_sha256"], "auth": evidence["auth"]})
    if passed:
        _consume_replay(evidence_id, trust["replay_guard"])
    context = evidence["context"]
    return {
        "schema_version": SCHEMA_VERSION, "kind": kind,
        "observation_id": context["observation_id"], "passed": passed, "checks": checks,
        "correlation": {
            "run_id": context["run_id"], "action_id": context["action_id"],
            "entity": context["entity"], "surface": context["surface"],
            "board_id": context["board_id"], "candidate_commit": context["candidate_commit"],
            "causal_index": context["causal_index"], "runtime_id": evidence["source"]["runtime_id"],
        },
        "expected_digest": _digest(expected), "evidence_id": evidence_id,
    }


def _normalized_semantic_text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "".join(character.lower() for character in text if character.isalnum())


def _semantic_contains(value: Any, expected: str) -> bool:
    needle = _normalized_semantic_text(expected)
    return bool(needle) and needle in _normalized_semantic_text(value)


def _evaluate_parent_fielded_conjunct(
    conjunct: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    """Evaluate the canonical parent form with exact typed paths and operators."""
    kind = evidence["kind"]
    conjunct = _closed(
        conjunct, {"kind", "source_id", "assertions"},
        f"parent fielded {kind} conjunct",
    )
    if conjunct["kind"] != kind or conjunct["source_id"] != evidence["source"]["source_id"]:
        raise TypedEvidenceError("parent conjunct source does not match evidence")
    assertions = conjunct["assertions"]
    if not isinstance(assertions, list) or not assertions or len(assertions) > 64:
        raise TypedEvidenceError("parent conjunct assertions must contain 1-64 items")
    record = evidence["record"]
    checks: list[bool] = []
    for assertion in assertions:
        if kind == "http_response":
            assertion = _closed(
                assertion, {"target", "path", "op", "value"},
                "parent http_response assertion",
            )
            target = assertion["target"]
            path = assertion["path"]
            if target == "status" and path == "":
                actual = record["response"]["status"]
            elif target == "action_origin" and path == "":
                actual = record["action_origin"]
            elif target == "field" and isinstance(path, str) and path.startswith("/"):
                actual = record["response"]["selected"].get(path, object())
            else:
                raise TypedEvidenceError("parent http_response assertion target/path is invalid")
        elif kind == "mcp_tool_response":
            assertion = _closed(
                assertion, {"path", "op", "value"},
                "parent mcp_tool_response assertion",
            )
            path = assertion["path"]
            if not isinstance(path, str) or not path.startswith("/"):
                raise TypedEvidenceError(
                    "parent mcp_tool_response assertion path is invalid"
                )
            actual = record["result"]["selected"].get(path, object())
        elif kind == "receipt_field":
            assertion = _closed(
                assertion, {"path", "op", "value"},
                "parent receipt_field assertion",
            )
            path = assertion["path"]
            if not isinstance(path, str) or not path.startswith("/"):
                raise TypedEvidenceError("parent receipt_field assertion path is invalid")
            actual = record["fields"].get(path, object())
        elif kind == "log_assertion":
            assertion = _closed(
                assertion, {"path", "op", "value"},
                "parent log_assertion assertion",
            )
            path = assertion["path"]
            if not isinstance(path, str) or not path.startswith("/"):
                raise TypedEvidenceError("parent log_assertion assertion path is invalid")
            actual = _pointer(record["entry"], path)
        elif kind == "state_transition":
            assertion = _closed(
                assertion, {"phase", "path", "op", "value"},
                "parent state_transition assertion",
            )
            phase = assertion["phase"]
            path = assertion["path"]
            if phase not in {"before", "action", "after"}:
                raise TypedEvidenceError("parent state_transition assertion phase is invalid")
            if path == "/status":
                actual = record[phase]["status"]
            elif isinstance(path, str) and path.startswith("/"):
                actual = record[phase]["selected"].get(path, object())
            else:
                raise TypedEvidenceError("parent state_transition assertion path is invalid")
        else:
            raise TypedEvidenceError("parent fielded conjunct kind is unsupported")
        checks.append(_compare(actual, {"op": assertion["op"], "value": assertion["value"]}))
    return all(checks)


def evaluate_parent_request(request: Any, trust_config: Any) -> dict[str, Any]:
    """Authenticate evidence and evaluate one canonical parent conjunct."""
    trust = _trust(trust_config)
    request = _closed(
        request,
        {
            "observation_id", "run_id", "action_id", "entity", "causal_index",
            "surface_id", "board_id", "candidate_commit", "conjunct",
            "evidence_path",
        },
        "parent evaluation request",
    )
    evidence_path = Path(str(request["evidence_path"])).expanduser().resolve()
    evidence_bytes = evidence_path.read_bytes()
    if len(evidence_bytes) > MAX_CONFIG_BYTES:
        raise TypedEvidenceError("parent evidence file is too large")
    try:
        evidence_value = json.loads(evidence_bytes)
    except json.JSONDecodeError:
        raise TypedEvidenceError("parent evidence is not valid JSON") from None
    evidence = _verify_evidence(evidence_value, trust)
    context = evidence["context"]
    expected_context = {
        "observation_id": request["observation_id"],
        "run_id": request["run_id"],
        "action_id": request["action_id"],
        "entity": request["entity"],
        "causal_index": request["causal_index"],
        "surface": request["surface_id"],
        "board_id": request["board_id"],
        "candidate_commit": request["candidate_commit"],
    }
    if any(context.get(key) != value for key, value in expected_context.items()):
        raise TypedEvidenceError("parent request correlation does not match evidence")
    conjunct = request["conjunct"]
    if not isinstance(conjunct, dict):
        raise TypedEvidenceError("parent conjunct fields do not match schema")
    kind = conjunct.get("kind")
    if kind != evidence["kind"]:
        raise TypedEvidenceError("parent conjunct kind does not match evidence")
    record = evidence["record"]
    source_id = evidence["source"]["source_id"]
    if set(conjunct) == {"kind", "source_id", "assertions"}:
        passed = _evaluate_parent_fielded_conjunct(conjunct, evidence)
    elif kind == "http_response":
        conjunct = _closed(
            conjunct, {"kind", "request", "status", "body_contains"},
            "parent http_response conjunct",
        )
        passed = (
            record["response"]["status"] == conjunct["status"]
            and _semantic_contains(source_id, conjunct["request"])
            and _semantic_contains(record["response"]["selected"], conjunct["body_contains"])
        )
    elif kind == "mcp_tool_response":
        conjunct = _closed(
            conjunct, {"kind", "tool", "field", "expected"},
            "parent mcp_tool_response conjunct",
        )
        field = str(conjunct["field"])
        pointer = field if field.startswith("/") else "/" + field
        passed = (
            record["result"]["tool"] == conjunct["tool"]
            and _semantic_contains(
                record["result"]["selected"].get(pointer, object()),
                conjunct["expected"],
            )
        )
    elif kind == "receipt_field":
        conjunct = _closed(
            conjunct, {"kind", "receipt", "field", "expected"},
            "parent receipt_field conjunct",
        )
        field = str(conjunct["field"])
        pointer = field if field.startswith("/") else "/" + field
        passed = (
            _semantic_contains(source_id, conjunct["receipt"])
            and record["fields"].get(pointer, object()) == conjunct["expected"]
        )
    elif kind == "log_assertion":
        conjunct = _closed(
            conjunct, {"kind", "stream", "expected"},
            "parent log_assertion conjunct",
        )
        passed = (
            _semantic_contains(source_id, conjunct["stream"])
            and _semantic_contains(record["entry"], conjunct["expected"])
        )
    elif kind == "state_transition":
        conjunct = _closed(
            conjunct, {"kind", "from", "to", "via"},
            "parent state_transition conjunct",
        )
        passed = (
            _semantic_contains(record["before"]["selected"], conjunct["from"])
            and _semantic_contains(record["action"]["selected"], conjunct["via"])
            and _semantic_contains(record["after"]["selected"], conjunct["to"])
        )
    else:
        raise TypedEvidenceError("parent conjunct kind is unsupported")
    if passed:
        evidence_id = _digest({
            "payload_sha256": evidence["payload_sha256"], "auth": evidence["auth"],
        })
        _consume_replay(evidence_id, trust["replay_guard"])
    return {
        "verifier_id": trust["verifier_id"],
        "observation_id": request["observation_id"],
        "run_id": request["run_id"],
        "action_id": request["action_id"],
        "entity": request["entity"],
        "causal_index": request["causal_index"],
        "surface_id": request["surface_id"],
        "board_id": request["board_id"],
        "candidate_commit": request["candidate_commit"],
        "kind": kind,
        "passed": passed,
        "predicate_sha256": _digest(conjunct),
        "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
    }


def install_module(destination: Path) -> dict[str, Any]:
    """Install this recorder outside its source checkout with private mode."""
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = destination / "typed_evidence.py"
    if target.exists() and target.resolve() == Path(__file__).resolve():
        raise TypedEvidenceError("install destination must differ from source")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".typed-evidence-", dir=destination)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(Path(__file__).resolve(), temporary)
        temporary.chmod(0o700)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"schema_version": SCHEMA_VERSION, "module": str(target), "module_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def _write_result(value: Any, output: str | None) -> None:
    text = json.dumps(value, sort_keys=True) + "\n"
    if output:
        path = Path(output).expanduser().resolve()
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
    else:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--dir", required=True)
    record = commands.add_parser("record")
    record.add_argument("--request", required=True)
    record.add_argument("--trust", required=True)
    record.add_argument("--output")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--evidence", required=True)
    evaluate.add_argument("--expected", required=True)
    evaluate.add_argument("--trust", required=True)
    evaluate.add_argument("--output")
    parent = commands.add_parser("evaluate-parent")
    parent.add_argument("--trust", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            _write_result(install_module(Path(args.dir)), None)
        elif args.command == "record":
            value = record_evidence(
                _read_json(Path(args.request).expanduser().resolve(), "record request"),
                _read_json(Path(args.trust).expanduser().resolve(), "trust config"),
            )
            _write_result(value, args.output)
        elif args.command == "evaluate":
            value = evaluate_evidence(
                _read_json(Path(args.evidence).expanduser().resolve(), "evidence"),
                _read_json(Path(args.expected).expanduser().resolve(), "expected predicate"),
                _read_json(Path(args.trust).expanduser().resolve(), "trust config"),
            )
            _write_result(value, args.output)
        else:
            value = evaluate_parent_request(
                json.load(sys.stdin),
                _read_json(Path(args.trust).expanduser().resolve(), "trust config"),
            )
            _write_result(value, None)
    except TypedEvidenceError as exc:
        sys.stderr.write(f"typed_evidence: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
