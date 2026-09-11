#!/usr/bin/env python3
"""Verifier-owned browser observer for real AionUi Home acceptance.

The acceptance harness (``harness.VerifierBrowserObserver``) spawns this file as
an opaque executable with a stripped environment (``PATH`` reset, ``cwd`` set to
the executable's own directory) and one JSON observation request on stdin. The
observer answers only from captures it recorded itself through a real browser
channel, so caller-authored report artifacts cannot forge a pass.

Two modes:

``<observer>``                      replay mode (harness contract, stdin JSON)
``<observer> capture --spec FILE``   capture mode (runner, real browser)

Configuration is read from ``observer.json`` beside this file, never from the
environment, because the harness strips the environment before spawning.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

SCHEMA_VERSION = 1
HOST_PRODUCT = "AionUi"
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
BUILD_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{5,127}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
OBSERVER_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:-]{7,127}")
OBSERVATION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_MAX_AGE_S = 43_200
MAX_CAPTURE_BYTES = 8_000_000
MIN_SNAPSHOT_NODES = 5

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNKNOWN_OBSERVATION = 3
EXIT_MISMATCH = 4
EXIT_STALE = 5
EXIT_CONFIG = 6
EXIT_CAPABILITY_UNAVAILABLE = 7
EXIT_CAPTURE_FAILED = 8

REQUEST_KEYS = {
    "observation_id", "target", "host_version", "host_build",
    "candidate_commit", "captured_at", "page_url", "assertions", "surface_id",
    "runtime_product", "runtime_identity_source",
}
CAPTURE_KEYS = {
    "observer_id", "observation_id", "target", "host_product", "host_version",
    "host_build", "host_identity_source", "candidate_commit", "captured_at", "page_url",
    "screenshot_base64", "snapshot", "surface_id",
    # The live transport challenge travels with the capture so the harness can
    # recompute it independently at validate time. Surfaces that carry no
    # challenge store an explicit null rather than omitting the field.
    "attestation", "attestation_nonce",
}
SURFACE_PRODUCTS = {
    "aionui": "AionUi",
    "fleet": "Pursers Fleet",
    "personal": "Pursers Personal",
}
TRANSITION_CONTEXT_KEYS = {
    "observation_id", "run_id", "action_id", "entity", "surface",
    "board_id", "candidate_commit", "issued_at", "causal_index",
}
TRANSITION_PROPERTIES = frozenset({
    "text", "value", "checked", "disabled", "count", "class", "hidden",
})
TRANSITION_ACTION_KEYS = {
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
    "click_response_json": {
        "kind", "selector", "method", "endpoint", "pointer", "path",
    },
}


class ObserverError(RuntimeError):
    """Observer refusal carrying the process exit code the harness sees."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: int, message: str) -> "ObserverError":
    return ObserverError(code, message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise _fail(EXIT_MISMATCH, f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(EXIT_MISMATCH, f"{label} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise _fail(EXIT_MISMATCH, f"{label} must carry a timezone")
    return parsed


def _require_private_mode(path: Path, label: str) -> None:
    if path.stat().st_mode & 0o022:
        raise _fail(EXIT_CONFIG, f"{label} must not be group/world writable")


def _validate_target(target: Any) -> dict[str, Any]:
    if not isinstance(target, dict) or set(target) != {"base_url", "board_id"}:
        raise _fail(EXIT_MISMATCH, "target must carry exact base_url and board_id")
    base_url = target["base_url"]
    if not isinstance(base_url, str):
        raise _fail(EXIT_MISMATCH, "target base_url must be a string")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise _fail(EXIT_MISMATCH, "target base_url must be a bare loopback origin")
    board_id = target["board_id"]
    if board_id is not None and (
        not isinstance(board_id, str)
        or not re.fullmatch(r"(?:sandbox|test)-[A-Za-z0-9._-]{1,71}", board_id)
    ):
        raise _fail(EXIT_MISMATCH, "board must have a sandbox- or test- prefix")
    return {"base_url": base_url.rstrip("/"), "board_id": board_id}


def _same_origin(page_url: Any, base_url: str) -> str:
    if not isinstance(page_url, str):
        raise _fail(EXIT_MISMATCH, "page_url must be a string")
    page = urlsplit(page_url)
    origin = urlsplit(base_url)
    if (
        page.scheme != origin.scheme
        or page.netloc != origin.netloc
        or page.username is not None
        or page.password is not None
        or page.query
        or page.fragment
    ):
        raise _fail(EXIT_MISMATCH, "page_url must use the target origin")
    return page_url


def _count_nodes(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(_count_nodes(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_nodes(item) for item in value)
    return 1


def _observer_home() -> Path:
    return Path(__file__).resolve().parent


def _load_config() -> dict[str, Any]:
    home = _observer_home()
    path = home / "observer.json"
    if not path.is_file():
        raise _fail(EXIT_CONFIG, "observer.json is missing beside the observer")
    _require_private_mode(home, "observer directory")
    _require_private_mode(path, "observer.json")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _fail(EXIT_CONFIG, "observer.json is not readable JSON") from None
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise _fail(EXIT_CONFIG, "observer.json schema_version must be 1")
    observer_id = config.get("observer_id")
    if not isinstance(observer_id, str) or not OBSERVER_ID.fullmatch(observer_id):
        raise _fail(EXIT_CONFIG, "observer.json needs an exact observer_id")
    store_dir = config.get("store_dir", "captures")
    if not isinstance(store_dir, str) or store_dir.startswith("/") or ".." in Path(store_dir).parts:
        raise _fail(EXIT_CONFIG, "store_dir must be a relative path inside the observer home")
    max_age_s = config.get("max_age_s", DEFAULT_MAX_AGE_S)
    if not isinstance(max_age_s, int) or not 60 <= max_age_s <= 604_800:
        raise _fail(EXIT_CONFIG, "max_age_s must be an int between 60 and 604800")
    return {
        "observer_id": observer_id,
        "store": home / store_dir,
        "max_age_s": max_age_s,
        "backend": config.get("backend"),
        "repository_root": config.get("repository_root"),
        "surfaces": config.get("surfaces"),
    }


def _store_path(config: dict[str, Any], observation_id: str) -> Path:
    if not OBSERVATION_ID.fullmatch(observation_id):
        raise _fail(EXIT_MISMATCH, "observation_id is not an exact identifier")
    return config["store"] / f"{observation_id}.json"


def _load_capture(config: dict[str, Any], observation_id: str) -> dict[str, Any]:
    path = _store_path(config, observation_id)
    if not path.is_file():
        raise _fail(
            EXIT_UNKNOWN_OBSERVATION,
            f"observer holds no capture for observation {observation_id}",
        )
    try:
        capture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _fail(EXIT_CONFIG, "stored capture is not readable JSON") from None
    if not isinstance(capture, dict) or not CAPTURE_KEYS.issubset(capture):
        raise _fail(EXIT_CONFIG, "stored capture does not match the capture schema")
    return capture


def _read_request(stream: Any) -> dict[str, Any]:
    raw = stream.read(1_000_000)
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        raise _fail(EXIT_USAGE, "observation request is not valid JSON") from None
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise _fail(EXIT_USAGE, "observation request fields do not match schema")
    request["target"] = _validate_target(request["target"])
    if request["surface_id"] not in SURFACE_PRODUCTS:
        raise _fail(EXIT_USAGE, "observation request surface_id is unsupported")
    if request["runtime_product"] != SURFACE_PRODUCTS[request["surface_id"]]:
        raise _fail(EXIT_USAGE, "observation request runtime product is invalid")
    _same_origin(request["page_url"], request["target"]["base_url"])
    _parse_timestamp(request["captured_at"], "request captured_at")
    if not isinstance(request["assertions"], list) or not request["assertions"]:
        raise _fail(EXIT_USAGE, "observation request needs explicit assertions")
    return request


def replay(stream: Any, out: Any) -> int:
    config = _load_config()
    request = _read_request(stream)
    capture = _load_capture(config, request["observation_id"])
    bound = (
        "surface_id", "target", "host_version", "host_build", "candidate_commit",
        "captured_at", "page_url",
    )
    for field in bound:
        if capture[field] != request[field]:
            raise _fail(
                EXIT_MISMATCH,
                f"stored capture {field} does not match the replayed request",
            )
    if (
        capture["host_product"] != request["runtime_product"]
        or capture["host_identity_source"] != request["runtime_identity_source"]
    ):
        raise _fail(EXIT_MISMATCH, "stored capture runtime identity does not match request")
    if capture["observer_id"] != config["observer_id"]:
        raise _fail(EXIT_MISMATCH, "stored capture belongs to another observer session")
    age = (_now() - _parse_timestamp(capture["captured_at"], "capture captured_at")).total_seconds()
    if age > config["max_age_s"]:
        raise _fail(
            EXIT_STALE,
            f"capture for {request['observation_id']} is stale by {int(age - config['max_age_s'])}s",
        )
    payload = {key: capture[key] for key in CAPTURE_KEYS}
    out.write(json.dumps(payload, sort_keys=True))
    return EXIT_OK


def _run_identity_command(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=4,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, f"{label} is unavailable") from None
    if completed.returncode != 0:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, f"{label} failed")
    return completed


def _argument_value(arguments: list[str], name: str) -> str:
    positions = [index for index, value in enumerate(arguments) if value == name]
    if len(positions) != 1 or positions[0] + 1 >= len(arguments):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, f"live AionCore lacks exact {name}")
    return arguments[positions[0] + 1]


def _probe_signed_bundle_listener(base_url: str) -> dict[str, str]:
    if sys.platform != "darwin":
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "signed AionUi listener proof requires macOS")
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    lsof = _run_identity_command(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        "live listener lookup",
    )
    pids = sorted({line.strip() for line in lsof.stdout.splitlines() if line.strip()})
    if len(pids) != 1 or not pids[0].isdigit():
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "target does not have one verifiable listener")
    process = _run_identity_command(
        ["/bin/ps", "-p", pids[0], "-o", "command="],
        "live listener command",
    )
    try:
        arguments = shlex.split(process.stdout.strip())
    except ValueError:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener command is not parseable") from None
    if not arguments:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener command is empty")
    try:
        executable = Path(arguments[0]).resolve(strict=True)
    except (OSError, RuntimeError):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener executable is unavailable") from None
    bundle = next(
        (candidate for candidate in executable.parents if candidate.suffix.lower() == ".app"),
        None,
    )
    if bundle is None:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener is not inside an AionUi app bundle")
    bundled_core = bundle / "Contents" / "Resources" / "bundled-aioncore"
    if not executable.is_relative_to(bundled_core):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener is not the bundled AionCore")
    listener_host = _argument_value(arguments, "--host")
    listener_port = _argument_value(arguments, "--port")
    app_version = _argument_value(arguments, "--app-version")
    identity_mode = _argument_value(arguments, "--identity-mode")
    if listener_host not in LOOPBACK_HOSTS or listener_port != str(port):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener does not match the loopback target")
    if identity_mode not in {"webui", "aionpro"}:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener identity mode is unsupported")
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "AionUi bundle metadata is unavailable") from None
    version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    if (
        info.get("CFBundleIdentifier") != "com.aionui.app"
        or not isinstance(version, str)
        or not SEMVER.fullmatch(version)
        or version != app_version
    ):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "live listener version does not match AionUi bundle")
    _run_identity_command(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
        "AionUi bundle signature verification",
    )
    signature = _run_identity_command(
        ["/usr/bin/codesign", "-dvvv", str(bundle)],
        "AionUi bundle signature inspection",
    )
    details = f"{signature.stdout}\n{signature.stderr}"
    fields = {
        name: value
        for line in details.splitlines()
        if "=" in line
        for name, value in [line.split("=", 1)]
    }
    build = fields.get("CDHash")
    if (
        fields.get("Identifier") != "com.aionui.app"
        or fields.get("TeamIdentifier") != "52JQX2HUSC"
        or not isinstance(build, str)
        or not re.fullmatch(r"[0-9a-f]{40}", build)
        or "Authority=Developer ID Application: AionUi Inc. (52JQX2HUSC)" not in details
        or "Notarization Ticket=stapled" not in details
    ):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "AionUi bundle signer identity is unverifiable")
    return {
        "product": HOST_PRODUCT,
        "version": version,
        "build": build,
        "source": f"signed-aionui-{identity_mode}-listener",
    }


