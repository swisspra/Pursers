#!/usr/bin/env python3
"""Headless Pursers worker/reviewer for OpenAI-compatible chat APIs."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
CLIENT_SRC = ROOT / "packages" / "client" / "src"
WAIT_ROOT = ROOT / "tools" / "wait-bridge"
for import_root in (CLIENT_SRC, WAIT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from pursers_client import BoardClient, BoardClientError  # noqa: E402

wait_bridge: Any | None = None

DIRECTIVE_PATH = WAIT_ROOT / "WORKER-DIRECTIVE.md"
REVIEWER_DIRECTIVE_PATH = (
    Path(__file__).resolve().with_name("REVIEWER-DIRECTIVE-API.md")
)
MAX_TOOL_OUTPUT = 20_000
MAX_FILE_READ = 100_000
MAX_REVIEW_DESCRIPTION = 12_000
MAX_REVIEW_FIELD = 5_000
MAX_SEEN_SUBMISSIONS = 2_048
REVIEW_STATE_SUFFIX = ".review-state.json"
LEASE_INTERVAL_S = 20.0
DEFAULT_WAIT_TIMEOUT_S = 21_600
WAIT_HOST_PROFILES = frozenset(
    {
        "codex",
        "codex-cli",
        "goose",
        "claude-code",
        "claude-desktop",
        "headless",
    }
)
TIER_ORDER = {"light": 0, "standard": 1, "heavy": 2}
ROLE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHELL_ENV_ALLOWLIST = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "USER",
)
AUTH_SCHEME_PARTS = ("Bea", "rer")
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
SECURITY_CLI = Path("/usr/bin/security")
KEYCHAIN_SERVICE = "pursers-worker"
REVIEWER_AUTH_ERROR = (
    "reviewer mode requires a dedicated board reviewer principal/token"
)
OPERATOR_CHECKOUT_REFUSAL = "operator checkout is read-only for seats"


@dataclass(frozen=True)
class Config:
    agent_name: str
    central_url: str
    token_file: Path
    boards: str | tuple[str, ...]
    base_url: str
    api_key_env: str | None
    api_key_file: Path | None
    api_key_keychain: str | None
    model: str
    max_tokens: int
    max_iterations: int
    command_timeout_s: int
    log_file: Path
    max_tier: str
    require_assigned_only: bool
    role: str = "worker"
    max_reviews_per_hour: int = 12
    roles: tuple[str, ...] = ()
    wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S
    wait_host_profile: str = "headless"


def _private_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    details = resolved.stat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise PermissionError(f"{label} must have mode 0600")
    return resolved


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def load_config(path: str | Path) -> Config:
    config_path = _private_file(Path(path), "config")
    raw = config_path.read_bytes()
    document = (
        json.loads(raw)
        if config_path.suffix.lower() == ".json"
        else tomllib.loads(raw.decode("utf-8"))
    )
    if not isinstance(document, dict):
        raise ValueError("config must be an object")
    seat = document.get("seat")
    llm = document.get("llm")
    if not isinstance(seat, dict) or not isinstance(llm, dict):
        raise ValueError("config requires seat and llm objects")
    if "token" in seat or "api_key" in llm:
        raise ValueError("inline tokens and API keys are forbidden")
    role = seat.get("role", "worker")
    if role not in {"worker", "reviewer"}:
        raise ValueError("seat.role must be worker or reviewer")
    claim = document.get("claim", {})
    if not isinstance(claim, dict):
        raise ValueError("claim must be an object")
    max_tier = claim.get("max_tier", "heavy")
    if not isinstance(max_tier, str) or max_tier not in TIER_ORDER:
        raise ValueError("claim.max_tier must be light, standard, or heavy")
    require_assigned_only = claim.get("require_assigned_only", False)
    if type(require_assigned_only) is not bool:
        raise ValueError("claim.require_assigned_only must be a boolean")
    roles_raw = claim.get("roles", [])
    if not isinstance(roles_raw, list) or not all(
        isinstance(item, str) and ROLE_SLUG_RE.fullmatch(item)
        for item in roles_raw
    ):
        raise ValueError("claim.roles must be a list of lowercase slug strings")
    roles = tuple(dict.fromkeys(roles_raw))
    review = document.get("review", {})
    if not isinstance(review, dict):
        raise ValueError("review must be an object")
    max_reviews_per_hour = review.get("max_reviews_per_hour", 12)
    if type(max_reviews_per_hour) is not int or max_reviews_per_hour < 1:
        raise ValueError("review.max_reviews_per_hour must be a positive integer")
    wait = document.get("wait", {})
    if not isinstance(wait, dict):
        raise ValueError("wait must be an object")
    wait_timeout_s = wait.get("timeout_s", DEFAULT_WAIT_TIMEOUT_S)
    if type(wait_timeout_s) is not int or wait_timeout_s < 1:
        raise ValueError("wait.timeout_s must be a positive integer")
    wait_host_profile = wait.get("host_profile", "headless")
    if wait_host_profile not in WAIT_HOST_PROFILES:
        raise ValueError(
            "wait.host_profile must be codex, codex-cli, goose, claude-code, "
            "claude-desktop, or headless"
        )
    boards_raw = document.get("boards", "registry")
    if boards_raw == "registry":
        boards: str | tuple[str, ...] = "registry"
    elif (
        isinstance(boards_raw, list)
        and boards_raw
        and all(isinstance(item, str) and item.strip() for item in boards_raw)
    ):
        boards = tuple(dict.fromkeys(item.strip() for item in boards_raw))
    else:
        raise ValueError("boards must be 'registry' or a non-empty string list")
    token_file = _private_file(
        Path(_text(seat.get("token_file"), "seat.token_file")), "token file"
    )
    base_url = _text(llm.get("base_url"), "llm.base_url").rstrip("/")
    key_env = llm.get("api_key_env")
    key_file_raw = llm.get("api_key_file")
    keychain_raw = llm.get("api_key_keychain")
    key_sources = sum(bool(value) for value in (key_env, key_file_raw, keychain_raw))
    hostname = (urlsplit(base_url).hostname or "").lower()
    keyless_loopback = hostname in {"127.0.0.1", "localhost", "::1"}
    if key_sources != 1 and not (key_sources == 0 and keyless_loopback):
        raise ValueError(
            "llm requires exactly one of api_key_env, api_key_file, or "
            "api_key_keychain (loopback providers may omit all three)"
        )
    key_file = (
        _private_file(Path(_text(key_file_raw, "llm.api_key_file")), "API key file")
        if key_file_raw
        else None
    )

    def positive(name: str, default: int) -> int:
        value = llm.get(name, default)
        if type(value) is not int or value < 1:
            raise ValueError(f"llm.{name} must be a positive integer")
        return value

    log_file_raw = document.get("log_file")
    log_file = (
        Path(log_file_raw).expanduser().resolve()
        if isinstance(log_file_raw, str) and log_file_raw
        else config_path.with_suffix(config_path.suffix + ".session.log")
    )
    return Config(
        agent_name=_text(seat.get("agent_name"), "seat.agent_name"),
        central_url=_text(seat.get("central_url"), "seat.central_url"),
        token_file=token_file,
        boards=boards,
        base_url=base_url,
        api_key_env=_text(key_env, "llm.api_key_env") if key_env else None,
        api_key_file=key_file,
        api_key_keychain=(
            _text(keychain_raw, "llm.api_key_keychain") if keychain_raw else None
        ),
        model=_text(llm.get("model"), "llm.model"),
        max_tokens=positive("max_tokens", 4_096),
        max_iterations=positive("max_iterations", 40),
        command_timeout_s=positive("command_timeout_s", 120),
        log_file=log_file,
        max_tier=max_tier,
        require_assigned_only=require_assigned_only,
        role=role,
        max_reviews_per_hour=max_reviews_per_hour,
        roles=roles,
        wait_timeout_s=wait_timeout_s,
        wait_host_profile=wait_host_profile,
    )


def ticket_tier(ticket: dict[str, Any]) -> str:
    """Return the highest valid tier tag; an absent/invalid tag is standard."""
    tags = ticket.get("tags")
    if not isinstance(tags, (list, tuple)):
        return "standard"
    tiers = [
        tag.removeprefix("tier:")
        for tag in tags
        if isinstance(tag, str) and tag in {f"tier:{tier}" for tier in TIER_ORDER}
    ]
    return max(tiers, key=TIER_ORDER.__getitem__, default="standard")


def ticket_roles(ticket: dict[str, Any]) -> set[str]:
    """Return valid role slugs, ignoring malformed ticket tags."""
    tags = ticket.get("tags")
    if not isinstance(tags, (list, tuple)):
        return set()
    roles = set()
    for tag in tags:
        if not isinstance(tag, str) or not tag.startswith("role:"):
            continue
        role = tag.removeprefix("role:")
        if ROLE_SLUG_RE.fullmatch(role):
            roles.add(role)
    return roles


def claim_priority(config: Config, ticket: dict[str, Any], agent_id: str) -> int | None:
    """Return assigned-first priority, or None when the ticket must be skipped."""
    if ticket.get("status") != "open":
        return None
    if TIER_ORDER[ticket_tier(ticket)] > TIER_ORDER[config.max_tier]:
        return None

    t_roles = ticket_roles(ticket)
    role_match = bool(set(config.roles) & t_roles)
    if config.role != "reviewer" and config.roles and t_roles and not role_match:
        return None

    assigned_id = ticket.get("assigned_to_agent_id")
    assigned_to = ticket.get("assigned_to")
    if assigned_id:
        assigned_to_me = str(assigned_id) == agent_id
    elif assigned_to:
        assigned_to_me = str(assigned_to).casefold() in {
            agent_id.casefold(),
            config.agent_name.casefold(),
        }
    else:
        if config.require_assigned_only:
            return None
        if role_match:
            return 1
        return 1 if not config.roles else 2

    return 0 if assigned_to_me else None


def read_keychain_secret(account: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("macOS Keychain API keys require macOS")
    try:
        result = subprocess.run(
            [
                str(SECURITY_CLI),
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("API key is unavailable in macOS Keychain") from exc
    value = result.stdout.strip()
    if not value:
        raise ValueError("API key is empty in macOS Keychain")
    return value


def read_secret(config: Config, kind: str) -> str:
    if kind == "token":
        value = config.token_file.read_text(encoding="utf-8").strip()
    elif config.api_key_env is not None:
        value = os.environ.get(config.api_key_env, "").strip()
    elif config.api_key_file is not None:
        value = config.api_key_file.read_text(encoding="utf-8").strip()
    elif config.api_key_keychain is not None:
        value = read_keychain_secret(config.api_key_keychain)
    else:  # pragma: no cover - Config prevents this.
        value = ""
    configured_api_source = any(
        source is not None
        for source in (
            config.api_key_env,
            config.api_key_file,
            config.api_key_keychain,
        )
    )
    if not value and (kind == "token" or configured_api_source):
        raise ValueError(f"{kind} is empty")
    return value


class SessionLog:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.path = path
        self.secrets = tuple(
            sorted((secret for secret in secrets if secret), key=len, reverse=True)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)

    @property
    def review_state_path(self) -> Path:
        return self.path.with_name(self.path.name + REVIEW_STATE_SUFFIX)

    def _write_review_state(self, fields: dict[str, Any]) -> None:
        board_id = fields.get("board_id")
        ticket_id = fields.get("ticket_id")
        if not (
            isinstance(board_id, str)
            and 0 < len(board_id) <= 256
            and isinstance(ticket_id, str)
            and 0 < len(ticket_id) <= 512
        ):
            return
        state = {
            "schema": 1,
            "board_id": board_id,
            "ticket_id": ticket_id,
        }
        for name in ("submitted_at", "submission_digest"):
            value = fields.get(name)
            if isinstance(value, str) and 0 < len(value) <= 256:
                state[name] = value
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        path = self.review_state_path
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        return value

    def write(self, event: str, **fields: Any) -> None:
        safe = self.scrub(fields)
        safe["event"] = event
        safe["ts"] = (
            datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n")
        if event == "review_started":
            self._write_review_state(safe)
        elif event in {"review_finished", "runtime_session_started"}:
            self.review_state_path.unlink(missing_ok=True)

    def begin_session(self, role: str) -> None:
        """Fence review state left by an uncleanly terminated runtime."""
        self.write(
            "runtime_session_started",
            role=role,
            session_id=f"{os.getpid()}-{time.time_ns()}",
        )



@dataclass(frozen=True)
class WorktreeSession:
    source_dir: Path
    work_dir: Path
    branch: str | None
    isolated: bool
    readonly: bool


class GitWorktreeManager:
    """Create one isolated checkout per ticket without touching the source tree."""

    def __init__(self, agent_name: str, log: SessionLog) -> None:
        self.agent_name = self._component(agent_name)
        self.log = log

    @staticmethod
    def _component(value: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
        if not clean:
            raise ValueError("worktree identity is empty after normalization")
        return clean[:96]

    @staticmethod
    def _run(
        repo: Path, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=check,
            capture_output=True,
            text=True,
        )

    def _repo(self, work_dir: Path) -> tuple[Path, Path] | None:
        try:
            root_result = self._run(work_dir, "rev-parse", "--show-toplevel")
            root = Path(root_result.stdout.strip()).resolve()
            common_result = self._run(root, "rev-parse", "--git-common-dir")
        except (OSError, subprocess.CalledProcessError):
            return None
        common = Path(common_result.stdout.strip())
        if not common.is_absolute():
            common = root / common
        return root, common.resolve()

    def _location(self, common: Path, ticket_id: str) -> tuple[Path, str]:
        ticket = self._component(ticket_id)
        suffix = self._component(ticket_id.rsplit("-", 1)[-1])
        return (
            common / "pursers-worktrees" / f"{self.agent_name}-{ticket}",
            f"api/{self.agent_name}-{suffix}",
        )

    async def prepare(
        self,
        source_dir: Path,
        ticket_id: str,
        integration_ref: str = "main",
        *,
        readonly: bool = False,
    ) -> WorktreeSession:
        return await asyncio.to_thread(
            self._prepare, source_dir, ticket_id, integration_ref, readonly
        )

    def _prepare(
        self,
        source_dir: Path,
        ticket_id: str,
        integration_ref: str,
        readonly: bool,
    ) -> WorktreeSession:
        source_dir = source_dir.resolve()
        resolved = self._repo(source_dir)
        if resolved is None:
            self.log.write(
                "worktree_passthrough", ticket_id=ticket_id, work_dir=str(source_dir)
            )
            return WorktreeSession(
                source_dir, source_dir, None, isolated=False, readonly=readonly
            )
        if (
            not integration_ref
            or integration_ref.startswith("-")
            or any(ord(character) < 0x20 for character in integration_ref)
        ):
            raise ValueError("integration_ref is unsafe")
        root, common = resolved
        worktree, branch = self._location(common, ticket_id)
        if worktree.exists():
            current_root = self._repo(worktree)
            current_branch = self._run(
                worktree, "branch", "--show-current", check=False
            ).stdout.strip()
            if current_root is None or current_root[0] != worktree:
                raise RuntimeError(f"dedicated worktree path is unsafe: {worktree}")
            if readonly and current_branch:
                raise RuntimeError(f"review worktree is not detached: {worktree}")
            if not readonly and current_branch != branch:
                raise RuntimeError(
                    f"dedicated worktree has unexpected branch: {worktree}"
                )
            self.log.write(
                "worktree_reused",
                ticket_id=ticket_id,
                work_dir=str(worktree),
                branch=current_branch or None,
                readonly=readonly,
            )
            return WorktreeSession(
                root,
                worktree,
                current_branch or None,
                isolated=True,
                readonly=readonly,
            )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._run(root, "rev-parse", "--verify", f"{integration_ref}^{{commit}}")
        if readonly:
            self._run(
                root,
                "worktree",
                "add",
                "--detach",
                str(worktree),
                integration_ref,
            )
            session_branch = None
        else:
            exists = self._run(
                root,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                check=False,
            ).returncode == 0
            if exists:
                self._run(root, "worktree", "add", str(worktree), branch)
            else:
                self._run(
                    root,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    integration_ref,
                )
            session_branch = branch
        self.log.write(
            "worktree_created",
            ticket_id=ticket_id,
            source_dir=str(root),
            work_dir=str(worktree),
            branch=session_branch,
            readonly=readonly,
        )
        return WorktreeSession(
            root, worktree, session_branch, isolated=True, readonly=readonly
        )

    async def cleanup(self, session: WorktreeSession, *, submitted: bool) -> bool:
        if not session.isolated:
            return False
        return await asyncio.to_thread(self._cleanup, session, submitted)

    def _cleanup(self, session: WorktreeSession, submitted: bool) -> bool:
        status = self._run(session.work_dir, "status", "--porcelain", check=False)
        clean = status.returncode == 0 and not status.stdout.strip()
        if not clean and not submitted:
            self.log.write(
                "worktree_retained_dirty",
                work_dir=str(session.work_dir),
                branch=session.branch,
            )
            return False
        self._run(
            session.source_dir,
            "worktree",
            "remove",
            "--force",
            str(session.work_dir),
        )
        self.log.write(
            "worktree_removed",
            work_dir=str(session.work_dir),
            branch=session.branch,
            submitted=submitted,
        )
        return True

    async def sweep(
        self,
        work_specs: list[tuple[Path, str]],
        active_claims: set[tuple[str, str]],
    ) -> None:
        await asyncio.to_thread(self._sweep, work_specs, active_claims)

    def _sweep(
        self,
        work_specs: list[tuple[Path, str]],
        active_claims: set[tuple[str, str]],
    ) -> None:
        active_ticket_ids = {
            self._component(ticket_id) for _board_id, ticket_id in active_claims
        }
        visited: set[Path] = set()
        for source_dir, _integration_ref in work_specs:
            resolved = self._repo(source_dir.resolve())
            if resolved is None:
                continue
            root, common = resolved
            if common in visited:
                continue
            visited.add(common)
            managed_root = common / "pursers-worktrees"
            result = self._run(root, "worktree", "list", "--porcelain")
            for line in result.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                path = Path(line.removeprefix("worktree ")).resolve()
                try:
                    relative = path.relative_to(managed_root)
                except ValueError:
                    continue
                prefix = f"{self.agent_name}-"
                if not relative.name.startswith(prefix):
                    continue
                ticket_component = relative.name.removeprefix(prefix)
                if ticket_component in active_ticket_ids:
                    continue
                status = self._run(path, "status", "--porcelain", check=False)
                clean = status.returncode == 0 and not status.stdout.strip()
                if not clean:
                    self.log.write("orphan_worktree_retained_dirty", work_dir=str(path))
                    continue
                self._run(root, "worktree", "remove", "--force", str(path))
                self.log.write("orphan_worktree_removed", work_dir=str(path))

class BoardAPI(Protocol):
    async def wait(self, cursors: dict[str, int]) -> dict[str, Any]: ...
    async def claim(self, board_id: str, ticket_id: str) -> dict[str, Any]: ...
    async def ticket(self, board_id: str, ticket_id: str) -> dict[str, Any]: ...
    async def agent_id(self, board_id: str) -> str: ...
    async def work_dir(self, board_id: str) -> Path: ...
    async def renew(self, board_id: str, ticket_id: str) -> None: ...
    async def submit(
        self, board_id: str, ticket_id: str, arguments: dict[str, Any]
    ) -> None: ...
    async def release(self, board_id: str, ticket_id: str, reason: str) -> None: ...
    async def submitted(self) -> list[tuple[str, dict[str, Any]]]: ...
    async def principal_id(self, board_id: str) -> str: ...
    async def review(
        self,
        board_id: str,
        ticket_id: str,
        verdict: str,
        *,
        review_notes: str,
        fix_instructions: str | None,
    ) -> None: ...
    async def ticket_list(self, board_id: str, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def boards(self) -> list[str]: ...
    async def integration_ref(self, board_id: str) -> str: ...
    async def work_specs(self) -> list[tuple[Path, str]]: ...
    async def active_claims(self) -> set[tuple[str, str]]: ...


class PursersBoardAPI:
    def __init__(self, config: Config, token: str) -> None:
        global wait_bridge
        self.config = config
        os.environ["PURSERS_HOST"] = config.wait_host_profile
        os.environ["PURSERS_HOST_TIMEOUT_S"] = str(config.wait_timeout_s)
        if wait_bridge is None:
            wait_bridge = importlib.import_module("pursers_wait_server")
        self.wait_bridge = wait_bridge
        self.client = BoardClient(
            config.central_url,
            token,
            self.wait_bridge.BOARD_ID,
            agent_name=config.agent_name,
            role=config.role,
        )
        self.registry: dict[str, Any] | None = None
        self.views: dict[str, Any] = {}

    async def __aenter__(self) -> PursersBoardAPI:
        try:
            await self.client.__aenter__()
        except BoardClientError as exc:
            if self._reviewer_role_denied(exc):
                raise PermissionError(REVIEWER_AUTH_ERROR) from exc
            raise
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.client.__aexit__(*args)

    async def _boards(self) -> list[str]:
        if self.config.boards == "registry":
            self.registry = await self.wait_bridge._read_project_registry(self.client)
            return self.wait_bridge._registry_boards(self.registry)
        return list(self.config.boards)

    async def _view(self, board_id: str) -> Any:
        view = self.views.get(board_id)
        if view is None:
            view = self.wait_bridge._BoardView(self.client, board_id)
            try:
                await view.board_join(
                    agent_name=self.config.agent_name,
                    task_focus=(
                        f"worker-runtime role={self.config.role} "
                        f"max_tier={self.config.max_tier}"
                    ),
                )
            except BoardClientError as exc:
                if self._reviewer_role_denied(exc):
                    raise PermissionError(REVIEWER_AUTH_ERROR) from exc
                raise
            if self.config.role == "reviewer" and (
                view.identity is None or view.identity.role != "reviewer"
            ):
                raise PermissionError(REVIEWER_AUTH_ERROR)
            self.views[board_id] = view
        return view

    def _reviewer_role_denied(self, exc: BaseException) -> bool:
        if self.config.role != "reviewer":
            return False
        detail = str(exc).lower()
        return "board role not authorized" in detail or "board:review" in detail

    async def wait(self, cursors: dict[str, int]) -> dict[str, Any]:
        return await self.wait_bridge._wait_for_work_many(
            self.client,
            boards=await self._boards(),
            since_seq=cursors,
            timeout_s=self.config.wait_timeout_s,
            only_mine=False,
            agent_name=self.config.agent_name,
            wait_for=(
                "submitted" if self.config.role == "reviewer" else "claimable"
            ),
            task_focus=(
                f"worker-runtime role={self.config.role} "
                f"max_tier={self.config.max_tier}"
            ),
        )

    async def claim(self, board_id: str, ticket_id: str) -> dict[str, Any]:
        if self.config.role == "worker":
            try:
                project = await self._project(board_id)
            except (BoardClientError, ValueError):
                project = None
            if (
                project is not None
                and project.get("work_dir_owner", "operator") == "operator"
                and not project.get("fleet_clone_dir")
            ):
                raise RuntimeError(OPERATOR_CHECKOUT_REFUSAL)
        result = await (await self._view(board_id))._call(
            "ticket_claim",
            {"agent_name": self.config.agent_name, "ticket_id": ticket_id},
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result

    async def ticket(self, board_id: str, ticket_id: str) -> dict[str, Any]:
        result = await (await self._view(board_id)).ticket_get(ticket_id)
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result.get("ticket", result)

    async def agent_id(self, board_id: str) -> str:
        view = await self._view(board_id)
        if view.identity is None:  # pragma: no cover - _view always joins.
            raise RuntimeError("board identity is unavailable")
        return str(view.identity.agent_id)

    async def principal_id(self, board_id: str) -> str:
        view = await self._view(board_id)
        if view.identity is None:  # pragma: no cover - _view always joins.
            raise RuntimeError("board identity is unavailable")
        return str(view.identity.principal_id)

    async def work_dir(self, board_id: str) -> Path:
        project = await self._project(board_id)
        if (
            project.get("work_dir_owner", "operator") == "operator"
            and not project.get("fleet_clone_dir")
        ):
            raise RuntimeError(OPERATOR_CHECKOUT_REFUSAL)
        return Path(project.get("fleet_clone_dir") or project["work_dir"]).resolve()

    async def _project(self, board_id: str) -> dict[str, Any]:
        self.registry = self.registry or await self.wait_bridge._read_project_registry(
            self.client
        )
        for project in self.registry["projects"].values():
            if project["board_id"] == board_id and project["status"] == "active":
                return project
        raise ValueError(f"no active project registry entry for board {board_id}")

    async def renew(self, board_id: str, ticket_id: str) -> None:
        result = await (await self._view(board_id)).lease_renew(ticket_id)
        if result.get("error"):
            raise RuntimeError(str(result["error"]))

    async def submit(
        self, board_id: str, ticket_id: str, arguments: dict[str, Any]
    ) -> None:
        result = await (await self._view(board_id))._call(
            "ticket_submit",
            {
                "agent_name": self.config.agent_name,
                "ticket_id": ticket_id,
                "summary": arguments.get("summary"),
                "files_changed": arguments.get("files_changed", []),
                "notes": arguments.get("notes"),
                "stay_active": True,
            },
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))

    async def release(self, board_id: str, ticket_id: str, reason: str) -> None:
        result = await (await self._view(board_id))._call(
            "ticket_unclaim",
            {"agent_name": self.config.agent_name, "ticket_id": ticket_id},
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))

    async def submitted(self) -> list[tuple[str, dict[str, Any]]]:
        submitted: list[tuple[str, dict[str, Any]]] = []
        for board_id in await self._boards():
            result = await (await self._view(board_id)).ticket_list(
                status="submitted", include_closed=False, limit=100
            )
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            submitted.extend(
                (board_id, ticket)
                for ticket in result.get("tickets", [])
                if isinstance(ticket, dict) and ticket.get("status") == "submitted"
            )
        return submitted

    async def ticket_list(self, board_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        result = await (await self._view(board_id)).ticket_list(**kwargs)
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result.get("tickets", [])

    async def boards(self) -> list[str]:
        return await self._boards()

    async def integration_ref(self, board_id: str) -> str:
        self.registry = self.registry or await self.wait_bridge._read_project_registry(
            self.client
        )
        for project in self.registry['projects'].values():
            if project['board_id'] == board_id and project['status'] == 'active':
                return str(project.get('integration_ref', 'main'))
        raise ValueError(f'no active project registry entry for board {board_id}')

    async def work_specs(self) -> list[tuple[Path, str]]:
        self.registry = self.registry or await self.wait_bridge._read_project_registry(
            self.client
        )
        specs: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        for project in self.registry['projects'].values():
            if project['status'] != 'active':
                continue
            if (
                project.get('work_dir_owner', 'operator') == 'operator'
                and not project.get('fleet_clone_dir')
            ):
                continue
            work_dir = Path(
                project.get('fleet_clone_dir') or project['work_dir']
            ).resolve()
            if work_dir in seen:
                continue
            seen.add(work_dir)
            specs.append((work_dir, str(project.get('integration_ref', 'main'))))
        return specs

    async def active_claims(self) -> set[tuple[str, str]]:
        claims: set[tuple[str, str]] = set()
        for board_id in await self._boards():
            agent_id = await self.agent_id(board_id)
            result = await (await self._view(board_id)).ticket_list(
                status='claimed', include_closed=False, limit=500
            )
            if result.get('error'):
                raise RuntimeError(str(result['error']))
            for ticket in result.get('tickets', []):
                if not isinstance(ticket, dict) or ticket.get('status') != 'claimed':
                    continue
                claimant = ticket.get('claimed_by_agent_id') or ticket.get(
                    'claimed_by'
                )
                if claimant in {agent_id, self.config.agent_name}:
                    claims.add((board_id, str(ticket.get('ticket_id', ''))))
        return {(board_id, ticket_id) for board_id, ticket_id in claims if ticket_id}

    async def review(
        self,
        board_id: str,
        ticket_id: str,
        verdict: str,
        *,
        review_notes: str,
        fix_instructions: str | None,
    ) -> None:
        result = await (await self._view(board_id))._call(
            "ticket_review",
            {
                "agent_name": self.config.agent_name,
                "ticket_id": ticket_id,
                "verdict": verdict,
                "review_notes": review_notes,
                **(
                    {"fix_instructions": fix_instructions}
                    if fix_instructions is not None
                    else {}
                ),
            },
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))


class OpenAICompatible:
    def __init__(self, config: Config, api_key: str) -> None:
        self.config = config
        self.api_key = api_key

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": self.config.max_tokens,
            },
            separators=(",", ":"),
        ).encode("utf-8")

        def request() -> dict[str, Any]:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = (
                    "".join(AUTH_SCHEME_PARTS) + " " + self.api_key
                )
            raw = urllib.request.Request(
                self.config.base_url + "/chat/completions",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(
                raw, timeout=self.config.command_timeout_s
            ) as response:
                result = json.load(response)
            return result["choices"][0]["message"]

        return await asyncio.to_thread(request)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a timeboxed shell command in the assigned work directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a bounded UTF-8 file inside the assigned work directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write UTF-8 text inside the assigned work directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_work",
            "description": "Submit completed ticket work for independent review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["summary", "notes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_up",
            "description": (
                "Release the claim with a local reason so another worker can retry."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]

REVIEWER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run an allowlisted read-only inspection or test command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a bounded UTF-8 file inside the project work directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "Return the final structured independent-review verdict.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["approve", "reject"]},
                    "review_notes": {"type": "string"},
                    "fix_instructions": {"type": "string"},
                },
                "required": ["verdict", "review_notes"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    review_notes: str
    fix_instructions: str | None


def parse_review_verdict(arguments: Any) -> ReviewVerdict:
    """Parse the terminal tool arguments without coercion or inferred defaults."""
    if not isinstance(arguments, dict):
        raise ValueError("review verdict must be an object")
    if set(arguments) - {"verdict", "review_notes", "fix_instructions"}:
        raise ValueError("review verdict contains unexpected fields")
    verdict = arguments.get("verdict")
    if verdict not in {"approve", "reject"}:
        raise ValueError("verdict must be approve or reject")
    review_notes = _text(arguments.get("review_notes"), "review_notes")
    if len(review_notes) > MAX_REVIEW_FIELD:
        raise ValueError("review_notes exceeds the bounded review field limit")
    raw_fix = arguments.get("fix_instructions")
    if raw_fix is not None and not isinstance(raw_fix, str):
        raise ValueError("fix_instructions must be a string")
    fix_instructions = raw_fix.strip() if isinstance(raw_fix, str) else None
    if fix_instructions is not None and len(fix_instructions) > MAX_REVIEW_FIELD:
        raise ValueError("fix_instructions exceeds the bounded review field limit")
    if verdict == "reject" and not fix_instructions:
        raise ValueError("reject requires fix_instructions")
    if verdict == "approve" and fix_instructions:
        raise ValueError("approve must not contain fix_instructions")
    return ReviewVerdict(verdict, review_notes, fix_instructions or None)


class ReviewRateLimiter:
    def __init__(
        self, limit: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.clock = clock
        self.timestamps: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self.timestamps and now - self.timestamps[0] >= 3_600:
            self.timestamps.popleft()

    def acquire(self) -> bool:
        now = self.clock()
        self._prune(now)
        if len(self.timestamps) >= self.limit:
            return False
        self.timestamps.append(now)
        return True

    def retry_after(self) -> float:
        now = self.clock()
        self._prune(now)
        if len(self.timestamps) < self.limit:
            return 0.0
        return max(0.0, 3_600 - (now - self.timestamps[0]))


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


def _latest_submission(ticket: dict[str, Any]) -> dict[str, Any]:
    history = ticket.get("submission_history")
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        return history[-1]
    return ticket


def _submission_principal(ticket: dict[str, Any]) -> str | None:
    latest = _latest_submission(ticket)
    value = latest.get("submitted_by_principal_id") or ticket.get(
        "submitted_by_principal_id"
    )
    return str(value) if value else None


def review_context(ticket: dict[str, Any]) -> dict[str, Any]:
    latest = _latest_submission(ticket)
    required = ticket.get("required_fields")
    files = latest.get("files_changed")
    return {
        "ticket_id": str(ticket.get("ticket_id", "")),
        "title": _clip(ticket.get("title"), MAX_REVIEW_FIELD),
        "description": _clip(ticket.get("description"), MAX_REVIEW_DESCRIPTION),
        "required_fields": [
            _clip(item, 200)
            for item in (required if isinstance(required, list) else [])[:100]
        ],
        "tags": [
            _clip(item, 200)
            for item in (
                ticket.get("tags") if isinstance(ticket.get("tags"), list) else []
            )[:100]
        ],
        "latest_submission": {
            "summary": _clip(latest.get("summary"), MAX_REVIEW_FIELD),
            "notes": _clip(latest.get("notes"), MAX_REVIEW_FIELD),
            "files_changed": [
                _clip(item, 500)
                for item in (files if isinstance(files, list) else [])[:200]
            ],
            "submitted_at": _clip(latest.get("submitted_at"), 100),
            "submitted_by_agent_id": _clip(latest.get("submitted_by_agent_id"), 200),
            "submitted_by_principal_id": _clip(
                latest.get("submitted_by_principal_id")
                or ticket.get("submitted_by_principal_id"),
                200,
            ),
        },
    }


def submission_revision(ticket: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return the stable identity of the exact bounded submission being reviewed."""
    latest = review_context(ticket)["latest_submission"]
    submitted_at = latest["submitted_at"]
    principal_id = latest["submitted_by_principal_id"]
    if not submitted_at or not principal_id:
        return None
    digest = hashlib.sha256(
        json.dumps(
            latest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return submitted_at, principal_id, digest


def _readonly_command(command: str) -> tuple[list[str], bool]:
    """Return argv and whether it must run in a disposable project copy."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise PermissionError("invalid read-only command") from exc
    if not argv or any("\n" in part or "\r" in part for part in argv):
        raise PermissionError("invalid read-only command")
    executable = Path(argv[0]).name
    if executable == "git":
        if len(argv) < 2:
            raise PermissionError("git command is not in the read-only allowlist")
        subcommand = argv[1]
        # Fully read-only git subcommands (no mutating sub-subcommands)
        if subcommand in {
            "status",
            "diff",
            "show",
            "log",
            "rev-parse",
            "merge-base",
            "cat-file",
            "ls-files",
            "blame",
            "grep",
        }:
            forbidden = (
                "--output",
                "--exec",
                "--upload-pack",
                "--receive-pack",
                "--ext-diff",
                "--textconv",
                "--no-index",
                "--filters",
                "--open-files-in-pager",
            )
            if any(part == "-o" or part.startswith(forbidden) for part in argv[2:]):
                raise PermissionError("git write-capable option is forbidden")
            return argv, True
        # branch -- read-only listing and --contains checks only
        if subcommand == "branch":
            mutating = {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "-t",
                        "--delete", "--move", "--copy", "--edit-description",
                        "--force", "--track", "--set-upstream-to", "--unset-upstream"}
            forbidden = (
                "--output", "--exec", "--upload-pack", "--receive-pack",
                "--ext-diff", "--textconv", "--no-index", "--filters",
                "--open-files-in-pager",
            )
            # Flags that consume a following positional value argument
            _value_taking = frozenset({
                "--contains", "--no-contains", "--merged", "--no-merged",
                "--points-at", "--sort", "--format", "--abbrev", "--pattern",
            })
            _combined_mutating = "dDmMcCftu"
            _i = 2
            while _i < len(argv):
                _part = argv[_i]
                if _part in mutating:
                    raise PermissionError("git branch mutation is forbidden")
                # Also handle --flag=value form for mutating flags
                if any(_part.startswith(_m + "=") for _m in mutating if _m.startswith("--")):
                    raise PermissionError("git branch mutation is forbidden")
                if _part == "-o" or _part.startswith(forbidden):
                    raise PermissionError("git write-capable option is forbidden")
                # Combined short flags like -dD, -vf, -vu
                if _part.startswith("-") and not _part.startswith("--") and len(_part) > 1:
                    if any(_c in _part[1:] for _c in _combined_mutating):
                        raise PermissionError("git branch mutation is forbidden")
                # Value-taking flags consume their next argument
                if _part in _value_taking:
                    _i += 2
                    continue
                # Also handle --flag=value form
                if any(_part.startswith(_f + "=") for _f in _value_taking):
                    _i += 1
                    continue
                # Positional non-flag argument = branch creation (e.g. "git branch newname")
                if not _part.startswith("-"):
                    raise PermissionError("git branch positional arguments are forbidden")
                _i += 1
            return argv, True
        # worktree -- only allow read-only list subcommand
        if subcommand == "worktree":
            if len(argv) < 3 or argv[2] != "list":
                raise PermissionError("git worktree only allows 'list' subcommand")
            _forbidden = (
                "--output", "--exec", "--upload-pack", "--receive-pack",
                "--ext-diff", "--textconv", "--no-index", "--filters",
                "--open-files-in-pager",
            )
            if any(_part == "-o" or _part.startswith(_forbidden) for _part in argv[3:]):
                raise PermissionError("git write-capable option is forbidden")
            return argv, True
        raise PermissionError("git command is not in the read-only allowlist")
    if executable in {"pytest", "py.test"}:
        return argv, True
    if (
        (executable in {"python", "python3"} or re.match(r"^python3\.\d+$", executable))
        and len(argv) >= 3
        and argv[1:3] in (["-m", "pytest"], ["-m", "unittest"])
    ):
        return argv, True
    if executable == "ruff" and len(argv) >= 2 and argv[1] in {"check", "format"}:
        if argv[1] == "format" and "--check" not in argv:
            raise PermissionError("ruff format requires --check in reviewer mode")
        return argv, True
    if executable in {"npm", "pnpm", "yarn"} and any(
        item in argv[1:3] for item in ("test", "check")
    ):
        return argv, True
    if executable in {"cargo", "go"} and len(argv) >= 2 and argv[1] == "test":
        return argv, True
    raise PermissionError("command is not in the reviewer read-only allowlist")


def _jailed(work_dir: Path, raw_path: str) -> Path:
    root = work_dir.resolve()
    candidate = (root / raw_path).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise PermissionError("path escapes assigned work directory")
    return candidate


class Worker:
    def __init__(
        self,
        config: Config,
        board: BoardAPI,
        llm: Any,
        log: SessionLog,
        *,
        directive: str | None = None,
        worktrees: GitWorktreeManager | None = None,
    ) -> None:
        self.config = config
        self.board = board
        self.llm = llm
        self.log = log
        self.directive = directive or DIRECTIVE_PATH.read_text(encoding="utf-8")
        self.worktrees = worktrees or GitWorktreeManager(config.agent_name, log)
        self.stop = asyncio.Event()
        self._active_claim: tuple[str, str] | None = None
        self._released_with_issues: set[str] = set()

    def messages(
        self,
        board_id: str,
        ticket: dict[str, Any],
        work_dir: Path,
        branch: str | None = None,
    ) -> list[dict[str, Any]]:
        context = {
            "board_id": board_id,
            "work_dir": str(work_dir),
            "checkout": (
                "dedicated per-ticket git worktree"
                if branch is not None
                else "registered non-git work directory"
            ),
            "ticket_branch": branch,
            "commit_requirement": (
                "commit all ticket changes on ticket_branch before submit_work"
                if branch is not None
                else None
            ),
            "review": (
                "independent reviewer required; never review or merge your own work"
            ),
        }
        return [
            {"role": "system", "content": self.directive},
            {
                "role": "system",
                "content": "BOARD CONTEXT\n" + json.dumps(context, sort_keys=True),
            },
            {
                "role": "user",
                "content": "DYNAMIC TICKET\n" + json.dumps(ticket, sort_keys=True),
            },
        ]

    @staticmethod
    def _sandbox_available() -> bool:
        if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
            return False
        try:
            subprocess.run(
                [str(SANDBOX_EXEC), "-p", "(version 1)(allow default)",
                 "/bin/sh", "-c", "true"],
                capture_output=True, timeout=5, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def _shell_argv(self, command: str) -> list[str] | None:
        if not self._sandbox_available():
            return None
        protected = [self.config.token_file]
        if self.config.api_key_file is not None:
            protected.append(self.config.api_key_file)
        denies = "\n".join(
            f"(deny file-read* (literal {json.dumps(str(path))}))" for path in protected
        )
        profile = f"(version 1)\n(allow default)\n{denies}"
        return [str(SANDBOX_EXEC), "-p", profile, "/bin/sh", "-lc", command]

    async def _tool(
        self,
        name: str,
        args: dict[str, Any],
        work_dir: Path,
        board_id: str,
        ticket_id: str,
    ) -> tuple[str, bool]:
        if name == "read_file":
            path = _jailed(work_dir, _text(args.get("path"), "path"))
            data = await asyncio.to_thread(path.read_bytes)
            return data[:MAX_FILE_READ].decode("utf-8", errors="replace"), False
        if name == "write_file":
            path = _jailed(work_dir, _text(args.get("path"), "path"))
            content = _text(args.get("content"), "content")
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
            self.log.write("write_file", path=str(path.relative_to(work_dir)))
            return "written", False
        if name == "run_shell":
            command = _text(args.get("command"), "command")
            self.log.write("run_shell", command=command)
            shell_env = {
                key: value
                for key in SHELL_ENV_ALLOWLIST
                if key != self.config.api_key_env
                and (value := os.environ.get(key)) is not None
            }
            shell_env["HOME"] = str(work_dir)
            shell_env["TMPDIR"] = str(work_dir)
            git_identity = GitWorktreeManager._component(self.config.agent_name)
            shell_env.update(
                {
                    "GIT_AUTHOR_NAME": f"Pursers {git_identity}",
                    "GIT_AUTHOR_EMAIL": f"{git_identity}@pursers.local",
                    "GIT_COMMITTER_NAME": f"Pursers {git_identity}",
                    "GIT_COMMITTER_EMAIL": f"{git_identity}@pursers.local",
                }
            )
            shell_argv = self._shell_argv(command)
            process_args = shell_argv or ["/bin/sh", "-lc", command]
            process = await asyncio.create_subprocess_exec(
                *process_args,
                cwd=work_dir,
                env=shell_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async def bounded_output() -> bytes:
                assert process.stdout is not None
                output = bytearray()
                while True:
                    chunk = await process.stdout.read(4_096)
                    if not chunk:
                        await process.wait()
                        return bytes(output)
                    remaining = MAX_TOOL_OUTPUT - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        process.kill()
                        await process.wait()
                        return bytes(output) + b"\n[output truncated]"

            try:
                output = await asyncio.wait_for(
                    bounded_output(), timeout=self.config.command_timeout_s
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return "command timed out", False
            return output.decode("utf-8", errors="replace"), False
        if name == "submit_work":
            safe_args = self.log.scrub(args)
            try:
                await self.board.submit(board_id, ticket_id, safe_args)
            except Exception as exc:
                self.log.write(
                    "submit_failed", ticket_id=ticket_id, error=type(exc).__name__
                )
                await self._release(board_id, ticket_id, "submission API failure")
                return "released", True
            self.log.write("submit_work", ticket_id=ticket_id)
            return "submitted", True
        if name == "give_up":
            reason = _text(args.get("reason"), "reason")
            self.log.write("give_up", ticket_id=ticket_id, reason=reason)
            await self._release(board_id, ticket_id, reason)
            return "released", True
        raise ValueError(f"unknown tool: {name}")

    async def _release(self, board_id: str, ticket_id: str, reason: str) -> None:
        try:
            await self.board.release(board_id, ticket_id, reason)
            self.log.write("release", ticket_id=ticket_id, reason=reason)
        except Exception as exc:
            self.log.write(
                "release_failed", ticket_id=ticket_id, error=type(exc).__name__
            )
        verified = await self._verify_release(board_id, ticket_id)
        if not verified:
            self._released_with_issues.add(board_id)

    async def _verify_release(self, board_id: str, ticket_id: str) -> bool:
        """Verify a released ticket is actually open; log and return False on mismatch."""
        try:
            current = await self.board.ticket(board_id, ticket_id)
        except Exception as exc:
            self.log.write(
                "release_unverified",
                board_id=board_id,
                ticket_id=ticket_id,
                reason=f"ticket fetch failed after release: {type(exc).__name__}",
            )
            return False
        if current.get("status") != "open":
            self.log.write(
                "release_unverified",
                board_id=board_id,
                ticket_id=ticket_id,
                actual_status=current.get("status"),
            )
            return False
        return True

    async def _renew(self, board_id: str, ticket_id: str) -> None:
        try:
            while not self.stop.is_set():
                await asyncio.sleep(LEASE_INTERVAL_S)
                try:
                    await self.board.renew(board_id, ticket_id)
                except Exception as exc:
                    self.log.write(
                        "lease_renew_failed",
                        ticket_id=ticket_id,
                        error=type(exc).__name__,
                    )
        except asyncio.CancelledError:
            raise

    async def run_ticket(
        self,
        board_id: str,
        ticket: dict[str, Any],
        work_dir: Path,
        branch: str | None = None,
    ) -> str:
        ticket_id = str(ticket["ticket_id"])
        messages = self.messages(board_id, ticket, work_dir, branch)
        renewal = asyncio.create_task(self._renew(board_id, ticket_id))
        try:
            for _ in range(self.config.max_iterations):
                if self.stop.is_set():
                    await self._release(board_id, ticket_id, "graceful shutdown")
                    return "released"
                message = await self.llm.complete(messages, TOOLS)
                messages.append({"role": "assistant", **message})
                calls = message.get("tool_calls") or []
                if not calls:
                    continue
                for call in calls:
                    function = call.get("function", {})
                    arguments = json.loads(function.get("arguments") or "{}")
                    try:
                        output, done = await self._tool(
                            str(function.get("name")),
                            arguments,
                            work_dir,
                            board_id,
                            ticket_id,
                        )
                    except Exception as exc:
                        output, done = f"error: {type(exc).__name__}: {exc}", False
                    output = self.log.redact(output)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": output[:MAX_TOOL_OUTPUT],
                        }
                    )
                    if done:
                        return output
            await self._release(board_id, ticket_id, "max iterations reached")
            self.log.write("max_iterations", ticket_id=ticket_id)
            return "released"
        except Exception as exc:
            self.log.write(
                "hard_failure", ticket_id=ticket_id, error=type(exc).__name__
            )
            await self._release(board_id, ticket_id, "LLM or runtime hard failure")
            return "released"
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def _find_own_claims(
        self, board_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        """Fetch all claimed tickets on a board and filter locally for this seat."""
        try:
            all_claimed = await self.board.ticket_list(board_id, status="claimed")
        except Exception:
            return []
        agent_id_lower = agent_id.casefold()
        name_lower = self.config.agent_name.casefold()
        own: list[dict[str, Any]] = []
        for ticket in all_claimed:
            cid = ticket.get("claimed_by_agent_id") or ""
            cb = ticket.get("claimed_by") or ""
            if cid and str(cid).casefold() == agent_id_lower:
                own.append(ticket)
            elif cb and str(cb).casefold() in {agent_id_lower, name_lower}:
                own.append(ticket)
        return own

    async def _startup_sweep(self) -> None:
        """Scan all boards for orphaned claims by this seat; resume or release.
        Also sweep orphaned worktrees from previous runs."""
        try:
            board_ids = await self.board.boards()
        except Exception as exc:
            self.log.write("startup_sweep_boards_failed", error=type(exc).__name__)
            return
        if not board_ids:
            return
        for board_id in board_ids:
            try:
                agent_id = await self.board.agent_id(board_id)
            except Exception:
                continue
            claimed = await self._find_own_claims(board_id, agent_id)
            if not claimed:
                continue
            self.log.write(
                "startup_sweep_found_orphans",
                board_id=board_id,
                count=len(claimed),
            )
            if len(claimed) == 1 and claimed[0].get("status") == "claimed":
                ticket = claimed[0]
                ticket_id = str(ticket["ticket_id"])
                session = None
                resumed_ok = False
                outcome = "released"
                try:
                    source_dir = await self.board.work_dir(board_id)
                    integration_ref = await self.board.integration_ref(board_id)
                    self._active_claim = (board_id, ticket_id)
                    session = await self.worktrees.prepare(
                        source_dir, ticket_id, integration_ref
                    )
                    self.log.write(
                        "startup_sweep_resume",
                        board_id=board_id,
                        ticket_id=ticket_id,
                    )
                    outcome = await self.run_ticket(
                        board_id, ticket, session.work_dir, session.branch
                    )
                    self._active_claim = None
                    resumed_ok = True
                except Exception as exc:
                    self.log.write(
                        "startup_sweep_resume_failed",
                        board_id=board_id,
                        ticket_id=ticket_id,
                        error=type(exc).__name__,
                    )
                    self._active_claim = None
                    outcome = "released"
                finally:
                    if session is not None:
                        try:
                            await self.worktrees.cleanup(
                                session, submitted=outcome == "submitted"
                            )
                        except Exception as exc:
                            self.log.write(
                                "startup_sweep_cleanup_failed",
                                ticket_id=ticket_id,
                                error=type(exc).__name__,
                            )
                if not resumed_ok:
                    await self._release(
                        board_id, ticket_id, "orphaned by restart"
                    )
                continue
            # Release all (multiple or resume failed)
            for ticket in claimed:
                ticket_id = str(ticket["ticket_id"])
                await self._release(board_id, ticket_id, "orphaned by restart")
        # Sweep orphaned worktrees from previous runs
        try:
            work_specs = await self.board.work_specs()
            active_claims = await self.board.active_claims()
            await self.worktrees.sweep(work_specs, active_claims)
        except Exception as exc:
            self.log.write("worktree_sweep_failed", error=type(exc).__name__)

    async def run(self) -> None:
        await self._startup_sweep()
        cursors: dict[str, int] = {}
        while not self.stop.is_set():
            self._released_with_issues.clear()
            board_wait = asyncio.create_task(self.board.wait(cursors))
            shutdown = asyncio.create_task(self.stop.wait())
            try:
                done, _ = await asyncio.wait(
                    (board_wait, shutdown), return_when=asyncio.FIRST_COMPLETED
                )
                if shutdown in done:
                    break
                waited = board_wait.result()
            finally:
                for task in (board_wait, shutdown):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(board_wait, shutdown, return_exceptions=True)
            cursors = dict(waited.get("new_seq", cursors))
            candidates: list[tuple[int, int, str, str, dict[str, Any]]] = []
            seen: set[tuple[str, str]] = set()
            for index, event in enumerate(waited.get("events", [])):
                ticket_id = event.get("ticket_id")
                board_id = event.get("board_id")
                if not ticket_id or not board_id:
                    continue
                key = (str(board_id), str(ticket_id))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    ticket = await self.board.ticket(str(board_id), str(ticket_id))
                    agent_id = await self.board.agent_id(str(board_id))
                except Exception as exc:
                    self.log.write(
                        "candidate_read_failed",
                        board_id=board_id,
                        ticket_id=ticket_id,
                        error=type(exc).__name__,
                    )
                    continue
                priority = claim_priority(self.config, ticket, agent_id)
                if priority is None:
                    self.log.write(
                        "ticket_skipped",
                        board_id=board_id,
                        ticket_id=ticket_id,
                        ticket_tier=ticket_tier(ticket),
                        max_tier=self.config.max_tier,
                        ticket_roles=sorted(ticket_roles(ticket)),
                        seat_roles=self.config.roles,
                        assigned_to=ticket.get("assigned_to_agent_id")
                        or ticket.get("assigned_to"),
                        require_assigned_only=self.config.require_assigned_only,
                    )
                    continue
                candidates.append(
                    (priority, index, str(board_id), str(ticket_id), ticket)
                )

            for _priority, _index, board_id, ticket_id, ticket in sorted(candidates):
                if board_id in self._released_with_issues:
                    self.log.write(
                        "claim_blocked_release_unverified",
                        board_id=board_id,
                        ticket_id=ticket_id,
                    )
                    continue
                if self._active_claim is not None:
                    active_board, active_ticket = self._active_claim
                    try:
                        current = await self.board.ticket(active_board, active_ticket)
                    except Exception:
                        current = None
                    if current is not None and current.get("status") == "claimed":
                        self.log.write(
                            "claim_blocked_holding",
                            board_id=board_id,
                            ticket_id=ticket_id,
                            active_board=active_board,
                            active_ticket=active_ticket,
                        )
                        break
                    self._active_claim = None
                # Board-level check: query server for any existing claim by this seat
                try:
                    current_agent_id = await self.board.agent_id(board_id)
                    existing = await self._find_own_claims(board_id, current_agent_id)
                    if existing:
                        self.log.write(
                            "claim_blocked_board_check",
                            board_id=board_id,
                            ticket_id=ticket_id,
                            existing_count=len(existing),
                        )
                        break
                except Exception:
                    pass
                try:
                    await self.board.claim(board_id, ticket_id)
                except Exception:
                    continue
                session: WorktreeSession | None = None
                outcome = "released"
                try:
                    ticket = await self.board.ticket(board_id, ticket_id)
                    source_dir = await self.board.work_dir(board_id)
                    integration_ref = await self.board.integration_ref(board_id)
                    self._active_claim = (board_id, ticket_id)
                    session = await self.worktrees.prepare(
                        source_dir, ticket_id, integration_ref
                    )
                    outcome = await self.run_ticket(
                        board_id,
                        ticket,
                        session.work_dir,
                        session.branch,
                    )
                    self._active_claim = None
                except Exception as exc:
                    self.log.write(
                        "claimed_ticket_failure",
                        ticket_id=ticket_id,
                        error=type(exc).__name__,
                    )
                    await self._release(board_id, ticket_id, "board API failure")
                finally:
                    if session is not None:
                        try:
                            await self.worktrees.cleanup(
                                session, submitted=outcome == "submitted"
                            )
                        except Exception as exc:
                            self.log.write(
                                "worktree_cleanup_failed",
                                ticket_id=ticket_id,
                                error=type(exc).__name__,
                            )
                break


class Reviewer:
    def __init__(
        self,
        config: Config,
        board: BoardAPI,
        llm: Any,
        log: SessionLog,
        *,
        directive: str | None = None,
        limiter: ReviewRateLimiter | None = None,
        worktrees: GitWorktreeManager | None = None,
    ) -> None:
        self.config = config
        self.board = board
        self.llm = llm
        self.log = log
        self.directive = directive or REVIEWER_DIRECTIVE_PATH.read_text(
            encoding="utf-8"
        )
        self.limiter = limiter or ReviewRateLimiter(config.max_reviews_per_hour)
        self.worktrees = worktrees or GitWorktreeManager(config.agent_name, log)
        self.stop = asyncio.Event()
        self._active_review: tuple[str, str] | None = None
        self.seen_submissions: set[tuple[str, str, str]] = set()
        self.seen_submission_order: deque[tuple[str, str, str]] = deque()

    def _remember_submission(self, key: tuple[str, str, str]) -> None:
        if key in self.seen_submissions:
            return
        self.seen_submissions.add(key)
        self.seen_submission_order.append(key)
        while len(self.seen_submission_order) > MAX_SEEN_SUBMISSIONS:
            self.seen_submissions.discard(self.seen_submission_order.popleft())

    def messages(
        self,
        board_id: str,
        ticket: dict[str, Any],
        work_dir: Path,
        branch: str | None = None,
    ) -> list[dict[str, Any]]:
        context = {
            "board_id": board_id,
            "work_dir": str(work_dir),
            "checkout": (
                "dedicated per-ticket git worktree (detached, read-only)"
                if branch is not None
                else "registered non-git work directory"
            ),
            "access": "read-only independent review; never claim, edit, or submit work",
        }
        return [
            {"role": "system", "content": self.directive},
            {
                "role": "system",
                "content": "BOARD CONTEXT\n" + json.dumps(context, sort_keys=True),
            },
            {
                "role": "user",
                "content": "DYNAMIC REVIEW TICKET\n"
                + json.dumps(review_context(ticket), sort_keys=True),
            },
        ]

    def _finding(self, kind: str, board_id: str, ticket_id: str, detail: str) -> None:
        finding = {
            "kind": kind,
            "board_id": board_id,
            "ticket_id": ticket_id,
            "detail": detail,
        }
        self.log.write("review_finding", **finding)
        print(
            "FINDING reviewer-runtime "
            + json.dumps(self.log.scrub(finding), sort_keys=True),
            file=sys.stderr,
            flush=True,
        )

    def _sandbox_argv(self, argv: list[str], work_dir: Path) -> list[str]:
        if not Worker._sandbox_available():
            return argv
        protected = [self.config.token_file]
        if self.config.api_key_file is not None:
            protected.append(self.config.api_key_file)
        denies = [
            f"(deny file-read* (literal {json.dumps(str(path))}))" for path in protected
        ]
        denies.append(
            f"(deny file-write* (subpath {json.dumps(str(work_dir.resolve()))}))"
        )
        profile = "(version 1)\n(allow default)\n" + "\n".join(denies)
        return [str(SANDBOX_EXEC), "-p", profile, *argv]

    async def _run_readonly_shell(self, command: str, work_dir: Path) -> str:
        argv, needs_copy = _readonly_command(command)
        self.log.write("review_run_shell", command=command)
        scratch: tempfile.TemporaryDirectory[str] | None = None
        command_dir = work_dir
        try:
            if needs_copy and (sys.platform != "darwin" or not SANDBOX_EXEC.is_file()):
                scratch = tempfile.TemporaryDirectory(prefix="pursers-review-")
                command_dir = Path(scratch.name) / "project"
                await asyncio.to_thread(
                    shutil.copytree,
                    work_dir,
                    command_dir,
                    ignore=shutil.ignore_patterns(
                        ".pytest_cache", ".ruff_cache", "__pycache__"
                    ),
                )
            shell_env = {
                key: value
                for key in SHELL_ENV_ALLOWLIST
                if key != self.config.api_key_env
                and (value := os.environ.get(key)) is not None
            }
            shell_env.update(
                {
                    "HOME": tempfile.gettempdir(),
                    "TMPDIR": tempfile.gettempdir(),
                    "GIT_PAGER": "cat",
                    "PAGER": "cat",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            process = await asyncio.create_subprocess_exec(
                *self._sandbox_argv(argv, work_dir),
                cwd=command_dir,
                env=shell_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async def bounded_output() -> bytes:
                assert process.stdout is not None
                output = bytearray()
                while True:
                    chunk = await process.stdout.read(4_096)
                    if not chunk:
                        await process.wait()
                        return bytes(output)
                    remaining = MAX_TOOL_OUTPUT - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        process.kill()
                        await process.wait()
                        return bytes(output) + b"\n[output truncated]"

            try:
                output = await asyncio.wait_for(
                    bounded_output(), timeout=self.config.command_timeout_s
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return "command timed out"
            return output.decode("utf-8", errors="replace")
        finally:
            if scratch is not None:
                await asyncio.to_thread(scratch.cleanup)

    async def _tool(
        self, name: str, args: dict[str, Any], work_dir: Path
    ) -> tuple[str, ReviewVerdict | None]:
        if name == "read_file":
            path = _jailed(work_dir, _text(args.get("path"), "path"))
            data = await asyncio.to_thread(path.read_bytes)
            return data[:MAX_FILE_READ].decode("utf-8", errors="replace"), None
        if name == "run_shell":
            command = _text(args.get("command"), "command")
            return await self._run_readonly_shell(command, work_dir), None
        if name == "submit_review":
            return "structured verdict accepted", parse_review_verdict(args)
        raise PermissionError(f"tool {name!r} is unavailable in reviewer mode")

    async def run_review(
        self,
        board_id: str,
        ticket: dict[str, Any],
        work_dir: Path,
        branch: str | None = None,
    ) -> str:
        ticket_id = str(ticket["ticket_id"])
        if self._active_review is not None:
            active_board, active_ticket = self._active_review
            self._finding(
                "concurrent_review_refused",
                board_id,
                ticket_id,
                f"already reviewing {active_ticket} on {active_board}",
            )
            return "skipped"
        reviewed_revision = submission_revision(ticket)
        if reviewed_revision is None:
            self._finding(
                "submission_revision_missing",
                board_id,
                ticket_id,
                "latest submission lacks stable submitted_at or principal provenance",
            )
            return "skipped"
        submitted_at, submitted_by_principal_id, revision_digest = reviewed_revision
        self.log.write(
            "review_started",
            board_id=board_id,
            ticket_id=ticket_id,
            submitted_at=submitted_at,
            submitted_by_principal_id=submitted_by_principal_id,
            submission_digest=revision_digest,
        )
        self._active_review = (board_id, ticket_id)

        def finished(outcome: str) -> str:
            self._active_review = None
            self.log.write(
                "review_finished",
                board_id=board_id,
                ticket_id=ticket_id,
                submitted_at=submitted_at,
                submission_digest=revision_digest,
                outcome=outcome,
            )
            return outcome

        messages = self.messages(board_id, ticket, work_dir, branch)
        try:
            for _ in range(self.config.max_iterations):
                if self.stop.is_set():
                    return finished("stopped")
                message = await self.llm.complete(messages, REVIEWER_TOOLS)
                messages.append({"role": "assistant", **message})
                calls = message.get("tool_calls") or []
                if not calls:
                    if message.get("content"):
                        self._finding(
                            "unstructured_verdict",
                            board_id,
                            ticket_id,
                            "model returned text instead of a structured tool call",
                        )
                        return finished("skipped")
                    continue
                for call in calls:
                    function = call.get("function", {})
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        output, verdict = await self._tool(
                            str(function.get("name")), arguments, work_dir
                        )
                    except Exception as exc:
                        output = f"error: {type(exc).__name__}: {exc}"
                        verdict = None
                        if str(function.get("name")) == "submit_review":
                            self._finding(
                                "invalid_verdict",
                                board_id,
                                ticket_id,
                                str(exc),
                            )
                            return finished("skipped")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": self.log.redact(output)[:MAX_TOOL_OUTPUT],
                        }
                    )
                    if verdict is None:
                        continue
                    current = await self.board.ticket(board_id, ticket_id)
                    principal_id = await self.board.principal_id(board_id)
                    current_revision = submission_revision(current)
                    if current_revision != reviewed_revision:
                        self._finding(
                            "submission_revision_changed",
                            board_id,
                            ticket_id,
                            (
                                "latest submission no longer matches the exact "
                                "revision supplied to the reviewer"
                            ),
                        )
                        return finished("skipped")
                    submitted_by = current_revision[1]
                    if (
                        current.get("status") != "submitted"
                        or submitted_by == principal_id
                    ):
                        self._finding(
                            "self_or_stale_review_refused",
                            board_id,
                            ticket_id,
                            (
                                "submission changed, lacks provenance, or matches "
                                "reviewer principal"
                            ),
                        )
                        return finished("skipped")
                    await self.board.review(
                        board_id,
                        ticket_id,
                        verdict.verdict,
                        review_notes=verdict.review_notes,
                        fix_instructions=verdict.fix_instructions,
                    )
                    self.log.write(
                        "review_submitted",
                        board_id=board_id,
                        ticket_id=ticket_id,
                        verdict=verdict.verdict,
                    )
                    return finished(verdict.verdict)
            self._finding(
                "review_max_iterations",
                board_id,
                ticket_id,
                "model did not produce a valid structured verdict",
            )
            return finished("skipped")
        except Exception as exc:
            self._finding(
                "review_hard_failure", board_id, ticket_id, type(exc).__name__
            )
            return finished("skipped")

    async def _wait_or_stop(
        self, cursors: dict[str, int]
    ) -> tuple[dict[str, Any] | None, bool]:
        board_wait = asyncio.create_task(self.board.wait(cursors))
        shutdown = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait(
                (board_wait, shutdown), return_when=asyncio.FIRST_COMPLETED
            )
            if shutdown in done:
                return None, True
            return board_wait.result(), False
        finally:
            for task in (board_wait, shutdown):
                if not task.done():
                    task.cancel()
            await asyncio.gather(board_wait, shutdown, return_exceptions=True)

    @staticmethod
    def _resync_required(waited: dict[str, Any]) -> bool:
        resynced = waited.get("resynced")
        if isinstance(resynced, dict):
            return any(bool(value) for value in resynced.values())
        return bool(resynced or waited.get("resync_required"))

    async def _submitted_from_cues(
        self, waited: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        for event in waited.get("events", []):
            if not isinstance(event, dict):
                continue
            if not (
                event.get("status_to") == "submitted"
                or event.get("kind") in {"ticket_submitted", "review_requested"}
            ):
                continue
            board_id = str(event.get("board_id") or "")
            ticket_id = str(event.get("ticket_id") or "")
            key = (board_id, ticket_id)
            if not board_id or not ticket_id or key in seen:
                continue
            seen.add(key)
            try:
                ticket = await self.board.ticket(board_id, ticket_id)
            except Exception as exc:
                self.log.write(
                    "submitted_cue_read_failed",
                    board_id=board_id,
                    ticket_id=ticket_id,
                    error=type(exc).__name__,
                )
                continue
            if ticket.get("status") == "submitted":
                candidates.append((board_id, ticket))
        return candidates

    async def run(self) -> None:
        cursors: dict[str, int] = {}
        # Sweep orphaned worktrees on startup
        try:
            work_specs = await self.board.work_specs()
            active_claims = await self.board.active_claims()
            await self.worktrees.sweep(work_specs, active_claims)
        except Exception as exc:
            self.log.write("worktree_sweep_failed", error=type(exc).__name__)
        pending: deque[tuple[str, dict[str, Any]]] = deque()
        try:
            pending.extend(await self.board.submitted())
        except Exception as exc:
            self.log.write("submitted_discovery_failed", error=type(exc).__name__)
        while not self.stop.is_set():
            while pending and not self.stop.is_set():
                board_id, listed_ticket = pending.popleft()
                ticket_id = str(listed_ticket.get("ticket_id", ""))
                latest = _latest_submission(listed_ticket)
                submission_key = (
                    board_id,
                    ticket_id,
                    str(latest.get("submitted_at") or latest.get("summary") or ""),
                )
                if not ticket_id or submission_key in self.seen_submissions:
                    continue
                ticket = await self.board.ticket(board_id, ticket_id)
                principal_id = await self.board.principal_id(board_id)
                submitted_by = _submission_principal(ticket)
                if submitted_by is None or submitted_by == principal_id:
                    self._finding(
                        "self_review_refused",
                        board_id,
                        ticket_id,
                        (
                            "submission principal is missing"
                            if submitted_by is None
                            else "reviewer principal authored latest submission"
                        ),
                    )
                    self._remember_submission(submission_key)
                    continue
                if TIER_ORDER[ticket_tier(ticket)] > TIER_ORDER[self.config.max_tier]:
                    self.log.write(
                        "review_tier_skipped",
                        board_id=board_id,
                        ticket_id=ticket_id,
                        ticket_tier=ticket_tier(ticket),
                        max_tier=self.config.max_tier,
                    )
                    self._remember_submission(submission_key)
                    continue
                if not self.limiter.acquire():
                    self._finding(
                        "review_rate_limited",
                        board_id,
                        ticket_id,
                        f"max {self.config.max_reviews_per_hour} reviews per hour",
                    )
                    try:
                        await asyncio.wait_for(
                            self.stop.wait(),
                            timeout=max(0.1, min(60.0, self.limiter.retry_after())),
                        )
                    except TimeoutError:
                        pass
                    pending.appendleft((board_id, listed_ticket))
                    continue
                session: WorktreeSession | None = None
                try:
                    source_dir = await self.board.work_dir(board_id)
                    integration_ref = await self.board.integration_ref(board_id)
                    session = await self.worktrees.prepare(
                        source_dir,
                        ticket_id,
                        integration_ref,
                        readonly=True,
                    )
                    await self.run_review(
                        board_id, ticket, session.work_dir, session.branch
                    )
                except Exception as exc:
                    self._finding(
                        "review_worktree_failure",
                        board_id,
                        ticket_id,
                        type(exc).__name__,
                    )
                finally:
                    if session is not None:
                        try:
                            await self.worktrees.cleanup(session, submitted=False)
                        except Exception as exc:
                            self.log.write(
                                "worktree_cleanup_failed",
                                ticket_id=ticket_id,
                                error=type(exc).__name__,
                            )
                self._remember_submission(submission_key)
            if self.stop.is_set():
                break
            waited, stopped = await self._wait_or_stop(cursors)
            if stopped or waited is None:
                break
            cursors = dict(waited.get("new_seq", cursors))
            if self._resync_required(waited):
                try:
                    pending.extend(await self.board.submitted())
                except Exception as exc:
                    self.log.write(
                        "submitted_discovery_failed", error=type(exc).__name__
                    )
            else:
                pending.extend(await self._submitted_from_cues(waited))


async def async_main(config_path: Path) -> None:
    config = load_config(config_path)
    token = read_secret(config, "token")
    api_key = read_secret(config, "API key")
    log = SessionLog(config.log_file, secrets=(token, api_key))
    log.begin_session(config.role)
    async with PursersBoardAPI(config, token) as board:
        runtime: Worker | Reviewer
        if config.role == "reviewer":
            runtime = Reviewer(config, board, OpenAICompatible(config, api_key), log)
        else:
            runtime = Worker(config, board, OpenAICompatible(config, api_key), log)
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is not None:
                loop.add_signal_handler(signum, runtime.stop.set)
        await runtime.run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run a headless Pursers worker or reviewer"
    )
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    asyncio.run(async_main(args.config))


if __name__ == "__main__":
    main()
