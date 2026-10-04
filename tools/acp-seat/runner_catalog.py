"""Bounded, non-executing view of the official ACP agent registry."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform as host_platform
import re
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
CACHE_SCHEMA = "pursers_acp_registry_cache_v1"
LOCK_SCHEMA = "pursers_acp_runner_lock_v1"
MAX_REGISTRY_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_AGE_S = 24 * 60 * 60
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
NPM_PACKAGE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]{0,127}/)?"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
PYTHON_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PLATFORMS = frozenset(
    {
        "darwin-aarch64",
        "darwin-x86_64",
        "linux-aarch64",
        "linux-x86_64",
        "windows-aarch64",
        "windows-x86_64",
    }
)
DISTRIBUTION_KINDS = frozenset({"binary", "npx", "uvx"})


class CatalogError(ValueError):
    """Registry, cache, resolution, or pin validation failed closed."""


@dataclass(frozen=True)
class CatalogView:
    registry_revision: str
    registry_version: str
    fetched_at_epoch: float
    agents: tuple[dict[str, Any], ...]
    stale: bool
    source_url: str

    def list_agents(self, target: str) -> list[dict[str, Any]]:
        """Return metadata and availability only; never install or launch."""
        if not isinstance(target, str) or target not in PLATFORMS:
            raise CatalogError("platform_invalid")
        rows: list[dict[str, Any]] = []
        for agent in self.agents:
            kinds = _available_kinds(agent["distribution"], target)
            rows.append(
                {
                    "id": agent["id"],
                    "name": agent["name"],
                    "version": agent["version"],
                    "enabled": bool(kinds),
                    "distribution_kinds": kinds,
                    "disabled_reason": None
                    if kinds
                    else "unsupported_platform_or_distribution",
                }
            )
        return rows

    def resolve(
        self,
        agent_id: str,
        version: str,
        target: str,
        distribution_kind: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an exact pin to an argv preview. Nothing is executed."""
        if not _is_exact_version(version):
            raise CatalogError("exact_version_required")
        if not isinstance(target, str) or target not in PLATFORMS:
            raise CatalogError("platform_invalid")
        matches = [agent for agent in self.agents if agent["id"] == agent_id]
        if not matches:
            raise CatalogError("agent_not_found")
        agent = matches[0]
        if agent["version"] != version:
            raise CatalogError("pin_drift")
        available = _available_kinds(agent["distribution"], target)
        if not available:
            raise CatalogError("unsupported_platform_or_distribution")
        kind = available[0] if distribution_kind is None else distribution_kind
        if not isinstance(kind, str) or kind not in available:
            raise CatalogError("distribution_unavailable")
        launch, source, integrity = _distribution_contract(
            kind, agent["distribution"][kind], target
        )
        return {
            "schema": "pursers_acp_resolved_runner_v1",
            "agent_id": agent_id,
            "agent_version": version,
            "platform": target,
            "registry_revision": self.registry_revision,
            "distribution": {
                "kind": kind,
                "source": source,
                "integrity": integrity,
            },
            "launch": launch,
        }


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _registry_revision(document: Mapping[str, Any]) -> str:
    basis = {"version": document["version"], "agents": document["agents"]}
    return "sha256:" + hashlib.sha256(canonical_json(basis)).hexdigest()


def platform_target(system: str | None = None, machine: str | None = None) -> str:
    if system is not None and (not isinstance(system, str) or not system):
        raise CatalogError("unsupported_host_platform")
    if machine is not None and (not isinstance(machine, str) or not machine):
        raise CatalogError("unsupported_host_platform")
    system_name = (system or host_platform.system()).lower()
    machine_name = (machine or host_platform.machine()).lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(
        system_name
    )
    architecture = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine_name)
    if os_name is None or architecture is None:
        raise CatalogError("unsupported_host_platform")
    return f"{os_name}-{architecture}"


