#!/usr/bin/env python3
"""Host-local, typed lifecycle executor for autonomous Pursers seats.

The executor deliberately has no board or model credentials.  It accepts only
the four actions committed by ``autonomous_butler_executor_v1`` over an
owner-only Unix socket, authenticates a pinned local caller, and delegates
service control to a narrow adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import plistlib
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SCHEMA = "autonomous_butler_executor_v1"
SUPERVISOR_ROSTER_SCHEMA = "pursers_supervisor_roster_v1"
SUPERVISOR_ROSTER_FIELDS = frozenset(
    {
        "schema",
        "revision",
        "board_id",
        "config_revision",
        "envelope_fingerprint_sha256",
        "host_seat_cap",
        "gate_concurrency_ceiling",
        "desired",
        "full_gate_concurrency",
        "project_admission",
        "seats",
        "actions",
        "plan_digest_sha256",
        "confirmed_at",
        "audit",
        "findings",
    }
)
SUPERVISOR_ACTION_ROSTER_KINDS = {
    "inspect": frozenset(),
    "start": frozenset({"provision", "start", "resume"}),
    "drain": frozenset({"drain"}),
    "stop": frozenset({"pause", "stop", "remove"}),
    "re_role": frozenset({"re_role"}),
    # External adoption is recovery of an existing seat, never provisioning.
    "adopt": frozenset({"resume"}),
}
SUPERVISOR_ROSTER_ACTION_KINDS = frozenset(
    kind
    for kinds in SUPERVISOR_ACTION_ROSTER_KINDS.values()
    for kind in kinds
)
SIGNING_CONTEXT = b"pursers-executor-v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "message_type",
        "operation_id",
        "board_id",
        "action",
        "seat_id",
        "template_id",
        "template_digest_sha256",
        "expected_seat_generation",
        "authorization_fingerprint_sha256",
        "identity_id",
        "state_id",
        "state_dir_id",
        "supervisor_roster_revision",
        "supervisor_roster_digest_sha256",
        "target_template_id",
        "target_template_digest_sha256",
        "deadline",
        "caller_auth",
    }
)
AUTH_FIELDS = frozenset(
    {
        "scheme",
        "key_id",
        "nonce",
        "signed_at",
        "request_digest_sha256",
        "signature_base64",
    }
)
TEMPLATE_FIELDS = frozenset(
    {
        "role",
        "principal_id",
        "credential_ref",
        "repository_root",
        "seat_root",
        "command",
        "boards",
        "capabilities",
    }
)
CAPABILITY_FIELDS = frozenset({"can_work", "can_review", "tier_max", "max_parallel"})


class PolicyError(ValueError):
    """A request failed closed before a service mutation."""


def canonical_json(value: Any) -> bytes:
    """Canonical bytes for the executor's integer/string-only protocol subset."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_digest(request: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in request.items() if key != "caller_auth"}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def template_digest(template: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(template)).hexdigest()


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PolicyError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise PolicyError(f"{field}_invalid")
    return parsed.astimezone(timezone.utc)


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise PolicyError(f"{field}_invalid")
    return value


def _inside(path: Path, roots: Sequence[Path], field: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise PolicyError(f"{field}_unavailable") from exc
    if path.is_symlink() or not any(resolved.is_relative_to(root) for root in roots):
        raise PolicyError(f"{field}_outside_approved_roots")
    return resolved


@dataclass(frozen=True)
class SeatTemplate:
    template_id: str
    role: str
    principal_id: str
    credential_ref: str
    repository_root: Path
    seat_root: Path
    command: tuple[str, ...]
    boards: str
    capabilities: Mapping[str, bool | int]
    digest_sha256: str

    @classmethod
    def from_record(cls, template_id: str, value: Mapping[str, Any]) -> "SeatTemplate":
        _require_id(template_id, "template_id")
        if set(value) != TEMPLATE_FIELDS:
            raise PolicyError("template_fields_invalid")
        role = value.get("role")
        if role not in {"worker", "reviewer", "acp_worker"}:
            raise PolicyError("template_role_invalid")
        principal_id = _require_id(value.get("principal_id"), "principal_id")
        credential_ref = _require_id(value.get("credential_ref"), "credential_ref")
        boards = value.get("boards")
        if boards != "registry":
            raise PolicyError("template_not_registry_mode")
        capabilities = value.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_FIELDS:
            raise PolicyError("template_capabilities_invalid")
        can_work = capabilities.get("can_work")
        can_review = capabilities.get("can_review")
        tier_max = capabilities.get("tier_max")
        max_parallel = capabilities.get("max_parallel")
        if (
            not isinstance(can_work, bool)
            or not isinstance(can_review, bool)
            or not isinstance(tier_max, int)
            or isinstance(tier_max, bool)
            or tier_max not in {1, 2, 3}
            or not isinstance(max_parallel, int)
            or isinstance(max_parallel, bool)
            or max_parallel != 1
            or (role in {"worker", "acp_worker"} and (not can_work or can_review))
            or (role == "reviewer" and (can_work or not can_review))
        ):
            raise PolicyError("template_capabilities_invalid")
        command = value.get("command")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 64
            or any(
                not isinstance(item, str) or not item or len(item) > 4096
                for item in command
            )
        ):
            raise PolicyError("template_command_invalid")
        canonical = dict(value)
        return cls(
            template_id=template_id,
            role=str(role),
            principal_id=principal_id,
            credential_ref=credential_ref,
            repository_root=Path(str(value.get("repository_root"))),
            seat_root=Path(str(value.get("seat_root"))),
            command=tuple(command),
            boards=boards,
            capabilities=dict(capabilities),
            digest_sha256=template_digest(canonical),
        )


@dataclass(frozen=True)
class ExecutorPolicy:
    authorization_fingerprint_sha256: str
    templates: Mapping[str, SeatTemplate]
    caller_keys: Mapping[str, Ed25519PublicKey]
    credential_paths: Mapping[str, Path]
    repository_roots: tuple[Path, ...]
    seat_roots: tuple[Path, ...]
    board_caps: Mapping[str, int]
    host_cap: int
    signature_skew_s: int = 90
    mutation_cooldown_s: int = 10
    failure_backoff_s: int = 30

    def __post_init__(self) -> None:
        if not SHA256.fullmatch(self.authorization_fingerprint_sha256):
            raise ValueError("authorization fingerprint must be sha256")
        if self.host_cap < 1 or any(value < 0 for value in self.board_caps.values()):
            raise ValueError("executor concurrency caps are invalid")
        if any(
            template.credential_ref not in self.credential_paths
            for template in self.templates.values()
        ):
            raise ValueError("every template credential reference must be operator-resolved")


@dataclass(frozen=True)
class ServiceObservation:
    exists: bool
    running: bool
    ready: bool
    identity_verified: bool
    process_ref: str | None = None


@dataclass(frozen=True)
class LeaseObservation:
    known: bool
    live_work: bool = False
    live_review: bool = False

    @property
    def live(self) -> bool:
        return self.live_work or self.live_review


@dataclass(frozen=True)
class RegistryReadinessObservation:
    known: bool
    ready: bool = False
    selected_active_boards: tuple[str, ...] = ()
    reason_code: str | None = None


class ServiceAdapter(Protocol):
    """Portable service boundary; systemd is the first concrete adapter."""

    def inspect(self, seat_id: str, template: SeatTemplate) -> ServiceObservation: ...

    def instantiate(self, seat_id: str, template: SeatTemplate) -> None: ...

    def start(self, seat_id: str, template: SeatTemplate) -> None: ...

    def drain(self, seat_id: str, template: SeatTemplate) -> None: ...

    def stop(self, seat_id: str, template: SeatTemplate) -> None: ...

    def replace(
        self, seat_id: str, source: SeatTemplate, target: SeatTemplate
    ) -> None: ...


class LeaseProvider(Protocol):
    def observe(self, board_id: str, seat_id: str) -> LeaseObservation: ...


class RegistryReadinessProvider(Protocol):
    def observe(
        self, board_id: str, seat_id: str, template: SeatTemplate
    ) -> RegistryReadinessObservation: ...


class ReceiptPublisher(Protocol):
    def publish(self, receipt: Mapping[str, Any]) -> None: ...


