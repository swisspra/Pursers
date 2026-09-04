#!/usr/bin/env python3
"""Host configuration, seat upgrades, prompts, doctor, and inventory.

The fleet dashboard imports this module.  The small CLI is intentionally only
for headless doctor/fix use; the operator-facing setup surface remains the
loopback dashboard Config page.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence


REPOSITORY = Path(__file__).resolve().parents[2]
WAIT_BRIDGE = REPOSITORY / "tools/wait-bridge"
SEAT_KIT = REPOSITORY / "tools/seat-kit/seat_new.py"
DEFAULT_STATE_DIR = Path("~/.pursers/fleet-dashboard").expanduser()
DEFAULT_INVENTORY = DEFAULT_STATE_DIR / "seats.json"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MANAGED_COMMENT = "# pursers-managed; edit through the fleet dashboard"


@dataclass(frozen=True)
class HostProfile:
    host_timeout_s: int
    block_s: int
    config_knob: str


HOST_PROFILES: dict[str, HostProfile] = {
    "codex": HostProfile(620, 560, "tool_timeout_sec"),
    "codex-cli": HostProfile(620, 560, "tool_timeout_sec"),
    "goose": HostProfile(300, 270, "timeout"),
    "claude-code": HostProfile(21_600, 21_540, "rotation_s"),
    "claude-desktop": HostProfile(240, 200, "host_deadline_s"),
    "headless": HostProfile(21_600, 21_540, "rotation_s"),
}


def wait_bridge_host_timeouts(path: Path | None = None) -> dict[str, int]:
    """Read the bridge table without importing its transport dependencies."""
    source = (path or WAIT_BRIDGE / "pursers_wait_server.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "HOST_TIMEOUTS_S"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            return {str(key): int(item) for key, item in value.items()}
    raise ValueError("wait bridge HOST_TIMEOUTS_S table is missing")


def _bridge_version() -> str:
    document = tomllib.loads((WAIT_BRIDGE / "pyproject.toml").read_text())
    return str(document["project"]["version"])


def _package_version(project: str) -> str:
    document = tomllib.loads(
        (REPOSITORY / f"packages/{project}/pyproject.toml").read_text()
    )
    return str(document["project"]["version"])


def _load_seat_new() -> Any:
    spec = importlib.util.spec_from_file_location("pursers_seat_new", SEAT_KIT)
    if spec is None or spec.loader is None:
        raise RuntimeError("seat-kit cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class DesiredSeat:
    host: str
    role: str
    name: str
    central_url: str
    home_board: str
    token_file: str
    ca_file: str
    bridge_command: str
    config_path: str
    seat_dir: str | None = None
    repository: str | None = None
    personal_command: str = "pursers-personal"
    token_env_var: str = "ONBOARD_CENTRAL_TOKEN"
    bridge_name: str | None = None

    def __post_init__(self) -> None:
        if self.host not in HOST_PROFILES:
            raise ValueError(f"unsupported host: {self.host}")
        if self.role not in {"worker", "reviewer"}:
            raise ValueError("role must be worker or reviewer")
        if not SAFE_NAME.fullmatch(self.name):
            raise ValueError("seat name must be a safe 1-80 character identifier")
        if not SAFE_NAME.fullmatch(self.home_board):
            raise ValueError("home board must be a safe 1-80 character identifier")
        if not ENV_NAME.fullmatch(self.token_env_var):
            raise ValueError("token env var must be a safe identifier")

    @property
    def connector_name(self) -> str:
        return self.bridge_name or f"pursers-wait-{self.name}"

    @property
    def profile(self) -> HostProfile:
        return HOST_PROFILES[self.host]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DesiredSeat":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass(frozen=True)
class Change:
    path: Path
    description: str
    before: str | None
    after: str | None
    mode: int = 0o600
    action: str = "write"

    def summary(self) -> str:
        state = "create" if self.before is None else "update"
        return f"{state} {self.path}: {self.description}"


@dataclass(frozen=True)
class ApplyResult:
    changed: tuple[str, ...]
    backups: tuple[str, ...]


@dataclass(frozen=True)
class DoctorCheck:
    seat: str
    check: str
    status: str
    message: str


class HostAdapter(Protocol):
    def inspect(self) -> dict[str, Any]: ...

    def plan(self, desired: DesiredSeat) -> list[Change]: ...

    def apply(self, plan: Sequence[Change]) -> ApplyResult: ...


def _backup_name(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    return candidate


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class FileAdapter:
    def apply(self, plan: Sequence[Change]) -> ApplyResult:
        changed: list[str] = []
        backups: list[str] = []
        for change in plan:
            if change.action == "git-ff":
                subprocess.run(
                    ["git", "pull", "--ff-only"], cwd=change.path, check=True
                )
                changed.append(str(change.path))
                continue
            if change.action == "personal-install":
                command = change.path
                python = command.parent / "python"
                if not python.exists():
                    subprocess.run(
                        [sys.executable, "-m", "venv", str(command.parent.parent)],
                        check=True,
                    )
                subprocess.run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        f"pursers-personal=={_package_version('personal')}",
                        f"pursers-central=={_package_version('central')}",
                        f"pursers-client=={_package_version('client')}",
                    ],
                    check=True,
                )
                changed.append(str(command))
                continue
            if change.after is None:
                continue
            path = change.path
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current == change.after:
                continue
            if current is not None:
                backup = _backup_name(path)
                shutil.copy2(path, backup)
                backups.append(str(backup))
            _atomic_write(path, change.after, change.mode)
            changed.append(str(path))
        return ApplyResult(tuple(changed), tuple(backups))


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _remove_toml_tables(text: str, names: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropping = False
    heading = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
    for line in lines:
        match = heading.match(line.rstrip("\n"))
        if match:
            dropping = match.group(1) in names
        if not dropping:
            kept.append(line)
    return "".join(kept).rstrip() + "\n"


def _bridge_shell_args() -> list[str]:
    script = (
        'token=$(tr -d "\\r\\n" < "$ONBOARD_CENTRAL_TOKEN_FILE"); '
        'export ONBOARD_CENTRAL_TOKEN="$token"; exec "$PURSERS_BRIDGE_COMMAND"'
    )
    return ["-c", script]


class CodexAdapter(FileAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def inspect(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        try:
            document = tomllib.loads(text) if text.strip() else {}
            error = None
        except tomllib.TOMLDecodeError as exc:
            document, error = {}, str(exc)
        return {"path": str(self.path), "document": document, "error": error}

    def _render(self, desired: DesiredSeat) -> str:
        name = desired.connector_name
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("bridge connector name is unsafe")
        current = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        tables = {
            f"mcp_servers.{name}",
            f"mcp_servers.{name}.env",
            "mcp_servers.pursers-dev",
            "mcp_servers.pursers-dev.env_http_headers",
        }
        prefix = _remove_toml_tables(current, tables)
        prefix = "".join(
            line
            for line in prefix.splitlines(keepends=True)
            if line.strip() != MANAGED_COMMENT
        )
        block = f"""