def probe_host_identity(base_url: str, _timeout_s: float = 4.0) -> dict[str, str]:
    return _probe_signed_bundle_listener(base_url)


def _observed_runtime_binding(
    observation: dict[str, Any], base_url: str
) -> dict[str, str]:
    """Bind host identity and candidate SHA from unforgeable observed sources.

    The signed live AionUi bundled-AionCore listener is always required for host
    identity. The authenticated same-origin status contract is helper-authored in
    the static-helper architecture, so even a syntactically valid host block cannot
    replace that process-level proof. The installed deterministic extension
    manifest independently binds the candidate SHA.
    """
    contract = observation.get("host_status")
    contract_type = contract.get("content_type") if isinstance(contract, dict) else None
    contract_payload = contract.get("payload") if isinstance(contract, dict) else None
    contract_host = contract_payload.get("host") if isinstance(contract_payload, dict) else None
    contract_ok = bool(
        isinstance(contract, dict)
        and contract.get("http_status") == 200
        and isinstance(contract_type, str)
        and "application/json" in contract_type.lower()
        and isinstance(contract_payload, dict)
        and contract_payload.get("schema_version") == SCHEMA_VERSION
        and isinstance(contract_host, dict)
        and contract_host.get("product") == HOST_PRODUCT
        and isinstance(contract_host.get("version"), str)
        and SEMVER.fullmatch(contract_host["version"])
        and isinstance(contract_host.get("build"), str)
        and BUILD_ID.fullmatch(contract_host["build"])
    )
    try:
        host = _probe_signed_bundle_listener(base_url)
    except ObserverError:
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "the live signed AionUi bundled-AionCore listener is not verifiable; "
            "helper-authored host status cannot establish host identity",
        ) from None
    if contract_ok and contract_host["version"] != host["version"]:
        raise _fail(
            EXIT_MISMATCH,
            "helper host status version does not match the signed AionUi listener",
        )
    contract_extension = (
        contract_payload.get("extension") if isinstance(contract_payload, dict) else None
    )
    contract_commit = (
        contract_extension.get("candidate_commit")
        if contract_ok and isinstance(contract_extension, dict)
        else None
    )
    if contract_commit is not None and (
        not isinstance(contract_commit, str) or not FULL_SHA.fullmatch(contract_commit)
    ):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "host status payload lacks the running extension candidate SHA",
        )
    candidate_status = observation.get("candidate_status")
    content_type = candidate_status.get("content_type") if isinstance(candidate_status, dict) else None
    candidate_payload = candidate_status.get("payload") if isinstance(candidate_status, dict) else None
    manifest_commit = (
        candidate_payload.get("candidate_commit")
        if isinstance(candidate_payload, dict)
        and candidate_payload.get("schema_version") == SCHEMA_VERSION
        and candidate_status.get("http_status") == 200
        and isinstance(content_type, str)
        and "application/json" in content_type.lower()
        else None
    )
    if not isinstance(manifest_commit, str) or not FULL_SHA.fullmatch(manifest_commit):
        manifest_commit = None
    if (
        manifest_commit is not None
        and contract_commit is not None
        and manifest_commit != contract_commit
    ):
        raise _fail(
            EXIT_MISMATCH,
            "installed extension candidate manifest does not match the authenticated "
            "same-origin host status",
        )
    candidate_commit = manifest_commit if manifest_commit is not None else contract_commit
    if candidate_commit is None:
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "installed extension candidate manifest is unavailable",
        )
    selected_board = observation.get("selected_board")
    if (
        not isinstance(selected_board, str)
        or not re.fullmatch(r"(?:sandbox|test)-[A-Za-z0-9._-]{1,71}", selected_board)
    ):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "authenticated Home UI does not expose a selected sandbox board",
        )
    return {
        "product": host["product"],
        "version": host["version"],
        "build": host["build"],
        "source": host["source"],
        "candidate_commit": candidate_commit,
        "selected_board": selected_board,
    }


def _git_identity(repository: Path) -> tuple[str, str]:
    head = _run_identity_command(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        "pinned repository HEAD",
    ).stdout.strip()
    status = _run_identity_command(
        ["/usr/bin/git", "-C", str(repository), "status", "--porcelain"],
        "pinned repository status",
    ).stdout
    if not FULL_SHA.fullmatch(head) or status:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "pinned repository is not clean at an exact commit")
    return head, status


def _surface_config(config: dict[str, Any], surface_id: str) -> dict[str, Any]:
    surfaces = config.get("surfaces")
    if not isinstance(surfaces, dict) or set(surfaces) != set(SURFACE_PRODUCTS):
        raise _fail(EXIT_CONFIG, "observer.json lacks exact surface bindings")
    surface = surfaces.get(surface_id)
    if not isinstance(surface, dict):
        raise _fail(EXIT_CONFIG, "observer surface binding is invalid")
    return surface