class FileLeaseProvider:
    """Read a bounded, product-produced lease snapshot without board credentials."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        max_bytes: int = 1024 * 1024,
    ) -> None:
        self.path = path
        self.clock = clock
        self.max_bytes = max_bytes

    def observe(self, board_id: str, seat_id: str) -> LeaseObservation:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_size > self.max_bytes
                    or info.st_mode & 0o022
                ):
                    raise PolicyError("lease_snapshot_untrusted")
                raw = os.read(descriptor, self.max_bytes + 1)
                if len(raw) > self.max_bytes or os.read(descriptor, 1):
                    raise PolicyError("lease_snapshot_untrusted")
            finally:
                os.close(descriptor)
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"boards"}:
                raise PolicyError("lease_snapshot_invalid")
            boards = value["boards"]
            if not isinstance(boards, dict):
                raise PolicyError("lease_snapshot_invalid")
            board = boards[board_id]
            if not isinstance(board, dict) or set(board) != {"seats"}:
                raise PolicyError("lease_snapshot_invalid")
            seats = board["seats"]
            if not isinstance(seats, dict):
                raise PolicyError("lease_snapshot_invalid")
            record = seats[seat_id]
            if (
                not isinstance(record, dict)
                or set(record) != {"stale_after", "work", "review"}
                or not isinstance(record["work"], bool)
                or not isinstance(record["review"], bool)
            ):
                raise PolicyError("lease_snapshot_invalid")
            stale_after = _parse_time(record["stale_after"], "stale_after").timestamp()
            if stale_after < self.clock():
                return LeaseObservation(False)
            return LeaseObservation(
                True,
                live_work=record["work"],
                live_review=record["review"],
            )
        except (
            OSError,
            KeyError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
            PolicyError,
        ):
            return LeaseObservation(False)


class FileRegistryReadinessProvider:
    """Verify fresh product read-back for every registry-selected active board."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        max_bytes: int = 1024 * 1024,
    ) -> None:
        self.path = path
        self.clock = clock
        self.max_bytes = max_bytes

    def observe(
        self, board_id: str, seat_id: str, template: SeatTemplate
    ) -> RegistryReadinessObservation:
        try:
            info = self.path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_size > self.max_bytes
                or info.st_mode & 0o022
            ):
                raise PolicyError("registry_readiness_snapshot_untrusted")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {
                "schema", "stale_after", "selected_active_boards", "boards"
            }:
                raise PolicyError("registry_readiness_snapshot_invalid")
            if value["schema"] != "pursers_registry_readiness_v1":
                raise PolicyError("registry_readiness_snapshot_invalid")
            if _parse_time(value["stale_after"], "stale_after").timestamp() < self.clock():
                raise PolicyError("registry_readiness_snapshot_stale")
            selected = value["selected_active_boards"]
            boards = value["boards"]
            if (
                not isinstance(selected, list)
                or not selected
                or len(selected) > 256
                or len(set(selected)) != len(selected)
                or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in selected)
                or not isinstance(boards, dict)
                or board_id not in selected
            ):
                raise PolicyError("registry_readiness_snapshot_invalid")
            expected_role = "reviewer" if template.role == "reviewer" else "member"
            expected_capabilities = dict(template.capabilities)
            for selected_board in selected:
                record = boards.get(selected_board)
                if not isinstance(record, dict) or set(record) != {"seats"}:
                    return RegistryReadinessObservation(
                        True, reason_code="registry_board_readback_missing"
                    )
                seats = record["seats"]
                seat = seats.get(seat_id) if isinstance(seats, dict) else None
                if not isinstance(seat, dict):
                    return RegistryReadinessObservation(
                        True, reason_code="registry_membership_missing"
                    )
                if (
                    seat.get("principal_id") != template.principal_id
                    or seat.get("role") != template.role
                    or seat.get("membership_role") != expected_role
                    or seat.get("lifecycle_status") != "active"
                ):
                    return RegistryReadinessObservation(
                        True, reason_code="registry_identity_mismatch"
                    )
                capabilities = seat.get("capabilities")
                if not isinstance(capabilities, dict) or any(
                    field not in capabilities
                    or type(capabilities[field]) is not type(expected)
                    or capabilities[field] != expected
                    for field, expected in expected_capabilities.items()
                ):
                    return RegistryReadinessObservation(
                        True, reason_code="registry_capabilities_mismatch"
                    )
            return RegistryReadinessObservation(
                True, True, tuple(selected), None
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError, PolicyError):
            return RegistryReadinessObservation(
                False, reason_code="registry_readiness_unknown"
            )


