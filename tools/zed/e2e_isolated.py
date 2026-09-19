#!/usr/bin/env python3
"""Run an isolated end-to-end Pursers MCP proof in Zed 1.20.2.

The harness keeps every mutable Zed and Central path below one scratch root.
It intentionally reads credentials from a token file and never prints them.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence


PROMPT_NAMES = ("board", "create", "watch", "evidence", "answer")
PROTOCOL_VERSION = "2025-11-25"
DEFAULT_EXTENSION_DIR = Path("integrations/zed/pursers-mcp")
DEFAULT_CLIENT_DIR = Path("packages/client")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~-]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SEED_SCRIPT = """\
import asyncio
import json
import sys
from pathlib import Path

from pursers_client import BoardClient


async def main():
    central_url, token_path, board_id, tools_path = sys.argv[1:]
    token = Path(token_path).read_text(encoding="utf-8").strip()
    async with BoardClient(
        central_url,
        token,
        board_id,
        agent_name="zed-e2e-owner",
        role="worker",
        capabilities={"can_work": True, "can_review": False, "tier_max": 2, "max_parallel": 1},
        allow_takeover=True,
    ) as client:
        await client.ticket_create(
            None,
            "Zed isolated E2E seed",
            description="Created by the isolated Zed E2E harness.",
            priority="low",
            scope="interactive",
            target_url=board_id,
            required_fields=["test_output"],
        )
        listed = await client._client.list_tools()
        Path(tools_path).write_text(
            json.dumps(sorted(tool.name for tool in listed.tools)),
            encoding="utf-8",
        )