[{f'mcp_servers.{name}'}]
command = "/bin/sh"
args = {_toml_array(_bridge_shell_args())}
tool_timeout_sec = {desired.profile.host_timeout_s}

[{f'mcp_servers.{name}.env'}]
PURSERS_BRIDGE_COMMAND = {_toml_string(desired.bridge_command)}
ONBOARD_CENTRAL_TOKEN_FILE = {_toml_string(desired.token_file)}
ONBOARD_CENTRAL_URL = {_toml_string(desired.central_url)}
ONBOARD_BOARD_ID = {_toml_string(desired.home_board)}
ONBOARD_AGENT_NAME = {_toml_string(desired.name)}
PURSERS_HOST = {_toml_string(desired.host)}
SSL_CERT_FILE = {_toml_string(desired.ca_file)}

[mcp_servers.pursers-dev]
url = {_toml_string(desired.central_url)}
bearer_token_env_var = {_toml_string(desired.token_env_var)}

[mcp_servers.pursers-dev.env_http_headers]
ONBOARD_BOARD_ID = {_toml_string(desired.home_board)}
"""
        result = prefix.rstrip() + "\n\n" + MANAGED_COMMENT + block
        tomllib.loads(result)
        return result

    def plan(self, desired: DesiredSeat) -> list[Change]:
        after = self._render(desired)
        before = self.path.read_text(encoding="utf-8") if self.path.exists() else None
        if before == after:
            return []
        return [Change(self.path, "Codex wait and board connectors", before, after)]


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _goose_block(desired: DesiredSeat) -> list[str]:
    name = desired.connector_name
    return [
        f"  {name}:\n",
        f"    {MANAGED_COMMENT}\n",
        f"    name: {_yaml_quote(name)}\n",
        "    type: stdio\n",
        "    enabled: true\n",
        f"    timeout: {desired.profile.host_timeout_s}\n",
        f"    cmd: {_yaml_quote('/bin/sh')}\n",
        "    args:\n",
        *[f"      - {_yaml_quote(item)}\n" for item in _bridge_shell_args()],
        "    envs:\n",
        f"      PURSERS_BRIDGE_COMMAND: {_yaml_quote(desired.bridge_command)}\n",
        f"      ONBOARD_CENTRAL_TOKEN_FILE: {_yaml_quote(desired.token_file)}\n",
        f"      ONBOARD_CENTRAL_URL: {_yaml_quote(desired.central_url)}\n",
        f"      ONBOARD_BOARD_ID: {_yaml_quote(desired.home_board)}\n",
        f"      ONBOARD_AGENT_NAME: {_yaml_quote(desired.name)}\n",
        "      PURSERS_HOST: goose\n",
        f"      SSL_CERT_FILE: {_yaml_quote(desired.ca_file)}\n",
    ]


def _replace_goose_extension(text: str, desired: DesiredSeat) -> str:
    lines = text.splitlines(keepends=True)
    name = desired.connector_name
    extension_start = next(
        (index for index, line in enumerate(lines) if line.rstrip() == "extensions:"),
        None,
    )
    block = _goose_block(desired)
    if extension_start is None:
        prefix = text.rstrip()
        return (prefix + "\n\n" if prefix else "") + "extensions:\n" + "".join(block)
    target = re.compile(rf"^  {re.escape(name)}:\s*(?:#.*)?$")
    start = next(
        (
            index
            for index in range(extension_start + 1, len(lines))
            if target.match(lines[index].rstrip("\n"))
        ),
        None,
    )
    if start is None:
        lines[extension_start + 1 : extension_start + 1] = block
        return "".join(lines)
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip(" ")) < 4:
            break
        end += 1
    lines[start:end] = block
    return "".join(lines)


def _seat_python(bridge_command: str) -> Path:
    command = Path(bridge_command).expanduser()
    resolved = command.resolve() if command.exists() else command
    candidate = resolved.parent / "python"
    return candidate if candidate.exists() else Path(sys.executable)


class GooseAdapter(FileAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def inspect(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return {
            "path": str(self.path),
            "text": text,
            "managed": MANAGED_COMMENT in text,
        }

    def plan(self, desired: DesiredSeat) -> list[Change]:
        current = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        rendered = _replace_goose_extension(current, desired)
        changes: list[Change] = []
        if current != rendered:
            changes.append(
                Change(self.path, "Goose stdio extension", current or None, rendered)
            )
        if desired.seat_dir:
            seat_new = _load_seat_new()
            root = Path(desired.seat_dir).expanduser()
            repo_leaf = seat_new._repo_leaf(desired.repository) if desired.repository else None
            python = _seat_python(desired.bridge_command)
            files = {
                root / "bin/board.sh": (
                    seat_new._board_shell(
                        name=desired.name,
                        board=desired.home_board,
                        central_url=desired.central_url,
                        token_file=Path(desired.token_file).expanduser(),
                        ca_file=Path(desired.ca_file).expanduser(),
                        python=python,
                    ),
                    0o755,
                ),
                root / "bin/board.py": (
                    seat_new._board_python(
                        desired.role,
                        repo_leaf,
                        desired.profile.block_s,
                    ),
                    0o644,
                ),
            }
            guidance = (
                PromptRenderer().render(desired)
                + "\n## Local seat CLI\n\n"
                + seat_new._instructions(
                    role=desired.role,
                    name=desired.name,
                    client="goose",
                )
            )
            files[root / "AGENTS.md"] = (guidance, 0o644)
            files[root / ".goosehints"] = (guidance, 0o644)
            for path, (after, mode) in files.items():
                before = path.read_text(encoding="utf-8") if path.exists() else None
                if before != after:
                    changes.append(
                        Change(path, "managed Goose seat file", before, after, mode)
                    )
            if repo_leaf:
                clone = root / repo_leaf
                status = (
                    subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=clone,
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                    if clone.is_dir()
                    else None
                )
                if (
                    status is not None
                    and status.returncode == 0
                    and not status.stdout.strip()
                ):
                    behind = subprocess.run(
                        ["git", "rev-list", "--count", "HEAD..@{upstream}"],
                        cwd=clone,
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                    if (
                        behind.returncode == 0
                        and int(behind.stdout.strip() or "0") > 0
                    ):
                        changes.append(
                            Change(
                                clone,
                                "fast-forward clean clone",
                                None,
                                None,
                                action="git-ff",
                            )
                        )
        return changes

    def apply(self, plan: Sequence[Change]) -> ApplyResult:
        result = super().apply(plan)
        for changed in result.changed:
            path = Path(changed)
            if path.name == "board.sh":
                path.chmod(0o755)
        return result


def _json_document(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration root must be an object: {path}")
    return value


def _bridge_json(desired: DesiredSeat) -> dict[str, Any]:
    return {
        "command": "/bin/sh",
        "args": _bridge_shell_args(),
        "env": {
            "PURSERS_BRIDGE_COMMAND": desired.bridge_command,
            "ONBOARD_CENTRAL_TOKEN_FILE": desired.token_file,
            "ONBOARD_CENTRAL_URL": desired.central_url,
            "ONBOARD_BOARD_ID": desired.home_board,
            "ONBOARD_AGENT_NAME": desired.name,
            "PURSERS_HOST": desired.host,
            "SSL_CERT_FILE": desired.ca_file,
        },
    }


class JsonHostAdapter(FileAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def inspect(self) -> dict[str, Any]:
        try:
            return {"path": str(self.path), "document": _json_document(self.path), "error": None}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"path": str(self.path), "document": {}, "error": str(exc)}

    def _document(self, desired: DesiredSeat) -> dict[str, Any]:
        document = _json_document(self.path)
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be an object")
        servers[desired.connector_name] = _bridge_json(desired)
        return document

    def plan(self, desired: DesiredSeat) -> list[Change]:
        document = self._document(desired)
        rendered = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        before = self.path.read_text(encoding="utf-8") if self.path.exists() else None
        if before == rendered:
            return []
        return [Change(self.path, "MCP server entries", before, rendered)]


class ClaudeDesktopAdapter(JsonHostAdapter):
    def _document(self, desired: DesiredSeat) -> dict[str, Any]:
        document = super()._document(desired)
        document["mcpServers"]["pursers-personal"] = {
            "command": desired.personal_command,
            "args": ["mcp", "--host-id", "claude-desktop", "--session", desired.name],
        }
        return document

    def plan(self, desired: DesiredSeat) -> list[Change]:
        changes = super().plan(desired)
        command = Path(desired.personal_command).expanduser()
        if command.is_absolute():
            actual = "missing"
            if command.is_file() and os.access(command, os.X_OK):
                result = subprocess.run(
                    [str(command), "--version"],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    actual = result.stdout.strip()
            expected = _package_version("personal")
            if actual != expected:
                changes.append(
                    Change(
                        command,
                        "install pinned Personal dashboard venv",
                        actual,
                        expected,
                        action="personal-install",
                    )
                )
        return changes


class ClaudeCodeAdapter(JsonHostAdapter):
    def __init__(self, path: str | Path):
        self.raw_path = str(path)
        super().__init__(path or ".mcp.json")

    def command(self, desired: DesiredSeat) -> str:
        payload = json.dumps(_bridge_json(desired), separators=(",", ":"))
        return (
            "claude mcp add-json "
            + shlex.quote(desired.connector_name)
            + " "
            + shlex.quote(payload)
        )

    def inspect(self) -> dict[str, Any]:
        if not self.raw_path:
            return {
                "path": None,
                "document": {},
                "error": None,
                "write_enabled": False,
            }
        result = super().inspect()
        result["write_enabled"] = True
        return result

    def plan(self, desired: DesiredSeat) -> list[Change]:
        if not self.raw_path:
            return []
        return super().plan(desired)


class BridgeInstaller:
    def __init__(
        self,
        version: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.version = version or _bridge_version()
        self.runner = runner

    def inspect(self) -> dict[str, Any]:
        command = shutil.which("pursers-wait-bridge")
        return {
            "version": self.version,
            "command": command,
            "installed": bool(command),
            "private_ca_active": bool(os.environ.get("SSL_CERT_FILE")),
        }

    def install(self) -> str:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required to install the wait bridge")
        environment = os.environ.copy()
        environment.pop("SSL_CERT_FILE", None)
        self.runner(
            [
                uv,
                "tool",
                "install",
                "--force",
                f"pursers-wait-bridge=={self.version}",
            ],
            check=True,
            env=environment,
            text=True,
            capture_output=True,
        )
        command = shutil.which("pursers-wait-bridge")
        if command is None:
            raise RuntimeError("bridge installed but its shim is not on PATH")
        return command


class PromptRenderer:
    def render(self, desired: DesiredSeat) -> str:
        host_note = {
            "codex": "This name is bound to this Codex window; never reuse it in another window.",
            "codex-cli": "This name is bound to this Codex CLI session.",
            "goose": "Run from the generated Goose seat folder and use its pinned interpreter.",
            "claude-desktop": (
                "Claude Desktop uses a 200s bridge block under its 240s host deadline."
            ),
            "claude-code": "Claude Code emits progress every 300s during its long rotation.",
            "headless": "Invoke a model only after an actionable cue.",
        }[desired.host]
        action = (
            "claim, implement, test, commit, push, and submit one ticket"
            if desired.role == "worker"
            else "review submitted work independently; never claim, edit, commit, or push"
        )
        template = (Path(__file__).with_name("seat_prompt_template.txt")).read_text(
            encoding="utf-8"
        )
        return template.format(
            name=desired.name,
            role=desired.role,
            quoted_name=json.dumps(desired.name),
            timeout_s=desired.profile.block_s,
            action=action,
            host_note=host_note,
        )


class SeatInventory:
    def __init__(self, path: str | Path = DEFAULT_INVENTORY):
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "seats": []}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("seats"), list):
            raise ValueError("seats.json must be a schema 1 inventory")
        return document

    def save(self, document: dict[str, Any]) -> None:
        _atomic_write(
            self.path,
            json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            0o600,
        )

    def upsert(
        self,
        desired: DesiredSeat,
        *,
        bridge_version: str,
        doctor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        document = self.load()
        record = {
            **asdict(desired),
            "bridge_version": bridge_version,
            "last_doctor": doctor,
        }
        seats = [row for row in document["seats"] if row.get("name") != desired.name]
        seats.append(record)
        document["seats"] = sorted(seats, key=lambda row: row["name"])
        self.save(document)
        return record


def adapter_for(desired: DesiredSeat) -> HostAdapter:
    if desired.host in {"codex", "codex-cli"}:
        return CodexAdapter(desired.config_path)
    if desired.host == "goose":
        return GooseAdapter(desired.config_path)
    if desired.host == "claude-desktop":
        return ClaudeDesktopAdapter(desired.config_path)
    if desired.host == "claude-code":
        return ClaudeCodeAdapter(desired.config_path)
    return ClaudeCodeAdapter(desired.config_path)


LiveProbe = Callable[[DesiredSeat, float], dict[str, Any]]


def _default_live_probe(desired: DesiredSeat, timeout_s: float) -> dict[str, Any]:
    """Probe board_status plus one real subscription handshake without leaking JWTs."""
    client_src = REPOSITORY / "packages/client/src"
    if str(client_src) not in sys.path:
        sys.path.insert(0, str(client_src))
    from pursers_client import BoardClient

    token = Path(desired.token_file).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("token file is empty")

    async def probe() -> dict[str, Any]:
        subscribed = asyncio.Event()
        async with BoardClient(
            desired.central_url,
            token,
            desired.home_board,
            agent_name=desired.name,
        ) as client:
            status = await client.board_status()
            state = await client.board_state_get("project_registry")
            registry_boards = [desired.home_board]
            raw = state.get("state", {}).get("value") if isinstance(state, dict) else None
            if isinstance(raw, str):
                try:
                    registry = json.loads(raw)
                    for row in registry.get("projects", {}).values():
                        board = row.get("board_id") if isinstance(row, dict) else None
                        if isinstance(board, str) and board not in registry_boards:
                            registry_boards.append(board)
                except (AttributeError, json.JSONDecodeError):
                    pass
            identity = client.identity
            if identity is None:
                raise RuntimeError("board join returned no identity")
            resources = (
                f"board://{desired.home_board}/journal",
                f"board://{desired.home_board}/agent/{identity.agent_id}",
            )

            async def listen() -> None:
                async for _event in client.events(
                    from_cursor=status.get("latest_seq"),
                    only_mine=False,
                    resource_subscriptions=resources,
                    acknowledge=False,
                    touch=False,
                    subscription_callback=subscribed.set,
                ):
                    break

            task = asyncio.create_task(listen())
            try:
                await subscribed.wait()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            available = [desired.home_board]
            skipped: dict[str, str] = {}
            for board in registry_boards[1:]:
                try:
                    async with BoardClient(
                        desired.central_url,
                        token,
                        board,
                        agent_name=desired.name,
                    ) as board_client:
                        await board_client.board_status()
                    available.append(board)
                except Exception as exc:
                    skipped[board] = type(exc).__name__
            return {
                "mode": "push",
                "registry_boards": available,
                "skipped_boards": skipped,
            }

    return asyncio.run(asyncio.wait_for(probe(), timeout=timeout_s))


class Doctor:
    def __init__(
        self,
        *,
        live_probe: LiveProbe | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.live_probe = live_probe or _default_live_probe
        self.runner = runner
        self.clock = clock

    def _check(
        self,
        desired: DesiredSeat,
        name: str,
        status: str,
        message: str,
    ) -> DoctorCheck:
        return DoctorCheck(desired.name, name, status, message)

    def run(self, desired: DesiredSeat) -> list[DoctorCheck]:
        rows: list[DoctorCheck] = []
        adapter = adapter_for(desired)
        inspection = adapter.inspect()
        if inspection.get("error"):
            rows.append(
                self._check(desired, "config", "FAIL", "configuration is invalid")
            )
        else:
            drift = adapter.plan(desired)
            config_drift = [change for change in drift if change.action == "write"]
            rows.append(
                self._check(
                    desired,
                    "config",
                    "PASS" if not config_drift else "FAIL",
                    (
                        "configuration matches"
                        if not config_drift
                        else f"{len(config_drift)} change(s) required"
                    ),
                )
            )
            personal_repairs = [
                change for change in drift if change.action == "personal-install"
            ]
            if desired.host == "claude-desktop" and Path(
                desired.personal_command
            ).is_absolute():
                rows.append(
                    self._check(
                        desired,
                        "personal-venv",
                        "FAIL" if personal_repairs else "PASS",
                        (
                            "pinned install required"
                            if personal_repairs
                            else f"version {_package_version('personal')}"
                        ),
                    )
                )
        timeout = desired.profile.host_timeout_s
        if desired.host in {"codex", "codex-cli"}:
            timeout = (
                inspection.get("document", {})
                .get("mcp_servers", {})
                .get(desired.connector_name, {})
                .get("tool_timeout_sec", 0)
            )
        elif desired.host == "goose":
            match = re.search(
                rf"(?ms)^  {re.escape(desired.connector_name)}:\s*$.*?^    timeout:\s*(\d+)\s*$",
                inspection.get("text", ""),
            )
            timeout = int(match.group(1)) if match else 0
        timeout_ok = isinstance(timeout, int) and timeout >= desired.profile.host_timeout_s
        rows.append(
            self._check(
                desired,
                "host-timeout",
                "PASS" if timeout_ok else "FAIL",
                f"configured={timeout}; required>={desired.profile.host_timeout_s}",
            )
        )
        for label, raw in (
            ("token-file", desired.token_file),
            ("ca-file", desired.ca_file),
        ):
            path = Path(raw).expanduser()
            ok = path.is_file() and os.access(path, os.R_OK)
            rows.append(
                self._check(
                    desired,
                    label,
                    "PASS" if ok else "FAIL",
                    "readable" if ok else "missing or unreadable",
                )
            )
        command = Path(desired.bridge_command).expanduser()
        executable = command.is_file() and os.access(command, os.X_OK)
        if executable:
            result = self.runner(
                [str(command), "--version"],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
            actual = result.stdout.strip()
            ok = result.returncode == 0 and actual == _bridge_version()
            rows.append(
                self._check(
                    desired,
                    "bridge",
                    "PASS" if ok else "FAIL",
                    f"version {actual or 'unknown'}",
                )
            )
        else:
            rows.append(self._check(desired, "bridge", "FAIL", "command does not resolve"))
        if desired.seat_dir:
            shell = Path(desired.seat_dir).expanduser() / "bin/board.sh"
            hints = Path(desired.seat_dir).expanduser() / ".goosehints"
            shell_text = shell.read_text(encoding="utf-8") if shell.is_file() else ""
            match = re.search(
                r"^exec\s+([^ ]+)\s+\"\$SCRIPT_DIR/board.py\"",
                shell_text,
                re.M,
            )
            interpreter = shlex.split(match.group(1))[0] if match else ""
            import_result = (
                self.runner(
                    [interpreter, "-c", "import httpx, mcp, pursers_client"],
                    check=False,
                    text=True,
                    capture_output=True,
                )
                if interpreter
                else None
            )
            ok = bool(import_result and import_result.returncode == 0)
            rows.append(
                self._check(
                    desired,
                    "seat-interpreter",
                    "PASS" if ok else "FAIL",
                    (
                        "required imports available"
                        if ok
                        else "required imports unavailable"
                    ),
                )
            )
            hint_text = hints.read_text(encoding="utf-8") if hints.is_file() else ""
            ok = (
                "sleep" not in hint_text.lower()
                and (
                    "a2a_wait" in hint_text
                    or ("wait --since" in hint_text and "Never poll" in hint_text)
                )
            )
            rows.append(
                self._check(
                    desired,
                    "seat-hints",
                    "PASS" if ok else "FAIL",
                    "push-wait guidance present" if ok else "wait guidance missing",
                )
            )
            if desired.repository:
                seat_new = _load_seat_new()
                clone = Path(desired.seat_dir).expanduser() / seat_new._repo_leaf(
                    desired.repository
                )
                clean = self.runner(
                    ["git", "status", "--porcelain"],
                    cwd=clone,
                    check=False,
                    text=True,
                    capture_output=True,
                )
                behind = self.runner(
                    ["git", "rev-list", "--count", "HEAD..@{upstream}"],
                    cwd=clone,
                    check=False,
                    text=True,
                    capture_output=True,
                )
                ok = (
                    clean.returncode == 0
                    and not clean.stdout.strip()
                    and behind.returncode == 0
                    and int(behind.stdout.strip() or "0") == 0
                )
                rows.append(
                    self._check(
                        desired,
                        "seat-clone",
                        "PASS" if ok else "WARN",
                        "clean and up to date" if ok else "dirty, missing, or behind",
                    )
                )
        try:
            result = self.live_probe(desired, 5.0)
            mode = result.get("mode")
            boards = result.get("registry_boards", [])
            skipped = result.get("skipped_boards", {})
            status = (
                "PASS"
                if mode == "push" and not skipped
                else "WARN"
                if mode in {"push", "poll"}
                else "FAIL"
            )
            rows.append(
                self._check(
                    desired,
                    "live-smoke",
                    status,
                    f"mode={mode}; boards={len(boards)}; skipped={len(skipped)}",
                )
            )
        except Exception as exc:  # Redacted boundary: never include arguments/tokens.
            rows.append(
                self._check(desired, "live-smoke", "FAIL", type(exc).__name__)
            )
        process_names = {
            "codex": "Codex",
            "codex-cli": "codex",
            "goose": "goose",
            "claude-desktop": "Claude",
            "claude-code": "claude",
            "headless": "pursers",
        }
        process = self.runner(
            ["ps", "-axo", "etimes=,comm="],
            check=False,
            text=True,
            capture_output=True,
        )
        ages = []
        for line in process.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if (
                len(parts) == 2
                and process_names[desired.host].lower() in parts[1].lower()
            ):
                try:
                    ages.append(int(parts[0]))
                except ValueError:
                    pass
        config = Path(desired.config_path).expanduser()
        needs_restart = bool(
            ages
            and config.exists()
            and self.clock() - config.stat().st_mtime < max(ages)
        )
        rows.append(
            self._check(
                desired,
                "restart",
                "WARN" if needs_restart else "PASS",
                (
                    "restart required"
                    if needs_restart
                    else "no stale host process detected"
                ),
            )
        )
        return rows


def _doctor_document(rows: Sequence[DoctorCheck]) -> dict[str, Any]:
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    overall = max((row.status for row in rows), key=order.get, default="PASS")
    return {
        "schema_version": 1,
        "overall": overall,
        "checks": [asdict(row) for row in rows],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="inspect configured seats")
    doctor.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    doctor.add_argument("--fix", action="store_true")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inventory = SeatInventory(args.inventory)
    document = inventory.load()
    outputs: list[dict[str, Any]] = []
    for record in document["seats"]:
        desired = DesiredSeat.from_dict(record)
        adapter = adapter_for(desired)
        if args.fix:
            adapter.apply(adapter.plan(desired))
        report = _doctor_document(Doctor().run(desired))
        inventory.upsert(desired, bridge_version=_bridge_version(), doctor=report)
        outputs.append({"seat": desired.name, **report})
    payload = {"schema_version": 1, "seats": outputs}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for seat in outputs:
            print(f"{seat['overall']:4} {seat['seat']}")
            for row in seat["checks"]:
                print(f"  {row['status']:4} {row['check']}: {row['message']}")
    return 1 if any(seat["overall"] == "FAIL" for seat in outputs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
