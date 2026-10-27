"""Fail-closed binding from a registered project to its reviewed delivery runtime.

This module intentionally binds the existing BatchDeliveryRuntime, connector
allowlist and Git adapter. It does not create a second delivery engine or infer
member validation evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _identity(value: Any) -> str:
    return str(value).rstrip("/").removesuffix(".git").casefold()


def _project_value(project: Any, name: str, default: Any = None) -> Any:
    return project.get(name, default) if isinstance(project, Mapping) else getattr(project, name, default)


def _route_name(project: Any) -> str | None:
    name = _project_value(project, "__project_name__")
    if not isinstance(name, str) or not name:
        name = _project_value(project, "name")
    return name if isinstance(name, str) and name and name == name.strip() else None


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _connector_route(runtime: Any, sources: Sequence[Any], name: str, board_id: str, home_board: str | None) -> bool:
    """Require project identity or an explicit connector source-to-project map.

    A connector config is loaded once using the Butler home board as its
    default. The source declaration's exact registry-key/project mapping is
    the approved authority for using that already-configured connector on a
    registry project's own board; never synthesize or mutate that runtime.
    """
    runtime_project = getattr(runtime, "project_id", None)
    if runtime_project == name and getattr(runtime, "board_id", None) == board_id:
        return True
    if getattr(runtime, "board_id", None) != board_id and getattr(runtime, "board_id", None) != home_board:
        return False
    connector_id = getattr(getattr(runtime, "declaration", None), "connector_id", None)
    for source in sources:
        if getattr(source, "enabled", True) is not True or getattr(source, "connector_id", None) != connector_id:
            continue
        routing = getattr(source, "routing", None)
        if routing is None:
            continue
        if (getattr(routing, "project_hint_is_registry_key", False)
                or name in getattr(routing, "project_map", {}).values()):
            return True
    return False


def _tools_are_approved(runtime: Any, mode: str) -> bool:
    declaration = getattr(runtime, "declaration", None)
    if declaration is None or getattr(declaration, "enabled", False) is not True:
        return False
    tools = {tool.name: tool for tool in getattr(declaration, "tools", ())}
    risky = set(getattr(declaration, "risky_tools", ()))
    expected = ({
        "ado_pull_requests_list": "read_only",
        "ado_pull_request_get": "read_only",
        "ado_pull_request_create": "mutating",
        "ado_pull_request_update": "mutating",
    } if mode == "batch_pr" else {})
    return all(
        name in tools and tools[name].effect == effect
        and (effect != "mutating" or name in risky)
        for name, effect in expected.items()
    )


def _private_ledger_path(root: Path, identity: Mapping[str, str]) -> Path:
    """Create/check a private, durable project ledger directory without symlinks."""
    if not root.is_absolute():
        raise ValueError("delivery ledger root must be absolute")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    info = root.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise ValueError("delivery ledger directory is not private and owner-controlled")
    material = {key: identity[key] for key in sorted(identity)}
    filename = hashlib.sha256(_canonical(material)).hexdigest() + ".json"
    path = root / filename
    if path.is_symlink():
        raise ValueError("delivery ledger may not be a symlink")
    if path.exists():
        file_info = path.stat()
        if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid()
                or file_info.st_nlink != 1 or file_info.st_mode & 0o077):
            raise ValueError("delivery ledger is not a private owner-controlled regular file")
    return path


class _DeliveryToolBridge:
    """Narrow approved-tool gateway retaining runtime policy-gate semantics."""

    def __init__(self, runtime: Any, backend: Any) -> None:
        self.runtime = runtime
        self.backend = backend
        lock = getattr(runtime, "_workflow_delivery_policy_lock", None)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            setattr(runtime, "_workflow_delivery_policy_lock", lock)
        self._lock = lock

    async def _delivery_tool(self, runtime: Any, tool_name: str, arguments: Mapping[str, Any], *, mutate: bool = False):
        butler = importlib.import_module("board_butler")
        tool = next((item for item in runtime.declaration.tools if item.name == tool_name), None)
        if (tool is None or tool.effect != ("mutating" if mutate else "read_only")
                or (mutate and tool_name not in runtime.declaration.risky_tools)):
            raise butler.ConnectorDenied("workflow delivery requires a declared " + tool_name + " tool")
        operation = "workflow-delivery-" + hashlib.sha256(
            _canonical({"tool": tool_name, "args": arguments})
        ).hexdigest()[:32]
        grant = butler.ConnectorPolicyRequest(
            runtime.board_id, runtime.project_id, runtime.declaration.connector_id,
            operation, tool_name, tool.effect,
            hashlib.sha256(_canonical(arguments)).hexdigest(),
        )
        if not mutate:
            result = await runtime.call_tool(operation, tool_name, arguments)
            return butler._source_payload_document(result.payload)

        # Add the exact ephemeral grant understood by the existing resident
        # source-writeback gate. Do not replace or wrap runtime.policy_gate:
        # other resident calls may share this runtime concurrently, and every
        # mutation must continue through its originally configured gate.
        if getattr(getattr(self.backend, "args", None), "runtime_mode", "shadow") != "active":
            raise butler.ConnectorDenied("workflow delivery mutation requires active runtime")
        if runtime.policy_gate is None:
            raise butler.ConnectorDenied("workflow delivery requires the existing connector policy gate")
        async with self._lock:
            poller = getattr(self.backend, "source_intake_poller", None)
            source_grants = getattr(poller, "_writeback_grants", None)
            if not isinstance(source_grants, set):
                raise butler.ConnectorDenied("workflow delivery requires the existing connector policy grant store")
            source_grants.add(grant)
            try:
                result = await runtime.call_tool(operation, tool_name, arguments)
                return butler._source_payload_document(result.payload)
            finally:
                source_grants.discard(grant)


def _registered_project(backend: Any, project: Any, board_id: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = _project_value(project, "__registry__")
    if isinstance(registry, Mapping):
        row = (registry.get("projects") or {}).get(name)
        if isinstance(row, Mapping):
            return dict(row), dict(registry)
    raise ValueError("exact registered project mapping is unavailable")


async def build_workflow_runtime(backend: Any, project: Any) -> tuple[Any, dict[str, Any]]:
    """Build (BatchDeliveryRuntime, compiled policy) for one exact registered project.

    The route is the exact project key and board in Central's project registry,
    then an existing board-scoped connector runtime bound either to that project
    key or to an explicit configured source project mapping. No ticket text,
    Sonar issue, credentials, or inferred repository route is consulted.
    """
    butler = importlib.import_module("board_butler")
    name = _route_name(project)
    board_id = _project_value(project, "board_id")
    if not name or not isinstance(board_id, str) or not board_id:
        raise butler.ConnectorDenied("workflow delivery project route is incomplete")

    reader = getattr(backend, "_source_project_reader", None)
    if callable(reader):
        # Prefer an authoritative Central read over workflow-supplied context.
        selected = await reader(board_id, project_id=name)
        if not isinstance(selected, Mapping) or selected.get("__project_name__") != name:
            raise butler.ConnectorDenied("registered project route is missing or ambiguous")
        registered = dict(selected)
        registry = registered.get("__registry__")
    else:
        registered, registry = _registered_project(backend, project, board_id, name)
    if registered is None or not isinstance(registry, Mapping):
        raise butler.ConnectorDenied("exact registered project mapping is unavailable")
    registry_projects = registry.get("projects") if isinstance(registry, Mapping) else None
    row = registry_projects.get(name) if isinstance(registry_projects, Mapping) else None
    if not isinstance(row, Mapping) or row.get("board_id") != board_id or row.get("status") != "active":
        raise butler.ConnectorDenied("project is not the exact active registered board route")
    repository_url = row.get("repository_url")
    project_repository = _project_value(project, "repository_url")
    if (not isinstance(repository_url, str) or not repository_url
            or (project_repository is not None and (not isinstance(project_repository, str)
                or _identity(repository_url) != _identity(project_repository)))):
        raise butler.ConnectorDenied("registered repository mapping differs from the workflow project")
    for field in ("fleet_clone_dir", "work_dir"):
        projected = _project_value(project, field)
        registered_value = row.get(field)
        if projected is not None and registered_value is not None:
            try:
                same_path = Path(projected).resolve() == Path(registered_value).resolve()
            except (TypeError, OSError):
                same_path = False
            if not same_path:
                raise butler.ConnectorDenied("registered protected clone mapping differs from the workflow project")
    projected_owner = _project_value(project, "work_dir_owner")
    if projected_owner is not None and row.get("work_dir_owner", "operator") != projected_owner:
        raise butler.ConnectorDenied("registered protected clone mapping differs from the workflow project")
    if row.get("work_dir_owner") != "fleet":
        raise butler.ConnectorDenied("delivery requires a fleet-owned protected clone")
    clone = row.get("fleet_clone_dir")
    if clone is None and row.get("work_dir_owner") == "fleet":
        clone = row.get("work_dir")
    if not isinstance(clone, str) or not Path(clone).is_absolute():
        raise butler.ConnectorDenied("registered fleet clone is unavailable")
    try:
        clone_path = Path(clone).resolve(strict=True)
    except OSError:
        raise butler.ConnectorDenied("registered fleet clone is unavailable") from None
    if not clone_path.is_dir() or not (clone_path / ".git").exists():
        raise butler.ConnectorDenied("registered fleet clone is not a Git clone")

    try:
        delivery_api = importlib.import_module("integration_delivery")
        resolved = butler._delivery_policy_api.resolve_delivery_policy(registry, name)
        policy = delivery_api.runtime_policy_from_resolved(resolved, {**dict(row), "repository_url": repository_url})
    except Exception as exc:
        raise butler.ConnectorDenied("effective delivery policy is unavailable: " + type(exc).__name__) from None
    if policy.get("mode") in {"batch_pr", "branch_only"}:
        activation = row.get("delivery_policy_activation")
        if (not isinstance(activation, Mapping)
                or activation.get("state") != "active"
                or activation.get("schema_version") != 1
                or activation.get("policy_revision") != policy.get("policy_revision")
                or not isinstance(activation.get("activation_id"), str)
                or not activation["activation_id"]):
            raise butler.ConnectorDenied("delivery policy is not activated for its effective revision")
        ready = resolved.get("runtime", {})
        if ready.get("ready") is not True:
            raise butler.ConnectorDenied("effective delivery policy is not supported: " + ",".join(ready.get("blockers", [])))
    if policy.get("mode") not in {"batch_pr", "branch_only"}:
        raise butler.ConnectorDenied("effective batch delivery mode is unavailable")

    args = getattr(backend, "args", None)
    sources = tuple(getattr(args, "_connector_sources", ()) or ())
    runtimes = tuple(getattr(args, "_connector_runtimes", ()) or ())
    home_board = getattr(args, "home_board", None)
    candidates = [runtime for runtime in runtimes
                  if _connector_route(runtime, sources, name, board_id, home_board)]
    # A source declaration's explicit project mapping is allowed to authorize
    # the configured home-board runtime for this exact registry project route.
    # Without sources, only a runtime explicitly configured for this exact
    # project and target board is authoritative; a generic board-default
    # runtime is not.
    routed_ids = set()
    if sources:
        for source in sources:
            routing = getattr(source, "routing", None)
            if (getattr(source, "enabled", True) is True
                    and (getattr(routing, "project_hint_is_registry_key", False)
                         or name in getattr(routing, "project_map", {}).values())):
                routed_ids.add(source.connector_id)
        candidates = [runtime for runtime in candidates
                      if runtime.declaration.connector_id in routed_ids
                      or runtime.project_id == name]
    if len(candidates) != 1:
        raise butler.ConnectorDenied("approved connector route is missing or ambiguous")
    runtime = candidates[0]
    if not _tools_are_approved(runtime, policy["mode"]):
        raise butler.ConnectorDenied("approved connector does not declare the required delivery tools")
    if runtime.board_id != board_id and runtime.declaration.connector_id not in routed_ids:
        raise butler.ConnectorDenied("connector board route does not match the project")

    full_identity = {
        "project": name, "board": board_id, "repository": _identity(repository_url),
        "policy_revision": policy.get("policy_revision", "per-ticket"),
        "connector": runtime.declaration.connector_id,
        "connector_policy": getattr(runtime, "policy_digest_sha256", ""),
        "clone": str(clone_path),
    }
    cache = getattr(backend, "_workflow_delivery_runtimes", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(backend, "_workflow_delivery_runtimes", cache)
    cache_key = tuple(full_identity[key] for key in sorted(full_identity))
    previous = cache.get(cache_key)
    if previous is not None:
        return previous

    fields = butler._repository_fields({"repository_url": repository_url})
    if not fields["repository_project"] or not fields["repository_name"]:
        raise butler.ConnectorDenied("registered repository is not an approved ADO mapping")
    connector_api = importlib.import_module("board_butler")
    bridge = _DeliveryToolBridge(runtime, backend)
    connector = connector_api._ResidentBatchConnector(bridge, runtime, fields)

    state_root = getattr(args, "source_intake_index_file", None)
    if state_root is not None:
        state_root = Path(state_root).parent / "batch-delivery"
    else:
        base = Path(os.environ.get("PURSERS_STATE_DIR", "~/.pursers")).expanduser()
        state_root = base / "board-butler" / "batch-delivery"
    if not state_root.is_absolute():
        raise butler.ConnectorDenied("durable delivery ledger location is unavailable")
    try:
        ledger_path = _private_ledger_path(state_root, full_identity)
        ledger = delivery_api.BatchLedger(ledger_path)
        adapter = delivery_api.VerifiedGitConnectorAdapter(
            clone_path, repository_url, connector,
            validation_commands=list(resolved.get("policy", {}).get("validation", {}).get("test_commands", [])),
        )
        batch_runtime = delivery_api.BatchDeliveryRuntime(ledger, adapter)
    except Exception as exc:
        raise butler.ConnectorDenied("delivery runtime construction blocked: " + type(exc).__name__) from None

    cache = getattr(backend, "_workflow_delivery_runtimes", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(backend, "_workflow_delivery_runtimes", cache)
    cache_key = tuple(full_identity[key] for key in sorted(full_identity))
    previous = cache.get(cache_key)
    if previous is not None:
        return previous
    cached = (batch_runtime, policy)
    cache[cache_key] = cached
    return cached
