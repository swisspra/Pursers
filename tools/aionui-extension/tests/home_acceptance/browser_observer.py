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
import json
import os
import re
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
    "candidate_commit", "captured_at", "page_url", "assertions",
}
CAPTURE_KEYS = {
    "observer_id", "observation_id", "target", "host_product", "host_version",
    "host_build", "candidate_commit", "captured_at", "page_url",
    "screenshot_base64", "snapshot",
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
    _same_origin(request["page_url"], request["target"]["base_url"])
    _parse_timestamp(request["captured_at"], "request captured_at")
    if not isinstance(request["assertions"], list) or not request["assertions"]:
        raise _fail(EXIT_USAGE, "observation request needs explicit assertions")
    return request


def replay(stream: Any, out: Any) -> int:
    config = _load_config()
    request = _read_request(stream)
    capture = _load_capture(config, request["observation_id"])
    bound = ("target", "host_version", "host_build", "candidate_commit", "captured_at", "page_url")
    for field in bound:
        if capture[field] != request[field]:
            raise _fail(
                EXIT_MISMATCH,
                f"stored capture {field} does not match the replayed request",
            )
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


def _observed_runtime_binding(observation: dict[str, Any]) -> dict[str, str]:
    """Validate identity/SHA from the authenticated browser and board from live UI."""
    contract = observation.get("host_status")
    if not isinstance(contract, dict) or contract.get("http_status") != 200:
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "authenticated same-origin host status is unavailable",
        )
    content_type = contract.get("content_type")
    payload = contract.get("payload")
    if (
        not isinstance(content_type, str)
        or "application/json" not in content_type.lower()
        or not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
    ):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "authenticated host status did not return the Pursers status contract",
        )
    host = payload.get("host")
    if not isinstance(host, dict):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "host status payload lacks a host identity block",
        )
    product, version, build = host.get("product"), host.get("version"), host.get("build")
    if (
        product != HOST_PRODUCT
        or not isinstance(version, str)
        or not SEMVER.fullmatch(version)
        or not isinstance(build, str)
        or not BUILD_ID.fullmatch(build)
    ):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "host status payload lacks a verifiable AionUi version/build",
        )
    extension = payload.get("extension")
    candidate_commit = extension.get("candidate_commit") if isinstance(extension, dict) else None
    if not isinstance(candidate_commit, str) or not FULL_SHA.fullmatch(candidate_commit):
        raise _fail(
            EXIT_CAPABILITY_UNAVAILABLE,
            "host status payload lacks the running extension candidate SHA",
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
        "product": product,
        "version": version,
        "build": build,
        "candidate_commit": candidate_commit,
        "selected_board": selected_board,
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
const boardResult = await cdp('Runtime.evaluate', {
  expression: `(() => {
    const node = document.querySelector('[data-helper-field="board"]')
    return node && node.textContent ? node.textContent.trim() : ''
  })()`,
  contextId: contextId,
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
  selected_board: boardResult && boardResult.result ? boardResult.result.value : null
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
        "page_url", "screenshot_base64", "snapshot", "host_status", "selected_board",
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


SPEC_KEYS = {
    "schema_version", "observation_id", "target", "candidate_commit",
    "page_url", "assertions",
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
    _same_origin(spec["page_url"], spec["target"]["base_url"])
    if not isinstance(spec["assertions"], list) or not spec["assertions"]:
        raise _fail(EXIT_USAGE, "capture spec needs explicit assertions")
    return spec


def capture(spec_path: Path, out: Any) -> int:
    config = _load_config()
    spec = _read_spec(spec_path)
    # A browser backend must not be able to relabel an unreachable/non-AionCore
    # origin with a syntactically valid same-origin contract.
    probe_runtime_health(spec["target"]["base_url"])
    observation = _run_backend(config, spec["page_url"])
    binding = _observed_runtime_binding(observation)
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
    payload = {
        "observer_id": config["observer_id"],
        "observation_id": spec["observation_id"],
        "target": observed_target,
        "host_product": binding["product"],
        "host_version": binding["version"],
        "host_build": binding["build"],
        "candidate_commit": binding["candidate_commit"],
        "captured_at": _now().isoformat().replace("+00:00", "Z"),
        "page_url": observed_page_url,
        "screenshot_base64": observation["screenshot_base64"],
        "snapshot": observation["snapshot"],
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
    "       browser_observer.py probe-browser --page URL  report browser-channel health only\n"
)


def probe_browser(page_url: str, out: Any) -> int:
    """Report real browser-channel health without recording any capture."""
    config = _load_config()
    observation = _run_backend(config, page_url)
    screenshot = base64.b64decode(observation["screenshot_base64"], validate=True)
    snapshot = observation["snapshot"]
    binding = _observed_runtime_binding(observation)
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
                },
                "candidate_commit": binding["candidate_commit"],
                "selected_board": binding["selected_board"],
                "evidence_written": False,
            },
            sort_keys=True,
        )
    )
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
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    except ObserverError as error:
        sys.stderr.write(f"observer: {error}\n")
        return error.code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
