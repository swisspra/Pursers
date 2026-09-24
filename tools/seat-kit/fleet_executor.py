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
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SCHEMA = "autonomous_butler_executor_v1"
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
            or not 0 <= tier_max <= 2
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
                if seat.get("capabilities") != expected_capabilities:
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
              principal_id TEXT NOT NULL, generation INTEGER NOT NULL,
              lifecycle TEXT NOT NULL, process_ref TEXT,
              last_mutation REAL NOT NULL, last_failure REAL
            );
            """
        )

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
            "SELECT board_id, template_id, template_digest, principal_id, generation, "
            "lifecycle, process_ref, last_mutation, last_failure FROM seats WHERE seat_id = ?",
            (seat_id,),
        ).fetchone()
        if not row:
            return None
        keys = (
            "board_id", "template_id", "template_digest", "principal_id", "generation",
            "lifecycle", "process_ref", "last_mutation", "last_failure",
        )
        return dict(zip(keys, row, strict=True))

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

    def save_seat(
        self,
        *,
        seat_id: str,
        board_id: str,
        template: SeatTemplate,
        generation: int,
        lifecycle: str,
        process_ref: str | None,
        now: float,
        failed: bool = False,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO seats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(seat_id) DO UPDATE SET board_id=excluded.board_id, "
                "template_id=excluded.template_id, template_digest=excluded.template_digest, "
                "principal_id=excluded.principal_id, generation=excluded.generation, "
                "lifecycle=excluded.lifecycle, process_ref=excluded.process_ref, "
                "last_mutation=excluded.last_mutation, "
                "last_failure=CASE WHEN ? THEN excluded.last_failure ELSE seats.last_failure END",
                (
                    seat_id,
                    board_id,
                    template.template_id,
                    template.digest_sha256,
                    template.principal_id,
                    generation,
                    lifecycle,
                    process_ref,
                    now,
                    now if failed else None,
                    int(failed),
                ),
            )


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self.store = store
        self.adapter = adapter
        self.leases = leases
        self.readiness = readiness
        self.publisher = publisher
        self.clock = clock

    def _authenticate(self, request: Mapping[str, Any]) -> tuple[str, str]:
        if set(request) != REQUEST_FIELDS:
            raise PolicyError("request_fields_invalid")
        if request.get("schema") != SCHEMA or request.get("schema_version") != 1:
            raise PolicyError("schema_invalid")
        if request.get("message_type") != "request":
            raise PolicyError("message_type_invalid")
        for field in ("operation_id", "board_id", "seat_id", "template_id"):
            _require_id(request.get(field), field)
        if request.get("action") not in {"inspect", "start", "drain", "stop"}:
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
        observation = self.adapter.inspect(seat_id, template)
        if (
            seat
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
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
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
                    generation=generation,
                    lifecycle="stopped" if rollback_ok else "unhealthy",
                    process_ref=None, now=self.clock(), failed=True,
                )
                raise
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
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
        self.adapter.stop(seat_id, template)
        stopped = self.adapter.inspect(seat_id, template)
        if stopped.running:
            self.store.save_seat(
                seat_id=seat_id, board_id=board_id, template=template,
                generation=generation, lifecycle="unhealthy",
                process_ref=stopped.process_ref, now=self.clock(), failed=True,
            )
            raise RuntimeError("seat_still_running")
        self.store.save_seat(
            seat_id=seat_id, board_id=board_id, template=template,
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy(args.config)
    state = args.state_dir.expanduser().resolve()
    executor = FleetExecutor(
        policy,
        ExecutorStore(state / "executor.sqlite3"),
        SystemdUserAdapter(
            Path.home() / ".config/systemd/user", state / "drain", policy.credential_paths
        ),
        FileLeaseProvider(state / "leases.json"),
        FileRegistryReadinessProvider(state / "registry-readiness.json"),
        JsonlReceiptPublisher(state / "receipts.jsonl"),
    )
    asyncio.run(UnixSocketServer(args.socket, executor).serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
