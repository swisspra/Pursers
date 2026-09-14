#!/usr/bin/env python3
"""Run one ACP v1 subprocess as a constrained Pursers worker seat."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[2]
CLIENT_SRC = ROOT / "packages" / "client" / "src"
WAIT_ROOT = ROOT / "tools" / "wait-bridge"
for import_root in (CLIENT_SRC, WAIT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from pursers_client import BoardClient  # noqa: E402

from acp_client import ACPClient  # noqa: E402

JSON = dict[str, Any]
COMPLETION_META_KEY = "pursers"
COMPLETION_PREFIX = "PURSERS_COMPLETION_JSON:"
MAX_UPDATE_TEXT = 2_000
MAX_COMPLETION_TEXT = 100_000
BRANCH_AND_COMMIT_RE = re.compile(
    r"(?m)^branch_and_commit:\s*([^\s@]+)@([0-9a-f]{40})\s*$"
)
SECRET_PARTS = ("token", "credential", "secret", ".git")


class BoardPort(Protocol):
    agent_id: str
    principal_id: str

    async def claim(self, ticket_id: str) -> JSON: ...
    async def ticket_get(self, ticket_id: str) -> JSON: ...
    async def checkpoint(
        self, ticket_id: str, summary: str, *, files: list[str] | None = None
    ) -> None: ...
    async def renew(self, ticket_id: str) -> None: ...
    async def submit(self, ticket_id: str, completion: JSON) -> None: ...
    async def unclaim(self, ticket_id: str) -> None: ...


@dataclass(frozen=True)
class SeatConfig:
    config_file: Path
    central_url: str
    board_id: str
    agent_name: str
    expected_agent_id: str
    expected_principal_id: str
    token_file: Path
    command: tuple[str, ...]
    repository: Path
    base_ref: str
    work_root: Path
    policy_file: Path | None
    lease_interval_s: float = 300.0
    wait_timeout_s: int = 180


def _private_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    details = resolved.stat()
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
        raise PermissionError(f"{label} must be a mode-0600 regular file")
    return resolved


def load_config(path: str | os.PathLike[str]) -> SeatConfig:
    config_path = _private_file(Path(path), "config")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    seat, acp = raw.get("seat"), raw.get("acp")
    if not isinstance(seat, dict) or not isinstance(acp, dict):
        raise ValueError("config requires seat and acp objects")
    if "token" in seat:
        raise ValueError("inline board tokens are forbidden")
    command = acp.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise ValueError("acp.command must be a non-empty string list")
    for key in ("agent_name", "expected_agent_id", "expected_principal_id"):
        if not isinstance(seat.get(key), str) or not seat[key].strip():
            raise ValueError(f"seat.{key} must be a non-empty string")
    repository = Path(str(acp["repository"])).expanduser().resolve()
    if not repository.is_dir():
        raise ValueError("acp.repository must be an existing directory")
    base_ref = str(acp.get("base_ref", "origin/main"))
    if (
        not base_ref
        or base_ref.startswith("-")
        or any(ord(char) < 0x20 for char in base_ref)
    ):
        raise ValueError("acp.base_ref is unsafe")
    token_file = _private_file(Path(str(seat["token_file"])), "token file")
    policy_raw = acp.get("policy_file")
    policy_file = _private_file(Path(policy_raw), "policy file") if policy_raw else None
    lease_interval = float(raw.get("lease_interval_s", 300.0))
    if lease_interval <= 0:
        raise ValueError("lease_interval_s must be positive")
    wait_timeout = int(raw.get("wait_timeout_s", 180))
    if wait_timeout <= 0:
        raise ValueError("wait_timeout_s must be positive")
    return SeatConfig(
        config_file=config_path,
        central_url=str(seat["central_url"]),
        board_id=str(seat["board_id"]),
        agent_name=str(seat["agent_name"]),
        expected_agent_id=str(seat["expected_agent_id"]),
        expected_principal_id=str(seat["expected_principal_id"]),
        token_file=token_file,
        command=tuple(command),
        repository=repository,
        base_ref=base_ref,
        work_root=Path(str(acp["work_root"])).expanduser().resolve(),
        policy_file=policy_file,
        lease_interval_s=lease_interval,
        wait_timeout_s=wait_timeout,
    )


def sanitized_agent_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a small subprocess environment with board credentials removed."""
    incoming = os.environ if source is None else source
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }
    return {
        key: value
        for key, value in incoming.items()
        if key in allowed and not key.startswith(("ONBOARD_", "PURSERS_"))
    }