def _probe_pinned_artifact(
    config: dict[str, Any], surface: dict[str, Any], base_url: str
) -> dict[str, str]:
    repository_value = config.get("repository_root")
    if not isinstance(repository_value, str):
        raise _fail(EXIT_CONFIG, "observer repository_root is unavailable")
    repository = Path(repository_value).resolve()
    head, status = _git_identity(repository)
    if head != surface.get("candidate_commit"):
        raise _fail(EXIT_MISMATCH, "pinned surface repository commit changed")
    relative = surface.get("artifact")
    if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
        raise _fail(EXIT_CONFIG, "pinned surface artifact path is invalid")
    try:
        artifact = (repository / relative).resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "pinned surface artifact is unavailable") from None
    if not artifact.is_relative_to(repository) or not artifact.is_file():
        raise _fail(EXIT_CONFIG, "pinned surface artifact is unavailable")
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != surface.get("artifact_sha256"):
        raise _fail(EXIT_MISMATCH, "pinned surface artifact digest changed")
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    listeners = _run_identity_command(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        "pinned surface listener lookup",
    )
    pids = sorted({line.strip() for line in listeners.stdout.splitlines() if line.strip()})
    if len(pids) != 1 or not pids[0].isdigit():
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "surface has no unique listener process")
    command = _run_identity_command(
        ["/bin/ps", "-p", pids[0], "-o", "command="],
        "pinned surface listener command",
    ).stdout.strip()
    try:
        command_arguments = shlex.split(command)
    except ValueError:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "surface listener command is malformed") from None
    if str(artifact) not in command_arguments:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "surface listener does not execute the pinned artifact")
    return {
        "product": surface["product"],
        "version": surface["version"],
        "build": surface["artifact_sha256"],
        "source": "verifier-pinned-process-artifact",
        "candidate_commit": head,
    }


def _observed_surface_binding(
    config: dict[str, Any], observation: dict[str, Any], surface_id: str, base_url: str
) -> dict[str, str]:
    if config.get("surfaces") is None:
        if surface_id != "aionui":
            raise _fail(EXIT_CONFIG, "non-AionUi capture needs verifier-pinned surface bindings")
        probe_runtime_health(base_url)
        return _observed_runtime_binding(observation, base_url)
    surface = _surface_config(config, surface_id)
    if surface.get("target", {}).get("base_url") != base_url:
        raise _fail(EXIT_MISMATCH, "capture target does not match verifier-pinned surface")
    adapter = surface.get("adapter")
    if adapter == "signed-aionui":
        probe_runtime_health(base_url)
        binding = _observed_runtime_binding(observation, base_url)
    elif adapter == "pinned-process-artifact":
        binding = _probe_pinned_artifact(config, surface, base_url)
        selected_board = observation.get("selected_board")
        if not isinstance(selected_board, str):
            raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "surface UI does not expose its selected board")
        binding["selected_board"] = selected_board
    elif adapter == "pinned-signed-aionui-personal-mcp":
        signed = _observed_runtime_binding(observation, base_url)
        pinned = _probe_personal_mcp_runtime(
            config, surface, observation.get("page_sha256")
        )
        binding = {
            **pinned,
            "source": "verifier-pinned-personal-mcp-runtime",
            "selected_board": signed["selected_board"],
        }
    else:
        raise _fail(EXIT_CONFIG, "surface adapter is unsupported")
    return binding


def _probe_pinned_artifact_without_listener(
    config: dict[str, Any], surface: dict[str, Any], observed_page_sha256: Any
) -> dict[str, str]:
    repository_value = config.get("repository_root")
    if not isinstance(repository_value, str):
        raise _fail(EXIT_CONFIG, "observer repository_root is unavailable")
    repository = Path(repository_value).resolve()
    head, status = _git_identity(repository)
    relative = surface.get("artifact")
    if head != surface.get("candidate_commit") or status or not isinstance(relative, str):
        raise _fail(EXIT_MISMATCH, "pinned surface candidate binding changed")
    try:
        artifact = (repository / relative).resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "pinned surface artifact is unavailable") from None
    if not artifact.is_relative_to(repository) or not artifact.is_file():
        raise _fail(EXIT_CONFIG, "pinned surface artifact escapes repository")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest != surface.get("artifact_sha256"):
        raise _fail(EXIT_MISMATCH, "pinned surface artifact digest changed")
    if observed_page_sha256 != digest:
        raise _fail(EXIT_MISMATCH, "served Personal artifact does not match verifier-pinned bytes")
    return {
        "product": surface["product"],
        "version": surface["version"],
        "build": digest,
        "candidate_commit": head,
    }


_PYTHON_EXECUTABLE = re.compile(r"^python(\d+(\.\d+)?)?$")

# CPython consumes a value for each of these; the interpreter never treats the
# consumed value as its execution selector.
_PYTHON_VALUE_SHORT_FLAGS = frozenset({"W", "X"})
_PYTHON_VALUE_LONG_FLAGS = frozenset({"--check-hash-based-pycs"})


def _python_execution_selector(argv: list[str]) -> tuple[str, str | None, int]:
    """Return the execution mode CPython actually selects for ``argv``.

    CPython stops at the FIRST of ``-c``, ``-m``, ``-`` or a script path and
    ignores every later occurrence, so a tuple such as
    ``-m pursers_personal.cli mcp`` appearing after an earlier ``-c`` is inert
    argument text, not the running program. Scanning for that tuple anywhere in
    the command line therefore accepts a decoy. The returned tuple is the
    selector kind, its value where one exists, and the index of the first
    argument that follows the selector.
    """

    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "-":
            return "stdin", None, index + 1
        if not argument.startswith("-"):
            return "script", argument, index + 1
        if argument.startswith("--"):
            if argument in _PYTHON_VALUE_LONG_FLAGS:
                index += 2
            else:
                index += 1
            continue
        position = 1
        while position < len(argument):
            letter = argument[position]
            if letter in {"c", "m"}:
                kind = "command" if letter == "c" else "module"
                attached = argument[position + 1 :]
                if attached:
                    return kind, attached, index + 1
                if index + 1 >= len(argv):
                    return kind, None, index + 1
                return kind, argv[index + 1], index + 2
            if letter in _PYTHON_VALUE_SHORT_FLAGS:
                if not argument[position + 1 :]:
                    index += 1
                break
            position += 1
        index += 1
    return "repl", None, len(argv)


def _private_runtime_path(value: Any, label: str, repository: Path) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _fail(EXIT_CONFIG, f"Personal runtime {label} must be absolute")
    path = Path(value).resolve()
    if path.is_relative_to(repository) or not path.is_file():
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, f"Personal runtime {label} is unavailable")
    _require_private_mode(path, f"Personal runtime {label}")
    return path


def _probe_personal_mcp_runtime(
    config: dict[str, Any], surface: dict[str, Any], observed_page_sha256: Any
) -> dict[str, str]:
    """Bind Personal evidence to the live exact-source stdio MCP server."""
    pinned = _probe_pinned_artifact_without_listener(
        config, surface, observed_page_sha256
    )
    repository_value = config.get("repository_root")
    runtime = surface.get("runtime")
    if not isinstance(repository_value, str) or not isinstance(runtime, dict):
        raise _fail(EXIT_CONFIG, "Personal MCP runtime binding is unavailable")
    repository = Path(repository_value).resolve()
    if set(runtime) != {
        "artifact",
        "artifact_sha256",
        "challenge_key",
        "pid_file",
        "receipt",
    }:
        raise _fail(EXIT_CONFIG, "Personal MCP runtime fields are invalid")
    relative = runtime["artifact"]
    if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
        raise _fail(EXIT_CONFIG, "Personal MCP runtime artifact is invalid")
    artifact = (repository / relative).resolve(strict=True)
    if not artifact.is_relative_to(repository) or not artifact.is_file():
        raise _fail(EXIT_CONFIG, "Personal MCP runtime artifact escapes repository")
    runtime_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if runtime_digest != runtime["artifact_sha256"]:
        raise _fail(EXIT_MISMATCH, "Personal MCP runtime artifact digest changed")
    pid_file = _private_runtime_path(runtime["pid_file"], "pid file", repository)
    receipt_path = _private_runtime_path(runtime["receipt"], "receipt", repository)
    challenge_key_path = _private_runtime_path(
        runtime["challenge_key"], "challenge key", repository
    )
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "Personal MCP runtime PID is invalid") from None
    command = _run_identity_command(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        "Personal MCP runtime process",
    ).stdout.strip()
    try:
        argv = shlex.split(command)
    except ValueError:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "Personal MCP runtime command is malformed") from None
    required_arguments = {
        "--candidate-source": str(artifact),
        "--candidate-commit": str(surface["candidate_commit"]),
        "--board-id": str(surface["target"]["board_id"]),
        "--acceptance-runtime-receipt": str(receipt_path),
        "--acceptance-challenge-key": str(challenge_key_path),
    }
    if not argv or not _PYTHON_EXECUTABLE.match(Path(argv[0]).name):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "Personal process is not a Python interpreter")
    selector, selected, after = _python_execution_selector(argv)
    if selector != "module" or selected != "pursers_personal.cli":
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            f"Personal process is not the MCP server: execution selector is {selector}",
        )
    if argv[after : after + 1] != ["mcp"]:
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "Personal process is not the MCP server")
    module_argv = argv[after:]
    for flag, expected in required_arguments.items():
        try:
            actual = module_argv[module_argv.index(flag) + 1]
        except (ValueError, IndexError):
            raise _fail(EXIT_CAPABILITY_UNAVAILABLE, f"Personal MCP process lacks {flag}") from None
        if actual != expected:
            raise _fail(EXIT_MISMATCH, f"Personal MCP process {flag} changed")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "Personal MCP runtime receipt is invalid") from None
    expected_receipt = {
        "schema_version": 1,
        "product": "Pursers Personal",
        "server_name": "On Board Personal",
        "version": surface["version"],
        "build": runtime_digest,
        "candidate_commit": surface["candidate_commit"],
        "candidate_source": str(artifact),
        "board_id": surface["target"]["board_id"],
        "pid": pid,
        "transport": "stdio",
    }
    if receipt != expected_receipt:
        raise _fail(EXIT_MISMATCH, "Personal MCP runtime receipt changed")
    return {
        **pinned,
        "version": receipt["version"],
        "build": receipt["build"],
        "candidate_commit": receipt["candidate_commit"],
        # Material for the live transport challenge. capture() recomputes the
        # attestation HMAC against these exact values, so a decoy process that
        # merely looks right on the command line still fails.
        "attestation_pid": str(pid),
        "attestation_build": runtime_digest,
        "attestation_source": str(artifact),
        "attestation_key_path": str(challenge_key_path),
    }


