#!/usr/bin/env python3
"""Create and validate a secret-free Pursers Central instance scaffold."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import stat
import sys
from pathlib import Path
from typing import Sequence


SCHEMA_VERSION = 1
MARKER = ".pursers-central-instance.json"
PROFILE = "profile.env"
REGISTRY = "project-registry.json"
LAUNCHER = "launch-central.sh"
DIRECTORIES = ("data", "jwt", "logs")
KEY_FILES = ("issuer_key.pem", "jwks.json")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
PLACEHOLDER = "FILL-ME"
WHEEL_COMPONENTS = (
    "PURSERS_CENTRAL",
    "PURSERS_CLIENT",
)


class ScaffoldError(RuntimeError):
    """A safe, caller-visible scaffold validation error."""


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ScaffoldError(f"{label} must match {IDENTIFIER_RE.pattern}")
    return value


def _port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ScaffoldError("port must be between 1 and 65535")
    return value


def _absolute_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ScaffoldError("root must be an absolute path")
    root = Path(os.path.abspath(os.fspath(raw)))
    for candidate in (root, *root.parents):
        if os.path.lexists(candidate) and candidate.is_symlink():
            raise ScaffoldError("root and its existing parents must not be symlinks")
    return root


def _instance_ancestor(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / MARKER).is_file():
            return candidate
    return None


def _write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    os.chmod(path, mode)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _profile(root: Path, name: str, port: int) -> bytes:
    def env(key: str, value: object) -> str:
        return f"{key}={shlex.quote(str(value))}"

    lines = [
        "# Secret-free template. Replace every FILL-ME before launch.",
        env("CENTRAL_NAME", name),
        env("CENTRAL_HOST", "127.0.0.1"),
        env("CENTRAL_PORT", port),
        env("CENTRAL_DATA_DIR", root / "data"),
        "CENTRAL_AUTH_MODE=jwt",
        "CENTRAL_ADMISSION=invite",
        "STORE_BACKEND=sqlite",
        "CENTRAL_JWT_ISSUER=FILL-ME",
        env("CENTRAL_JWT_AUDIENCE", f"http://127.0.0.1:{port}/mcp"),
        env("CENTRAL_JWKS_PATH", root / "jwt" / "jwks.json"),
        "CENTRAL_JWT_CLOCK_SKEW=30",
    ]
    for component in WHEEL_COMPONENTS:
        lines.extend(
            [
                f"{component}_WHEEL=FILL-ME",
                f"{component}_WHEEL_SHA256=FILL-ME",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _jwt_readme() -> bytes:
    return (
        "# Operator-owned JWT material\n\n"
        "This directory intentionally contains no key or token material after init.\n"
        "The operator must create an RSA issuer key and a public JWKS here. Keep the\n"
        "private key mode 0600, keep tokens outside this scaffold, and never commit\n"
        "either. The established `jwt_provision.py` is a no-argument template: copy\n"
        "it into an operator-private directory, edit its ISSUER, AUDIENCE, ROOT, and\n"
        "SEATS constants, then run that private copy once. That one run creates the\n"
        "output filenames `issuer_key.pem` and `jwks.json`, plus individual `.jwt`\n"
        "files under `tokens/`, without printing token values. No instance path or\n"
        "file is read or copied by this scaffold.\n"
    ).encode("utf-8")


def _launcher(root: Path) -> bytes:
    profile = shlex.quote(str(root / PROFILE))
    python = shlex.quote(str(root / ".venv" / "bin" / "python"))
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "set -a\n"
        f". {profile}\n"
        "set +a\n"
        f"exec {python} -m pursers_central.pursers_central_runtime "
        '--host "$CENTRAL_HOST" --port "$CENTRAL_PORT" '
        '--data-dir "$CENTRAL_DATA_DIR"\n'
    ).encode("utf-8")


def _plist(root: Path, label: str) -> bytes:
    document = {
        "Label": label,
        "ProgramArguments": [str(root / LAUNCHER)],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(root / "logs" / "central.out.log"),
        "StandardErrorPath": str(root / "logs" / "central.err.log"),
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def runtime_install_command(root: str = "<INSTANCE_ROOT>") -> str:
    profile = f"{root}/{PROFILE}"
    python = f"{root}/.venv/bin/python"
    central_digest = '$(shasum -a 256 "$PURSERS_CENTRAL_WHEEL" | cut -d\' \' -f1)'
    client_digest = '$(shasum -a 256 "$PURSERS_CLIENT_WHEEL" | cut -d\' \' -f1)'
    return (
        f"set -a; . {shlex.quote(profile)}; set +a; "
        f'test "{central_digest}" = "$PURSERS_CENTRAL_WHEEL_SHA256" && '
        f'test "{client_digest}" = "$PURSERS_CLIENT_WHEEL_SHA256" && '
        f"python3 -m venv {shlex.quote(f'{root}/.venv')} && "
        f"{shlex.quote(python)} -m pip install "
        '"$PURSERS_CENTRAL_WHEEL" "$PURSERS_CLIENT_WHEEL"'
    )


def runbook(label: str) -> list[str]:
    generic_root = "<INSTANCE_ROOT>"
    plist = f"{generic_root}/{label}.plist"
    return [
        "Prepare the no-argument JWT template before generating JWKS/keys: cp "
        "<TOOLS_DIR>/jwt_provision.py <OPERATOR_PRIVATE_DIR>/jwt_provision.py && "
        "<EDITOR> <OPERATOR_PRIVATE_DIR>/jwt_provision.py # set ISSUER, AUDIENCE, "
        "ROOT, and SEATS",
        "Generate JWKS/keys and Mint principal tokens in the supported single "
        "no-argument run: python <OPERATOR_PRIVATE_DIR>/jwt_provision.py",
        f"Fill profile.env: <EDITOR> {generic_root}/{PROFILE}",
        "Install the dependency-compatible Central/client wheels into a fresh "
        f"venv: {runtime_install_command()}",
        "Load the launchd plist: cp "
        f"{plist} ~/Library/LaunchAgents/{label}.plist && launchctl bootstrap "
        f"gui/$(id -u) ~/Library/LaunchAgents/{label}.plist",
        "Provision seats with seat_admin: ONBOARD_CENTRAL_URL=<CENTRAL_URL> "
        'ONBOARD_CENTRAL_TOKEN="$(cat <ADMIN_TOKEN_FILE>)" '
        f"{generic_root}/.venv/bin/python "
        "<TOOLS_DIR>/wait-bridge/seat_admin.py add --name <SEAT_NAME> "
        "--principal <PRINCIPAL_ID> --role <ROLE> --boards registry "
        "--token-path <SEAT_TOKEN_FILE>",
        f"Import a board with board_move: {generic_root}/.venv/bin/python "
        "<TOOLS_DIR>/board_move/board_move.py import "
        f"--data-dir {generic_root}/data --archive <BOARD_ARCHIVE> "
        "--principal-map <OLD_PRINCIPAL>=<NEW_PRINCIPAL> --require-full-map --commit",
    ]


def _print_runbook(label: str) -> None:
    print("RUNBOOK (operator actions; placeholders are literal)")
    for number, command in enumerate(runbook(label), 1):
        print(f"{number}. {command}")


def init_instance(root_value: str | Path, name: str, port_value: int) -> Path:
    root = _absolute_root(root_value)
    label = _identifier(name, "name")
    port = _port(port_value)
    ancestor = _instance_ancestor(root.parent)
    if ancestor is not None:
        raise ScaffoldError(
            "refusing to initialize inside an existing instance root"
        )
    if os.path.lexists(root):
        raise ScaffoldError("refusing to initialize an existing root")
    if not _real_directory(root.parent):
        raise ScaffoldError("root parent must be an existing real directory")

    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
        os.chmod(root, 0o700)
        for name_part in DIRECTORIES:
            directory = root / name_part
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        _write(
            root / MARKER,
            _json_bytes(
                {"schema_version": SCHEMA_VERSION, "name": label, "port": port}
            ),
        )
        _write(
            root / REGISTRY,
            _json_bytes({"schema_version": SCHEMA_VERSION, "projects": {}}),
        )
        _write(root / PROFILE, _profile(root, label, port))
        _write(root / "jwt" / "README.md", _jwt_readme())
        _write(root / LAUNCHER, _launcher(root), mode=0o700)
        _write(root / f"{label}.plist", _plist(root, label))
    except BaseException:
        if created:
            shutil.rmtree(root)
        raise
    return root


def _load_json(path: Path, label: str) -> object:
    if not _regular_file(path):
        raise ScaffoldError(f"{label} is missing or not a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScaffoldError(f"{label} is not valid UTF-8 JSON") from exc


def _load_profile(path: Path) -> dict[str, str]:
    if not _regular_file(path):
        raise ScaffoldError("profile.env is missing or not a regular file")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ScaffoldError(f"profile.env line {number} is malformed")
        key, raw_value = line.split("=", 1)
        if not key or key in values:
            raise ScaffoldError(f"profile.env line {number} has an invalid key")
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise ScaffoldError(
                f"profile.env line {number} has invalid shell quoting"
            ) from exc
        if len(parsed) != 1:
            raise ScaffoldError(
                f"profile.env line {number} must contain one literal value"
            )
        values[key] = parsed[0]
    return values


def _listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((host, port)) == 0


def check_instance(root_value: str | Path) -> int:
    root = _absolute_root(root_value)
    if not _real_directory(root):
        raise ScaffoldError("root is missing or not a real directory")
    problems: list[str] = []
    if _mode(root) != 0o700:
        problems.append(f"root mode is {_mode(root):04o}; expected 0700")
    for name in DIRECTORIES:
        directory = root / name
        if not _real_directory(directory):
            problems.append(f"{name}/ is missing or not a real directory")
        elif _mode(directory) != 0o700:
            problems.append(f"{name}/ mode is {_mode(directory):04o}; expected 0700")

    for relative in (MARKER, REGISTRY, PROFILE, "jwt/README.md"):
        path = root / relative
        if not _regular_file(path):
            problems.append(f"{relative} is missing or not a regular file")
        elif _mode(path) != 0o600:
            problems.append(f"{relative} mode is {_mode(path):04o}; expected 0600")

    marker = _load_json(root / MARKER, "instance marker")
    if not isinstance(marker, dict) or set(marker) != {
        "schema_version",
        "name",
        "port",
    }:
        problems.append("instance marker shape is invalid")
        marker = {}
    registry = _load_json(root / REGISTRY, "project registry")
    if registry != {"schema_version": SCHEMA_VERSION, "projects": {}}:
        problems.append("project registry is not the seeded empty registry")

    profile = _load_profile(root / PROFILE)
    placeholders = sorted(
        key for key, value in profile.items() if PLACEHOLDER in value
    )
    try:
        profile_port = int(profile.get("CENTRAL_PORT", ""))
        _port(profile_port)
    except (ValueError, ScaffoldError):
        problems.append("profile CENTRAL_PORT is invalid")
        profile_port = 0
    marker_port = marker.get("port") if isinstance(marker, dict) else None
    if profile_port and marker_port != profile_port:
        problems.append("profile CENTRAL_PORT does not match the instance marker")
    host = profile.get("CENTRAL_HOST")
    if host != "127.0.0.1":
        problems.append("profile CENTRAL_HOST must remain 127.0.0.1")

    label = marker.get("name") if isinstance(marker, dict) else None
    plist_path = root / f"{label}.plist" if isinstance(label, str) else None
    if plist_path is None or not _regular_file(plist_path):
        problems.append("launchd plist is missing")
    else:
        try:
            plist = plistlib.loads(plist_path.read_bytes())
        except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
            raise ScaffoldError("launchd plist is invalid") from exc
        if plist.get("Label") != label or plist.get("KeepAlive") is not True:
            problems.append("launchd plist label or KeepAlive is invalid")
        if _mode(plist_path) != 0o600:
            problems.append(
                f"launchd plist mode is {_mode(plist_path):04o}; expected 0600"
            )
    if not _regular_file(root / LAUNCHER) or _mode(root / LAUNCHER) != 0o700:
        problems.append("launch-central.sh is missing or is not mode 0700")
    if not _regular_file(root / "jwt" / "README.md"):
        problems.append("jwt/README.md is missing")

    missing_operator_files = [
        f"jwt/{name}" for name in KEY_FILES if not _regular_file(root / "jwt" / name)
    ]
    private_key = root / "jwt" / "issuer_key.pem"
    if _regular_file(private_key) and _mode(private_key) != 0o600:
        problems.append(
            f"jwt/issuer_key.pem mode is {_mode(private_key):04o}; expected 0600"
        )
    listening = bool(profile_port and _listening("127.0.0.1", profile_port))

    print(f"root={root}")
    print("layout=" + ("ok" if not problems else "invalid"))
    for problem in problems:
        print(f"problem={problem}")
    print("placeholders=" + (",".join(placeholders) if placeholders else "none"))
    print(
        "operator_files_missing="
        + (",".join(missing_operator_files) if missing_operator_files else "none")
    )
    print(
        f"port={'listening-refused' if listening else 'available'}:"
        f"{profile_port or 'unknown'}"
    )
    if listening:
        print("check=failed: configured port already has a listener")
        return 2
    if problems:
        print("check=failed: layout validation")
        return 2
    if placeholders or missing_operator_files:
        print("check=incomplete: operator steps remain")
        return 1
    print("check=ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or validate a secret-free Pursers Central instance scaffold"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a new instance scaffold")
    init.add_argument("--root", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--port", required=True, type=int)
    check = commands.add_parser("check", help="validate an existing scaffold")
    check.add_argument("--root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            root = init_instance(args.root, args.name, args.port)
            print(f"created={root}")
            _print_runbook(args.name)
            return 0
        return check_instance(args.root)
    except ScaffoldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