asyncio.run(main())
"""


class HarnessError(RuntimeError):
    """Expected, actionable harness failure."""


def redact(text: str) -> str:
    """Remove credential-shaped values before emitting captured process text."""
    return _JWT_RE.sub("<redacted-jwt>", _TOKEN_RE.sub("Bearer <redacted>", text))


def write_settings(
    path: Path,
    *,
    central_url: str,
    token_file: Path,
    board_id: str,
    package_spec: Path,
    timeout: int,
    uvx_path: Path | None = None,
) -> dict[str, Any]:
    """Write the extension-backed context-server settings for one scratch Zed."""
    payload: dict[str, Any] = {
        "telemetry": {"diagnostics": False, "metrics": False},
        "context_server_timeout": min(timeout, 600),
        "context_servers": {
            "pursers": {
                "enabled": True,
                "settings": {
                    "central_url": central_url,
                    "token_file": str(token_file.resolve()),
                    "board_id": board_id,
                    "package_spec": str(package_spec.resolve()),
                    "uvx_path": str((uvx_path or Path(shutil.which("uvx") or "uvx")).resolve()),
                },
            }
        },
        "agent": {"tool_permissions": {"default": "allow"}},
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return payload


@dataclass
class LogMatcher:
    """Incrementally retain literal lines and mark required Zed evidence."""

    expected_argv_terms: tuple[str, ...]
    buffer: str = ""
    lines: list[str] = field(default_factory=list)
    started: bool = False
    argv: bool = False
    tools_list: bool = False
    prompts_list: bool = False
    prompts: set[str] = field(default_factory=set)

    def feed(self, chunk: str) -> None:
        self.buffer += chunk
        rows = self.buffer.splitlines(keepends=True)
        self.buffer = ""
        if rows and not rows[-1].endswith(("\n", "\r")):
            self.buffer = rows.pop()
        for row in rows:
            self._line(row.rstrip("\r\n"))

    def finish(self) -> None:
        if self.buffer:
            self._line(self.buffer)
            self.buffer = ""

    def _line(self, line: str) -> None:
        safe = redact(line)
        lower = safe.lower()
        relevant = False
        if "starting context server pursers" in lower:
            self.started = True
            relevant = True
        if all(term.lower() in lower for term in self.expected_argv_terms):
            self.argv = True
            relevant = True
        if re.search(r'"method"\s*:\s*"tools/list"', safe) or "method=tools/list" in lower:
            self.tools_list = True
            relevant = True
        if re.search(r'"method"\s*:\s*"prompts/list"', safe) or "method=prompts/list" in lower:
            self.prompts_list = True
            relevant = True
        for name in PROMPT_NAMES:
            if re.search(rf'"name"\s*:\s*"{re.escape(name)}"', safe) or f"prompt {name}" in lower:
                self.prompts.add(name)
                relevant = True
        if relevant:
            self.lines.append(safe)

    @property
    def complete(self) -> bool:
        return (
            self.started
            and self.argv
            and self.tools_list
            and self.prompts_list
            and self.prompts == set(PROMPT_NAMES)
        )

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.started:
            missing.append("context server start")
        if not self.argv:
            missing.append("expected argv")
        if not self.tools_list:
            missing.append("tools/list")
        if not self.prompts_list:
            missing.append("prompts/list")
        missing.extend(f"prompt:{name}" for name in PROMPT_NAMES if name not in self.prompts)
        return missing


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=True,
        )
    except FileNotFoundError as exc:
        raise HarnessError(f"command not found: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = redact((exc.stderr or exc.stdout or "").strip())
        raise HarnessError(f"command failed ({exc.returncode}): {' '.join(argv)}\n{detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(f"command timed out: {' '.join(argv)}") from exc


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _require_inputs(extension_dir: Path, client_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    missing: list[str] = []
    extension_manifest = extension_dir / "extension.toml"
    extension_cargo = extension_dir / "Cargo.toml"
    client_project = client_dir / "pyproject.toml"
    for label, path in (
        ("B1 extension.toml", extension_manifest),
        ("B1 Cargo.toml", extension_cargo),
        ("B2 client pyproject.toml", client_project),
    ):
        if not path.is_file():
            missing.append(f"{label}: {path}")
    extension = (
        tomllib.loads(extension_manifest.read_text(encoding="utf-8"))
        if extension_manifest.is_file()
        else {}
    )
    client = (
        tomllib.loads(client_project.read_text(encoding="utf-8"))
        if client_project.is_file()
        else {}
    )
    if client_project.is_file() and "pursers-mcp" not in client.get("project", {}).get("scripts", {}):
        missing.append(f"B2 pursers-mcp entry point: {client_project}")
    if extension_manifest.is_file() and "pursers" not in extension.get("context_servers", {}):
        missing.append(f"B1 [context_servers.pursers]: {extension_manifest}")
    if missing:
        raise HarnessError("B1/B2 not present: " + "; ".join(missing))
    return extension, client


def _build_extension(extension_dir: Path, scratch: Path, extension_id: str) -> Path:
    rustup = shutil.which("rustup")
    if rustup is None:
        raise HarnessError("rustup is required to build the Zed extension")
    cargo = _run((rustup, "which", "cargo")).stdout.strip()
    rustc = _run((rustup, "which", "rustc")).stdout.strip()
    if not cargo or not rustc:
        raise HarnessError("rustup could not resolve cargo and rustc")
    env = os.environ.copy()
    target = scratch / "cargo-target"
    env["CARGO_TARGET_DIR"] = str(target)
    env["TMPDIR"] = str(scratch / "tmp")
    env["RUSTC"] = rustc
    env["PATH"] = os.pathsep.join((str(Path(cargo).parent), env.get("PATH", "")))
    if sys.platform == "darwin":
        toolchain_lib = str(Path(rustc).parent.parent / "lib")
        inherited = env.get("DYLD_LIBRARY_PATH")
        env["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            value for value in (toolchain_lib, inherited) if value
        )
    _run((rustup, "target", "add", "wasm32-wasip2"), env=env)
    _run((cargo, "build", "--release", "--target", "wasm32-wasip2"), cwd=extension_dir, env=env)
    release = target / "wasm32-wasip2" / "release"
    artifacts = sorted(path for path in release.glob("*.wasm") if path.is_file())
    if len(artifacts) != 1:
        raise HarnessError(f"expected one extension WASM under {release}, found {len(artifacts)}")
    staged = scratch / "dev-extension"
    shutil.copytree(
        extension_dir,
        staged,
        ignore=shutil.ignore_patterns(".git", "target", "extension.wasm"),
    )
    shutil.copy2(artifacts[0], staged / "extension.wasm")
    manifest = tomllib.loads((staged / "extension.toml").read_text(encoding="utf-8"))
    if manifest.get("id") != extension_id:
        raise HarnessError("extension id changed while staging")
    return staged


def _install_dev_extension(zed_data: Path, staged: Path, extension_id: str) -> Path:
    installed = zed_data / "extensions" / "installed"
    installed.mkdir(mode=0o700, parents=True, exist_ok=True)
    link = installed / extension_id
    link.symlink_to(staged, target_is_directory=True)
    return link


def _central_command() -> list[str]:
    configured = os.environ.get("PURSERS_CENTRAL_BIN")
    if configured:
        return [configured]
    installed = shutil.which("pursers-central")
    if installed:
        return [installed]
    uvx = shutil.which("uvx")
    local_central = REPOSITORY_ROOT / "packages" / "central"
    if uvx and (local_central / "pyproject.toml").is_file():
        return [uvx, "--from", str(local_central), "pursers-central"]
    raise HarnessError("pursers-central is not installed (or set PURSERS_CENTRAL_BIN)")


def _wait_health(url: str, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            raise HarnessError(f"throwaway Central exited early with status {process.returncode}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError(f"throwaway Central did not become healthy within {timeout:.1f}s")
        try:
            with urllib.request.urlopen(url, timeout=min(1.0, remaining)) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            # Wait without time.sleep; the Event provides one bounded blocking wait.
            import threading

            threading.Event().wait(min(0.05, remaining))


def _stdio_command(client_dir: Path, central_url: str, token_file: Path, board_id: str) -> tuple[list[str], dict[str, str]]:
    uvx = shutil.which("uvx")
    if uvx is None:
        raise HarnessError("uvx is required to run unreleased packages/client")
    argv = [
        uvx,
        "--from",
        str(client_dir.resolve()),
        "pursers-mcp",
        "--central-url",
        central_url,
        "--board",
        board_id,
        "--token-file",
        str(token_file.resolve()),
    ]
    env = os.environ.copy()
    for name in (
        "ONBOARD_CENTRAL_TOKEN",
        "ONBOARD_CENTRAL_TOKEN_FILE",
        "ONBOARD_TOKEN",
        "ONBOARD_TOKEN_FILE",
    ):
        env.pop(name, None)
    return argv, env


def read_default_tools(client_dir: Path) -> set[str]:
    """Read B2's advertised default allowlist without importing product code."""
    source_path = client_dir / "src" / "pursers_client" / "mcp_proxy.py"
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        raise HarnessError(f"cannot read B2 DEFAULT_TOOLS from {source_path}: {exc}") from exc
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "DEFAULT_TOOLS" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset" and len(value.args) == 1:
            value = value.args[0]
        try:
            names = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise HarnessError("B2 DEFAULT_TOOLS is not a literal collection") from exc
        if not isinstance(names, (set, frozenset, tuple, list)) or not all(
            isinstance(name, str) and name for name in names
        ):
            raise HarnessError("B2 DEFAULT_TOOLS is not a collection of names")
        return set(names)
    raise HarnessError(f"B2 DEFAULT_TOOLS not found in {source_path}")