def validate_catalog_pin(raw: Any) -> dict[str, str]:
    """Validate the portable subset needed to resolve one catalog runner."""
    fields = {
        "agent_id",
        "agent_version",
        "platform",
        "registry_revision",
        "distribution_kind",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise CatalogError("catalog_pin_invalid")
    if not isinstance(raw["agent_id"], str) or not SAFE_ID.fullmatch(raw["agent_id"]):
        raise CatalogError("catalog_pin_agent_id_invalid")
    if not _is_exact_version(raw["agent_version"]):
        raise CatalogError("catalog_pin_version_invalid")
    if not isinstance(raw["platform"], str) or raw["platform"] not in PLATFORMS:
        raise CatalogError("catalog_pin_platform_invalid")
    if (
        not isinstance(raw["distribution_kind"], str)
        or raw["distribution_kind"] not in DISTRIBUTION_KINDS
    ):
        raise CatalogError("catalog_pin_distribution_invalid")
    if not isinstance(raw["registry_revision"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", raw["registry_revision"]
    ):
        raise CatalogError("registry_revision_invalid")
    return dict(raw)


def refresh_catalog(
    cache_path: Path,
    *,
    url: str = REGISTRY_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = MAX_REGISTRY_BYTES,
    fetch: Callable[[str, float, int], bytes] | None = None,
    now: float | None = None,
) -> CatalogView:
    """Explicitly fetch, validate, then atomically replace the last-good cache."""
    if not _positive_finite_number(timeout_s) or not _valid_max_bytes(max_bytes):
        raise CatalogError("refresh_limits_invalid")
    if now is not None and not _finite_number(now):
        raise CatalogError("refresh_time_invalid")
    _https_url(url, "registry_url")
    payload = (fetch or _fetch)(url, timeout_s, max_bytes)
    if not isinstance(payload, bytes):
        raise CatalogError("registry_malformed")
    if len(payload) > max_bytes:
        raise CatalogError("registry_oversized")
    document = _validate_registry_bytes(payload)
    fetched_at = time.time() if now is None else float(now)
    revision = _registry_revision(document)
    cache = {
        "schema": CACHE_SCHEMA,
        "source_url": url,
        "registry_revision": revision,
        "registry_version": document["version"],
        "fetched_at_epoch": fetched_at,
        "agents": document["agents"],
    }
    _atomic_replace(cache_path, canonical_json(cache) + b"\n", mode=0o600)
    return _view(cache, now=fetched_at, max_age_s=DEFAULT_MAX_AGE_S)


def load_cached_catalog(
    cache_path: Path,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    now: float | None = None,
    max_bytes: int = MAX_REGISTRY_BYTES,
) -> CatalogView:
    if (
        not _nonnegative_finite_number(max_age_s)
        or not _valid_max_bytes(max_bytes)
        or (now is not None and not _finite_number(now))
    ):
        raise CatalogError("cache_limits_invalid")
    try:
        info = cache_path.lstat()
        if cache_path.is_symlink() or info.st_size > max_bytes:
            raise CatalogError("cache_untrusted")
        raw = cache_path.read_bytes()
        cache = json.loads(raw, parse_constant=_reject_json_constant)
    except CatalogError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("cache_unavailable") from exc
    _validate_cache(cache)
    return _view(cache, now=time.time() if now is None else now, max_age_s=max_age_s)


def persist_selection_lock(path: Path, resolved: Mapping[str, Any]) -> bool:
    """Create an immutable exact selection; identical retries are idempotent."""
    if not isinstance(resolved, Mapping):
        raise CatalogError("resolution_invalid")
    normalized = _validate_resolution(dict(resolved))
    lock = {"schema": LOCK_SCHEMA, "resolved": normalized}
    encoded = canonical_json(lock) + b"\n"
    if path.exists() or path.is_symlink():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise CatalogError("selection_lock_unavailable") from exc
        if existing == encoded:
            return False
        raise CatalogError("selection_lock_immutable")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CatalogError("selection_lock_raced") from exc
    return True


def load_selection_lock(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_lock_constant
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("selection_lock_unavailable") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema", "resolved"}:
        raise CatalogError("selection_lock_invalid")
    if raw["schema"] != LOCK_SCHEMA:
        raise CatalogError("selection_lock_invalid")
    return _validate_resolution(raw["resolved"])


def _fetch(url: str, timeout_s: float, max_bytes: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Pursers-ACP-Catalog/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = response.read(max_bytes + 1)
    except (OSError, TimeoutError) as exc:
        raise CatalogError("registry_refresh_failed") from exc
    if len(payload) > max_bytes:
        raise CatalogError("registry_oversized")
    return payload


def _validate_registry_bytes(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload, parse_constant=_reject_registry_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("registry_malformed") from exc
    if (
        not isinstance(document, dict)
        or not {"version", "agents"} <= set(document)
        or not set(document) <= {"version", "agents", "extensions"}
    ):
        raise CatalogError("registry_shape_invalid")
    extensions = document.get("extensions", [])
    if not isinstance(extensions, list) or len(extensions) > 10_000:
        raise CatalogError("registry_extensions_invalid")
    if not isinstance(document["version"], str) or not document["version"]:
        raise CatalogError("registry_version_invalid")
    agents = document["agents"]
    if not isinstance(agents, list) or len(agents) > 10_000:
        raise CatalogError("registry_agents_invalid")
    seen: set[str] = set()
    validated = []
    for raw in agents:
        agent = _validate_agent(raw)
        if agent["id"] in seen:
            raise CatalogError("registry_agent_duplicate")
        seen.add(agent["id"])
        validated.append(agent)
    return {"version": document["version"], "agents": validated}


def _validate_agent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CatalogError("registry_agent_invalid")
    for field in ("id", "name", "version", "distribution"):
        if field not in raw:
            raise CatalogError("registry_agent_invalid")
    if not isinstance(raw["id"], str) or not SAFE_ID.fullmatch(raw["id"]):
        raise CatalogError("registry_agent_id_invalid")
    if not _is_exact_version(raw["version"]):
        raise CatalogError("registry_agent_version_invalid")
    if not isinstance(raw["name"], str) or not raw["name"].strip():
        raise CatalogError("registry_agent_name_invalid")
    distribution = _validate_distribution(raw["distribution"], raw["version"])
    return {
        "id": raw["id"],
        "name": raw["name"],
        "version": raw["version"],
        "distribution": distribution,
    }


def _validate_distribution(raw: Any, agent_version: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw or not set(raw) <= {"binary", "npx", "uvx"}:
        raise CatalogError("registry_distribution_invalid")
    result: dict[str, Any] = {}
    for kind, value in raw.items():
        if kind == "binary":
            if not isinstance(value, dict) or not value:
                raise CatalogError("registry_binary_invalid")
            result[kind] = {
                target: _validate_binary(item)
                for target, item in value.items()
                if target in PLATFORMS
            }
            if not result[kind]:
                raise CatalogError("registry_binary_invalid")
        else:
            result[kind] = _validate_package(kind, value, agent_version)
    return result


def _safe_string(value: Any, field: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 0x20 for char in value)
    ):
        raise CatalogError(f"{field}_invalid")
    return value


def _args(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise CatalogError("registry_args_invalid")
    return [_safe_string(item, "registry_arg") for item in value]


def _validate_binary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not {"archive", "cmd"} <= set(raw):
        raise CatalogError("registry_binary_invalid")
    cmd = _validate_binary_cmd(raw["cmd"])
    result = {
        "archive": _https_url(raw["archive"], "registry_binary_archive"),
        "cmd": cmd,
        "args": _args(raw.get("args")),
    }
    sha256 = raw.get("sha256")
    if sha256 is not None:
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise CatalogError("registry_binary_sha256_invalid")
        result["sha256"] = sha256.lower()
    return result


def _validate_package(kind: str, raw: Any, agent_version: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or "package" not in raw:
        raise CatalogError(f"registry_{kind}_invalid")
    package = _safe_string(raw["package"], f"registry_{kind}_package", maximum=512)
    if kind == "npx":
        name, separator, version = package.rpartition("@")
        valid = bool(separator and NPM_PACKAGE.fullmatch(name))
    else:
        if "==" in package:
            name, separator, version = package.rpartition("==")
        else:
            name, separator, version = package.rpartition("@")
        valid = bool(separator and PYTHON_PACKAGE.fullmatch(name))
    if not valid or version != agent_version:
        raise CatalogError(f"registry_{kind}_package_invalid")
    return {
        "package": package,
        "args": _args(raw.get("args")),
    }


def _is_exact_version(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and SAFE_VERSION.fullmatch(value)
        and value.lower() not in {"latest", "stable", "preview"}
    )


def _validate_binary_cmd(value: Any) -> str:
    cmd = _safe_string(value, "registry_binary_cmd", maximum=512)
    path = PurePosixPath(cmd.replace("\\", "/"))
    if (
        path.is_absolute()
        or PureWindowsPath(cmd).drive
        or ".." in path.parts
        or any(char.isspace() for char in cmd)
    ):
        raise CatalogError("registry_binary_cmd_unsafe")
    return cmd


def _https_url(value: Any, field: str) -> str:
    url = _safe_string(value, field)
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise CatalogError(f"{field}_invalid")
    return url


def _available_kinds(distribution: Mapping[str, Any], target: str) -> list[str]:
    result = []
    if "binary" in distribution and target in distribution["binary"]:
        result.append("binary")
    result.extend(kind for kind in ("npx", "uvx") if kind in distribution)
    return result


def _distribution_contract(
    kind: str, raw: Mapping[str, Any], target: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str] | None]:
    if kind == "binary":
        item = raw[target]
        launch = {"argv": [item["cmd"], *item["args"]], "cwd": "install_root"}
        source = {"archive": item["archive"]}
        integrity = (
            {"algorithm": "sha256", "digest": item["sha256"]}
            if "sha256" in item
            else None
        )
        return launch, source, integrity
    package = raw["package"]
    executable = "npx" if kind == "npx" else "uvx"
    launch = {"argv": [executable, package, *raw["args"]], "cwd": None}
    return launch, {"package": package}, None


def _validate_cache(cache: Any) -> None:
    fields = {
        "schema",
        "source_url",
        "registry_revision",
        "registry_version",
        "fetched_at_epoch",
        "agents",
    }
    if (
        not isinstance(cache, dict)
        or set(cache) != fields
        or cache["schema"] != CACHE_SCHEMA
    ):
        raise CatalogError("cache_invalid")
    fetched_at = cache["fetched_at_epoch"]
    if not _finite_number(fetched_at):
        raise CatalogError("cache_invalid")
    _https_url(cache["source_url"], "cache_source_url")
    if not isinstance(cache["registry_revision"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", cache["registry_revision"]
    ):
        raise CatalogError("cache_invalid")
    cached_document = {
        "version": cache["registry_version"],
        "agents": cache["agents"],
    }
    document = _validate_registry_bytes(canonical_json(cached_document))
    if canonical_json(document) != canonical_json(cached_document):
        raise CatalogError("cache_invalid")
    if _registry_revision(document) != cache["registry_revision"]:
        raise CatalogError("cache_revision_mismatch")


def _reject_json_constant(_value: str) -> None:
    raise CatalogError("cache_invalid")


def _reject_registry_constant(_value: str) -> None:
    raise CatalogError("registry_malformed")


def _reject_lock_constant(_value: str) -> None:
    raise CatalogError("selection_lock_invalid")


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _positive_finite_number(value: Any) -> bool:
    return _finite_number(value) and value > 0


def _nonnegative_finite_number(value: Any) -> bool:
    return _finite_number(value) and value >= 0


def _valid_max_bytes(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 < value <= MAX_REGISTRY_BYTES
    )


def _view(cache: Mapping[str, Any], *, now: float, max_age_s: float) -> CatalogView:
    age = max(0.0, float(now) - float(cache["fetched_at_epoch"]))
    return CatalogView(
        registry_revision=cache["registry_revision"],
        registry_version=cache["registry_version"],
        fetched_at_epoch=float(cache["fetched_at_epoch"]),
        agents=tuple(cache["agents"]),
        stale=age > max_age_s,
        source_url=cache["source_url"],
    )


def _validate_resolution(raw: Any) -> dict[str, Any]:
    fields = {
        "schema",
        "agent_id",
        "agent_version",
        "platform",
        "registry_revision",
        "distribution",
        "launch",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise CatalogError("resolution_invalid")
    if raw["schema"] != "pursers_acp_resolved_runner_v1":
        raise CatalogError("resolution_invalid")
    if (
        not isinstance(raw["agent_id"], str)
        or not SAFE_ID.fullmatch(raw["agent_id"])
        or not _is_exact_version(raw["agent_version"])
        or not isinstance(raw["platform"], str)
        or raw["platform"] not in PLATFORMS
    ):
        raise CatalogError("resolution_invalid")
    if not isinstance(raw["registry_revision"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", raw["registry_revision"]
    ):
        raise CatalogError("resolution_invalid")
    launch = raw["launch"]
    if not isinstance(launch, dict) or set(launch) != {"argv", "cwd"}:
        raise CatalogError("resolution_invalid")
    argv = _args(launch["argv"])
    cwd = launch["cwd"]
    if not argv or (cwd is not None and cwd != "install_root"):
        raise CatalogError("resolution_invalid")
    distribution = raw["distribution"]
    if not isinstance(distribution, dict) or set(distribution) != {
        "kind",
        "source",
        "integrity",
    }:
        raise CatalogError("resolution_invalid")
    kind = distribution["kind"]
    source = distribution["source"]
    integrity = distribution["integrity"]
    if (
        not isinstance(kind, str)
        or kind not in DISTRIBUTION_KINDS
        or not isinstance(source, dict)
    ):
        raise CatalogError("resolution_invalid")
    if kind == "binary":
        if set(source) != {"archive"} or launch["cwd"] != "install_root":
            raise CatalogError("resolution_invalid")
        _https_url(source["archive"], "resolution_archive")
        _validate_binary_cmd(argv[0])
        _validate_integrity(integrity)
    else:
        if set(source) != {"package"} or launch["cwd"] is not None:
            raise CatalogError("resolution_invalid")
        package = _validate_package(
            kind,
            {"package": source["package"]},
            raw["agent_version"],
        )["package"]
        if integrity is not None or len(argv) < 2:
            raise CatalogError("resolution_invalid")
        executable = "npx" if kind == "npx" else "uvx"
        if argv[0] != executable or argv[1] != package:
            raise CatalogError("resolution_invalid")
    return json.loads(canonical_json(raw))


def _validate_integrity(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"algorithm", "digest"}
        or raw["algorithm"] != "sha256"
        or not isinstance(raw["digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", raw["digest"])
    ):
        raise CatalogError("resolution_integrity_invalid")
    return {"algorithm": "sha256", "digest": raw["digest"]}


def _atomic_replace(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
