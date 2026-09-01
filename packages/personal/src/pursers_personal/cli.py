"""Single user-facing console for On Board Personal Preview."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import (
    ArtifactVerificationError,
    import_verified_component,
    safe_component_summary,
)
from .integration import (
    ENTRY_NAME,
    LAUNCHCTL_PATH,
    MAX_CONFIG_BYTES,
    IntegrationError,
    _apply_integration_locked,
    _integration_lock,
    _rollback_integration_locked,
    integration_status,
    launchctl_commands,
    prepare_integration,
)

PGREP_PATH = "/usr/bin/pgrep"
_PROFILE_DIRECTORY_RE = re.compile(r"^project-[0-9a-f]{24}$")


class RotationActivationError(IntegrationError):
    """Credential rotation completed but new credentials were not activated."""

    def __init__(self, result: dict[str, Any]):
        super().__init__(
            "capability rotated, but Central did not restart and verify the new credential"
        )
        self.result = result


def _profile_api():
    from . import profile

    return profile


def _default_launch_agents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _default_claude_config() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def _default_claude_code_config() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _console_path(value: Path | None) -> Path:
    expected = Path(sys.executable).absolute().parent / "pursers-personal"
    candidate = value.expanduser().absolute() if value is not None else expected
    if candidate != expected:
        raise IntegrationError(
            "--console must name the pursers-personal entry point beside this Python runtime"
        )
    result = candidate
    if not result.is_file() or result.is_symlink():
        raise IntegrationError("pursers-personal console is unavailable or unsafe")
    mode = result.stat().st_mode
    if not mode & 0o100 or mode & 0o022:
        raise IntegrationError(
            "pursers-personal console must be owner-executable and not group/world writable"
        )
    return result


def _selected_profile(args: argparse.Namespace):
    api = _profile_api()
    profile, source = _profile_action(
        api,
        "select",
        api.select_personal_profile,
        explicit_profile=getattr(args, "profile", None),
        project_root=getattr(args, "project", None),
        profiles_root=getattr(args, "profiles_root", None),
    )
    return profile, source


def _profile_action(api: Any, action: str, function: Any, *args: Any, **kwargs: Any):
    """Translate verified profile failures into secret-safe CLI errors."""
    try:
        return function(*args, **kwargs)
    except api.ProfileSecurityError:
        raise IntegrationError(
            f"Personal profile {action} failed security verification"
        ) from None
    except api.PersonalProfileError:
        raise IntegrationError(
            f"Personal profile {action} failed because it is missing or invalid"
        ) from None


def _tcp_status(url: str) -> dict[str, Any]:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((str(parsed.hostname), port), timeout=0.5):
            pass
    except OSError as exc:
        return {
            "healthy": False,
            "message": f"Central is not accepting loopback connections ({type(exc).__name__})",
        }
    return {"healthy": True, "message": "Central loopback port is accepting connections"}


def _setup_port(api: Any, args: argparse.Namespace) -> int:
    """Reuse an existing profile port or choose an ephemeral loopback port once."""
    if args.port is not None:
        return int(args.port)
    root = args.profiles_root or api.default_profiles_root()
    project = args.project.expanduser().resolve(strict=True)
    profile_path = api.profile_path_for_project(project, root)
    if profile_path.exists() and not profile_path.is_symlink():
        profile = _profile_action(
            api, "load", api.load_personal_profile, profile_path
        )
        return int(profile.central_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    if not 1024 <= port <= 65535:
        raise IntegrationError("macOS did not select a safe loopback port")
    return port


def _resolved_target(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise IntegrationError("cannot resolve the requested host config path") from exc


def _targets_claude_desktop_config(host_config: Path) -> bool:
    return _resolved_target(host_config) == _resolved_target(_default_claude_config())


def _setup_coordinates(api: Any, args: argparse.Namespace) -> tuple[Path, Path, Path]:
    project = args.project.expanduser().resolve(strict=True)
    if not project.is_dir():
        raise IntegrationError("project root must be a directory")
    root = (args.profiles_root or api.default_profiles_root()).expanduser().absolute()
    if root.is_symlink():
        raise IntegrationError("profiles root must not be a symlink")
    profile_path = api.profile_path_for_project(project, root)
    return project, root, profile_path


def _host_entry_action(host_config: Path) -> str:
    target = host_config.expanduser().absolute()
    if not target.exists() and not target.is_symlink():
        return "create-config-and-add-entry"
    if target.is_symlink() or not target.is_file():
        raise IntegrationError("host config must be a regular non-symlink file")
    if target.stat().st_size > MAX_CONFIG_BYTES:
        raise IntegrationError("host config is too large")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("host config is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise IntegrationError("host config root must be an object")
    servers = document.get("mcpServers")
    if servers is None:
        return "add-mcpServers-and-entry"
    if not isinstance(servers, dict):
        raise IntegrationError("host config mcpServers must be an object")
    return "add-entry" if ENTRY_NAME not in servers else "conflict-existing-entry"


def _setup_plan(args: argparse.Namespace, api: Any) -> dict[str, Any]:
    if args.activate:
        raise IntegrationError("--activate requires --apply")
    project, root, profile_path = _setup_coordinates(api, args)
    if profile_path.exists() or profile_path.is_symlink():
        profile = _profile_action(api, "load", api.load_personal_profile, profile_path)
        identity = _profile_action(
            api,
            "identity resolution",
            api.doctor_identity_summary,
            host=args.host_id,
            session=args.session,
            explicit_profile=profile.profile_path,
        )
        current = integration_status(profile.profile_path)
        if current["state"] == "applied":
            requested_console = _console_path(args.console)
            expected = {
                "profile_path": str(profile.profile_path),
                "console_path": str(requested_console),
                "host_id": args.host_id,
                "session": args.session,
            }
            if any(current.get(key) != value for key, value in expected.items()):
                raise IntegrationError(
                    "requested profile, console, host, or session differs from installed integration"
                )
            return {
                "status": "existing",
                "product_version": __version__,
                "identity_statement": identity["identity_statement"],
                "profile": identity,
                "integration": current,
                "host_restart_required": False,
                "central_initialization_required": False,
            }
        if current["state"] == "preparing":
            raise IntegrationError(
                "interrupted integration detected; run rollback before setup"
            )
        if current["state"] not in {"not-installed", "rolled_back", "uninstalled"}:
            raise IntegrationError("integration receipt state is unsupported")
        plan = prepare_integration(
            profile,
            console_path=_console_path(args.console),
            launch_agents_dir=args.launch_agents_dir,
            host_config_path=args.host_config,
            host_id=args.host_id,
            session=args.session,
        )
        integration = {"status": "planned", **plan.safe_summary()}
        integration["profile_action"] = "reuse"
        integration["port_strategy"] = "reuse-existing"
        integration["port"] = profile.central_port
        return {
            "status": "planned",
            "product_version": __version__,
            "identity_statement": identity["identity_statement"],
            "profile": identity,
            "integration": integration,
            "host_support": "candidate-unverified-until-host-gate",
            "host_restart_required": False,
            "central_initialization_required": False,
        }

    if args.port is not None and not 1 <= int(args.port) <= 65_535:
        raise IntegrationError("port must be between 1 and 65535")
    console = _console_path(args.console)
    integration = {
        "status": "planned",
        "profile_action": "create-on-apply",
        "profile_path": str(profile_path),
        "project_root": str(project),
        "profiles_root": str(root),
        "port_strategy": "explicit" if args.port is not None else "ephemeral-on-apply",
        "port": int(args.port) if args.port is not None else None,
        "console_path": str(console),
        "launch_agents_dir": str(args.launch_agents_dir.expanduser().absolute()),
        "service_action": "create-on-apply",
        "host_target": str(args.host_config.expanduser().absolute()),
        "host_entry_action": _host_entry_action(args.host_config),
        "host_id": args.host_id,
        "session": args.session,
        "contains_credential": False,
    }
    return {
        "status": "planned",
        "product_version": __version__,
        "profile": {
            "action": "create-on-apply",
            "profile_path": str(profile_path),
            "project_root": str(project),
        },
        "integration": integration,
        "host_support": "candidate-unverified-until-host-gate",
        "host_restart_required": False,
        "central_initialization_required": False,
    }


def _remove_created_profile_directory(profile_path: Path, root: Path) -> bool:
    directory = profile_path.parent
    if not directory.exists() or directory.is_symlink():
        return False
    if directory.parent != root or not _PROFILE_DIRECTORY_RE.fullmatch(directory.name):
        return False
    try:
        state = integration_status(profile_path)["state"]
    except (IntegrationError, OSError, ValueError):
        return False
    if state not in {"not-installed", "rolled_back", "uninstalled"}:
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True


def _authenticated_status(profile: Any, *, host_id: str, session: str) -> dict[str, Any]:
    """Verify Central through pure authenticated reads; never join or mutate."""
    api = _profile_api()
    context = api.resolve_personal_context(profile, host=host_id, session=session)
    client_module = import_verified_component(
        "pursers-client",
        "pursers_client",
        "pursers_client.client",
        package_member="pursers_client/__init__.py",
        module_member="pursers_client/client.py",
    )

    async def probe() -> dict[str, Any]:
        async with asyncio.timeout(12):
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    client_module.httpx2.AsyncClient(
                        headers={
                            "Authorization": f"Bearer {context.capability_token}"
                        },
                        timeout=client_module.httpx2.Timeout(10.0),
                        trust_env=False,
                    )
                )
                transport = client_module.streamable_http_client(
                    context.central_url, http_client=http
                )
                client = await stack.enter_async_context(
                    client_module.Client(transport, mode="2026-07-28", cache=None)
                )
                status = client_module.BoardClient._decode(
                    await client.call_tool(
                        "board_status", {"board_id": context.board_id}
                    )
                )
                snapshot = client_module.BoardClient._decode(
                    await client.call_tool(
                        "board_snapshot", {"board_id": context.board_id}
                    )
                )
                agents = snapshot.get("agents", [])
                agent_present = any(
                    isinstance(item, dict)
                    and item.get("agent_name") == context.agent_name
                    for item in agents
                )
                matches = (
                    status.get("board_id") == context.board_id
                    and snapshot.get("board", {}).get("board_id")
                    == context.board_id
                    and status.get("review_policy") == api.PERSONAL_REVIEW_POLICY
                    and agent_present
                )
                return {
                    "healthy": matches,
                    "message": (
                        "Authenticated board, registered agent, and workflow policy verified with pure reads"
                        if matches
                        else "Authenticated Central board, registered agent, or workflow policy does not match the selected profile"
                    ),
                    "authenticated": True,
                    "mutating": False,
                    "review_policy": status.get("review_policy"),
                }

    try:
        return asyncio.run(probe())
    except Exception as exc:
        return {
            "healthy": False,
            "message": f"Authenticated Central probe failed ({type(exc).__name__})",
            "authenticated": False,
            "mutating": False,
            "review_policy": None,
        }


def _initialize_personal_board(
    profile: Any, *, host_id: str, session: str
) -> dict[str, Any]:
    """Perform the explicit setup mutation before the read-only App is opened."""
    api = _profile_api()
    context = _profile_action(
        api,
        "identity resolution",
        api.resolve_personal_context,
        profile,
        host=host_id,
        session=session,
    )
    client_module = import_verified_component(
        "pursers-client",
        "pursers_client",
        "pursers_client.client",
        package_member="pursers_client/__init__.py",
        module_member="pursers_client/client.py",
    )

    async def initialize() -> dict[str, Any]:
        last_error: Exception | None = None
        for delay in (0.0, 0.15, 0.3, 0.6, 1.2, 2.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(12):
                    async with client_module.BoardClient(
                        context.central_url,
                        context.capability_token,
                        context.board_id,
                        agent_name=context.agent_name,
                    ) as client:
                        policy = await api.bootstrap_personal_review_policy(client)
                        status = await client.board_status()
                        identity = client.identity
                        if (
                            identity is None
                            or identity.principal_id
                            != context.authenticated_principal_id
                            or identity.agent_name != context.agent_name
                            or status.get("board_id") != context.board_id
                            or status.get("review_policy")
                            != api.PERSONAL_REVIEW_POLICY
                        ):
                            raise IntegrationError(
                                "Personal board initialization returned a mismatched identity or policy"
                            )
                        return {
                            "status": "ready",
                            "board_id": context.board_id,
                            "principal_id": context.authenticated_principal_id,
                            "agent_name": context.agent_name,
                            "review_policy": status.get("review_policy"),
                            "policy_changed": bool(policy.get("changed")),
                            "mutating": True,
                        }
            except IntegrationError:
                raise
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise IntegrationError(
            f"Personal Central did not become ready ({type(last_error).__name__})"
        ) from None

    return asyncio.run(initialize())


def _run_launchctl(command: list[str]) -> None:
    if platform.system() != "Darwin":
        raise IntegrationError("launchctl activation is supported only on macOS")
    if not command or command[0] != LAUNCHCTL_PATH:
        raise IntegrationError("refusing an unapproved launchctl command")
    _system_binary(Path(LAUNCHCTL_PATH), "launchctl")
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as exc:
        raise IntegrationError("launchctl command failed for the owned Personal service") from exc


def _host_is_running(host_id: str) -> bool:
    if host_id != "claude-desktop":
        print(
            "pursers-personal: lifecycle check skipped for an unrecognized host id",
            file=sys.stderr,
        )
        return False
    if platform.system() != "Darwin":
        raise IntegrationError("Claude Desktop lifecycle checks require macOS")
    _system_binary(Path(PGREP_PATH), "pgrep")
    queries = [
        [PGREP_PATH, "-x", "-u", str(os.getuid()), "Claude"],
        [
            PGREP_PATH,
            "-f",
            "-u",
            str(os.getuid()),
            "/Applications/Claude.app/Contents/",
        ],
    ]
    for command in queries:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            raise IntegrationError("cannot verify whether Claude Desktop is closed")
    return False


def _system_binary(path: Path, label: str) -> None:
    try:
        value = path.lstat()
    except OSError as exc:
        raise IntegrationError(f"approved macOS {label} binary is unavailable") from exc
    if (
        path.is_symlink()
        or not path.is_file()
        or value.st_uid != 0
        or value.st_mode & 0o022
        or not value.st_mode & 0o111
    ):
        raise IntegrationError(f"approved macOS {label} binary is unsafe")


def _require_host_closed(host_id: str) -> None:
    if _host_is_running(host_id):
        raise IntegrationError(
            "quit Claude Desktop completely before changing its Personal integration"
        )


def _service_is_loaded(label: str) -> bool:
    if platform.system() != "Darwin":
        raise IntegrationError("launchctl activation is supported only on macOS")
    domain = f"gui/{os.getuid()}/{label}"
    _system_binary(Path(LAUNCHCTL_PATH), "launchctl")
    result = subprocess.run(
        [LAUNCHCTL_PATH, "print", domain],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 113:
        return False
    raise IntegrationError("cannot verify the owned Personal service state")


def _activate_service(label: str, service_target: Path) -> str:
    commands = launchctl_commands(label, service_target)
    loaded = _service_is_loaded(label)
    _run_launchctl(commands["restart"] if loaded else commands["start"])
    if not _service_is_loaded(label):
        raise IntegrationError("owned Personal service did not become loaded")
    return "restarted" if loaded else "started"


def _deactivate_service(label: str, service_target: Path) -> str:
    commands = launchctl_commands(label, service_target)
    if not _service_is_loaded(label):
        return "already-stopped"
    _run_launchctl(commands["stop"])
    if _service_is_loaded(label):
        raise IntegrationError("owned Personal service is still loaded")
    return "stopped"


def _emit(value: dict[str, Any], *, as_json: bool, identity_first: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if identity_first and value.get("identity_statement"):
        print(value["identity_statement"])
    print(json.dumps(value, indent=2, sort_keys=True))


def _setup_integration(
    args: argparse.Namespace, profile: Any, identity: dict[str, Any]
) -> dict[str, Any]:
    """Serialize receipt, service lifecycle, and board initialization."""
    with _integration_lock(profile.profile_path):
        current = integration_status(profile.profile_path)
        if current["state"] == "applied":
            requested_console = _console_path(args.console)
            expected = {
                "profile_path": str(profile.profile_path),
                "console_path": str(requested_console),
                "host_id": args.host_id,
                "session": args.session,
            }
            if any(current.get(key) != value for key, value in expected.items()):
                raise IntegrationError(
                    "requested profile, console, host, or session differs from installed integration"
                )
            result = {
                "status": "existing",
                "product_version": __version__,
                "identity_statement": identity["identity_statement"],
                "profile": identity,
                "integration": current,
            }
            if args.activate:
                service_target = next(
                    Path(item["path"])
                    for item in current["targets"]
                    if item["kind"] == "service"
                )
                result["activation"] = _activate_service(
                    str(current["label"]), service_target
                )
                result["initialization"] = _initialize_personal_board(
                    profile, host_id=args.host_id, session=args.session
                )
            result["host_restart_required"] = True
            result["central_initialization_required"] = not bool(args.activate)
            return result
        if current["state"] == "preparing":
            raise IntegrationError(
                "interrupted integration detected; run rollback before setup"
            )
        if current["state"] not in {"not-installed", "rolled_back", "uninstalled"}:
            raise IntegrationError("integration receipt state is unsupported")
        plan = prepare_integration(
            profile,
            console_path=_console_path(args.console),
            launch_agents_dir=args.launch_agents_dir,
            host_config_path=args.host_config,
            host_id=args.host_id,
            session=args.session,
        )
        if args.apply:
            integration = _apply_integration_locked(plan)
            if args.activate:
                integration["activation"] = _activate_service(
                    plan.label, plan.service_target
                )
                integration["initialization"] = _initialize_personal_board(
                    profile, host_id=args.host_id, session=args.session
                )
        else:
            if args.activate:
                raise IntegrationError("--activate requires --apply")
            integration = {"status": "planned", **plan.safe_summary()}
        return {
            "status": integration["status"],
            "product_version": __version__,
            "identity_statement": identity["identity_statement"],
            "profile": identity,
            "integration": integration,
            "host_support": "candidate-unverified-until-host-gate",
            "host_restart_required": bool(args.apply),
            "central_initialization_required": bool(args.apply and not args.activate),
        }


def command_setup(args: argparse.Namespace) -> dict[str, Any]:
    # Fail before profile/key/config creation if any shipped runtime byte drifts.
    safe_component_summary()
    if not args.apply:
        return _setup_plan(args, _profile_api())
    host_config = getattr(args, "host_config", _default_claude_config())
    if _targets_claude_desktop_config(host_config):
        _require_host_closed(args.host_id)
    api = _profile_api()
    _project, root, profile_path = _setup_coordinates(api, args)
    created_this_run = not profile_path.parent.exists()
    try:
        port = _setup_port(api, args)
        profile = _profile_action(
            api,
            "setup",
            api.ensure_personal_profile,
            args.project,
            profiles_root=args.profiles_root,
            port=port,
        )
        identity = _profile_action(
            api,
            "identity resolution",
            api.doctor_identity_summary,
            host=args.host_id,
            session=args.session,
            explicit_profile=profile.profile_path,
        )
        return _setup_integration(args, profile, identity)
    except (IntegrationError, OSError, ValueError) as exc:
        if not created_this_run or not profile_path.parent.exists():
            raise
        if _remove_created_profile_directory(profile_path, root):
            raise
        raise IntegrationError(
            "setup failed after creating a profile; profile retained at "
            f"{profile_path.parent}; inspect it with `profiles list` and remove an "
            "orphan with `profiles prune --orphaned --commit`"
        ) from exc


def _value_references_path(value: Any, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, list):
        return any(_value_references_path(item, target) for item in value)
    if isinstance(value, dict):
        return any(_value_references_path(item, target) for item in value.values())
    return False


def _host_config_reference(path: Path, profile_path: Path) -> tuple[bool, bool]:
    target = path.expanduser().absolute()
    if not target.exists() and not target.is_symlink():
        return False, False
    if target.is_symlink() or not target.is_file():
        return False, True
    try:
        if target.stat().st_size > MAX_CONFIG_BYTES:
            return False, True
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, True
    return _value_references_path(document, str(profile_path)), False


def _profile_scan_paths(args: argparse.Namespace) -> tuple[set[Path], set[Path]]:
    host_configs = {
        _default_claude_config().expanduser().absolute(),
        _default_claude_code_config().expanduser().absolute(),
    }
    host_configs.update(
        path.expanduser().absolute() for path in getattr(args, "host_config", [])
    )
    launch_dirs = {_default_launch_agents().expanduser().absolute()}
    launch_dirs.update(
        path.expanduser().absolute()
        for path in getattr(args, "launch_agents_dir", [])
    )
    return host_configs, launch_dirs


def _scan_profiles(args: argparse.Namespace) -> tuple[Path, list[dict[str, Any]]]:
    api = _profile_api()
    root = (args.profiles_root or api.default_profiles_root()).expanduser().absolute()
    if not root.exists() and not root.is_symlink():
        return root, []
    if root.is_symlink() or not root.is_dir():
        raise IntegrationError("profiles root must be a regular directory")
    base_host_configs, base_launch_dirs = _profile_scan_paths(args)
    results: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not _PROFILE_DIRECTORY_RE.fullmatch(directory.name):
            continue
        profile_path = directory / "profile.json"
        if directory.is_symlink() or not directory.is_dir():
            results.append(
                {
                    "profile_path": str(profile_path),
                    "status": "unsafe",
                    "orphaned": False,
                    "prunable": False,
                    "reason": "profile directory is not a regular directory",
                }
            )
            continue
        try:
            profile = _profile_action(api, "load", api.load_personal_profile, profile_path)
        except (IntegrationError, OSError, ValueError):
            results.append(
                {
                    "profile_path": str(profile_path),
                    "status": "invalid",
                    "orphaned": False,
                    "prunable": False,
                    "reason": "profile could not be verified",
                }
            )
            continue

        host_configs = set(base_host_configs)
        launch_targets = {
            launch_dir / f"com.onboard.personal.{profile.profile_id}.plist"
            for launch_dir in base_launch_dirs
        }
        uncertain = False
        try:
            integration = integration_status(profile_path)
        except (IntegrationError, OSError, ValueError):
            integration = {"targets": []}
            uncertain = True
        for target in integration.get("targets", []):
            if not isinstance(target, dict) or not isinstance(target.get("path"), str):
                uncertain = True
                continue
            target_path = Path(target["path"]).expanduser().absolute()
            if target.get("kind") == "host-config":
                host_configs.add(target_path)
            elif target.get("kind") == "service":
                launch_targets.add(target_path)

        host_references: list[str] = []
        for host_config in sorted(host_configs, key=str):
            referenced, unreadable = _host_config_reference(host_config, profile_path)
            uncertain = uncertain or unreadable
            if referenced:
                host_references.append(str(host_config))
        launch_references = sorted(
            str(path)
            for path in launch_targets
            if path.exists() or path.is_symlink()
        )
        orphaned = not host_references and not launch_references and not uncertain
        results.append(
            {
                "profile_path": str(profile_path),
                "project_root": str(profile.project_root),
                "profile_id": profile.profile_id,
                "board_id": profile.board_id,
                "status": "verified",
                "host_references": host_references,
                "launch_agent_references": launch_references,
                "reference_check_complete": not uncertain,
                "orphaned": orphaned,
                "prunable": orphaned,
                "reason": (
                    "no known host entry or LaunchAgent references this profile"
                    if orphaned
                    else "profile is referenced or reference verification was incomplete"
                ),
            }
        )
    return root, results


def command_profiles_list(args: argparse.Namespace) -> dict[str, Any]:
    root, profiles = _scan_profiles(args)
    return {
        "status": "ok",
        "profiles_root": str(root),
        "count": len(profiles),
        "profiles": profiles,
    }


def command_profiles_prune(args: argparse.Namespace) -> dict[str, Any]:
    root, profiles = _scan_profiles(args)
    candidates = [profile for profile in profiles if profile.get("prunable")]
    removed: list[str] = []
    if args.commit:
        for candidate in candidates:
            profile_path = Path(str(candidate["profile_path"]))
            fresh_root, fresh_profiles = _scan_profiles(args)
            fresh = next(
                (
                    item
                    for item in fresh_profiles
                    if item.get("profile_path") == str(profile_path)
                ),
                None,
            )
            directory = profile_path.parent
            if (
                fresh_root != root
                or fresh is None
                or not fresh.get("prunable")
                or directory.parent != root
                or directory.is_symlink()
                or not _PROFILE_DIRECTORY_RE.fullmatch(directory.name)
            ):
                continue
            shutil.rmtree(directory)
            removed.append(str(profile_path))
    return {
        "status": "pruned" if args.commit else "planned",
        "mode": "commit" if args.commit else "dry-run",
        "profiles_root": str(root),
        "candidate_count": len(candidates),
        "removed_count": len(removed),
        "candidates": [str(item["profile_path"]) for item in candidates],
        "removed": removed,
    }


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    api = _profile_api()
    profile, source = _selected_profile(args)
    integration = integration_status(profile.profile_path)
    installed_host = integration.get("host_id") if integration["state"] == "applied" else None
    installed_session = integration.get("session") if integration["state"] == "applied" else None
    if args.host_id is not None and installed_host is not None and args.host_id != installed_host:
        raise IntegrationError("requested host identity differs from installed integration")
    if args.session is not None and installed_session is not None and args.session != installed_session:
        raise IntegrationError("requested session identity differs from installed integration")
    host_id = str(installed_host or args.host_id or "claude-desktop")
    session = str(installed_session or args.session or "primary")
    identity = _profile_action(
        api,
        "identity resolution",
        api.doctor_identity_summary,
        host=host_id,
        session=session,
        explicit_profile=profile.profile_path,
    )
    identity["profile_source"] = source
    components = safe_component_summary()
    tcp = _tcp_status(profile.central_url)
    central = (
        _authenticated_status(profile, host_id=host_id, session=session)
        if tcp["healthy"]
        else {
            "healthy": False,
            "message": "Authenticated Central probe skipped because the loopback port is unavailable",
            "authenticated": False,
            "review_policy": None,
        }
    )
    checks = [
        {"name": "profile", "healthy": True, "message": "Private Personal profile verified"},
        {"name": "components", "healthy": True, "message": "Installed component bytes verified"},
        {
            "name": "integration",
            "healthy": integration["state"] == "applied",
            "message": (
                "Owned service and host config match their receipt"
                if integration["state"] == "applied"
                else f"Integration state is {integration['state']}"
            ),
        },
        {"name": "central-tcp", **tcp},
        {"name": "central-authenticated", **central},
        {
            "name": "host-proof",
            "healthy": False,
            "message": "Exact supported-host runtime proof is pending",
        },
    ]
    return {
        "healthy": all(item["healthy"] for item in checks[:-1]),
        "product_version": __version__,
        "identity_statement": identity["identity_statement"],
        "effective_identity": identity,
        "components": components,
        "integration": integration,
        "checks": checks,
        "host_support": "UNVERIFIED",
        "host_restart_required": integration["state"] == "applied",
    }


def command_central(args: argparse.Namespace) -> None:
    profile, _source = _selected_profile(args)
    from .artifacts import import_verified_component

    os.environ["STORE_BACKEND"] = "sqlite"
    os.environ["CENTRAL_ADMISSION"] = "invite"
    os.environ["CENTRAL_AUTH_MODE"] = "jwt"
    os.environ["CENTRAL_JWT_ISSUER"] = profile.issuer
    os.environ["CENTRAL_JWT_AUDIENCE"] = profile.audience
    os.environ["CENTRAL_JWKS_PATH"] = str(profile.jwks_path)
    os.environ["CENTRAL_JWT_CLOCK_SKEW"] = "30"
    central = import_verified_component(
        "pursers-central",
        "pursers_central",
        "pursers_central.central",
        package_member="pursers_central/__init__.py",
        module_member="pursers_central/central.py",
    )

    mcp, _service = central.build_server(
        "127.0.0.1", profile.central_port, profile.central_data_dir
    )
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=profile.central_port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )


def command_mcp(args: argparse.Namespace) -> None:
    from .apps_server import run_personal_mcp

    run_personal_mcp(args.profile, args.host_id, args.session)


def command_rotate(args: argparse.Namespace) -> dict[str, Any]:
    api = _profile_api()
    _require_host_closed("claude-desktop")
    rotation_path = args.profile.expanduser().absolute()
    if rotation_path.is_dir():
        rotation_path = rotation_path / "profile.json"
    with _integration_lock(rotation_path):
        status: dict[str, Any] | None = None
        service_target: Path | None = None
        if args.activate:
            status = integration_status(rotation_path)
            if status["state"] != "applied":
                raise IntegrationError("cannot restart: integration is not applied")
            service_target = next(
                Path(item["path"])
                for item in status["targets"]
                if item["kind"] == "service"
            )
        profile = _profile_action(
            api, "rotation", api.rotate_personal_capability, args.profile
        )
        result = {
            "status": "rotated",
            "profile_path": str(profile.profile_path),
            "principal_id": profile.principal_id,
            "kid": profile.kid,
            "central_restart_required": True,
            "host_restart_required": True,
        }
        if args.activate:
            assert status is not None and service_target is not None
            try:
                result["activation"] = _activate_service(
                    str(status["label"]), service_target
                )
                result["initialization"] = _initialize_personal_board(
                    profile,
                    host_id=str(status.get("host_id") or "claude-desktop"),
                    session=str(status.get("session") or "primary"),
                )
            except (IntegrationError, OSError) as exc:
                result["status"] = "rotated-central-restart-failed"
                result["activation"] = "failed"
                result["activation_error"] = type(exc).__name__
                raise RotationActivationError(result) from None
            else:
                result["central_restart_required"] = False
        return result


def command_restart(args: argparse.Namespace) -> dict[str, Any]:
    profile, _source = _selected_profile(args)
    with _integration_lock(profile.profile_path):
        status = integration_status(profile.profile_path)
        if status["state"] != "applied":
            raise IntegrationError("cannot restart: integration is not applied")
        service_target = next(
            Path(item["path"])
            for item in status["targets"]
            if item["kind"] == "service"
        )
        command = launchctl_commands(str(status["label"]), service_target)["restart"]
        if args.activate:
            activation = _activate_service(str(status["label"]), service_target)
            initialization = _initialize_personal_board(
                profile,
                host_id=str(status.get("host_id") or "claude-desktop"),
                session=str(status.get("session") or "primary"),
            )
        else:
            activation = "planned"
            initialization = None
        return {
            "status": activation,
            "command": command,
            "initialization": initialization,
            "host_restart_required": True,
        }


def _maintenance_profile_path(args: argparse.Namespace) -> Path:
    """Resolve owned integration state without requiring a valid bearer."""
    if args.profile is not None:
        candidate = args.profile.expanduser().absolute()
        return candidate / "profile.json" if candidate.is_dir() else candidate
    if args.project is not None:
        api = _profile_api()
        root = args.profiles_root or api.default_profiles_root()
        project = args.project.expanduser().resolve(strict=True)
        return api.profile_path_for_project(project, root)
    profile, _source = _selected_profile(args)
    return profile.profile_path


def command_remove(args: argparse.Namespace, *, uninstall: bool) -> dict[str, Any]:
    profile_path = _maintenance_profile_path(args)
    with _integration_lock(profile_path):
        status = integration_status(profile_path)
        if status["state"] == "not-installed":
            return {
                "status": "existing",
                "state": "not-installed",
                "service_stop": "already-stopped",
                "host_restart_required": False,
                "profile_retained": profile_path.exists(),
            }
        if status["state"] in {"applied", "preparing", "rolling_back"}:
            _require_host_closed(str(status.get("host_id") or "claude-desktop"))
        service_stop = "already-stopped"
        if status.get("label"):
            service_target = next(
                Path(item["path"])
                for item in status["targets"]
                if item["kind"] == "service"
            )
            service_stop = _deactivate_service(
                str(status["label"]), service_target
            )
        result = _rollback_integration_locked(
            profile_path,
            terminal_state="uninstalled" if uninstall else "rolled_back",
        )
        result["service_stop"] = service_stop
        result["host_restart_required"] = True
        return result


def _add_profile_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--profiles-root", type=Path)


def _add_profile_inventory_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profiles-root", type=Path)
    parser.add_argument(
        "--host-config",
        type=Path,
        action="append",
        default=[],
        help="additional host config to inspect for profile references",
    )
    parser.add_argument(
        "--launch-agents-dir",
        type=Path,
        action="append",
        default=[],
        help="additional LaunchAgents directory to inspect",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="create a profile and plan/apply local integration")
    setup.add_argument("--project", type=Path, required=True)
    setup.add_argument("--profiles-root", type=Path)
    setup.add_argument(
        "--port",
        type=int,
        help="explicit loopback port; omitted chooses and persists an available port",
    )
    setup.add_argument("--host-id", default="claude-desktop")
    setup.add_argument("--session", default="primary")
    setup.add_argument("--console", type=Path)
    setup.add_argument("--launch-agents-dir", type=Path, default=_default_launch_agents())
    setup.add_argument("--host-config", type=Path, default=_default_claude_config())
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--activate", action="store_true")

    profiles = subparsers.add_parser(
        "profiles", help="list profiles and safely prune verified orphans"
    )
    profile_commands = profiles.add_subparsers(
        dest="profiles_command", required=True
    )
    profiles_list = profile_commands.add_parser("list", help="list profile references")
    _add_profile_inventory_options(profiles_list)
    profiles_prune = profile_commands.add_parser(
        "prune", help="remove only verified orphan profiles"
    )
    _add_profile_inventory_options(profiles_prune)
    profiles_prune.add_argument("--orphaned", action="store_true", required=True)
    prune_mode = profiles_prune.add_mutually_exclusive_group(required=True)
    prune_mode.add_argument("--dry-run", action="store_true")
    prune_mode.add_argument("--commit", action="store_true")

    doctor = subparsers.add_parser("doctor", help="show effective identity and actionable checks")
    _add_profile_selector(doctor)
    doctor.add_argument("--host-id")
    doctor.add_argument("--session")

    central = subparsers.add_parser("central", help="run the strict loopback Central service")
    _add_profile_selector(central)

    mcp = subparsers.add_parser("mcp", help="run the profile-backed Apps/chat stdio facade")
    mcp.add_argument("--profile", type=Path, required=True)
    mcp.add_argument("--host-id", required=True)
    mcp.add_argument("--session", required=True)

    rotate = subparsers.add_parser("rotate", help="rotate the local signing key and capability")
    rotate.add_argument("--profile", type=Path, required=True)
    rotate.add_argument("--activate", action="store_true", help="restart the owned service")

    restart = subparsers.add_parser("restart", help="restart only the owned Personal service")
    _add_profile_selector(restart)
    restart.add_argument("--activate", action="store_true")

    rollback = subparsers.add_parser("rollback", help="restore exact pre-setup integration bytes")
    _add_profile_selector(rollback)
    rollback.add_argument(
        "--activate",
        action="store_true",
        help="deprecated compatibility flag; the owned service is always stopped safely",
    )

    uninstall = subparsers.add_parser("uninstall", help="remove integration but retain profile/data")
    _add_profile_selector(uninstall)
    uninstall.add_argument(
        "--activate",
        action="store_true",
        help="deprecated compatibility flag; the owned service is always stopped safely",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            result = command_setup(args)
            _emit(result, as_json=args.json, identity_first=True)
        elif args.command == "profiles":
            if args.profiles_command == "list":
                result = command_profiles_list(args)
            else:
                result = command_profiles_prune(args)
            _emit(result, as_json=args.json)
        elif args.command == "doctor":
            result = command_doctor(args)
            _emit(result, as_json=args.json, identity_first=True)
        elif args.command == "central":
            command_central(args)
        elif args.command == "mcp":
            command_mcp(args)
        elif args.command == "rotate":
            _emit(command_rotate(args), as_json=args.json)
        elif args.command == "restart":
            _emit(command_restart(args), as_json=args.json)
        elif args.command == "rollback":
            _emit(command_remove(args, uninstall=False), as_json=args.json)
        else:
            _emit(command_remove(args, uninstall=True), as_json=args.json)
    except RotationActivationError as exc:
        _emit(exc.result, as_json=args.json)
        parser.exit(2, f"pursers-personal: {exc}\n")
    except (ArtifactVerificationError, IntegrationError, OSError, ValueError) as exc:
        parser.exit(2, f"pursers-personal: {exc}\n")


if __name__ == "__main__":
    main()