def probe_runtime_health(base_url: str, timeout_s: float = 4.0) -> dict[str, str]:
    """Read the independently exposed AionCore runtime version and build."""
    opener = build_opener(ProxyHandler({}))
    endpoint = urljoin(f"{base_url}/", "health")
    try:
        with opener.open(endpoint, timeout=timeout_s) as response:
            body = response.read(1_048_577)
    except HTTPError as exc:
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            f"runtime health route answered HTTP {exc.code}; AionCore identity unavailable",
        ) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            f"runtime health route unreachable ({type(exc).__name__}); AionCore identity unavailable",
        ) from None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "runtime health route did not return the AionCore health contract",
        ) from None
    status = payload.get("status") if isinstance(payload, dict) else None
    version = payload.get("version") if isinstance(payload, dict) else None
    build = payload.get("build_time") if isinstance(payload, dict) else None
    build = str(build) if isinstance(build, (str, int)) and not isinstance(build, bool) else None
    if (
        status != "ok"
        or not isinstance(version, str)
        or not SEMVER.fullmatch(version)
        or not isinstance(build, str)
        or not BUILD_ID.fullmatch(build)
    ):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "runtime health payload lacks a verifiable AionCore version/build",
        )
    return {"product": "AionCore", "version": version, "build": build}


EGO_SCRIPT = """
const taskSpace = %s
const target = %s
const task = await useOrCreateTaskSpace(taskSpace)
await openOrReuseTab(target, { wait: true, timeout: 25 })
await waitForLoad()
const info = await pageInfo()
if (!info || !info.url || info.w === 0 || info.h === 0) {
  throw new Error('viewport unavailable')
}
const frameTree = await cdp('Page.getFrameTree')
const frameId = frameTree && frameTree.frameTree && frameTree.frameTree.frame
  ? frameTree.frameTree.frame.id : null
if (!frameId) {
  throw new Error('main frame unavailable')
}
const isolated = await cdp('Page.createIsolatedWorld', {
  frameId: frameId,
  worldName: 'pursers-verifier-observer',
  grantUniveralAccess: false
})
const contextId = isolated ? isolated.executionContextId : null
if (!contextId) {
  throw new Error('isolated verifier world unavailable')
}
const shot = await cdp('Page.captureScreenshot', { format: 'png' })
const ax = await cdp('Accessibility.getFullAXTree')
const statusResult = await cdp('Runtime.evaluate', {
  expression: `(async () => {
    try {
      const response = await fetch('/pursers/status', {
        credentials: 'same-origin', cache: 'no-store', headers: { accept: 'application/json' }
      })
      let payload = null
      try { payload = await response.json() } catch (_error) {}
      return {
        http_status: response.status,
        content_type: response.headers.get('content-type') || '',
        payload: payload
      }
    } catch (_error) {
      return { http_status: 0, content_type: '', payload: null }
    }
  })()`,
  contextId: contextId,
  awaitPromise: true,
  returnByValue: true
})
const candidateResult = await cdp('Runtime.evaluate', {
  expression: `(async () => {
    try {
      const response = await fetch(new URL('candidate.json', window.location.href), {
        credentials: 'same-origin', cache: 'no-store', headers: { accept: 'application/json' }
      })
      let payload = null
      try { payload = await response.json() } catch (_error) {}
      return {
        http_status: response.status,
        content_type: response.headers.get('content-type') || '',
        payload: payload
      }
    } catch (_error) {
      return { http_status: 0, content_type: '', payload: null }
    }
  })()`,
  contextId: contextId,
  awaitPromise: true,
  returnByValue: true
})
const boardResult = await cdp('Runtime.evaluate', {
  expression: `(() => {
    const node = document.querySelector('[data-helper-field="board"], [data-board-id], #board-id')
    if (!node) return ''
    return (node.getAttribute('data-board-id') || node.textContent || '').trim()
  })()`,
  contextId: contextId,
  returnByValue: true
})
const pageDigestResult = await cdp('Runtime.evaluate', {
  expression: `(async () => {
    try {
      const response = await fetch(window.location.href, { credentials: 'same-origin', cache: 'no-store' })
      if (!response.ok) return ''
      const digest = await crypto.subtle.digest('SHA-256', await response.arrayBuffer())
      return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('')
    } catch (_error) { return '' }
  })()`,
  contextId: contextId,
  awaitPromise: true,
  returnByValue: true
})
const nodes = (ax && ax.nodes ? ax.nodes : []).slice(0, 400).map(function (node) {
  return {
    nodeId: node.nodeId,
    role: node.role && node.role.value ? node.role.value : null,
    name: node.name && node.name.value ? node.name.value : null,
    ignored: node.ignored === true
  }
})
cliLog(JSON.stringify({
  page_url: info.url,
  screenshot_base64: shot.data,
  snapshot: { title: info.title || '', viewport: { w: info.w, h: info.h }, nodes: nodes },
  host_status: statusResult && statusResult.result ? statusResult.result.value : null,
  candidate_status: candidateResult && candidateResult.result ? candidateResult.result.value : null,
  selected_board: boardResult && boardResult.result ? boardResult.result.value : null,
  page_sha256: pageDigestResult && pageDigestResult.result ? pageDigestResult.result.value : null
}))
"""


