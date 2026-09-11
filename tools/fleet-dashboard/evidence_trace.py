"""Opt-in, bounded evidence tracing for real Fleet Dashboard requests.

The trace sink is intentionally passive: it observes a closed allowlist of
existing Fleet APIs and never grants authority or creates a mutation route.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CONFIG_KEYS = frozenset(
    {"schema_version", "output_path", "max_bytes", "runtime_id", "board_id", "surface"}
)
CORRELATION_HEADERS = {
    "observation_id": "X-Pursers-Observation-Id",
    "run_id": "X-Pursers-Run-Id",
    "action_id": "X-Pursers-Action-Id",
    "entity": "X-Pursers-Entity-Id",
}
ACTION_DIGEST_HEADER = "X-Pursers-Action-SHA256"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SHA256_RE = re.compile(r"[a-f0-9]{64}")
COMMIT_RE = re.compile(r"[a-f0-9]{40}")
MIN_TRACE_BYTES = 4_096
MAX_TRACE_BYTES = 1_048_576
MAX_CONFIG_BYTES = 8_192
TRACE_ROUTES = frozenset(
    {
        ("GET", "/api/attention"),
        ("POST", "/api/attention"),
        ("POST", "/api/projects/add"),
    }
)


class EvidenceTraceConfigError(ValueError):
    """Raised when verifier-owned trace configuration is unsafe or malformed."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _private_regular(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EvidenceTraceConfigError(f"cannot inspect {label}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise EvidenceTraceConfigError(f"{label} must be a regular 0600 file")
    if info.st_uid != os.getuid():
        raise EvidenceTraceConfigError(f"{label} must be owned by the current user")
    return info