class JsonlReceiptPublisher:
    """Durable bounded handoff for a separate board-authorized publisher."""

    def __init__(self, path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def publish(self, receipt: Mapping[str, Any]) -> None:
        line = canonical_json(receipt) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise RuntimeError("receipt_queue_symlink")
        if self.path.exists():
            operation_id = receipt.get("operation_id")
            request_digest_sha256 = receipt.get("request_digest_sha256")
            for raw in self.path.read_bytes().splitlines():
                try:
                    prior = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("receipt_queue_invalid") from exc
                if prior.get("operation_id") != operation_id:
                    continue
                if prior.get("request_digest_sha256") != request_digest_sha256:
                    raise RuntimeError("receipt_operation_digest_changed")
                return
        if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
            raise RuntimeError("receipt_queue_full")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ExecutorStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("executor state database cannot be a symlink")
        self.connection = sqlite3.connect(path)
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS nonces (
              key_id TEXT NOT NULL, nonce TEXT NOT NULL, digest TEXT NOT NULL,
              operation_id TEXT NOT NULL, reserved_at REAL NOT NULL,
              PRIMARY KEY (key_id, nonce)
            );
            CREATE TABLE IF NOT EXISTS operations (
              operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
              state TEXT NOT NULL, result_json TEXT, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seats (
              seat_id TEXT PRIMARY KEY, board_id TEXT NOT NULL,
              template_id TEXT NOT NULL, template_digest TEXT NOT NULL,
              principal_id TEXT NOT NULL, identity_id TEXT, state_id TEXT,
              state_dir_id TEXT, generation INTEGER NOT NULL,
              lifecycle TEXT NOT NULL, process_ref TEXT,
              last_mutation REAL NOT NULL, last_failure REAL
            );
            """
        )
        existing = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(seats)").fetchall()
        }
        for column in ("identity_id", "state_id", "state_dir_id"):
            if column not in existing:
                self.connection.execute(f"ALTER TABLE seats ADD COLUMN {column} TEXT")

    def operation(self, operation_id: str) -> tuple[str, str, str | None] | None:
        row = self.connection.execute(
            "SELECT digest, state, result_json FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return tuple(row) if row else None

    def reserve(
        self,
        key_id: str,
        nonce: str,
        digest: str,
        operation_id: str,
        now: float,
    ) -> None:
        with self.connection:
            prior = self.connection.execute(
                "SELECT digest, operation_id FROM nonces WHERE key_id = ? AND nonce = ?",
                (key_id, nonce),
            ).fetchone()
            if prior and prior != (digest, operation_id):
                raise PolicyError("nonce_reuse")
            if prior:
                raise PolicyError("ambiguous_prior_attempt")
            self.connection.execute(
                "INSERT INTO nonces VALUES (?, ?, ?, ?, ?)",
                (key_id, nonce, digest, operation_id, now),
            )
            self.connection.execute(
                "INSERT INTO operations VALUES (?, ?, 'pending', NULL, ?)",
                (operation_id, digest, now),
            )

    def finish(self, operation_id: str, result: Mapping[str, Any], now: float) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE operations SET state = 'terminal', result_json = ?, updated_at = ? "
                "WHERE operation_id = ?",
                (canonical_json(result).decode("utf-8"), now, operation_id),
            )

    def seat(self, seat_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT board_id, template_id, template_digest, principal_id, "
            "identity_id, state_id, state_dir_id, generation, "
            "lifecycle, process_ref, last_mutation, last_failure FROM seats WHERE seat_id = ?",
            (seat_id,),
        ).fetchone()
        if not row:
            return None
        keys = (
            "board_id", "template_id", "template_digest", "principal_id",
            "identity_id", "state_id", "state_dir_id", "generation",
            "lifecycle", "process_ref", "last_mutation", "last_failure",
        )
        return dict(zip(keys, row, strict=True))

    def observation_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return detached seat evidence through the supported store interface."""
        with self.connection:
            ids = [row[0] for row in self.connection.execute("SELECT seat_id FROM seats")]
            return {seat_id: record for seat_id in ids if (record := self.seat(seat_id)) is not None}

    def active_counts(self, board_id: str) -> tuple[int, int]:
        active = ("starting", "ready", "busy", "draining", "unhealthy")
        marks = ",".join("?" for _ in active)
        host = self.connection.execute(
            f"SELECT COUNT(*) FROM seats WHERE lifecycle IN ({marks})", active
        ).fetchone()[0]
        board = self.connection.execute(
            f"SELECT COUNT(*) FROM seats WHERE board_id = ? AND lifecycle IN ({marks})",
            (board_id, *active),
        ).fetchone()[0]
        return int(host), int(board)

    def principal_active_elsewhere(self, principal_id: str, seat_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM seats WHERE principal_id = ? AND seat_id <> ? "
            "AND lifecycle NOT IN ('stopped', 'retired') LIMIT 1",
            (principal_id, seat_id),
        ).fetchone()
        return row is not None

    def binding_used_elsewhere(
        self,
        seat_id: str,
        identity_id: str | None,
        state_id: str | None,
        state_dir_id: str | None,
    ) -> bool:
        if any(value is None for value in (identity_id, state_id, state_dir_id)):
            return False
        row = self.connection.execute(
            "SELECT 1 FROM seats WHERE seat_id <> ? AND "
            "(identity_id = ? OR state_id = ? OR state_dir_id = ?) LIMIT 1",
            (seat_id, identity_id, state_id, state_dir_id),
        ).fetchone()
        return row is not None

    def save_seat(
        self,
        *,
        seat_id: str,
        board_id: str,
        template: SeatTemplate,
        identity_id: str | None,
        state_id: str | None,
        state_dir_id: str | None,
        generation: int,
        lifecycle: str,
        process_ref: str | None,
        now: float,
        failed: bool = False,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO seats (seat_id, board_id, template_id, template_digest, "
                "principal_id, identity_id, state_id, state_dir_id, generation, lifecycle, "
                "process_ref, last_mutation, last_failure) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(seat_id) DO UPDATE SET board_id=excluded.board_id, "
                "template_id=excluded.template_id, template_digest=excluded.template_digest, "
                "principal_id=excluded.principal_id, identity_id=excluded.identity_id, "
                "state_id=excluded.state_id, state_dir_id=excluded.state_dir_id, "
                "generation=excluded.generation, "
                "lifecycle=excluded.lifecycle, process_ref=excluded.process_ref, "
                "last_mutation=excluded.last_mutation, "
                "last_failure=CASE WHEN ? THEN excluded.last_failure ELSE seats.last_failure END",
                (
                    seat_id,
                    board_id,
                    template.template_id,
                    template.digest_sha256,
                    template.principal_id,
                    identity_id,
                    state_id,
                    state_dir_id,
                    generation,
                    lifecycle,
                    process_ref,
                    now,
                    now if failed else None,
                    int(failed),
                ),
            )

    def reconcile_unexpected_stop(
        self,
        seat_id: str,
        *,
        expected_generation: int,
        expected_process_ref: str,
        now: float,
    ) -> dict[str, Any] | None:
        """CAS a verified lost process into one new stopped incarnation.

        The caller must prove process absence with the configured service adapter.
        Matching both generation and process reference prevents a stale observer or
        controller restart from retiring a newer process.
        """
        if expected_generation < 1 or not expected_process_ref:
            raise ValueError("unexpected stop evidence is incomplete")
        active = ("starting", "ready", "busy", "draining", "unhealthy")
        marks = ",".join("?" for _ in active)
        with self.connection:
            changed = self.connection.execute(
                "UPDATE seats SET generation = generation + 1, lifecycle = 'stopped', "
                "process_ref = NULL, last_mutation = ?, last_failure = ? "
                f"WHERE seat_id = ? AND generation = ? AND process_ref = ? "
                f"AND lifecycle IN ({marks})",
                (now, now, seat_id, expected_generation, expected_process_ref, *active),
            ).rowcount
        return self.seat(seat_id) if changed == 1 else None

    def adopt_verified_process(
        self,
        seat_id: str,
        *,
        expected_generation: int,
        expected_process_ref: str,
        process_ref: str,
        now: float,
    ) -> dict[str, Any] | None:
        """CAS an explicitly authorized, identity-verified external restart."""
        if expected_generation < 1 or not expected_process_ref or not process_ref:
            raise ValueError("process adoption evidence is incomplete")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE seats SET generation = generation + 1, lifecycle = 'ready', "
                "process_ref = ?, last_mutation = ? WHERE seat_id = ? AND generation = ? "
                "AND process_ref = ? AND lifecycle IN ('starting','ready','busy','draining','unhealthy')",
                (process_ref, now, seat_id, expected_generation, expected_process_ref),
            ).rowcount
        return self.seat(seat_id) if changed == 1 else None


class SystemdUserAdapter:
    """Narrow ``systemctl --user`` adapter with injected runner for tests."""

    def __init__(
        self,
        unit_dir: Path,
        drain_dir: Path,
        credential_paths: Mapping[str, Path],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.unit_dir = unit_dir
        self.drain_dir = drain_dir
        self.credential_paths = credential_paths
        self.runner = runner
        self.proc_root = proc_root

    @staticmethod
    def _unit_name(seat_id: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", seat_id)[:80]
        suffix = hashlib.sha256(seat_id.encode()).hexdigest()[:12]
        return f"pursers-{slug}-{suffix}.service"

    def _unit_path(self, seat_id: str) -> Path:
        return self.unit_dir / self._unit_name(seat_id)

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def _escape_unit_path(value: str) -> str:
        safe = b"/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:+-"
        escaped: list[str] = []
        for byte in value.encode("utf-8"):
            if byte == ord("%"):
                escaped.append("%%")
            elif byte in safe:
                escaped.append(chr(byte))
            else:
                escaped.append(f"\\x{byte:02x}")
        return "".join(escaped)

    def _unit(self, seat_id: str, template: SeatTemplate) -> str:
        command = " ".join(self._quote(item) for item in template.command)
        credential_path = self.credential_paths.get(template.credential_ref)
        if credential_path is None:
            raise RuntimeError("credential_reference_unknown")
        return (
            "[Unit]\nDescription=Pursers managed seat " + seat_id + "\n"
            "[Service]\nType=simple\nWorkingDirectory="
            + self._escape_unit_path(str(template.repository_root))
            + "\n"
            "EnvironmentFile=" + self._escape_unit_path(str(credential_path)) + "\n"
            "ExecStart=" + command + "\nRestart=no\n"
            "[Install]\nWantedBy=default.target\n"
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner(
            ["systemctl", "--user", *args], check=False, text=True, capture_output=True
        )

    @staticmethod
    def _show_properties(output: str) -> dict[str, str]:
        properties: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key or key in properties:
                raise ValueError("systemd_show_invalid")
            properties[key] = value
        return properties

    @staticmethod
    def _execstart_matches(value: str, command: tuple[str, ...]) -> bool:
        match = re.fullmatch(
            r"\{ path=(.+?) ; argv\[\]=(.+?) ; ignore_errors=(?:yes|no) ;.*\}",
            value,
        )
        if match is None:
            return False
        try:
            path = shlex.split(match.group(1))
            argv = shlex.split(match.group(2))
        except ValueError:
            return False
        return path == [command[0]] and tuple(argv) == command

    def _process_identity(
        self,
        pid: int,
        template: SeatTemplate,
        control_group: str,
    ) -> str | None:
        try:
            process = self.proc_root / str(pid)
            executable = (process / "exe").resolve(strict=True)
            expected_executable = Path(template.command[0]).resolve(strict=True)
            command = (process / "cmdline").read_bytes().split(b"\0")
            if command and command[-1] == b"":
                command.pop()
            argv = tuple(item.decode("utf-8") for item in command)
            cgroups = (process / "cgroup").read_text(encoding="utf-8").splitlines()
            cgroup_paths = {
                line.split(":", 2)[2]
                for line in cgroups
                if line.count(":") == 2
            }
            stat_value = (process / "stat").read_text(encoding="utf-8")
            close = stat_value.rfind(")")
            remaining = stat_value[close + 2 :].split() if close >= 0 else []
            start_ticks = remaining[19]
            if (
                executable != expected_executable
                or argv != template.command
                or not control_group
                or control_group not in cgroup_paths
                or not start_ticks.isdigit()
            ):
                return None
            return start_ticks
        except (OSError, UnicodeError, IndexError, ValueError):
            return None

    def inspect(self, seat_id: str, template: SeatTemplate) -> ServiceObservation:
        unit_path = self._unit_path(seat_id)
        unit_exists = unit_path.is_file() and not unit_path.is_symlink()
        expected = self._unit(seat_id, template)
        identity = False
        try:
            identity = (
                unit_path.resolve(strict=True).parent == self.unit_dir.resolve(strict=True)
                and unit_path.read_text(encoding="utf-8") == expected
            )
        except OSError:
            pass
        property_names = (
            "LoadState",
            "ActiveState",
            "SubState",
            "MainPID",
            "FragmentPath",
            "DropInPaths",
            "ExecStart",
            "ControlGroup",
        )
        result = self._run(
            "show",
            self._unit_name(seat_id),
            *(f"--property={name}" for name in property_names),
            "--no-pager",
        )
        if result.returncode != 0:
            return ServiceObservation(unit_exists, False, False, identity)
        try:
            properties = self._show_properties(result.stdout)
        except ValueError:
            return ServiceObservation(True, False, False, False)
        # systemd omits ExecStart for units that have never been created.
        # Treat only a proven inactive, absent unit as available to instantiate.
        if (
            not unit_exists
            and not unit_path.is_symlink()
            and properties.get("LoadState") == "not-found"
            and properties.get("ActiveState") == "inactive"
            and properties.get("SubState") == "dead"
            and properties.get("MainPID") == "0"
            and not properties.get("FragmentPath")
            and not properties.get("DropInPaths")
            and not properties.get("ExecStart")
            and not properties.get("ControlGroup")
        ):
            return ServiceObservation(False, False, False, False)
        if set(properties) != set(property_names):
            return ServiceObservation(True, False, False, False)
        load = properties["LoadState"]
        active = properties["ActiveState"]
        sub = properties["SubState"]
        pid = properties["MainPID"]
        try:
            fragment = Path(properties["FragmentPath"]).resolve(strict=True)
        except OSError:
            fragment = Path()
        identity = (
            identity
            and fragment == unit_path.resolve()
            and not properties["DropInPaths"]
            and self._execstart_matches(properties["ExecStart"], template.command)
        )
        running = active in {"active", "activating"}
        ready = active == "active" and sub == "running" and pid.isdigit() and int(pid) > 0
        process_ref = None
        if running or ready:
            start_ticks = (
                self._process_identity(int(pid), template, properties["ControlGroup"])
                if pid.isdigit() and int(pid) > 0
                else None
            )
            identity = identity and start_ticks is not None
            ready = ready and identity
            if identity and start_ticks is not None:
                process_ref = (
                    f"systemd:{self._unit_name(seat_id)}:{pid}:{start_ticks}"
                )
        return ServiceObservation(load == "loaded", running, ready, identity, process_ref)

    def instantiate(self, seat_id: str, template: SeatTemplate) -> None:
        self.unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._unit_path(seat_id)
        if path.is_symlink():
            raise RuntimeError("unit_path_symlink")
        content = self._unit(seat_id, template)
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        result = self._run("daemon-reload")
        if result.returncode != 0:
            raise RuntimeError("systemd_daemon_reload_failed")

    def start(self, seat_id: str, template: SeatTemplate) -> None:
        result = self._run("start", self._unit_name(seat_id))
        if result.returncode != 0:
            raise RuntimeError("systemd_start_failed")

    def drain(self, seat_id: str, template: SeatTemplate) -> None:
        self.drain_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.drain_dir / f"{seat_id}.json"
        if path.is_symlink():
            raise RuntimeError("drain_path_symlink")
        payload = canonical_json({"schema": "pursers_seat_drain_v1", "seat_id": seat_id})
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def stop(self, seat_id: str, template: SeatTemplate) -> None:
        result = self._run("stop", self._unit_name(seat_id))
        if result.returncode != 0:
            raise RuntimeError("systemd_stop_failed")

    def replace(
        self, seat_id: str, source: SeatTemplate, target: SeatTemplate
    ) -> None:
        self.instantiate(seat_id, target)


class LaunchdUserAdapter:
    """Narrow per-user ``launchctl`` adapter with no shell command surface."""

    def __init__(
        self,
        agent_dir: Path,
        drain_dir: Path,
        credential_paths: Mapping[str, Path],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        uid: int | None = None,
        helper_path: Path | None = None,
    ) -> None:
        self.agent_dir = agent_dir
        self.drain_dir = drain_dir
        self.credential_paths = credential_paths
        self.runner = runner
        self.uid = os.getuid() if uid is None else uid
        self.helper_path = (
            Path(__file__).with_name("launchd_env_exec.py")
            if helper_path is None
            else helper_path
        ).resolve()

    @staticmethod
    def _label(seat_id: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9.-]", "-", seat_id)[:80]
        suffix = hashlib.sha256(seat_id.encode()).hexdigest()[:12]
        return f"com.pursers.seat.{slug}.{suffix}"

    def _target(self, seat_id: str) -> str:
        return f"gui/{self.uid}/{self._label(seat_id)}"

    def _plist_path(self, seat_id: str) -> Path:
        return self.agent_dir / f"{self._label(seat_id)}.plist"

    def _arguments(self, template: SeatTemplate) -> list[str]:
        credential_path = self.credential_paths.get(template.credential_ref)
        if credential_path is None:
            raise RuntimeError("credential_reference_unknown")
        return [str(self.helper_path), str(credential_path), *template.command]

    def _plist(self, seat_id: str, template: SeatTemplate) -> bytes:
        return plistlib.dumps(
            {
                "Label": self._label(seat_id),
                "ProgramArguments": self._arguments(template),
                "WorkingDirectory": str(template.repository_root),
                "ProcessType": "Background",
                "RunAtLoad": False,
                "KeepAlive": False,
            },
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner(
            ["launchctl", *args], check=False, text=True, capture_output=True
        )

    @staticmethod
    def _print_fields(output: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in output.splitlines():
            match = re.fullmatch(
                r"\s*(state|pid|program|working directory)\s*=\s*(.*?)\s*",
                line,
            )
            if match is not None and match.group(1) not in fields:
                fields[match.group(1)] = match.group(2)
        return fields

    @staticmethod
    def _print_arguments(output: str) -> tuple[str, ...] | None:
        """Parse the effective argv block emitted by ``launchctl print``."""
        arguments: tuple[str, ...] | None = None
        lines = output.splitlines()
        for index, line in enumerate(lines):
            if re.fullmatch(r"\s*arguments\s*=\s*\{\s*", line) is None:
                continue
            if arguments is not None:
                return None
            values: list[str] = []
            for value_line in lines[index + 1 :]:
                if re.fullmatch(r"\s*}\s*", value_line) is not None:
                    arguments = tuple(values)
                    break
                value = value_line.strip()
                if not value:
                    return None
                values.append(value)
            else:
                return None
        return arguments

    def _loaded_identity_matches(
        self, output: str, template: SeatTemplate
    ) -> bool:
        fields = self._print_fields(output)
        return (
            fields.get("program") == str(self.helper_path)
            and self._print_arguments(output) == tuple(self._arguments(template))
            and fields.get("working directory") == str(template.repository_root)
        )

    def inspect(self, seat_id: str, template: SeatTemplate) -> ServiceObservation:
        path = self._plist_path(seat_id)
        expected = self._plist(seat_id, template)
        exists = path.is_file() and not path.is_symlink()
        identity = False
        try:
            identity = (
                path.resolve(strict=True).parent == self.agent_dir.resolve(strict=True)
                and path.read_bytes() == expected
            )
        except OSError:
            pass
        result = self._run("print", self._target(seat_id))
        if result.returncode != 0:
            return ServiceObservation(exists, False, False, identity)
        fields = self._print_fields(result.stdout)
        state = fields.get("state")
        pid = fields.get("pid")
        running = state == "running"
        identity = identity and self._loaded_identity_matches(result.stdout, template)
        ready = running and pid is not None and pid.isdigit() and int(pid) > 0 and identity
        process_ref = (
            f"launchd:{self._label(seat_id)}:{pid}" if ready and pid is not None else None
        )
        return ServiceObservation(True, running, ready, identity, process_ref)

    def instantiate(self, seat_id: str, template: SeatTemplate) -> None:
        if not self.helper_path.is_file() or self.helper_path.is_symlink():
            raise RuntimeError("launchd_helper_unavailable")
        self.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._plist_path(seat_id)
        if path.is_symlink():
            raise RuntimeError("launchd_plist_symlink")
        expected = self._plist(seat_id, template)
        staged = path.is_file()
        if staged:
            try:
                if path.read_bytes() != expected:
                    raise RuntimeError("launchd_plist_identity_mismatch")
            except OSError as exc:
                raise RuntimeError("launchd_plist_unavailable") from exc
            loaded = self._run("print", self._target(seat_id))
            if loaded.returncode == 0:
                if not self._loaded_identity_matches(loaded.stdout, template):
                    raise RuntimeError("launchd_loaded_identity_mismatch")
                return
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        result = self._run("bootstrap", f"gui/{self.uid}", str(path))
        if result.returncode != 0:
            raise RuntimeError("launchd_bootstrap_failed")

    def start(self, seat_id: str, template: SeatTemplate) -> None:
        result = self._run("kickstart", "-k", self._target(seat_id))
        if result.returncode != 0:
            raise RuntimeError("launchd_start_failed")

    def drain(self, seat_id: str, template: SeatTemplate) -> None:
        self.drain_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.drain_dir / f"{seat_id}.json"
        if path.is_symlink():
            raise RuntimeError("drain_path_symlink")
        payload = canonical_json({"schema": "pursers_seat_drain_v1", "seat_id": seat_id})
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def stop(self, seat_id: str, template: SeatTemplate) -> None:
        result = self._run("kill", "SIGTERM", self._target(seat_id))
        if result.returncode != 0:
            raise RuntimeError("launchd_stop_failed")

    def replace(
        self, seat_id: str, source: SeatTemplate, target: SeatTemplate
    ) -> None:
        path = self._plist_path(seat_id)
        result = self._run("bootout", self._target(seat_id))
        if result.returncode != 0:
            raise RuntimeError("launchd_bootout_failed")
        path.unlink()
        self.instantiate(seat_id, target)


def service_adapter(
    policy: ExecutorPolicy,
    state: Path,
    *,
    manager: str = "auto",
    platform: str = sys.platform,
) -> ServiceAdapter:
    """Select only an explicitly supported user-service manager."""
    selected = manager
    if selected == "auto":
        selected = "launchd" if platform == "darwin" else "systemd" if platform == "linux" else ""
    if selected == "launchd":
        if platform != "darwin":
            raise PolicyError("launchd_platform_invalid")
        return LaunchdUserAdapter(
            Path.home() / "Library/LaunchAgents",
            state / "drain",
            policy.credential_paths,
        )
    if selected == "systemd":
        if platform != "linux":
            raise PolicyError("systemd_platform_invalid")
        return SystemdUserAdapter(
            Path.home() / ".config/systemd/user",
            state / "drain",
            policy.credential_paths,
        )
    raise PolicyError("service_manager_unsupported")


class FleetExecutor:
    def __init__(
        self,
        policy: ExecutorPolicy,
        store: ExecutorStore,
        adapter: ServiceAdapter,
        leases: LeaseProvider,
        readiness: RegistryReadinessProvider,
        publisher: ReceiptPublisher,
        *,
        supervisor_control: (
            Mapping[str, Any]
            | Callable[[], Mapping[str, Any] | None]
            | None
        ) = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self.store = store
        self.adapter = adapter
        self.leases = leases
        self.readiness = readiness
        self.publisher = publisher
        self.supervisor_control = supervisor_control
        self.clock = clock

    def _authorize_supervisor_roster(self, request: Mapping[str, Any]) -> None:
        control = (
            self.supervisor_control()
            if callable(self.supervisor_control)
            else self.supervisor_control
        )
        has_canonical_binding = any(
            request.get(field) is not None
            for field in (
                "identity_id", "state_id", "state_dir_id",
                "supervisor_roster_revision",
                "supervisor_roster_digest_sha256",
            )
        )
        if not isinstance(control, Mapping):
            if has_canonical_binding:
                raise PolicyError("canonical_supervisor_roster_unavailable")
            return
        if control.get("source") != "canonical":
            if has_canonical_binding:
                raise PolicyError("canonical_supervisor_roster_unavailable")
            return
        roster = control.get("document")
        if not isinstance(roster, Mapping) or not _valid_supervisor_roster(roster):
            raise PolicyError("supervisor_roster_invalid")
        roster_digest = hashlib.sha256(canonical_json(roster)).hexdigest()
        if (
            roster.get("board_id") != request.get("board_id")
            or roster.get("envelope_fingerprint_sha256")
            != request.get("authorization_fingerprint_sha256")
            or roster.get("revision") != request.get("supervisor_roster_revision")
            or not hmac.compare_digest(
                roster_digest, str(request.get("supervisor_roster_digest_sha256"))
            )
        ):
            raise PolicyError("supervisor_roster_authorization_mismatch")
        if request.get("action") == "inspect":
            return
        matches = [
            row
            for row in roster.get("actions", [])
            if isinstance(row, Mapping)
            and row.get("seat_id") == request.get("seat_id")
            and row.get("kind")
            in SUPERVISOR_ACTION_ROSTER_KINDS.get(
                str(request.get("action")), frozenset()
            )
            and row.get("generation") == request.get("expected_seat_generation")
            and row.get("identity_id") == request.get("identity_id")
            and row.get("state_id") == request.get("state_id")
            and row.get("state_dir_id") == request.get("state_dir_id")
            and (
                row.get("template_id") is None
                or row.get("template_id")
                == (
                    request.get("target_template_id")
                    if row.get("kind") == "re_role"
                    else request.get("template_id")
                )
            )
        ]
        if len(matches) != 1:
            raise PolicyError("operation_not_in_supervisor_roster")

    def _authenticate(self, request: Mapping[str, Any]) -> tuple[str, str]:
        if set(request) != REQUEST_FIELDS:
            raise PolicyError("request_fields_invalid")
        if request.get("schema") != SCHEMA or request.get("schema_version") != 1:
            raise PolicyError("schema_invalid")
        if request.get("message_type") != "request":
            raise PolicyError("message_type_invalid")
        for field in ("operation_id", "board_id", "seat_id", "template_id"):
            _require_id(request.get(field), field)
        if request.get("action") not in {"inspect", "start", "drain", "stop", "re_role", "adopt"}:
            raise PolicyError("action_invalid")
        if not isinstance(request.get("expected_seat_generation"), int) or isinstance(
            request.get("expected_seat_generation"), bool
        ) or request["expected_seat_generation"] < 1:
            raise PolicyError("expected_seat_generation_invalid")
        for field in ("template_digest_sha256", "authorization_fingerprint_sha256"):
            if not isinstance(request.get(field), str) or not SHA256.fullmatch(
                request[field]
            ):
                raise PolicyError(f"{field}_invalid")
        for field in ("identity_id", "state_id", "state_dir_id"):
            value = request.get(field)
            if value is not None:
                _require_id(value, field)
        roster_revision = request.get("supervisor_roster_revision")
        if roster_revision is not None and (
            not isinstance(roster_revision, int)
            or isinstance(roster_revision, bool)
            or roster_revision < 1
        ):
            raise PolicyError("supervisor_roster_revision_invalid")
        roster_digest = request.get("supervisor_roster_digest_sha256")
        if roster_digest is not None and (
            not isinstance(roster_digest, str) or not SHA256.fullmatch(roster_digest)
        ):
            raise PolicyError("supervisor_roster_digest_sha256_invalid")
        target_id = request.get("target_template_id")
        target_digest = request.get("target_template_digest_sha256")
        if request.get("action") == "re_role":
            _require_id(target_id, "target_template_id")
            if not isinstance(target_digest, str) or not SHA256.fullmatch(target_digest):
                raise PolicyError("target_template_digest_sha256_invalid")
        elif target_id is not None or target_digest is not None:
            raise PolicyError("target_template_invalid")
        now = self.clock()
        if _parse_time(request.get("deadline"), "deadline").timestamp() < now:
            raise PolicyError("deadline_expired")
        auth = request.get("caller_auth")
        if not isinstance(auth, dict) or set(auth) != AUTH_FIELDS:
            raise PolicyError("caller_auth_invalid")
        if auth.get("scheme") != "local_ed25519_v1":
            raise PolicyError("caller_auth_scheme_invalid")
        key_id = _require_id(auth.get("key_id"), "key_id")
        nonce = _require_id(auth.get("nonce"), "nonce")
        signed_at = _parse_time(auth.get("signed_at"), "signed_at").timestamp()
        if abs(now - signed_at) > self.policy.signature_skew_s:
            raise PolicyError("signed_at_outside_skew")
        digest = request_digest(request)
        if not hmac.compare_digest(str(auth.get("request_digest_sha256")), digest):
            raise PolicyError("request_digest_mismatch")
        key = self.policy.caller_keys.get(key_id)
        if key is None:
            raise PolicyError("caller_key_unknown")
        message = b"\0".join(
            (
                SIGNING_CONTEXT,
                digest.encode("ascii"),
                key_id.encode(),
                nonce.encode(),
                str(auth["signed_at"]).encode(),
            )
        )
        try:
            signature = base64.b64decode(auth.get("signature_base64", ""), validate=True)
            key.verify(signature, message)
        except (ValueError, InvalidSignature) as exc:
            raise PolicyError("signature_invalid") from exc
        return digest, key_id

    @staticmethod
    def _result(
        request: Mapping[str, Any],
        digest: str,
        outcome: str,
        *,
        committed: bool,
        reason_code: str | None = None,
        process_ref: str | None = None,
        replayed: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "schema_version": 1,
            "message_type": "result",
            "operation_id": request["operation_id"],
            "board_id": request["board_id"],
            "request_digest_sha256": digest,
            "replayed": replayed,
            "outcome": outcome,
            "committed": committed,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if reason_code:
            result["reason_code"] = reason_code
        if process_ref:
            result["process_ref"] = process_ref
        return result

    def _template(self, request: Mapping[str, Any]) -> SeatTemplate:
        template = self.policy.templates.get(str(request["template_id"]))
        if template is None:
            raise PolicyError("template_not_approved")
        if not hmac.compare_digest(
            template.digest_sha256, request["template_digest_sha256"]
        ):
            raise PolicyError("template_digest_mismatch")
        if not hmac.compare_digest(
            self.policy.authorization_fingerprint_sha256,
            request["authorization_fingerprint_sha256"],
        ):
            raise PolicyError("authorization_fingerprint_mismatch")
        _inside(template.repository_root, self.policy.repository_roots, "repository_root")
        _inside(template.seat_root, self.policy.seat_roots, "seat_root")
        return template

    def _target_template(self, request: Mapping[str, Any]) -> SeatTemplate:
        template = self.policy.templates.get(str(request.get("target_template_id")))
        if template is None:
            raise PolicyError("target_template_not_approved")
        if not hmac.compare_digest(
            template.digest_sha256,
            str(request.get("target_template_digest_sha256")),
        ):
            raise PolicyError("target_template_digest_mismatch")
        _inside(template.repository_root, self.policy.repository_roots, "repository_root")
        _inside(template.seat_root, self.policy.seat_roots, "seat_root")
        return template

    def _check_generation(
        self, request: Mapping[str, Any], seat: Mapping[str, Any] | None
    ) -> int:
        current = int(seat["generation"]) if seat else 1
        if request["expected_seat_generation"] != current:
            raise PolicyError("seat_generation_mismatch")
        return current

    def _check_cooldown(self, seat: Mapping[str, Any] | None, now: float) -> None:
        if not seat:
            return
        last_failure = seat.get("last_failure")
        if last_failure is not None and now - float(last_failure) < self.policy.failure_backoff_s:
            raise PolicyError("failure_backoff_active")
        if now - float(seat["last_mutation"]) < self.policy.mutation_cooldown_s:
            raise PolicyError("mutation_cooldown_active")

    def _check_registry_readiness(
        self, board_id: str, seat_id: str, template: SeatTemplate
    ) -> RegistryReadinessObservation:
        readiness = self.readiness.observe(board_id, seat_id, template)
        if not readiness.known:
            raise PolicyError("registry_readiness_unknown")
        if not readiness.ready:
            raise PolicyError(readiness.reason_code or "registry_readiness_failed")
        return readiness

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        digest, key_id = self._authenticate(request)
        operation_id = str(request["operation_id"])
        previous = self.store.operation(operation_id)
        if previous:
            prior_digest, state, result_json = previous
            if prior_digest != digest:
                raise PolicyError("operation_id_payload_changed")
            if state == "terminal" and result_json:
                result = json.loads(result_json)
                result["replayed"] = True
                self.publisher.publish(result)
                return result
            raise PolicyError("operation_outcome_unknown")
        self._authorize_supervisor_roster(request)
        auth = request["caller_auth"]
        self.store.reserve(key_id, auth["nonce"], digest, operation_id, self.clock())
        try:
            result = self._execute(request, digest)
        except PolicyError as exc:
            result = self._result(
                request, digest, "rejected", committed=False, reason_code=str(exc)
            )
        except Exception:
            result = self._result(
                request, digest, "failed", committed=False, reason_code="service_operation_failed"
            )
        self.store.finish(operation_id, result, self.clock())
        self.publisher.publish(result)
        return result

    def _execute(self, request: Mapping[str, Any], digest: str) -> dict[str, Any]:
        template = self._template(request)
        seat_id = str(request["seat_id"])
        board_id = str(request["board_id"])
        action = str(request["action"])
        identity_id = request.get("identity_id")
        state_id = request.get("state_id")
        state_dir_id = request.get("state_dir_id")
        now = self.clock()
        seat = self.store.seat(seat_id)
        generation = self._check_generation(request, seat)
        if seat and (
            seat["board_id"] != board_id
            or seat["template_id"] != template.template_id
            or seat["template_digest"] != template.digest_sha256
            or seat["principal_id"] != template.principal_id
        ):
            raise PolicyError("seat_identity_or_template_drift")
        if seat:
            binding_fields = ("identity_id", "state_id", "state_dir_id")
            stored_binding = tuple(seat.get(field) for field in binding_fields)
            requested_binding = tuple(request.get(field) for field in binding_fields)
            adopting_legacy = all(value is None for value in stored_binding) and all(
                isinstance(value, str) for value in requested_binding
            )
            if stored_binding != requested_binding and not adopting_legacy:
                raise PolicyError("seat_identity_or_state_drift")
        target: ServiceTemplate | None = None
        if action == "re_role":
            target = self._target_template(request)
            if target.template_id == template.template_id:
                raise PolicyError("target_template_unchanged")
            if self.store.principal_active_elsewhere(target.principal_id, seat_id):
                raise PolicyError("principal_not_independent")
        observation = self.adapter.inspect(seat_id, template)
        if (
            action != "adopt"
            and seat
            and observation.running
            and seat["process_ref"]
            and seat["process_ref"] != observation.process_ref
        ):
            raise PolicyError("process_identity_unknown")
        if action == "inspect":
            if observation.exists and not observation.identity_verified:
                raise PolicyError("process_identity_unknown")
            if observation.running or observation.ready:
                self._check_registry_readiness(board_id, seat_id, template)
            return self._result(
                request,
                digest,
                "succeeded",
                committed=False,
                process_ref=observation.process_ref,
            )
        if action == "adopt":
            if not seat:
                raise PolicyError("seat_unknown")
            if (
                not observation.exists
                or not observation.running
                or not observation.ready
                or not observation.identity_verified
                or not observation.process_ref
                or observation.process_ref == seat.get("process_ref")
            ):
                raise PolicyError("process_adoption_unverified")
            self._check_registry_readiness(board_id, seat_id, template)
            lease = self.leases.observe(board_id, seat_id)
            if not lease.known:
                raise PolicyError("lease_state_unknown")
            if lease.live:
                raise PolicyError("live_lease")
            adopted = self.store.adopt_verified_process(
                seat_id,
                expected_generation=generation,
                expected_process_ref=str(seat.get("process_ref") or ""),
                process_ref=observation.process_ref,
                now=now,
            )
            if adopted is None:
                raise PolicyError("seat_generation_mismatch")
            return self._result(
                request, digest, "succeeded", committed=True,
                process_ref=observation.process_ref,
            )
        self._check_cooldown(seat, now)
        if action == "start":
            if observation.exists and not observation.identity_verified:
                raise PolicyError("process_identity_unknown")
            self._check_registry_readiness(board_id, seat_id, template)
            if observation.running:
                if not observation.identity_verified:
                    raise PolicyError("process_identity_unknown")
                if not observation.ready:
                    raise PolicyError("seat_not_ready")
                return self._result(
                    request, digest, "succeeded", committed=False,
                    process_ref=observation.process_ref,
                )
            host_count, board_count = self.store.active_counts(board_id)
            if host_count >= self.policy.host_cap:
                raise PolicyError("host_concurrency_cap")
            if board_count >= self.policy.board_caps.get(board_id, 0):
                raise PolicyError("board_concurrency_cap")
            if self.store.principal_active_elsewhere(template.principal_id, seat_id):
                raise PolicyError("principal_not_independent")
            if self.store.binding_used_elsewhere(
                seat_id, identity_id, state_id, state_dir_id
            ):
                raise PolicyError("identity_or_state_reused")
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
                identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
                generation=generation, lifecycle="starting", process_ref=None, now=now,
            )
            try:
                self.adapter.instantiate(seat_id, template)
                self.adapter.start(seat_id, template)
                observation = self.adapter.inspect(seat_id, template)
                if not observation.ready or not observation.identity_verified:
                    raise RuntimeError("seat_readiness_failed")
                registry = self.readiness.observe(board_id, seat_id, template)
                if not registry.known or not registry.ready:
                    raise RuntimeError("registry_readiness_failed")
            except Exception:
                rollback_ok = False
                try:
                    self.adapter.stop(seat_id, template)
                    rollback_ok = not self.adapter.inspect(seat_id, template).running
                except Exception:
                    rollback_ok = False
                self.store.save_seat(
                    seat_id=seat_id, board_id=board_id, template=template,
                    identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
                    generation=generation,
                    lifecycle="stopped" if rollback_ok else "unhealthy",
                    process_ref=None, now=self.clock(), failed=True,
                )
                raise
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
                identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
                generation=generation, lifecycle="ready",
                process_ref=observation.process_ref, now=self.clock(),
            )
            return self._result(
                request, digest, "succeeded", committed=True,
                process_ref=observation.process_ref,
            )
        if not seat:
            raise PolicyError("seat_unknown")
        if not observation.identity_verified:
            raise PolicyError("process_identity_unknown")
        if not observation.running:
            raise PolicyError("seat_not_running")
        if action == "drain":
            self.adapter.drain(seat_id, template)
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
                identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
                generation=generation, lifecycle="draining",
                process_ref=observation.process_ref, now=now,
            )
            return self._result(
                request, digest, "succeeded", committed=True,
                process_ref=observation.process_ref,
            )
        lease = self.leases.observe(board_id, seat_id)
        if not lease.known:
            raise PolicyError("lease_state_unknown")
        if lease.live:
            raise PolicyError("live_lease")
        if action == "re_role":
            if seat.get("lifecycle") != "draining":
                raise PolicyError("seat_not_drained")
            assert target is not None
            self.adapter.stop(seat_id, template)
            stopped = self.adapter.inspect(seat_id, template)
            if stopped.running:
                raise RuntimeError("seat_still_running")
            try:
                self.adapter.replace(seat_id, template, target)
                self.adapter.start(seat_id, target)
                changed = self.adapter.inspect(seat_id, target)
                if not changed.ready or not changed.identity_verified:
                    raise RuntimeError("seat_readiness_failed")
                registry = self.readiness.observe(board_id, seat_id, target)
                if not registry.known or not registry.ready:
                    raise RuntimeError("registry_readiness_failed")
            except Exception:
                self.store.save_seat(
                    seat_id=seat_id, board_id=board_id, template=target,
                    identity_id=identity_id, state_id=state_id,
                    state_dir_id=state_dir_id, generation=generation + 1,
                    lifecycle="unhealthy", process_ref=None, now=self.clock(), failed=True,
                )
                raise
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=target,
                identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
                generation=generation + 1, lifecycle="ready",
                process_ref=changed.process_ref, now=self.clock(),
            )
            return self._result(
                request, digest, "succeeded", committed=True,
                process_ref=changed.process_ref,
            )
        self.adapter.stop(seat_id, template)
        stopped = self.adapter.inspect(seat_id, template)
        if stopped.running:
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
                identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
                generation=generation, lifecycle="unhealthy",
                process_ref=stopped.process_ref, now=self.clock(), failed=True,
            )
            raise RuntimeError("seat_still_running")
        self.store.save_seat(
            seat_id=seat_id, board_id=board_id, template=template,
            identity_id=identity_id, state_id=state_id, state_dir_id=state_dir_id,
            generation=generation + 1, lifecycle="stopped", process_ref=None, now=self.clock(),
        )
        return self._result(request, digest, "succeeded", committed=True)


class UnixSocketServer:
    def __init__(
        self,
        path: Path,
        executor: FleetExecutor,
        *,
        max_request_bytes: int = 64 * 1024,
    ) -> None:
        self.path = path
        self.executor = executor
        self.max_request_bytes = max_request_bytes

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.readline()
            if not data or len(data) > self.max_request_bytes or not data.endswith(b"\n"):
                raise PolicyError("request_size_invalid")
            request = json.loads(data)
            if not isinstance(request, dict):
                raise PolicyError("request_invalid")
            result = self.executor.handle(request)
        except (json.JSONDecodeError, UnicodeError, PolicyError) as exc:
            result = {"error": str(exc) or "invalid_request"}
        writer.write(canonical_json(result) + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve(self) -> None:
        if len(os.fsencode(self.path)) > 100:
            raise ValueError("executor socket path exceeds portable AF_UNIX limit")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            if not self.path.is_socket():
                raise RuntimeError("executor socket path is not a socket")
            self.path.unlink()
        server = await asyncio.start_unix_server(self._client, path=self.path)
        self.path.chmod(0o600)
        async with server:
            await server.serve_forever()


def load_policy(path: Path) -> ExecutorPolicy:
    document = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "authorization_fingerprint_sha256", "caller_keys", "templates",
        "credential_paths", "repository_roots", "seat_roots", "board_caps", "host_cap",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise PolicyError("executor_config_fields_invalid")
    if document.get("schema") != "pursers_fleet_executor_config_v1":
        raise PolicyError("executor_config_schema_invalid")
    keys: dict[str, Ed25519PublicKey] = {}
    for key_id, encoded in document["caller_keys"].items():
        _require_id(key_id, "key_id")
        try:
            keys[key_id] = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(encoded, validate=True)
            )
        except (ValueError, TypeError) as exc:
            raise PolicyError("caller_public_key_invalid") from exc
    templates = {
        template_id: SeatTemplate.from_record(template_id, value)
        for template_id, value in document["templates"].items()
    }
    credential_paths: dict[str, Path] = {}
    for reference, raw_path in document["credential_paths"].items():
        _require_id(reference, "credential_ref")
        try:
            unresolved = Path(raw_path).expanduser()
            if unresolved.is_symlink():
                raise PolicyError("credential_reference_unavailable")
            resolved = unresolved.resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise PolicyError("credential_reference_unavailable") from exc
        if not resolved.is_file():
            raise PolicyError("credential_reference_unavailable")
        credential_paths[reference] = resolved
    repository_roots = tuple(
        Path(value).expanduser().resolve(strict=True)
        for value in document["repository_roots"]
    )
    seat_roots = tuple(
        Path(value).expanduser().resolve(strict=True)
        for value in document["seat_roots"]
    )
    return ExecutorPolicy(
        authorization_fingerprint_sha256=document["authorization_fingerprint_sha256"],
        templates=templates,
        caller_keys=keys,
        credential_paths=credential_paths,
        repository_roots=repository_roots,
        seat_roots=seat_roots,
        board_caps={key: int(value) for key, value in document["board_caps"].items()},
        host_cap=int(document["host_cap"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--supervisor-roster", type=Path)
    parser.add_argument("--legacy-supervisor-config", type=Path)
    parser.add_argument(
        "--service-manager",
        choices=("auto", "launchd", "systemd"),
        default="auto",
    )
    return parser


def load_supervisor_control(
    roster_path: Path | None, legacy_path: Path | None
) -> Mapping[str, Any] | None:
    """Prefer a canonical roster; use legacy only when it is absent."""
    if roster_path is None:
        return None
    if roster_path.exists():
        try:
            info = roster_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > 2 * 1024 * 1024
            ):
                raise PolicyError("supervisor_roster_invalid")
            document = json.loads(roster_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyError("supervisor_roster_invalid") from exc
        if not _valid_supervisor_roster(document):
            raise PolicyError("supervisor_roster_invalid")
        return {"source": "canonical", "document": dict(document)}
    if legacy_path is None or not legacy_path.is_file():
        raise PolicyError("supervisor_roster_unavailable")
    try:
        info = legacy_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 1024 * 1024
        ):
            raise PolicyError("legacy_supervisor_config_invalid")
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError("legacy_supervisor_config_invalid") from exc
    if not isinstance(legacy, Mapping):
        raise PolicyError("legacy_supervisor_config_invalid")
    return {"source": "legacy", "document": dict(legacy)}


def _valid_supervisor_roster(document: Any) -> bool:
    """Validate the persisted executor boundary without trusting producer code."""
    if not isinstance(document, Mapping) or set(document) != SUPERVISOR_ROSTER_FIELDS:
        return False
    desired = document.get("desired")
    seats = document.get("seats")
    actions = document.get("actions")
    project_admission = document.get("project_admission")
    if (
        document.get("schema") != SUPERVISOR_ROSTER_SCHEMA
        or not isinstance(document.get("revision"), int)
        or isinstance(document.get("revision"), bool)
        or document["revision"] < 1
        or not isinstance(document.get("config_revision"), int)
        or isinstance(document.get("config_revision"), bool)
        or document["config_revision"] < 1
        or not isinstance(document.get("host_seat_cap"), int)
        or isinstance(document.get("host_seat_cap"), bool)
        or not 1 <= document["host_seat_cap"] <= 256
        or not isinstance(document.get("gate_concurrency_ceiling"), int)
        or isinstance(document.get("gate_concurrency_ceiling"), bool)
        or not 1 <= document["gate_concurrency_ceiling"] <= 256
        or not isinstance(document.get("full_gate_concurrency"), int)
        or isinstance(document.get("full_gate_concurrency"), bool)
        or not 0 <= document["full_gate_concurrency"] <= document["gate_concurrency_ceiling"]
        or not isinstance(desired, Mapping)
        or set(desired) != {"worker", "reviewer", "verifier"}
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in desired.values())
        or sum(desired.values()) > document["host_seat_cap"]
        or not isinstance(seats, list)
        or not isinstance(actions, list)
        or not isinstance(project_admission, list)
        or not isinstance(document.get("audit"), list)
        or not isinstance(document.get("findings"), list)
        or not isinstance(document.get("board_id"), str)
        or not SAFE_ID.fullmatch(document["board_id"])
        or not isinstance(document.get("envelope_fingerprint_sha256"), str)
        or not SHA256.fullmatch(document["envelope_fingerprint_sha256"])
        or not isinstance(document.get("plan_digest_sha256"), str)
        or not SHA256.fullmatch(document["plan_digest_sha256"])
    ):
        return False
    try:
        _parse_time(document.get("confirmed_at"), "confirmed_at")
    except PolicyError:
        return False
    project_ids: set[str] = set()
    for project in project_admission:
        if not isinstance(project, Mapping) or set(project) != {
            "project_id", "priority", "share_units", "role_pressure"
        }:
            return False
        project_id = project.get("project_id")
        pressure = project.get("role_pressure")
        if (
            not isinstance(project_id, str)
            or not SAFE_ID.fullmatch(project_id)
            or project_id in project_ids
            or not isinstance(project.get("priority"), int)
            or isinstance(project.get("priority"), bool)
            or not 1 <= project["priority"] <= 100
            or not isinstance(project.get("share_units"), int)
            or isinstance(project.get("share_units"), bool)
            or project["share_units"] < 0
            or not isinstance(pressure, Mapping)
            or set(pressure) != {"worker", "reviewer", "verifier"}
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in pressure.values()
            )
        ):
            return False
        project_ids.add(project_id)
    identifiers: set[tuple[str, str]] = set()
    seats_by_id: dict[str, Mapping[str, Any]] = {}
    for seat in seats:
        if not isinstance(seat, Mapping) or set(seat) != {
            "seat_id", "identity_id", "state_id", "state_dir_id", "generation",
            "role", "lifecycle", "work_claim", "review_lease", "transition_at",
        }:
            return False
        if any(
            not isinstance(seat.get(field), str) or not SAFE_ID.fullmatch(seat[field])
            for field in ("seat_id", "identity_id", "state_id", "state_dir_id")
        ):
            return False
        for field in ("seat_id", "identity_id", "state_id", "state_dir_id"):
            key = (field, seat[field])
            if key in identifiers:
                return False
            identifiers.add(key)
        if (
            not isinstance(seat.get("generation"), int)
            or isinstance(seat.get("generation"), bool)
            or seat["generation"] < 1
            or seat.get("role") not in {"worker", "reviewer", "verifier"}
            or seat.get("lifecycle") not in {"ready", "busy", "draining", "paused", "stopped"}
            or not isinstance(seat.get("work_claim"), bool)
            or not isinstance(seat.get("review_lease"), bool)
        ):
            return False
        try:
            _parse_time(seat.get("transition_at"), "transition_at")
        except PolicyError:
            return False
        seats_by_id[str(seat["seat_id"])] = seat
    provisioned_identifiers = set(identifiers)
    for action in actions:
        if (
            not isinstance(action, Mapping)
            or action.get("kind") not in SUPERVISOR_ROSTER_ACTION_KINDS
        ):
            return False
        required = {"kind", "seat_id", "identity_id", "state_id", "state_dir_id", "generation"}
        expected = set(required)
        if action["kind"] == "provision":
            expected.update({"target_role", "template_id"})
        elif action["kind"] == "re_role":
            expected.update({"target_role", "template_id"})
        elif action["kind"] == "drain" and "target_role" in action:
            expected.add("target_role")
        if set(action) != expected:
            return False
        if any(not isinstance(action.get(field), str) or not SAFE_ID.fullmatch(action[field]) for field in ("seat_id", "identity_id", "state_id", "state_dir_id")):
            return False
        if not isinstance(action.get("generation"), int) or isinstance(action.get("generation"), bool) or action["generation"] < 1:
            return False
        if action["kind"] == "provision" and (
            not isinstance(action.get("template_id"), str)
            or not SAFE_ID.fullmatch(action["template_id"])
            or action.get("target_role") not in {"worker", "reviewer", "verifier"}
        ):
            return False
        if action["kind"] == "provision":
            for field in ("seat_id", "identity_id", "state_id", "state_dir_id"):
                key = (field, str(action[field]))
                if key in provisioned_identifiers:
                    return False
                provisioned_identifiers.add(key)
        if action["kind"] != "provision":
            seat = seats_by_id.get(str(action["seat_id"]))
            if seat is None or any(
                action.get(field) != seat.get(field)
                for field in (
                    "identity_id", "state_id", "state_dir_id", "generation"
                )
            ):
                return False
            if action["kind"] in {"pause", "stop", "remove", "re_role"} and (
                seat.get("lifecycle") != "draining"
                or seat.get("work_claim") is True
                or seat.get("review_lease") is True
            ):
                return False
        if action["kind"] == "re_role" and (
            action.get("target_role") not in {"worker", "reviewer", "verifier"}
            or not isinstance(action.get("template_id"), str)
            or not SAFE_ID.fullmatch(str(action["template_id"]))
        ):
            return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy(args.config)
    state = args.state_dir.expanduser().resolve()
    executor = FleetExecutor(
        policy,
        ExecutorStore(state / "executor.sqlite3"),
        service_adapter(policy, state, manager=args.service_manager),
        FileLeaseProvider(state / "leases.json"),
        FileRegistryReadinessProvider(state / "registry-readiness.json"),
        JsonlReceiptPublisher(state / "receipts.jsonl"),
        supervisor_control=lambda: load_supervisor_control(
            args.supervisor_roster, args.legacy_supervisor_config
        ),
    )
    asyncio.run(UnixSocketServer(args.socket, executor).serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