EGO_TRANSITION_SCRIPT = """
const taskSpace = %s
const target = %s
const recipe = %s
const task = await useOrCreateTaskSpace(taskSpace)
await openOrReuseTab(target, { wait: true, timeout: 25 })
await waitForLoad()
const info = await pageInfo()
if (!info || !info.url || info.w === 0 || info.h === 0) throw new Error('viewport unavailable')
const frameTree = await cdp('Page.getFrameTree')
const frameId = frameTree && frameTree.frameTree && frameTree.frameTree.frame
  ? frameTree.frameTree.frame.id : null
if (!frameId) throw new Error('main frame unavailable')
const isolated = await cdp('Page.createIsolatedWorld', {
  frameId: frameId,
  worldName: 'pursers-verifier-transition',
  grantUniveralAccess: false
})
const contextId = isolated ? isolated.executionContextId : null
if (!contextId) throw new Error('isolated verifier world unavailable')
const responseActions = recipe.actions.filter(spec => spec.kind === 'click_response_json')
if (responseActions.length > 1) throw new Error('multiple response captures are unavailable')
const responseAction = responseActions[0] || null
const responseCaptureKey = '__pursersVerifierFetchCapture'
if (responseAction) {
  const installed = await cdp('Runtime.evaluate', {
    expression: `(() => {
      const key = ${JSON.stringify(responseCaptureKey)}
      const method = ${JSON.stringify(responseAction.method)}
      const endpoint = ${JSON.stringify(responseAction.endpoint)}
      const pointer = ${JSON.stringify(responseAction.pointer)}
      if (typeof state !== 'object' || !state || typeof state.helper !== 'object' ||
          !state.helper || typeof state.helper.baseUrl !== 'string' ||
          typeof state.helper.token !== 'string' || !state.helper.token) {
        throw new Error('connected helper state unavailable')
      }
      const helperUrl = new URL(state.helper.baseUrl)
      const helperToken = state.helper.token
      const helperHost = helperUrl.hostname.replace(/^\\[|\\]$/g, '').toLowerCase()
      const helperIpv4 = helperHost.split('.').map(Number)
      const helperIsLoopback = helperHost === 'localhost'
        || helperHost.endsWith('.localhost')
        || helperHost === '::1'
        || (helperIpv4.length === 4 && helperIpv4[0] === 127 &&
            helperIpv4.every(part => Number.isInteger(part) && part >= 0 && part <= 255))
      if (helperUrl.protocol !== 'http:' || !helperIsLoopback || helperUrl.username ||
          helperUrl.password || helperUrl.pathname !== '/' || helperUrl.search || helperUrl.hash) {
        throw new Error('configured helper origin is invalid')
      }
      const helperOrigin = helperUrl.origin
      const prior = window[key]
      if (prior && prior.wrapper && window.fetch === prior.wrapper) {
        window.fetch = prior.original
      }
      const original = window.fetch
      const selectJson = (value) => {
        let current = value
        for (const encoded of pointer.slice(1).split('/')) {
          const token = encoded.replace(/~1/g, '/').replace(/~0/g, '~')
          if (current === null || typeof current !== 'object' ||
              !Object.prototype.hasOwnProperty.call(current, token)) {
            throw new Error('response JSON pointer absent')
          }
          current = current[token]
        }
        return current
      }
      const capture = { original, values: [], error: null, wrapper: null, timer: null }
      const wrapper = async function(input, init = {}) {
        const response = await original.call(this, input, init)
        const requestUrl = typeof input === 'string' ? input : input.url
        const requestMethod = String(init.method || input.method || 'GET').toUpperCase()
        let parsed = null
        try { parsed = new URL(requestUrl, window.location.href) } catch (_error) {}
        let requestToken = ''
        try { requestToken = new Headers(init.headers).get('x-pursers-home-token') || '' }
        catch (_error) {}
        const trustedHelperRequest = parsed && parsed.origin === helperOrigin
          && parsed.pathname === endpoint && !parsed.search && !parsed.hash
          && init.credentials === 'omit' && init.cache === 'no-store'
          && init.referrerPolicy === 'no-referrer' && requestToken === helperToken
        if (requestMethod === method && trustedHelperRequest) {
          try {
            const body = await response.clone().json()
            capture.values.push(selectJson({ status: response.status, body }))
          } catch (error) {
            capture.error = String(error && error.message ? error.message : error)
          }
        }
        return response
      }
      capture.wrapper = wrapper
      window.fetch = wrapper
      capture.timer = window.setTimeout(() => {
        if (window.fetch === wrapper) window.fetch = original
        if (window[key] === capture) delete window[key]
      }, 30000)
      window[key] = capture
      return true
    })()`,
    awaitPromise: true,
    returnByValue: true
  })
  if (!installed || installed.exceptionDetails || !installed.result ||
      installed.result.value !== true) throw new Error('response capture unavailable')
}
const transitionResult = await cdp('Runtime.evaluate', {
  expression: `(async () => {
    const recipe = ${JSON.stringify(recipe)}
    const read = (selectors) => {
      const result = {}
      for (const spec of selectors) {
        const nodes = Array.from(document.querySelectorAll(spec.selector))
        const node = nodes[0] || null
        if (spec.property === 'count') result[spec.path] = nodes.length
        else if (!node) result[spec.path] = null
        else if (spec.property === 'text') result[spec.path] = (node.textContent || '').trim()
        else if (spec.property === 'value') result[spec.path] = String(node.value ?? '')
        else if (spec.property === 'checked') result[spec.path] = node.checked === true
        else if (spec.property === 'disabled') result[spec.path] = node.disabled === true
        else if (spec.property === 'class') result[spec.path] = String(node.className || '')
        else if (spec.property === 'hidden') result[spec.path] = node.hidden === true
        else throw new Error('unsupported selector property')
      }
      return result
    }
    const beforeAt = new Date().toISOString()
    const before = read(recipe.before)
    const actionAt = new Date().toISOString()
    const action = {}
    const selectJson = (value, pointer) => {
      let current = value
      for (const encoded of pointer.slice(1).split('/')) {
        const token = encoded.replace(/~1/g, '/').replace(/~0/g, '~')
        if (current === null || typeof current !== 'object' ||
            !Object.prototype.hasOwnProperty.call(current, token)) {
          throw new Error('fetch JSON pointer absent')
        }
        current = current[token]
      }
      return current
    }
    for (const spec of recipe.actions) {
      if (spec.kind === 'observe') action[spec.path] = 'observed'
      else if (spec.kind === 'wait') {
        await new Promise(resolve => setTimeout(resolve, spec.milliseconds))
        action[spec.path] = spec.milliseconds
      } else if (spec.kind === 'resource_delta') {
        const startedAt = performance.now()
        await new Promise(resolve => setTimeout(resolve, spec.milliseconds))
        action[spec.path] = performance.getEntriesByType('resource').filter(entry => {
          try {
            const url = new URL(entry.name, window.location.href)
            return entry.startTime >= startedAt && url.origin === window.location.origin
              && url.pathname === spec.endpoint
          } catch (_error) { return false }
        }).length
      } else if (spec.kind === 'fetch' || spec.kind === 'fetch_json') {
        const init = { method: spec.method, credentials: 'same-origin', cache: 'no-store', headers: { accept: 'application/json' } }
        if (spec.body !== null) {
          init.headers['content-type'] = 'application/json'
          init.body = JSON.stringify(spec.body)
        }
        const response = await fetch(spec.endpoint, init)
        let body = null
        try { body = await response.json() } catch (_error) {}
        const envelope = { status: response.status, body: body }
        action[spec.path] = spec.kind === 'fetch_json'
          ? selectJson(envelope, spec.pointer) : envelope
      } else {
        const node = document.querySelector(spec.selector)
        if (!node) throw new Error('action selector absent: ' + spec.selector)
        if (spec.kind === 'click' || spec.kind === 'click_response_json') {
          node.click(); action[spec.path] = 'clicked'
        } else if (spec.kind === 'set_value' || spec.kind === 'select') {
          node.value = spec.value
          node.dispatchEvent(new Event('input', { bubbles: true }))
          node.dispatchEvent(new Event('change', { bubbles: true }))
          action[spec.path] = String(node.value)
        } else if (spec.kind === 'submit') {
          if (typeof node.requestSubmit === 'function') node.requestSubmit()
          else node.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
          action[spec.path] = 'submitted'
        } else if (spec.kind === 'press_key') {
          if (typeof node.focus === 'function') node.focus()
          node.dispatchEvent(new KeyboardEvent('keydown', {
            key: spec.key, bubbles: true, cancelable: true
          }))
          action[spec.path] = spec.key
        } else throw new Error('unsupported browser action')
      }
    }
    if (recipe.settle_milliseconds) {
      await new Promise(resolve => setTimeout(resolve, recipe.settle_milliseconds))
    }
    const afterAt = new Date().toISOString()
    const after = read(recipe.after)
    return { before: before, action: action, after: after, order: {
      before_at: beforeAt, action_at: actionAt, after_at: afterAt
    }}
  })()`,
  contextId: contextId,
  awaitPromise: true,
  returnByValue: true
})
const bindingResult = await cdp('Runtime.evaluate', {
  expression: `(async () => {
    const readJson = async (url) => {
      try {
        const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { accept: 'application/json' } })
        let payload = null
        try { payload = await response.json() } catch (_error) {}
        return { http_status: response.status, content_type: response.headers.get('content-type') || '', payload: payload }
      } catch (_error) { return { http_status: 0, content_type: '', payload: null } }
    }
    const host = await readJson('/pursers/status')
    const candidate = await readJson(new URL('candidate.json', window.location.href))
    const boardNode = document.querySelector('[data-helper-field="board"], [data-board-id], #board-id')
    const selectedBoard = boardNode ? (boardNode.getAttribute('data-board-id') || boardNode.textContent || '').trim() : ''
    let pageSha = ''
    try {
      const response = await fetch(window.location.href, { credentials: 'same-origin', cache: 'no-store' })
      if (response.ok) {
        const digest = await crypto.subtle.digest('SHA-256', await response.arrayBuffer())
        pageSha = Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('')
      }
    } catch (_error) {}
    return { host_status: host, candidate_status: candidate, selected_board: selectedBoard, page_sha256: pageSha }
  })()`,
  contextId: contextId,
  awaitPromise: true,
  returnByValue: true
})
const transition = transitionResult && transitionResult.result ? transitionResult.result.value : null
const binding = bindingResult && bindingResult.result ? bindingResult.result.value : null
if (!transition || !binding) throw new Error('transition result unavailable')
if (responseAction) {
  const captured = await cdp('Runtime.evaluate', {
    expression: `(() => {
      const key = ${JSON.stringify(responseCaptureKey)}
      const state = window[key]
      if (!state) return null
      window.clearTimeout(state.timer)
      if (window.fetch === state.wrapper) window.fetch = state.original
      delete window[key]
      return { values: state.values, error: state.error }
    })()`,
    returnByValue: true
  })
  const value = captured && captured.result ? captured.result.value : null
  if (!value || value.error || !Array.isArray(value.values) ||
      value.values.length !== 1) throw new Error('response capture did not match exactly once')
  transition.action[responseAction.path] = value.values[0]
}
cliLog(JSON.stringify({
  page_url: info.url,
  before: transition.before,
  action: transition.action,
  after: transition.after,
  order: transition.order,
  host_status: binding.host_status,
  candidate_status: binding.candidate_status,
  selected_board: binding.selected_board,
  page_sha256: binding.page_sha256
}))
"""