def _checkout_identity(entrypoint: Path) -> tuple[Path, str]:
    try:
        root = subprocess.run(
            ["git", "-C", str(entrypoint.parent), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceTraceConfigError("cannot resolve Fleet checkout identity") from exc
    checkout = Path(root).resolve()
    if not COMMIT_RE.fullmatch(commit) or not entrypoint.is_relative_to(checkout):
        raise EvidenceTraceConfigError("Fleet entrypoint is not in a git checkout")
    return checkout, commit


@dataclass(frozen=True)
class TraceContext:
    observation_id: str
    run_id: str
    action_id: str
    entity: str
    action_sha256: str | None

    def public(self) -> dict[str, str]:
        return {
            "observation_id": self.observation_id,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "entity": self.entity,
        }


class EvidenceTrace:
    """Sanitized request observer backed by a verifier-private bounded JSONL file."""

    def __init__(
        self,
        *,
        output_path: Path,
        max_bytes: int,
        runtime_id: str,
        board_id: str,
        surface: str,
        candidate_commit: str,
        entrypoint_sha256: str,
    ) -> None:
        self.output_path = output_path
        self.max_bytes = max_bytes
        self.runtime_id = runtime_id
        self.board_id = board_id
        self.surface = surface
        self.candidate_commit = candidate_commit
        self.entrypoint_sha256 = entrypoint_sha256
        self._lock = threading.Lock()
        self._seen_actions: set[tuple[str, str, str, str, str]] = set()
        self._load_seen_actions()

    @classmethod
    def from_config(cls, config_path: str | Path, entrypoint_path: str | Path) -> "EvidenceTrace":
        config_file = Path(config_path).expanduser()
        _private_regular(config_file, "evidence trace config")
        try:
            raw = config_file.read_bytes()
            if len(raw) > MAX_CONFIG_BYTES:
                raise EvidenceTraceConfigError("evidence trace config is too large")
            config = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceTraceConfigError("cannot read evidence trace config") from exc
        if not isinstance(config, dict) or set(config) != CONFIG_KEYS:
            raise EvidenceTraceConfigError("evidence trace config has an invalid schema")
        if config["schema_version"] != 1:
            raise EvidenceTraceConfigError("unsupported evidence trace schema")
        max_bytes = config["max_bytes"]
        if not isinstance(max_bytes, int) or not MIN_TRACE_BYTES <= max_bytes <= MAX_TRACE_BYTES:
            raise EvidenceTraceConfigError("evidence trace max_bytes is out of range")
        for key in ("runtime_id", "board_id"):
            if not isinstance(config[key], str) or not ID_RE.fullmatch(config[key]):
                raise EvidenceTraceConfigError(f"invalid evidence trace {key}")
        if config["surface"] != "fleet":
            raise EvidenceTraceConfigError("evidence trace surface must be fleet")
        output_path = Path(config["output_path"])
        if not output_path.is_absolute():
            raise EvidenceTraceConfigError("evidence trace output_path must be absolute")
        output_path = output_path.resolve(strict=False)
        parent = output_path.parent
        try:
            parent_info = parent.stat()
        except OSError as exc:
            raise EvidenceTraceConfigError(
                "evidence trace output directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o077
        ):
            raise EvidenceTraceConfigError("evidence trace output directory must be private")
        if output_path.exists():
            info = _private_regular(output_path, "evidence trace output")
            if info.st_size > max_bytes:
                raise EvidenceTraceConfigError("evidence trace output already exceeds max_bytes")
        entrypoint = Path(entrypoint_path).resolve(strict=True)
        _checkout, candidate_commit = _checkout_identity(entrypoint)
        return cls(
            output_path=output_path,
            max_bytes=max_bytes,
            runtime_id=config["runtime_id"],
            board_id=config["board_id"],
            surface=config["surface"],
            candidate_commit=candidate_commit,
            entrypoint_sha256=hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
        )

    def _load_seen_actions(self) -> None:
        if not self.output_path.exists():
            return
        try:
            for raw in self.output_path.read_bytes().splitlines():
                record = json.loads(raw)
                key = self._action_key_from_record(record)
                if key is not None:
                    self._seen_actions.add(key)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceTraceConfigError("existing evidence trace output is invalid") from exc

    @staticmethod
    def _action_key_from_record(record: Any) -> tuple[str, str, str, str, str] | None:
        if not isinstance(record, dict) or record.get("method") != "POST":
            return None
        values = tuple(
            record.get(key)
            for key in ("observation_id", "run_id", "action_id", "entity", "action_sha256")
        )
        if all(isinstance(value, str) for value in values):
            return values  # type: ignore[return-value]
        return None

    def context(self, headers: Mapping[str, str], method: str, route: str) -> TraceContext | None:
        if (method, route) not in TRACE_ROUTES:
            return None
        values: dict[str, str] = {}
        for key, header in CORRELATION_HEADERS.items():
            value = headers.get(header)
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                return None
            values[key] = value
        action_sha256 = headers.get(ACTION_DIGEST_HEADER)
        if action_sha256 is not None and not SHA256_RE.fullmatch(action_sha256):
            return None
        if method == "POST" and action_sha256 is None:
            return None
        return TraceContext(**values, action_sha256=action_sha256)

    def observe(
        self,
        *,
        context: TraceContext,
        method: str,
        route: str,
        status: int,
        before: Any,
        after: Any,
        result_body: bytes,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return response metadata and append the real POST action once when possible."""
        if (method, route) not in TRACE_ROUTES:
            return None, False
        changed = _digest(before) != _digest(after)
        effect_prefix = "attention_state" if route == "/api/attention" else "project_state"
        record: dict[str, Any] = {
            "schema_version": 1,
            "emitter": "fleet-dashboard-runtime",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "runtime_id": self.runtime_id,
            "pid": os.getpid(),
            "candidate_commit": self.candidate_commit,
            "entrypoint_sha256": self.entrypoint_sha256,
            "board_id": self.board_id,
            "surface": self.surface,
            **context.public(),
            "method": method,
            "path": route,
            "status": status,
            "outcome": "succeeded" if 200 <= status < 300 else "failed",
            "effect": f"{effect_prefix}_{'changed' if changed else 'unchanged'}",
            "changed": changed,
            "before_sha256": _digest(before),
            "after_sha256": _digest(after),
            "result_sha256": hashlib.sha256(result_body).hexdigest(),
            "action_sha256": context.action_sha256,
        }
        emitted = False
        if method == "POST" and context.action_sha256 is not None:
            key = (
                context.observation_id,
                context.run_id,
                context.action_id,
                context.entity,
                context.action_sha256,
            )
            with self._lock:
                if key in self._seen_actions or not self._append(record):
                    return None, False
                self._seen_actions.add(key)
                emitted = True
        metadata = dict(record)
        metadata["log_emitted"] = emitted
        return metadata, emitted

    def _append(self, record: dict[str, Any]) -> bool:
        line = _json_bytes(record) + b"\n"
        if len(line) > self.max_bytes:
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.output_path, flags, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()
                    or info.st_size + len(line) > self.max_bytes
                ):
                    return False
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except OSError:
            return False