def _component(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
    if not clean:
        raise ValueError("worktree component is empty after normalization")
    return clean[:96]


def _sandbox_available() -> bool:
    executable = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not executable.is_file():
        return False
    try:
        subprocess.run(
            [str(executable), "-p", "(version 1)(allow default)", "/usr/bin/true"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _executable_read_roots(executable: str) -> set[Path]:
    """Return narrow lexical and real installation trees for an executable."""
    located = shutil.which(executable) if os.sep not in executable else executable
    if not located:
        return set()
    lexical = Path(os.path.abspath(os.path.expanduser(located)))
    if not lexical.exists():
        return set()

    roots: set[Path] = set()
    for path in (lexical, Path(os.path.realpath(lexical))):
        roots.add(path)
        parts = path.parts
        cellar = max(
            (index for index, part in enumerate(parts) if part == "Cellar"),
            default=-1,
        )
        if cellar >= 0 and len(parts) > cellar + 2:
            roots.add(Path(*parts[: cellar + 3]))
            continue
        opt = max(
            (index for index, part in enumerate(parts) if part == "opt"),
            default=-1,
        )
        if opt >= 0 and len(parts) > opt + 1:
            roots.add(Path(*parts[: opt + 2]))
            continue
        bin_dir = max(
            (
                index
                for index, part in enumerate(parts[:-1])
                if part in {"bin", "sbin"}
            ),
            default=-1,
        )
        roots.add(Path(*parts[:bin_dir]) if bin_dir > 0 else path.parent)
    return roots


def _ancestor_metadata_roots(paths: Iterable[Path]) -> set[Path]:
    """Return ancestors needed to traverse lexical and real path chains."""
    ancestors: set[Path] = set()
    for path in paths:
        lexical = Path(os.path.abspath(os.path.expanduser(path)))
        real = Path(os.path.realpath(lexical))
        ancestors.update(lexical.parents)
        ancestors.update(real.parents)
    return ancestors


def sandboxed_agent_command(
    command: Sequence[str],
    work_dir: Path,
    *,
    readable_roots: Sequence[Path] = (),
    protected_files: Sequence[Path] = (),
) -> tuple[str, ...]:
    """Apply the macOS process boundary used by the production ACP seat."""
    if not _sandbox_available():
        raise RuntimeError("macOS sandbox-exec is required for an ACP seat")
    readable = {
        work_dir.resolve(),
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/dev"),
        Path("/sbin"),
        Path("/Library"),
        Path("/private/etc"),
        Path("/private/var/db"),
    }
    for path in readable_roots:
        readable.add(Path(os.path.abspath(os.path.expanduser(path))))
        readable.add(Path(os.path.realpath(path)))
    for executable in (sys.executable, command[0] if command else ""):
        if executable:
            readable.update(_executable_read_roots(executable))
    for part in command:
        candidate = Path(part).expanduser()
        if candidate.exists():
            readable.add(Path(os.path.abspath(candidate)))
            readable.add(candidate.resolve())
    scratch = Path(tempfile.gettempdir()).resolve()
    if (work_dir / ".git").exists():
        _require_git_metadata_within(work_dir, (work_dir, scratch))
    readable.add(scratch)
    read_rules = [
        '(allow file-read* (literal "/"))',
        *(
            f"(allow file-read* (subpath {json.dumps(str(path))}))"
            for path in sorted(readable, key=str)
        ),
    ]
    metadata_rules = "\n".join(
        f"(allow file-read-metadata (literal {json.dumps(str(path))}))"
        for path in sorted(
            _ancestor_metadata_roots(
                (work_dir, scratch, *readable_roots, *readable)
            ),
            key=str,
        )
    )
    reads = "\n".join(read_rules)
    protects = "\n".join(
        f"(deny file-read* (literal {json.dumps(str(path.resolve()))}))"
        for path in protected_files
    )
    profile = (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        f"{reads}\n"
        f"{metadata_rules}\n"
        f"(allow file-write* (subpath {json.dumps(str(work_dir.resolve()))}))\n"
        f"(allow file-write* (subpath {json.dumps(str(scratch))}))\n"
        "(deny network*)\n"
        f"{protects}"
    )
    return ("/usr/bin/sandbox-exec", "-p", profile, *command)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _git_metadata_paths(work_dir: Path) -> dict[str, Path]:
    """Return every Git metadata path that must remain writable for a commit."""
    commands = {
        "git-dir": ("rev-parse", "--absolute-git-dir"),
        "common-dir": ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        "index": ("rev-parse", "--path-format=absolute", "--git-path", "index"),
    }
    return {
        label: Path(_git(work_dir, *command)).resolve()
        for label, command in commands.items()
    }


def _require_git_metadata_within(
    work_dir: Path, writable_roots: Sequence[Path]
) -> dict[str, Path]:
    """Fail closed when a sandboxed checkout stores mutable Git state elsewhere."""
    roots = tuple(path.resolve() for path in writable_roots)
    metadata = _git_metadata_paths(work_dir.resolve())
    escaped = [
        label
        for label, path in metadata.items()
        if not any(_inside(path, root) for root in roots)
    ]
    if escaped:
        raise RuntimeError(
            "ACP seat Git metadata escapes writable sandbox roots: "
            + ", ".join(sorted(escaped))
        )
    return metadata


class SeatPermissionPolicy:
    """Fail-closed ACP permission policy for one ticket checkout."""

    def __init__(self, work_dir: Path, document: JSON | None = None) -> None:
        self.work_dir = work_dir.resolve()
        raw = document or {}
        self.allow_terminal = raw.get("terminal", True) is True
        if raw.get("network", False) is not False:
            raise ValueError("ACP seat network access is not implemented")
        self.allow_network = False
        extra = raw.get("fs_roots", [])
        if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
            raise ValueError("policy fs_roots must be a string list")
        if any(not Path(path).expanduser().is_absolute() for path in extra):
            raise ValueError("policy fs_roots must contain only absolute paths")
        self.fs_roots = (self.work_dir, *(Path(p).resolve() for p in extra))
        self.decisions: list[str] = []

    @classmethod
    def load(cls, work_dir: Path, policy_file: Path | None) -> "SeatPermissionPolicy":
        document: JSON | None = None
        if policy_file is not None:
            parsed = json.loads(policy_file.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("policy file must contain an object")
            document = parsed
        return cls(work_dir, document)

    def __call__(self, params: JSON) -> str | None:
        tool = params.get("toolCall")
        options = params.get("options")
        if not isinstance(tool, dict) or not isinstance(options, list):
            return None
        kind = tool.get("kind", "other")
        allowed = False
        if kind in {"read", "search", "edit", "delete", "move"}:
            allowed = self._safe_locations(tool)
        elif kind == "execute":
            allowed = self.allow_terminal and self._safe_execution(tool)
        elif kind == "fetch":
            allowed = self.allow_network
        elif kind == "think":
            allowed = True
        desired = "allow_once" if allowed else "reject_once"
        event: JSON = {
            "decision": desired,
            "kind": kind,
            "title": str(tool.get("title", ""))[:200],
        }
        raw_input = tool.get("rawInput")
        if kind == "execute" and isinstance(raw_input, dict):
            event["cwd"] = str(raw_input.get("cwd", ""))[:500]
            argv = raw_input.get("argv")
            if isinstance(argv, list):
                event["argv"] = [str(part)[:500] for part in argv[:64]]
        self.decisions.append(json.dumps(event, sort_keys=True)[:MAX_UPDATE_TEXT])
        for option in options:
            if isinstance(option, dict) and option.get("kind") == desired:
                option_id = option.get("optionId")
                return option_id if isinstance(option_id, str) else None
        return None

    def _safe_locations(self, tool: JSON) -> bool:
        locations = tool.get("locations")
        if not isinstance(locations, list) or not locations:
            return False
        for location in locations:
            if not isinstance(location, dict) or not isinstance(location.get("path"), str):
                return False
            raw = Path(location["path"])
            if not raw.is_absolute():
                return False
            candidate = raw.resolve(strict=False)
            lowered = {part.casefold() for part in candidate.parts}
            if lowered.intersection(SECRET_PARTS):
                return False
            if not any(_inside(candidate, root) for root in self.fs_roots):
                return False
        return True

    def _safe_execution(self, tool: JSON) -> bool:
        raw_input = tool.get("rawInput")
        if not isinstance(raw_input, dict):
            return False
        cwd = raw_input.get("cwd")
        argv = raw_input.get("argv")
        if not isinstance(cwd, str) or not isinstance(argv, list) or not argv:
            return False
        if not all(isinstance(part, str) and "\x00" not in part for part in argv):
            return False
        resolved = Path(cwd).resolve(strict=False)
        return _inside(resolved, self.work_dir)


def build_prompt(ticket: JSON, work_dir: Path) -> str:
    """Build a principal-free prompt from ticket scope and decision text."""
    decisions = [
        str(item.get("text"))
        for item in ticket.get("annotations", [])
        if isinstance(item, dict)
        and item.get("kind") == "decision"
        and isinstance(item.get("text"), str)
    ]
    payload = {
        "ticket_id": ticket.get("ticket_id"),
        "title": ticket.get("title"),
        "description": ticket.get("description"),
        "scope": ticket.get("scope"),
        "required_fields": ticket.get("required_fields", []),
        "forbidden": ticket.get("forbidden", []),
        "decision_annotations": decisions,
        "work_dir": str(work_dir.resolve()),
    }
    return (
        "You are an ACP coding agent working on one already-claimed Pursers ticket. "
        "Do not access board credentials, identify or assert any board principal, or "
        "work outside work_dir. Commit the completed ticket branch, run the affected "
        "tests, and emit bounded ACP updates. The seat runtime publishes the branch. "
        "When finished, include "
        "_meta.pursers.completion with summary, files_changed, and notes containing "
        "branch_and_commit plus literal test-command/test-output/observations fields, "
        "or put the same object on one final line prefixed "
        f"{COMPLETION_PREFIX}\n\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )


def _bounded_update(update: JSON) -> str | None:
    kind = update.get("sessionUpdate")
    if kind not in {"plan", "agent_message_chunk", "tool_call", "tool_call_update"}:
        return None
    safe = {
        key: update[key]
        for key in ("sessionUpdate", "title", "kind", "status")
        if key in update
    }
    content = update.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        safe["text"] = str(content.get("text", ""))[:MAX_UPDATE_TEXT]
    return json.dumps(safe, sort_keys=True)[:MAX_UPDATE_TEXT]


def _completion_from(update: JSON) -> JSON | None:
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return None
    pursers = meta.get(COMPLETION_META_KEY)
    if not isinstance(pursers, dict):
        return None
    completion = pursers.get("completion")
    return completion if isinstance(completion, dict) else None


def _completion_from_text(text: str) -> JSON | None:
    for line in reversed(text.splitlines()):
        if not line.startswith(COMPLETION_PREFIX):
            continue
        try:
            parsed = json.loads(line.removeprefix(COMPLETION_PREFIX).strip())
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _offered_ticket_id(event: object) -> str | None:
    if not isinstance(event, dict) or event.get("reason") != "offer":
        return None
    direct = event.get("ticket_id")
    if isinstance(direct, str):
        return direct
    offer = event.get("offer")
    nested = offer.get("ticket_id") if isinstance(offer, dict) else None
    return nested if isinstance(nested, str) else None


def _git(work_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=work_dir, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def validate_completion(work_dir: Path, completion: JSON) -> JSON:
    """Validate completion metadata against the actual Git tip and tip diff."""
    summary = completion.get("summary")
    notes = completion.get("notes")
    files = completion.get("files_changed")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("completion summary is required")
    if not isinstance(notes, str):
        raise ValueError("completion notes are required")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("completion files_changed must be a string list")
    match = BRANCH_AND_COMMIT_RE.search(notes)
    if match is None:
        raise ValueError("completion notes need literal branch_and_commit")
    branch, commit = match.groups()
    actual_commit = _git(work_dir, "rev-parse", "--verify", "HEAD^{commit}")
    actual_branch = _git(work_dir, "branch", "--show-current")
    if _git(work_dir, "status", "--porcelain"):
        raise ValueError("completion checkout has uncommitted changes")
    actual_files = sorted(
        item
        for item in _git(
            work_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).splitlines()
        if item
    )
    if (branch, commit) != (actual_branch, actual_commit):
        raise ValueError("branch_and_commit does not match the checkout")
    if sorted(files) != actual_files:
        raise ValueError("files_changed does not match the exact tip diff")
    for field in ("test-command:", "test-output:", "observations:"):
        if field not in notes:
            raise ValueError(f"completion notes missing {field}")
    return {"summary": summary.strip(), "files_changed": files, "notes": notes}


def publish_branch(
    work_dir: Path,
    completion: JSON,
    *,
    publish_repository: Path | None = None,
) -> None:
    """Publish the validated branch outside the no-network child sandbox."""
    match = BRANCH_AND_COMMIT_RE.search(str(completion["notes"]))
    if match is None:  # validate_completion has already checked this.
        raise ValueError("completion notes need literal branch_and_commit")
    branch, commit = match.groups()
    if publish_repository is None:
        origin = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=work_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if origin.returncode != 0 or not origin.stdout.strip():
            return
        publish_target = "origin"
        push_options = ["--set-upstream"]
    else:
        publish_target = str(publish_repository.resolve())
        push_options = []
    subprocess.run(
        [
            "git",
            "push",
            *push_options,
            publish_target,
            f"HEAD:refs/heads/{branch}",
        ],
        cwd=work_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    remote = subprocess.run(
        [
            "git",
            "ls-remote",
            "--heads",
            publish_target,
            f"refs/heads/{branch}",
        ],
        cwd=work_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not remote or remote[0] != commit:
        raise RuntimeError("published ACP ticket branch does not match the tested commit")
    if publish_repository is None:
        return
    local_commit = _git(
        publish_repository, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
    )
    if local_commit != commit:
        raise RuntimeError("ACP source repository branch does not match the tested commit")
    upstream = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=publish_repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if upstream.returncode != 0 or not upstream.stdout.strip():
        return
    subprocess.run(
        [
            "git",
            "push",
            "--set-upstream",
            "origin",
            f"refs/heads/{branch}:refs/heads/{branch}",
        ],
        cwd=publish_repository,
        check=True,
        capture_output=True,
        text=True,
    )
    published = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=publish_repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not published or published[0] != commit:
        raise RuntimeError("published ACP ticket branch does not match the tested commit")


class ACPSeatRuntime:
    def __init__(
        self,
        board: BoardPort,
        command: Sequence[str],
        work_root: Path,
        *,
        repository: Path | None = None,
        base_ref: str = "origin/main",
        seat_name: str = "acp-seat",
        policy_file: Path | None = None,
        protected_files: Sequence[Path] = (),
        enforce_os_sandbox: bool = False,
        lease_interval_s: float = 300.0,
        request_timeout_s: float = 3_600.0,
    ) -> None:
        self.board = board
        self.command = tuple(command)
        self.work_root = work_root.resolve()
        self.repository = repository.resolve() if repository is not None else None
        self.base_ref = base_ref
        self.seat_name = seat_name
        self.policy_file = policy_file
        self.protected_files = tuple(path.resolve() for path in protected_files)
        self.enforce_os_sandbox = enforce_os_sandbox
        self.lease_interval_s = lease_interval_s
        self.request_timeout_s = request_timeout_s

    async def run_ticket(self, ticket_id: str) -> str:
        try:
            await self.board.claim(ticket_id)
        except Exception:
            return "claim_failed"
        completion: JSON | None = None
        completion_text: list[str] = []
        completion_text_chars = 0
        completion_ready = asyncio.Event()
        renewal: asyncio.Task[None] | None = None
        updates: asyncio.Task[None] | None = None
        policy: SeatPermissionPolicy | None = None
        permission_log_index = 0

        async def flush_permission_log() -> None:
            nonlocal permission_log_index
            if policy is None:
                return
            while permission_log_index < len(policy.decisions):
                entry = policy.decisions[permission_log_index]
                permission_log_index += 1
                await self.board.checkpoint(
                    ticket_id, f"ACP permission for {ticket_id}: {entry}"
                )

        try:
            ticket = await self.board.ticket_get(ticket_id)
            work_dir = await asyncio.to_thread(self._prepare_work_dir, ticket_id)
            policy = SeatPermissionPolicy.load(work_dir, self.policy_file)
            command = self.command
            if self.enforce_os_sandbox:
                command = sandboxed_agent_command(
                    command,
                    work_dir,
                    readable_roots=policy.fs_roots,
                    protected_files=self.protected_files,
                )
            async with ACPClient(
                command,
                process_cwd=work_dir,
                env=sanitized_agent_env(),
                permission_policy=policy,
                request_timeout=self.request_timeout_s,
            ) as client:
                await client.initialize(
                    client_capabilities={
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    }
                )
                session_id = await client.new_session(work_dir)
                renewal = asyncio.create_task(self._renew(ticket_id))

                async def consume_updates() -> None:
                    nonlocal completion, completion_text_chars
                    while True:
                        params = await client.next_update()
                        update = params.get("update")
                        if not isinstance(update, dict):
                            continue
                        found = _completion_from(update)
                        if found is not None:
                            completion = found
                            completion_ready.set()
                        content = update.get("content")
                        if (
                            update.get("sessionUpdate") == "agent_message_chunk"
                            and isinstance(content, dict)
                            and content.get("type") == "text"
                            and completion_text_chars < MAX_COMPLETION_TEXT
                        ):
                            chunk = str(content.get("text", ""))[
                                : MAX_COMPLETION_TEXT - completion_text_chars
                            ]
                            completion_text.append(chunk)
                            completion_text_chars += len(chunk)
                        bounded = _bounded_update(update)
                        if bounded:
                            await self.board.checkpoint(
                                ticket_id,
                                f"ACP update for {ticket_id}: {bounded}",
                            )

                updates = asyncio.create_task(consume_updates())
                result = await client.prompt(
                    session_id,
                    build_prompt(ticket, work_dir),
                    timeout=self.request_timeout_s,
                )
                if result.get("stopReason") != "end_turn":
                    raise RuntimeError(f"ACP turn stopped: {result.get('stopReason')}")
                if completion is None:
                    try:
                        await asyncio.wait_for(completion_ready.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
                await flush_permission_log()
                if completion is None:
                    completion = _completion_from_text("".join(completion_text))
            if completion is None:
                raise ValueError("end_turn did not include completion metadata")
            current = await self.board.ticket_get(ticket_id)
            if (
                current.get("status") != "claimed"
                or current.get("claimed_by_agent_id") != self.board.agent_id
            ):
                raise RuntimeError("seat no longer holds the ticket claim")
            validated = validate_completion(work_dir, completion)
            await asyncio.to_thread(
                publish_branch,
                work_dir,
                validated,
                publish_repository=self.repository,
            )
            await self.board.submit(ticket_id, validated)
            return "submitted"
        except asyncio.CancelledError:
            await flush_permission_log()
            await self.board.checkpoint(ticket_id, f"ACP seat cancelled {ticket_id}")
            await self.board.unclaim(ticket_id)
            raise
        except Exception as exc:
            await flush_permission_log()
            await self.board.checkpoint(
                ticket_id,
                f"ACP seat released {ticket_id} after {type(exc).__name__}: {exc}",
            )
            await self.board.unclaim(ticket_id)
            return "released"
        finally:
            for task in (updates, renewal):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (updates, renewal) if task is not None),
                return_exceptions=True,
            )

    async def _renew(self, ticket_id: str) -> None:
        while True:
            await asyncio.sleep(self.lease_interval_s)
            await self.board.renew(ticket_id)

    def _prepare_work_dir(self, ticket_id: str) -> Path:
        work_dir = (self.work_root / _component(ticket_id)).resolve()
        if not _inside(work_dir, self.work_root):
            raise ValueError("ticket work directory escapes work root")
        if self.repository is None:
            work_dir.mkdir(parents=True, exist_ok=True)
            return work_dir
        if not self.base_ref or self.base_ref.startswith("-"):
            raise ValueError("unsafe ACP seat base_ref")
        branch = f"acp/{_component(self.seat_name)}-{_component(ticket_id)}"
        if work_dir.exists():
            root = _git(work_dir, "rev-parse", "--show-toplevel")
            current = _git(work_dir, "branch", "--show-current")
            if Path(root).resolve() != work_dir or current != branch:
                raise RuntimeError("existing ACP ticket checkout is not reusable")
            _require_git_metadata_within(work_dir, (work_dir,))
            return work_dir
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        base_commit = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "rev-parse",
                "--verify",
                f"{self.base_ref}^{{commit}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "--no-local",
                "--",
                str(self.repository),
                str(work_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if (
            subprocess.run(
                ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"],
                cwd=work_dir,
                check=False,
                capture_output=True,
                text=True,
            ).returncode
            != 0
        ):
            subprocess.run(
                ["git", "fetch", "--no-tags", "origin", base_commit],
                cwd=work_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "checkout", "-B", branch, base_commit],
            cwd=work_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=work_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        _require_git_metadata_within(work_dir, (work_dir,))
        return work_dir


class CentralBoard:
    """Production board adapter; the board token never enters the ACP process."""

    def __init__(self, config: SeatConfig, token: str) -> None:
        import pursers_wait_server as wait_bridge

        self.config = config
        self.wait_bridge = wait_bridge
        self.client = BoardClient(
            config.central_url,
            token,
            config.board_id,
            agent_name=config.agent_name,
            role="worker",
            capabilities={
                "can_work": True,
                "can_review": False,
                "tier_max": 2,
                "max_parallel": 1,
            },
            allow_takeover=True,
        )
        self.agent_id = ""
        self.principal_id = ""

    async def __aenter__(self) -> "CentralBoard":
        await self.client.__aenter__()
        joined = await self.client.board_onboard(
            role="worker",
            capabilities={
                "can_work": True,
                "can_review": False,
                "tier_max": 2,
                "max_parallel": 1,
            },
            allow_takeover=True,
            task_focus="ACP seat runtime",
        )
        self.agent_id = str(joined["agent_id"])
        self.principal_id = str(joined["principal_id"])
        if joined.get("agent_name") != self.config.agent_name:
            raise PermissionError("Central returned a different ACP seat name")
        if joined.get("role") != "worker":
            raise PermissionError("Central did not onboard the ACP seat as worker")
        if self.agent_id != self.config.expected_agent_id:
            raise PermissionError("Central returned a different ACP seat agent_id")
        if self.principal_id != self.config.expected_principal_id:
            raise PermissionError("Central returned a different ACP seat principal_id")
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.__aexit__(*args)

    async def wait(self, cursor: int | None) -> JSON:
        return await self.wait_bridge._a2a_wait_impl(
            self.client,
            since_seq=None if cursor is None else {self.config.board_id: cursor},
            timeout_s=self.config.wait_timeout_s,
            only_mine=True,
            agent_name=self.config.agent_name,
            boards=[self.config.board_id],
        )

    async def claim(self, ticket_id: str) -> JSON:
        return await self.client.ticket_claim(ticket_id)

    async def ticket_get(self, ticket_id: str) -> JSON:
        return (await self.client.ticket_get(ticket_id))["ticket"]

    async def checkpoint(
        self, ticket_id: str, summary: str, *, files: list[str] | None = None
    ) -> None:
        await self.client.memory_checkpoint(
            summary,
            files=files or [],
            remaining_tasks=[ticket_id],
        )

    async def renew(self, ticket_id: str) -> None:
        await self.client.lease_renew(ticket_id)

    async def submit(self, ticket_id: str, completion: JSON) -> None:
        await self.client.ticket_submit(ticket_id, **completion, stay_active=False)

    async def unclaim(self, ticket_id: str) -> None:
        await self.client._call(
            "ticket_unclaim", {"agent_name": self.config.agent_name, "ticket_id": ticket_id}
        )


async def run(config: SeatConfig) -> None:
    token = config.token_file.read_text(encoding="utf-8").strip()
    config.work_root.mkdir(parents=True, exist_ok=True)
    async with CentralBoard(config, token) as board:
        runtime = ACPSeatRuntime(
            board,
            config.command,
            config.work_root,
            repository=config.repository,
            base_ref=config.base_ref,
            seat_name=config.agent_name,
            policy_file=config.policy_file,
            protected_files=tuple(
                path
                for path in (config.config_file, config.token_file, config.policy_file)
                if path is not None
            ),
            enforce_os_sandbox=True,
            lease_interval_s=config.lease_interval_s,
        )
        cursor: int | None = None
        while True:
            waited = await board.wait(cursor)
            new_seq = waited.get("new_seq", {}).get(config.board_id)
            if isinstance(new_seq, int):
                cursor = new_seq
            for event in waited.get("events", []):
                ticket_id = _offered_ticket_id(event)
                if ticket_id is not None:
                    await runtime.run_ticket(ticket_id)
                    break


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="mode-0600 JSON configuration")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