def _validate_backend_command(config: dict[str, Any], command: list[str]) -> list[str]:
    if not command or not all(isinstance(part, str) for part in command):
        raise _fail(EXIT_CONFIG, "backend command must be a list of strings")
    executable = Path(command[0])
    if not executable.is_absolute():
        raise _fail(EXIT_CONFIG, "backend command must be an absolute path")
    try:
        resolved = executable.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "backend command is unavailable") from None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise _fail(EXIT_CAPABILITY_UNAVAILABLE, "backend command is not executable")
    repository_root = config.get("repository_root")
    if isinstance(repository_root, str) and repository_root:
        root = Path(repository_root).resolve()
        if resolved.is_relative_to(root):
            raise _fail(EXIT_CONFIG, "backend command must live outside the checkout")
    if resolved.is_relative_to(config["store"].resolve().parent / config["store"].name):
        raise _fail(EXIT_CONFIG, "backend command must live outside the capture store")
    return [str(resolved), *command[1:]]


def _run_backend(config: dict[str, Any], page_url: str) -> dict[str, Any]:
    backend = config.get("backend")
    if not isinstance(backend, dict) or backend.get("kind") not in {"ego-browser", "command"}:
        raise _fail(EXIT_CONFIG, "observer.json needs an ego-browser or command backend")
    raw_command = backend.get("command")
    command = [raw_command] if isinstance(raw_command, str) else raw_command
    if not isinstance(command, list):
        raise _fail(EXIT_CONFIG, "backend command must be a string or list of strings")
    command = _validate_backend_command(config, command)
    if backend["kind"] == "ego-browser":
        argv = [*command, "nodejs"]
        task_space = backend.get("task_space", "pursers-home-acceptance")
        if not isinstance(task_space, (str, int)) or isinstance(task_space, bool):
            raise _fail(EXIT_CONFIG, "ego-browser task_space must be a string or integer")
        if isinstance(task_space, str) and not 1 <= len(task_space) <= 128:
            raise _fail(EXIT_CONFIG, "ego-browser task_space must be 1-128 characters")
        if isinstance(task_space, int) and task_space < 1:
            raise _fail(EXIT_CONFIG, "ego-browser task_space must be positive")
        payload = EGO_SCRIPT % (json.dumps(task_space), json.dumps(page_url))
        extra_path = str(Path(command[0]).parent)
    else:
        argv = command
        payload = json.dumps({"page_url": page_url}, sort_keys=True)
        extra_path = str(Path(command[0]).parent)
    env = {
        "PATH": os.pathsep.join([extra_path, os.defpath]),
        "LANG": "C",
        "LC_ALL": "C",
        # Capture mode runs from the runner, where the browser channel needs its
        # own installed state. Replay mode never reaches the backend, so the
        # stripped harness environment stays intact.
        "HOME": os.environ.get("HOME") or str(_observer_home()),
    }
    for passthrough in ("TMPDIR", "USER", "LOGNAME", "SHELL", "XDG_RUNTIME_DIR"):
        value = os.environ.get(passthrough)
        if value:
            env[passthrough] = value
    if isinstance(backend.get("env"), dict):
        for key, value in backend["env"].items():
            if isinstance(key, str) and isinstance(value, str):
                env[key] = value
    try:
        completed = subprocess.run(
            argv,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=int(backend.get("timeout_s", 120)),
            cwd=_observer_home(),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(
            EXIT_CAPTURE_FAILED, f"browser backend execution failed ({type(exc).__name__})"
        ) from None
    if completed.returncode != 0:
        tail = completed.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise _fail(EXIT_CAPTURE_FAILED, f"browser backend exited {completed.returncode}: {tail[0][:200]}")
    observation: dict[str, Any] | None = None
    # Backends differ in which stream carries their result line: the
    # ego-browser CLI logs through stderr, scripted backends through stdout.
    for stream in (completed.stdout, completed.stderr):
        for line in reversed(stream.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "screenshot_base64" in candidate:
                observation = candidate
                break
        if observation is not None:
            break
    expected_keys = {
        "page_url", "screenshot_base64", "snapshot", "host_status", "candidate_status",
        "selected_board", "page_sha256",
    }
    if observation is None or set(observation) != expected_keys:
        diagnostic = (completed.stderr.strip().splitlines() or [""])[-1][:120]
        raise _fail(
            EXIT_CAPTURE_FAILED,
            f"browser backend returned no valid observation JSON ({diagnostic or 'no stderr'})",
        )
    try:
        screenshot = base64.b64decode(observation["screenshot_base64"], validate=True)
    except (TypeError, ValueError, binascii.Error):
        raise _fail(EXIT_CAPTURE_FAILED, "browser backend screenshot is not valid base64") from None
    if screenshot[:8] != b"\x89PNG\r\n\x1a\n" or len(screenshot) < 256:
        raise _fail(EXIT_CAPTURE_FAILED, "browser backend screenshot is not a substantive PNG")
    snapshot = observation["snapshot"]
    serialized = json.dumps(snapshot, sort_keys=True)
    if (
        not isinstance(snapshot, (dict, list))
        or _count_nodes(snapshot) < MIN_SNAPSHOT_NODES
        or len(serialized) < 128
        or len(serialized) > MAX_CAPTURE_BYTES
    ):
        raise _fail(EXIT_CAPTURE_FAILED, "browser backend accessibility snapshot is not substantive")
    return observation


def _validate_transition_selectors(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise _fail(EXIT_USAGE, f"{label} selectors are empty or unbounded")
    paths: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "selector", "property"}:
            raise _fail(EXIT_USAGE, f"{label} selector fields do not match schema")
        path = item["path"]
        selector = item["selector"]
        if (
            not isinstance(path, str) or not path.startswith("/") or path in paths
            or not isinstance(selector, str) or not selector or len(selector) > 512
            or item["property"] not in TRANSITION_PROPERTIES
        ):
            raise _fail(EXIT_USAGE, f"{label} selector is invalid")
        paths.add(path)
    return value


def _validate_transition_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 32:
        raise _fail(EXIT_USAGE, "transition actions are empty or unbounded")
    paths: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in TRANSITION_ACTION_KEYS:
            raise _fail(EXIT_USAGE, "transition action kind is unsupported")
        kind = item["kind"]
        if set(item) != TRANSITION_ACTION_KEYS[kind]:
            raise _fail(EXIT_USAGE, "transition action fields do not match schema")
        path = item["path"]
        if not isinstance(path, str) or not path.startswith("/") or path in paths:
            raise _fail(EXIT_USAGE, "transition action path is invalid")
        paths.add(path)
        if kind in {
            "click", "set_value", "select", "submit", "press_key",
            "click_response_json",
        } and (
            not isinstance(item["selector"], str) or not item["selector"]
            or len(item["selector"]) > 512
        ):
            raise _fail(EXIT_USAGE, "transition action selector is invalid")
        if kind in {"set_value", "select"} and not isinstance(item["value"], str):
            raise _fail(EXIT_USAGE, "transition action value is invalid")
        if kind == "press_key" and item["key"] not in {
            "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", " ",
            "Escape",
        }:
            raise _fail(EXIT_USAGE, "transition key action is invalid")
        if kind in {"wait", "resource_delta"} and (
            not isinstance(item["milliseconds"], int)
            or isinstance(item["milliseconds"], bool)
            or not 0 <= item["milliseconds"] <= 10_000
        ):
            raise _fail(EXIT_USAGE, "transition wait is invalid")
        if kind == "resource_delta" and (
            not isinstance(item["endpoint"], str)
            or not item["endpoint"].startswith("/")
            or item["endpoint"].startswith("//")
            or "?" in item["endpoint"]
            or "#" in item["endpoint"]
        ):
            raise _fail(EXIT_USAGE, "transition resource endpoint is invalid")
        if kind in {"fetch", "fetch_json"} and (
            item["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(item["endpoint"], str)
            or not item["endpoint"].startswith("/")
            or item["endpoint"].startswith("//") or "#" in item["endpoint"]
            or item["body"] is not None and not isinstance(item["body"], dict)
        ):
            raise _fail(EXIT_USAGE, "transition fetch action is invalid")
        if kind == "click_response_json" and (
            item["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(item["endpoint"], str)
            or not item["endpoint"].startswith("/")
            or item["endpoint"].startswith("//")
            or "#" in item["endpoint"]
        ):
            raise _fail(EXIT_USAGE, "transition response capture is invalid")
        if kind in {"fetch_json", "click_response_json"} and (
            not isinstance(item["pointer"], str)
            or len(item["pointer"]) > 512
            or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)+", item["pointer"])
            is None
        ):
            raise _fail(EXIT_USAGE, "transition fetch JSON pointer is invalid")
    if sum(item["kind"] == "click_response_json" for item in value) > 1:
        raise _fail(EXIT_USAGE, "transition response capture must be unique")
    return value


def _validate_transition_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "before", "actions", "after", "settle_milliseconds"
    }:
        raise _fail(EXIT_USAGE, "transition recipe fields do not match schema")
    _validate_transition_selectors(value["before"], "before")
    _validate_transition_actions(value["actions"])
    _validate_transition_selectors(value["after"], "after")
    delay = value["settle_milliseconds"]
    if (
        not isinstance(delay, int) or isinstance(delay, bool)
        or not 0 <= delay <= 10_000
    ):
        raise _fail(EXIT_USAGE, "transition settle delay is invalid")
    return value


def _run_transition_backend(
    config: dict[str, Any], page_url: str, recipe: dict[str, Any]
) -> dict[str, Any]:
    backend = config.get("backend")
    if not isinstance(backend, dict) or backend.get("kind") not in {"ego-browser", "command"}:
        raise _fail(EXIT_CONFIG, "observer.json needs an ego-browser or command backend")
    raw_command = backend.get("command")
    command = [raw_command] if isinstance(raw_command, str) else raw_command
    if not isinstance(command, list):
        raise _fail(EXIT_CONFIG, "backend command must be a string or list of strings")
    command = _validate_backend_command(config, command)
    if backend["kind"] == "ego-browser":
        argv = [*command, "nodejs"]
        task_space = backend.get("task_space", "pursers-home-acceptance")
        if not isinstance(task_space, (str, int)) or isinstance(task_space, bool):
            raise _fail(EXIT_CONFIG, "ego-browser task_space must be a string or integer")
        payload = EGO_TRANSITION_SCRIPT % (
            json.dumps(task_space), json.dumps(page_url), json.dumps(recipe)
        )
    else:
        argv = command
        payload = json.dumps(
            {"page_url": page_url, "recipe": recipe}, sort_keys=True
        )
    environment = {
        "PATH": os.pathsep.join([str(Path(command[0]).parent), os.defpath]),
        "LANG": "C", "LC_ALL": "C",
        "HOME": os.environ.get("HOME") or str(_observer_home()),
    }
    for passthrough in ("TMPDIR", "USER", "LOGNAME", "SHELL", "XDG_RUNTIME_DIR"):
        value = os.environ.get(passthrough)
        if value:
            environment[passthrough] = value
    if isinstance(backend.get("env"), dict):
        environment.update({
            key: value for key, value in backend["env"].items()
            if isinstance(key, str) and isinstance(value, str)
        })
    try:
        completed = subprocess.run(
            argv, input=payload, text=True, capture_output=True, check=False,
            timeout=int(backend.get("timeout_s", 120)), cwd=_observer_home(),
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(
            EXIT_CAPTURE_FAILED,
            f"browser transition backend failed ({type(exc).__name__})",
        ) from None
    if completed.returncode:
        tail = completed.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise _fail(
            EXIT_CAPTURE_FAILED,
            f"browser transition backend exited {completed.returncode}: {tail[0][:200]}",
        )
    result: dict[str, Any] | None = None
    for stream in (completed.stdout, completed.stderr):
        for line in reversed(stream.splitlines()):
            if not line.strip().startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "before" in candidate:
                result = candidate
                break
        if result is not None:
            break
    expected = {
        "page_url", "before", "action", "after", "order", "host_status",
        "candidate_status", "selected_board", "page_sha256",
    }
    if not isinstance(result, dict) or set(result) != expected:
        raise _fail(EXIT_CAPTURE_FAILED, "browser transition result fields do not match schema")
    for field in ("before", "action", "after"):
        if not isinstance(result[field], dict):
            raise _fail(EXIT_CAPTURE_FAILED, f"browser transition {field} is not structured")
    order = result["order"]
    if not isinstance(order, dict) or set(order) != {"before_at", "action_at", "after_at"}:
        raise _fail(EXIT_CAPTURE_FAILED, "browser transition order is invalid")
    moments = [
        _parse_timestamp(order[field], f"transition {field}")
        for field in ("before_at", "action_at", "after_at")
    ]
    if moments != sorted(moments):
        raise _fail(EXIT_CAPTURE_FAILED, "browser transition order is not causal")
    return result


ATTESTATION_NONCE = re.compile(r"^[0-9a-f]{32,128}$")

ATTESTATION_CLAIM_KEYS = (
    "schema_version",
    "server_name",
    "version",
    "build",
    "candidate_commit",
    "candidate_source",
    "board_id",
    "pid",
    "transport",
    "nonce",
)


def _iter_snapshot_strings(value: Any) -> Any:
    """Yield every string the accessibility snapshot carries, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_snapshot_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_snapshot_strings(item)


def _snapshot_attestations(snapshot: Any) -> list[dict[str, Any]]:
    """Extract every candidate attestation object the capture contains.

    The observer has no channel to the Personal stdio transport, so the answer
    to the verifier nonce travels that transport into the conversation and
    lands in the accessibility snapshot the reviewer already captures. Text in
    the snapshot is untrusted until the HMAC is recomputed.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    for text in _iter_snapshot_strings(snapshot):
        if "signature" not in text:
            continue
        index = text.find("{")
        while index != -1:
            try:
                candidate, end = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                index = text.find("{", index + 1)
                continue
            if isinstance(candidate, dict) and "signature" in candidate:
                found.append(candidate)
            index = text.find("{", max(end, index + 1))
    return found


def _verify_acceptance_attestation(
    binding: dict[str, str], spec: dict[str, Any], snapshot: Any
) -> dict[str, Any]:
    """Recompute the live transport challenge carried by the capture."""
    key_path = Path(binding["attestation_key_path"])
    if key_path.is_symlink():
        raise _fail(EXIT_CONFIG, "acceptance challenge key must not be a symlink")
    try:
        status = key_path.stat()
        key = key_path.read_bytes()
    except OSError:
        raise _fail(EXIT_CONFIG, "acceptance challenge key is unreadable") from None
    if status.st_mode & 0o077:
        raise _fail(EXIT_CONFIG, "acceptance challenge key must be private")
    if len(key) < 32:
        raise _fail(EXIT_CONFIG, "acceptance challenge key is too short")
    expected = {
        "schema_version": 1,
        "server_name": "On Board Personal",
        "version": binding["version"],
        "build": binding["attestation_build"],
        "candidate_commit": binding["candidate_commit"],
        "candidate_source": binding["attestation_source"],
        "board_id": binding["selected_board"],
        "pid": int(binding["attestation_pid"]),
        "transport": "stdio",
        "nonce": spec["attestation_nonce"],
    }
    payload = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    for candidate in _snapshot_attestations(snapshot):
        if set(candidate) != {*ATTESTATION_CLAIM_KEYS, "signature"}:
            continue
        claim = {name: candidate[name] for name in ATTESTATION_CLAIM_KEYS}
        if claim != expected:
            continue
        if not isinstance(candidate["signature"], str):
            continue
        if hmac.compare_digest(candidate["signature"], signature):
            return candidate
    raise _fail(
        EXIT_MISMATCH,
        "capture carries no valid acceptance_runtime_attest answer for this nonce",
    )


SPEC_KEYS = {
    "schema_version", "observation_id", "target", "candidate_commit",
    "page_url", "assertions", "surface_id", "attestation_nonce",
}


def _read_spec(spec_path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _fail(EXIT_USAGE, "capture spec is not readable JSON") from None
    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS:
        raise _fail(EXIT_USAGE, "capture spec fields do not match schema")
    if spec["schema_version"] != SCHEMA_VERSION:
        raise _fail(EXIT_USAGE, "capture spec schema_version must be 1")
    if not OBSERVATION_ID.fullmatch(str(spec["observation_id"])):
        raise _fail(EXIT_USAGE, "capture spec observation_id is not an exact identifier")
    commit = spec["candidate_commit"]
    if not isinstance(commit, str) or not FULL_SHA.fullmatch(commit):
        raise _fail(EXIT_USAGE, "capture spec candidate_commit must be a full 40-hex SHA")
    spec["target"] = _validate_target(spec["target"])
    if spec["surface_id"] not in SURFACE_PRODUCTS:
        raise _fail(EXIT_USAGE, "capture spec surface_id is unsupported")
    _same_origin(spec["page_url"], spec["target"]["base_url"])
    if not isinstance(spec["assertions"], list) or not spec["assertions"]:
        raise _fail(EXIT_USAGE, "capture spec needs explicit assertions")
    nonce = spec["attestation_nonce"]
    if not isinstance(nonce, str) or not ATTESTATION_NONCE.fullmatch(nonce):
        raise _fail(
            EXIT_USAGE,
            "capture spec attestation_nonce must be 32-128 lowercase hex characters",
        )
    return spec


def capture(spec_path: Path, out: Any) -> int:
    config = _load_config()
    spec = _read_spec(spec_path)
    observation = _run_backend(config, spec["page_url"])
    binding = _observed_surface_binding(
        config, observation, spec["surface_id"], spec["target"]["base_url"]
    )
    observed_page_url = _same_origin(observation["page_url"], spec["target"]["base_url"])
    if binding["candidate_commit"] != spec["candidate_commit"]:
        raise _fail(
            EXIT_MISMATCH,
            "requested candidate_commit does not match the running extension",
        )
    if binding["selected_board"] != spec["target"]["board_id"]:
        raise _fail(
            EXIT_MISMATCH,
            "requested board does not match the selected Home board",
        )
    observed_target = {
        "base_url": spec["target"]["base_url"],
        "board_id": binding["selected_board"],
    }
    # The Personal adapter answers a verifier nonce over the same stdio
    # transport AionUi drives, so the attestation arrives inside this capture's
    # accessibility snapshot. A missing, stale, replayed or unsigned answer
    # fails closed here.
    attestation = None
    if "attestation_key_path" in binding:
        attestation = _verify_acceptance_attestation(
            binding, spec, observation["snapshot"]
        )
    payload = {
        "observer_id": config["observer_id"],
        "observation_id": spec["observation_id"],
        "surface_id": spec["surface_id"],
        "target": observed_target,
        "host_product": binding["product"],
        "host_version": binding["version"],
        "host_build": binding["build"],
        "host_identity_source": binding["source"],
        "candidate_commit": binding["candidate_commit"],
        "captured_at": _now().isoformat().replace("+00:00", "Z"),
        "page_url": observed_page_url,
        "screenshot_base64": observation["screenshot_base64"],
        "snapshot": observation["snapshot"],
        "attestation_nonce": spec["attestation_nonce"],
        "attestation": attestation,
    }
    record = {
        **payload,
        "screenshot_sha256": hashlib.sha256(
            base64.b64decode(payload["screenshot_base64"])
        ).hexdigest(),
        "assertions": spec["assertions"],
        "recorded_at": payload["captured_at"],
    }
    store = config["store"]
    store.mkdir(parents=True, exist_ok=True)
    os.chmod(store, 0o700)
    destination = _store_path(config, spec["observation_id"])
    destination.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.chmod(destination, 0o600)
    out.write(json.dumps(payload, sort_keys=True))
    return EXIT_OK


USAGE = (
    "usage: browser_observer.py                          replay one stdin observation request\n"
    "       browser_observer.py capture --spec FILE       record one real browser observation\n"
    "       browser_observer.py transition                record one typed browser transition\n"
    "       browser_observer.py probe-browser --page URL  report browser-channel health only\n"
)


def probe_browser(page_url: str, out: Any) -> int:
    """Report real browser-channel health without recording any capture."""
    config = _load_config()
    observation = _run_backend(config, page_url)
    screenshot = base64.b64decode(observation["screenshot_base64"], validate=True)
    snapshot = observation["snapshot"]
    binding = _observed_runtime_binding(observation, urlsplit(page_url)._replace(path="", query="", fragment="").geturl())
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else None
    out.write(
        json.dumps(
            {
                "observed_page_url": observation["page_url"],
                "screenshot_bytes": len(screenshot),
                "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
                "snapshot_nodes": len(nodes) if isinstance(nodes, list) else _count_nodes(snapshot),
                "snapshot_bytes": len(json.dumps(snapshot, sort_keys=True)),
                "host": {
                    "product": binding["product"],
                    "version": binding["version"],
                    "build": binding["build"],
                    "source": binding["source"],
                },
                "candidate_commit": binding["candidate_commit"],
                "selected_board": binding["selected_board"],
                "evidence_written": False,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def _read_transition_spec(stream: Any) -> dict[str, Any]:
    try:
        value = json.load(stream)
    except (OSError, json.JSONDecodeError):
        raise _fail(EXIT_USAGE, "transition spec is not readable JSON") from None
    expected = {
        "schema_version", "context", "surface_id", "target",
        "candidate_commit", "page_url", "recipe",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise _fail(EXIT_USAGE, "transition spec fields do not match schema")
    if value["schema_version"] != SCHEMA_VERSION:
        raise _fail(EXIT_USAGE, "transition schema_version is unsupported")
    context = value["context"]
    if not isinstance(context, dict) or set(context) != TRANSITION_CONTEXT_KEYS:
        raise _fail(EXIT_USAGE, "transition context fields do not match schema")
    value["target"] = _validate_target(value["target"])
    for field in ("observation_id", "run_id", "action_id", "entity"):
        if not OBSERVATION_ID.fullmatch(str(context[field])):
            raise _fail(EXIT_USAGE, f"transition {field} is invalid")
    if (
        context["surface"] != value["surface_id"]
        or context["board_id"] != value["target"].get("board_id")
        or context["candidate_commit"] != value["candidate_commit"]
        or not isinstance(context["causal_index"], int)
        or isinstance(context["causal_index"], bool)
        or context["causal_index"] < 0
    ):
        raise _fail(EXIT_USAGE, "transition context does not match its target")
    _parse_timestamp(context["issued_at"], "transition issued_at")
    if value["surface_id"] not in SURFACE_PRODUCTS:
        raise _fail(EXIT_USAGE, "transition surface is unsupported")
    if not FULL_SHA.fullmatch(str(value["candidate_commit"])):
        raise _fail(EXIT_USAGE, "transition candidate_commit must be a full SHA")
    _same_origin(value["page_url"], value["target"]["base_url"])
    value["recipe"] = _validate_transition_recipe(value["recipe"])
    return value


def _transition_phase(
    phase: str,
    selected: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    payload = json.dumps(
        selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return {
        "method": "POST" if phase == "action" else "GET",
        "path": f"/browser/state/{phase}",
        "status": 200,
        "selected": selected,
        "response_sha256": hashlib.sha256(payload).hexdigest(),
        "correlation": {
            field: context[field]
            for field in ("observation_id", "run_id", "action_id", "entity")
        },
    }


def transition(stream: Any, out: Any) -> int:
    spec = _read_transition_spec(stream)
    config = _load_config()
    observation = _run_transition_backend(
        config, spec["page_url"], spec["recipe"]
    )
    observed_page = _same_origin(
        observation["page_url"], spec["target"]["base_url"]
    )
    requested_page = urlsplit(spec["page_url"])
    actual_page = urlsplit(observed_page)
    if actual_page.path != requested_page.path:
        raise _fail(EXIT_MISMATCH, "transition browser loaded the wrong page")
    binding = _observed_surface_binding(
        config, observation, spec["surface_id"], spec["target"]["base_url"]
    )
    if (
        binding["candidate_commit"] != spec["candidate_commit"]
        or binding.get("selected_board") != spec["target"]["board_id"]
    ):
        raise _fail(EXIT_MISMATCH, "transition runtime binding changed")
    recipe = spec["recipe"]
    expected_paths = {
        "before": {item["path"] for item in recipe["before"]},
        "action": {item["path"] for item in recipe["actions"]},
        "after": {item["path"] for item in recipe["after"]},
    }
    for phase in ("before", "action", "after"):
        if set(observation[phase]) != expected_paths[phase]:
            raise _fail(
                EXIT_MISMATCH,
                f"transition {phase} selectors differ from verifier recipe",
            )
    result = {
        "schema_version": SCHEMA_VERSION,
        "context": spec["context"],
        "surface_id": spec["surface_id"],
        "target": spec["target"],
        "candidate_commit": spec["candidate_commit"],
        "page_url": spec["page_url"],
        "runtime": {
            "product": binding["product"],
            "version": binding["version"],
            "build": binding["build"],
            "source": binding["source"],
        },
        "before": _transition_phase("before", observation["before"], spec["context"]),
        "action": _transition_phase("action", observation["action"], spec["context"]),
        "after": _transition_phase("after", observation["after"], spec["context"]),
        "order": observation["order"],
    }
    out.write(json.dumps(result, sort_keys=True))
    return EXIT_OK


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 1:
            return replay(sys.stdin, sys.stdout)
        if argv[1] == "probe-browser":
            if len(argv) != 4 or argv[2] != "--page":
                sys.stderr.write(USAGE)
                return EXIT_USAGE
            return probe_browser(argv[3], sys.stdout)
        if argv[1] == "capture":
            if len(argv) != 4 or argv[2] != "--spec":
                sys.stderr.write(USAGE)
                return EXIT_USAGE
            return capture(Path(argv[3]), sys.stdout)
        if argv[1] == "transition":
            if len(argv) != 2:
                sys.stderr.write(USAGE)
                return EXIT_USAGE
            return transition(sys.stdin, sys.stdout)
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    except ObserverError as error:
        sys.stderr.write(f"observer: {error}\n")
        return error.code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