def _seed_throwaway_board(
    client_dir: Path,
    central_url: str,
    token_file: Path,
    board_id: str,
    env: Mapping[str, str],
    timeout: float,
) -> set[str]:
    """Seed Central through BoardClient, independently of the MCP proxy under test."""
    uv = shutil.which("uv")
    if uv is None:
        raise HarnessError("uv is required to seed the throwaway Central")
    tools_path = token_file.parent / "upstream-tools.json"
    _run(
        (
            uv,
            "run",
            "--project",
            str(client_dir.resolve()),
            "python",
            "-c",
            _SEED_SCRIPT,
            central_url,
            str(token_file.resolve()),
            board_id,
            str(tools_path.resolve()),
        ),
        env=env,
        timeout=timeout,
    )
    try:
        names = json.loads(tools_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("direct Central seed did not record its advertised tools") from exc
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise HarnessError("direct Central seed recorded invalid advertised tools")
    return set(names)


class StdioMcp:
    """Small newline-delimited JSON-RPC client for the B2 process."""

    def __init__(self, argv: Sequence[str], env: Mapping[str, str], timeout: float):
        self.timeout = timeout
        self.process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            start_new_session=True,
        )
        if self.process.stdin is None or self.process.stdout is None or self.process.stderr is None:
            raise HarnessError("failed to open pursers-mcp stdio pipes")
        self._next_id = 1
        self._selector = select.poll()
        self._selector.register(self.process.stdout.fileno(), select.POLLIN | select.POLLHUP)
        self._selector.register(self.process.stderr.fileno(), select.POLLIN | select.POLLHUP)
        self._stdout = b""
        self._stderr = b""

    def close(self) -> None:
        _terminate(self.process)

    def _send(self, message: Mapping[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
        deadline = time.monotonic() + self.timeout
        while True:
            row = self._read_line(deadline)
            try:
                message = json.loads(row)
            except json.JSONDecodeError as exc:
                raise HarnessError(f"pursers-mcp wrote non-JSON stdout: {redact(row)!r}") from exc
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise HarnessError(f"MCP {method} failed: {redact(json.dumps(message['error'], sort_keys=True))}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise HarnessError(f"MCP {method} returned a non-object result")
            return result

    def _read_line(self, deadline: float) -> str:
        assert self.process.stdout is not None and self.process.stderr is not None
        while b"\n" not in self._stdout:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = redact(self._stderr.decode("utf-8", "replace")[-2000:])
                raise HarnessError(f"timed out waiting for pursers-mcp response\n{detail}")
            events = self._selector.poll(max(1, int(remaining * 1000)))
            if not events:
                continue
            for fd, _flags in events:
                chunk = os.read(fd, 65536)
                if fd == self.process.stdout.fileno():
                    self._stdout += chunk
                else:
                    self._stderr += chunk
            if self.process.poll() is not None and b"\n" not in self._stdout:
                detail = redact(self._stderr.decode("utf-8", "replace")[-2000:])
                raise HarnessError(f"pursers-mcp exited with status {self.process.returncode}\n{detail}")
        row, self._stdout = self._stdout.split(b"\n", 1)
        return row.decode("utf-8", "replace")


def _initialize_and_probe(
    client: StdioMcp, board_id: str, expected_tools: set[str]
) -> tuple[list[str], list[str], dict[str, Any]]:
    client.call(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pursers-zed-e2e", "version": "1"},
        },
    )
    client.notify("notifications/initialized")
    tools = client.call("tools/list").get("tools", [])
    prompts = client.call("prompts/list").get("prompts", [])
    tool_names = sorted(item.get("name") for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str))
    prompt_names = sorted(item.get("name") for item in prompts if isinstance(item, dict) and isinstance(item.get("name"), str))
    actual_tools = set(tool_names)
    if actual_tools != expected_tools:
        missing = sorted(expected_tools - actual_tools)
        unexpected = sorted(actual_tools - expected_tools)
        raise HarnessError(
            "pursers-mcp tools/list differs from B2 DEFAULT_TOOLS: "
            f"missing={missing}, unexpected={unexpected}"
        )
    missing_prompts = sorted(set(PROMPT_NAMES) - set(prompt_names))
    if missing_prompts:
        raise HarnessError("pursers-mcp prompts/list omitted: " + ", ".join(missing_prompts))
    status = client.call("tools/call", {"name": "board_status", "arguments": {"board_id": board_id}})
    if status.get("isError") is True:
        raise HarnessError("board_status returned isError=true")
    return tool_names, prompt_names, status


def _find_zed() -> Path:
    configured = os.environ.get("PURSERS_ZED_BIN")
    if configured:
        path = Path(configured)
    elif shutil.which("zed"):
        path = Path(shutil.which("zed") or "")
    else:
        path = Path("/Applications/Zed.app/Contents/MacOS/zed")
    if not path.is_file():
        raise HarnessError(f"Zed executable not found: {path}")
    return path.resolve()


def _isolated_zed_binary(source: Path, scratch: Path) -> Path:
    parts = source.parts
    try:
        app_index = next(index for index, part in enumerate(parts) if part.endswith(".app"))
    except StopIteration:
        return source
    app = Path(*parts[: app_index + 1])
    if sys.platform != "darwin":
        return source
    copied = scratch / f"ZedPursersE2E-{os.getpid()}.app"
    _run(("ditto", str(app), str(copied)))
    plist = copied / "Contents" / "Info.plist"
    bundle = f"dev.zed.Zed.PursersE2E.{os.getpid()}"
    _run(("plutil", "-replace", "CFBundleIdentifier", "-string", bundle, str(plist)))
    _run(("codesign", "--force", "--deep", "--sign", "-", str(copied)))
    relative = source.relative_to(app)
    return copied / relative


def _follow_zed_log(log_path: Path, matcher: LogMatcher, process: subprocess.Popen[bytes], timeout: float) -> None:
    if not hasattr(select, "kqueue"):
        raise HarnessError("event-driven Zed log following requires kqueue")
    deadline = time.monotonic() + timeout
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not log_path.exists():
        directory_fd = os.open(log_path.parent, os.O_RDONLY)
        queue = select.kqueue()
        event = select.kevent(
            directory_fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME,
        )
        queue.control([event], 0, 0)
        try:
            while not log_path.exists():
                if process.poll() is not None:
                    raise HarnessError(f"Zed exited with status {process.returncode} before creating its log")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessError("Zed did not create its scratch log before timeout")
                queue.control(None, 1, remaining)
        finally:
            queue.close()
            os.close(directory_fd)
    with log_path.open("rb", buffering=0) as stream:
        queue = select.kqueue()
        flags = select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE
        event = select.kevent(
            stream.fileno(),
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=flags,
        )
        queue.control([event], 0, 0)
        try:
            while not matcher.complete:
                chunk = stream.read()
                if chunk:
                    matcher.feed(chunk.decode("utf-8", "replace"))
                    continue
                if process.poll() is not None:
                    matcher.finish()
                    raise HarnessError(f"Zed exited with status {process.returncode}; missing: {', '.join(matcher.missing())}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    matcher.finish()
                    raise HarnessError(f"Zed log timeout; missing: {', '.join(matcher.missing())}")
                queue.control(None, 1, remaining)
        finally:
            queue.close()


def _terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-dir", type=Path, default=DEFAULT_EXTENSION_DIR)
    parser.add_argument("--client-dir", type=Path, default=DEFAULT_CLIENT_DIR)
    parser.add_argument("--keep", action="store_true", help="leave the scratch root for debugging")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.timeout <= 0:
        print("FAIL: --timeout must be positive", file=sys.stderr)
        return 2
    extension_dir = args.extension_dir.resolve()
    client_dir = args.client_dir.resolve()
    base = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="pursers-zed-e2e-", dir=base))
    central: subprocess.Popen[bytes] | None = None
    zed: subprocess.Popen[bytes] | None = None
    central_log: BinaryIO | None = None
    zed_stdio: BinaryIO | None = None
    try:
        extension, _client = _require_inputs(extension_dir, client_dir)
        extension_id = extension.get("id")
        if not isinstance(extension_id, str) or not extension_id:
            raise HarnessError("B1 extension.toml has no non-empty id")
        for directory in (scratch / "tmp", scratch / "home", scratch / "project", scratch / "zed-data"):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

        port = _free_port()
        board_id = f"zed-e2e-{os.getpid()}"
        instance = scratch / "central"
        central_command = _central_command()
        _run(
            (*central_command, "init", str(instance), "--port", str(port), "--board", board_id),
            timeout=args.timeout,
        )
        central_log = (scratch / "central.log").open("wb")
        central = subprocess.Popen(
            [*central_command, "run", str(instance)],
            stdout=central_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        central_url = f"http://127.0.0.1:{port}/mcp"
        _wait_health(f"http://127.0.0.1:{port}/healthz", central, min(args.timeout, 30.0))
        token_file = instance / "admin.jwt"

        staged = _build_extension(extension_dir, scratch, extension_id)
        zed_data = scratch / "zed-data"
        _install_dev_extension(zed_data, staged, extension_id)
        stdio_argv, stdio_env = _stdio_command(client_dir, central_url, token_file, board_id)
        uv_cache = (scratch / "uv-cache").resolve()
        uv_cache.mkdir(mode=0o700)
        stdio_env["UV_CACHE_DIR"] = str(uv_cache)
        upstream_tools = _seed_throwaway_board(
            client_dir,
            central_url,
            token_file,
            board_id,
            stdio_env,
            args.timeout,
        )
        expected_tools = read_default_tools(client_dir) & upstream_tools
        settings_path = zed_data / "config" / "settings.json"
        write_settings(
            settings_path,
            central_url=central_url,
            token_file=token_file,
            board_id=board_id,
            package_spec=client_dir,
            timeout=int(args.timeout),
            uvx_path=Path(stdio_argv[0]),
        )

        probe = StdioMcp(stdio_argv, stdio_env, args.timeout)
        try:
            tool_names, prompt_names, _status = _initialize_and_probe(
                probe, board_id, expected_tools
            )
        finally:
            probe.close()

        zed_bin = _isolated_zed_binary(_find_zed(), scratch)
        zed_home = (scratch / "home").resolve()
        zed_log_path = zed_home / "Library" / "Logs" / "Zed" / "Zed.log"
        zed_stdio = (scratch / "zed-stdio.log").open("wb")
        zed_env = os.environ.copy()
        zed_env.update(
            {
                "HOME": str(zed_home),
                "TMPDIR": str((scratch / "tmp").resolve()),
                "ZED_STATELESS": "1",
                "RUST_LOG": "zed=info,extension_host=debug,context_server=trace",
                "UV_CACHE_DIR": str(uv_cache),
            }
        )
        zed = subprocess.Popen(
            [str(zed_bin), "--user-data-dir", str(zed_data), str(scratch / "project")],
            stdout=zed_stdio,
            stderr=subprocess.STDOUT,
            env=zed_env,
            start_new_session=True,
        )
        matcher = LogMatcher(tuple(stdio_argv[1:]))
        _follow_zed_log(zed_log_path, matcher, zed, args.timeout)

        print("PASS: isolated Zed Pursers MCP E2E")
        print(f"tools_listed={len(tool_names)} prompts={','.join(prompt_names)}")
        print("tool_call_mode=direct-stdio-fallback tool=board_status result=ok")
        print("literal_log_lines:")
        for line in matcher.lines:
            print(line)
        if args.keep:
            print(f"scratch={scratch}")
        return 0
    except HarnessError as exc:
        print(f"FAIL: {redact(str(exc))}", file=sys.stderr)
        if args.keep:
            print(f"scratch={scratch}", file=sys.stderr)
        return 1
    finally:
        _terminate(zed)
        _terminate(central)
        if zed_stdio is not None:
            zed_stdio.close()
        if central_log is not None:
            central_log.close()
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
