#!/usr/bin/env python3
"""Prepare a private, reproducible Pursers Home acceptance handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import zipfile


FULL_SHA = __import__("re").compile(r"[0-9a-f]{40}")
SAFE_BOARD = __import__("re").compile(r"(?:sandbox|test)-[a-z0-9][a-z0-9._-]*")
OBSERVATIONS = (
    "fresh_install", "door_connect", "team_setup", "six_workers_two_reviewers",
    "ticket_offer_claim", "ticket_submit_independent_review", "result_visible",
    "pause_resume_stop", "clean_reconnect_after_rotation",
)

AIONCORE_LAUNCHER = """#!/usr/bin/env python3
import os
import stat
import sys


def fail(message: str) -> None:
    print(f"AIONPRO_BOOTSTRAP_SECRET_INVALID: {message}", file=sys.stderr)
    raise SystemExit(2)


if len(sys.argv) < 4:
    fail("launcher arguments are incomplete")
identity_mode, secret_path = sys.argv[1:3]
command = sys.argv[3:]
environment = os.environ.copy()
environment.pop("AIONCORE_BOOTSTRAP_SECRET", None)
if identity_mode == "aionpro":
    if secret_path == "-":
        fail("an explicit sandbox secret file is required")
    try:
        supplied = os.lstat(secret_path)
        if stat.S_ISLNK(supplied.st_mode):
            fail("secret file must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(secret_path, flags)
    except OSError as error:
        fail(f"cannot open secret file: {error.strerror or error.__class__.__name__}")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            fail("secret file must be regular")
        if info.st_uid != os.geteuid():
            fail("secret file must be owned by the current operator")
        if stat.S_IMODE(info.st_mode) != 0o600:
            fail("secret file mode must be exactly 0600")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > 4096:
        fail("secret must contain 1 to 4096 bytes")
    value = raw[:-1] if raw.endswith(b"\\n") else raw
    if not value or b"\\n" in value or b"\\r" in value or b"\\0" in value:
        fail("secret must be one non-empty line")
    try:
        environment["AIONCORE_BOOTSTRAP_SECRET"] = value.decode("utf-8")
    except UnicodeDecodeError:
        fail("secret must be UTF-8")
elif identity_mode == "webui":
    if secret_path != "-":
        fail("webui mode must not receive an AionPro secret file")
else:
    fail("identity mode must be webui or aionpro")
os.execve(command[0], command, environment)
"""


class HandoffError(ValueError):
    pass


def _absolute(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise HandoffError(f"{label} must be an absolute path")
    return candidate.resolve()


def _supplied(path: str, label: str) -> Path:
    """Return an absolute supplied path without ever dereferencing it first.

    ``_absolute`` resolves, which erases the evidence that the operator handed
    over a symlink: a later ``lstat`` observes the resolved regular target and
    the documented no-symlink boundary silently passes. Protected file and
    executable inputs must therefore come through here, so the symlink check in
    ``_regular`` inspects the path as supplied. Containment checks still run on
    the resolved path, which ``_regular`` returns after the check.
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        raise HandoffError(f"{label} must be an absolute path")
    candidate = Path(os.path.normpath(candidate))
    if candidate.is_symlink():
        raise HandoffError(f"{label} must be a regular file, not a symlink: {candidate}")
    return candidate


def _regular(path: Path, label: str, *, executable: bool = False) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise HandoffError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HandoffError(f"{label} must be a regular file, not a symlink: {path}")
    if executable and not os.access(path, os.X_OK):
        raise HandoffError(f"{label} must be executable: {path}")
    return path.resolve()


def _private_secret(path: str) -> Path:
    supplied = _supplied(path, "AionPro bootstrap secret file")
    resolved = _regular(supplied, "AionPro bootstrap secret file")
    info = supplied.lstat()
    if info.st_uid != os.geteuid():
        raise HandoffError("AionPro bootstrap secret file must be owned by the current operator")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise HandoffError("AionPro bootstrap secret file mode must be exactly 0600")
    with supplied.open("rb") as stream:
        raw = stream.read(4097)
    if not raw or len(raw) > 4096:
        raise HandoffError("AionPro bootstrap secret must contain 1 to 4096 bytes")
    value = raw[:-1] if raw.endswith(b"\n") else raw
    if not value or b"\n" in value or b"\r" in value or b"\0" in value:
        raise HandoffError("AionPro bootstrap secret must be one non-empty line")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HandoffError("AionPro bootstrap secret must be UTF-8") from error
    return resolved


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _head(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    value = result.stdout.strip()
    if result.returncode or not FULL_SHA.fullmatch(value):
        raise HandoffError("candidate checkout must resolve to an exact git HEAD")
    return value


def _validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise HandoffError("origin has an invalid port") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise HandoffError("origin must be an absolute loopback HTTP origin with an explicit port")
    return value.rstrip("/")


def _candidate_zip_commit(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if names.count("webui/candidate.json") != 1:
                raise HandoffError("candidate ZIP must contain exactly one root webui/candidate.json")
            payload = json.loads(archive.read("webui/candidate.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise HandoffError(f"candidate ZIP is invalid: {error}") from error
    value = payload.get("candidate_commit") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not FULL_SHA.fullmatch(value):
        raise HandoffError("candidate ZIP has no exact candidate_commit")
    return value


def _install_zip(package: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(package) as archive:
        for member in archive.infolist():
            relative = Path(member.filename)
            mode = member.external_attr >> 16
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or stat.S_ISLNK(mode)
                or member.is_dir()
            ):
                raise HandoffError(f"candidate ZIP has unsafe member: {member.filename}")
            target = destination / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write(target, archive.read(member), 0o600)


def _verify_host_bundle(bundle: Path, core: Path, codesign: Path, cdhash: str, version: str) -> None:
    if not bundle.is_dir() or bundle.suffix != ".app":
        raise HandoffError("host bundle must be an existing .app directory")
    bundled_core = bundle / "Contents" / "Resources" / "bundled-aioncore"
    if not _inside(core, bundled_core):
        raise HandoffError("AionCore binary must be inside the signed host bundle")
    info_path = bundle / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as error:
        raise HandoffError("host bundle has no valid Info.plist") from error
    if info.get("CFBundleIdentifier") != "com.aionui.app" or info.get("CFBundleShortVersionString") != version:
        raise HandoffError("host bundle identity/version does not match requested AionUi host")
    verify = subprocess.run(
        [str(codesign), "--verify", "--deep", "--strict", str(bundle)],
        capture_output=True, check=False, text=True, timeout=20,
    )
    details = subprocess.run(
        [str(codesign), "-dvvv", str(bundle)],
        capture_output=True, check=False, text=True, timeout=20,
    )
    evidence = details.stdout + "\n" + details.stderr
    required = (
        "Identifier=com.aionui.app",
        "TeamIdentifier=52JQX2HUSC",
        "Authority=Developer ID Application: AionUi Inc. (52JQX2HUSC)",
        "Notarization Ticket=stapled",
        f"CDHash={cdhash}",
    )
    if verify.returncode or details.returncode or any(value not in evidence for value in required):
        raise HandoffError("host bundle signature/provenance verification failed")


def _quote(parts: list[str]) -> str:
    return " \\\n  ".join(shlex.quote(part) for part in parts)


def _write(path: Path, text: str | bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    kwargs = {} if isinstance(text, bytes) else {"encoding": "utf-8"}
    with os.fdopen(fd, "wb" if isinstance(text, bytes) else "w", **kwargs) as stream:
        stream.write(text)
    os.chmod(path, mode)


def prepare(args: argparse.Namespace) -> dict[str, object]:
    if not FULL_SHA.fullmatch(args.commit):
        raise HandoffError("commit must be a full lowercase 40-character SHA")
    if not FULL_SHA.fullmatch(args.runtime_commit):
        raise HandoffError("runtime commit must be a full lowercase 40-character SHA")
    if not SAFE_BOARD.fullmatch(args.board):
        raise HandoffError("board must start with sandbox- or test-")
    if args.identity_mode not in {"webui", "aionpro"}:
        raise HandoffError("identity mode must be webui or aionpro")
    secret_argument = getattr(args, "aionpro_bootstrap_secret_file", None)
    if args.identity_mode == "aionpro":
        if not secret_argument:
            raise HandoffError("aionpro identity mode requires --aionpro-bootstrap-secret-file")
        aionpro_secret = _private_secret(secret_argument)
    else:
        if secret_argument:
            raise HandoffError("--aionpro-bootstrap-secret-file is valid only with aionpro identity mode")
        aionpro_secret = None
    origin = _validate_origin(args.origin)
    if not args.page_path.startswith("/api/extensions/") or ".." in Path(args.page_path).parts:
        raise HandoffError("page path must be an absolute AionUi extension asset path")
    page = origin + args.page_path

    checkout = _absolute(args.candidate_checkout, "candidate checkout")
    if not checkout.is_dir() or _head(checkout) != args.commit:
        raise HandoffError("candidate checkout HEAD does not match commit")
    package = _regular(_supplied(args.candidate_zip, "candidate ZIP"), "candidate ZIP")
    if _candidate_zip_commit(package) != args.commit:
        raise HandoffError("candidate ZIP commit does not match checkout HEAD")

    observer_runner = _regular(
        _supplied(args.observer_runner, "observer runner"), "observer runner"
    )
    if _sha256(observer_runner) != args.observer_sha256:
        raise HandoffError("observer runner SHA-256 does not match approved provenance")
    observer_backend = _regular(
        _supplied(args.observer_backend, "observer backend"), "observer backend"
    )
    if _sha256(observer_backend) != args.observer_backend_sha256:
        raise HandoffError("observer backend SHA-256 does not match approved provenance")
    observer_harness = _regular(
        _supplied(args.observer_harness, "observer harness"), "observer harness"
    )
    if _sha256(observer_harness) != args.observer_harness_sha256:
        raise HandoffError("observer harness SHA-256 does not match approved provenance")
    if observer_runner.parent != observer_backend.parent or observer_runner.parent != observer_harness.parent:
        raise HandoffError("approved observer runner, backend, and harness must share one source directory")
    helper = _regular(_supplied(args.helper, "helper"), "helper")
    if _sha256(helper) != args.helper_sha256:
        raise HandoffError("helper SHA-256 does not match approved runtime provenance")
    node = _regular(_supplied(args.node, "node"), "node", executable=True)
    bridge = _regular(_supplied(args.bridge_bin, "bridge binary"), "bridge binary", executable=True)
    aioncore = _regular(_supplied(args.aioncore_bin, "AionCore binary"), "AionCore binary", executable=True)
    ego_browser = _regular(
        _supplied(args.ego_browser, "ego-browser binary"), "ego-browser binary", executable=True
    )
    host_bundle = _absolute(args.host_bundle, "signed host bundle")
    codesign = _regular(_supplied(args.codesign, "codesign"), "codesign", executable=True)
    if not FULL_SHA.fullmatch(args.host_cdhash):
        raise HandoffError("host CDHash must be a full lowercase 40-character hash")
    _verify_host_bundle(host_bundle, aioncore, codesign, args.host_cdhash, args.host_version)

    root = _absolute(args.sandbox_root, "sandbox root")
    observer_dir = _absolute(args.observer_install_dir, "observer install directory")
    for path, label in ((root, "sandbox root"), (observer_runner, "observer runner"),
                        (observer_backend, "observer backend"), (observer_harness, "observer harness"),
                        (helper, "helper"), (observer_dir, "observer install directory")):
        if _inside(path, checkout):
            raise HandoffError(f"{label} must be outside the candidate checkout")
    for path, label in ((root, "sandbox root"), (observer_dir, "observer install directory")):
        if path.exists():
            raise HandoffError(f"{label} already exists: {path}")
    root.mkdir(mode=0o700, parents=False)
    os.chmod(root, 0o700)
    evidence_dir = root / "evidence"
    bridge_state = root / "bridge-state"
    core_data = root / "aioncore-data"
    extensions = root / "extensions"
    installed = extensions / "pursers-home"
    for directory in (evidence_dir, bridge_state, core_data, extensions):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    _install_zip(package, installed)
    installed_commit = json.loads((installed / "webui" / "candidate.json").read_text())["candidate_commit"]
    if installed_commit != args.commit:
        raise HandoffError("installed candidate identity does not match exact commit")

    token = root / "helper-token"
    _write(token, secrets.token_hex(32) + "\n", 0o600)
    assertion_paths: dict[str, Path] = {}
    for observation in OBSERVATIONS:
        assertion_paths[observation] = root / f"{observation}.assertions.json"
        _write(assertion_paths[observation], "[]\n", 0o600)

    launcher = root / "launch-aioncore.py"
    _write(launcher, AIONCORE_LAUNCHER, 0o700)
    runner = str(observer_runner)
    report = evidence_dir / "home-acceptance.json"
    core_command = [sys.executable, str(launcher), args.identity_mode,
                    str(aionpro_secret) if aionpro_secret else "-", str(aioncore),
                    "--host", urlsplit(origin).hostname or "127.0.0.1",
                    "--port", str(urlsplit(origin).port), "--data-dir", str(core_data),
                    "--app-version", args.host_version, "--identity-mode", args.identity_mode]
    helper_command = [
        str(node), str(helper), "--board", args.board, "--origin", origin,
        "--token-file", str(token), "--bridge-state-dir", str(bridge_state),
        "--bridge-bin", str(bridge), "--aioncore-bin", str(aioncore),
        "--core-version", args.core_version,
    ]
    install = [sys.executable, runner, "install-observer", "--dir", str(observer_dir),
               "--ego-browser", str(ego_browser), "--task-space", args.task_space]
    doctor = [sys.executable, runner, "doctor", "--observer", str(observer_dir),
              "--target", origin, "--probe-browser", page]
    validate = [sys.executable, runner, "validate", "--observer", str(observer_dir),
                "--report", str(report), "--target", origin, "--board", args.board,
                "--commit", args.commit]

    def start_text(command: list[str], pid_name: str, environment: str = "") -> str:
        return (
            "#!/bin/sh\nset -eu\numask 077\n"
            f"pid_file=$(dirname \"$0\")/{pid_name}\n"
            "test ! -e \"$pid_file\" || { echo \"stale pid file: $pid_file\" >&2; exit 1; }\n"
            "echo $$ > \"$pid_file\"\n"
            + environment
            + "exec " + _quote(command) + "\n"
        )
    reviewer_text = (
        "#!/bin/sh\nset -eu\numask 077\n"
        + _quote(install) + "\n"
        + _quote(doctor) + "\n"
        + "# Fill every *.assertions.json with fresh verifier-chosen assertions before capture.\n"
        + "".join(
            _quote([
                sys.executable, runner, "capture", "--observer", str(observer_dir),
                "--evidence", str(report), "--observation", observation, "--target", origin,
                "--board", args.board, "--commit", args.commit, "--page", page,
                "--assertions", str(assertion_paths[observation]),
            ]) + "\n"
            for observation in OBSERVATIONS
        )
        + "PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY " + _quote(validate) + "\n"
        + "cd " + shlex.quote(str(checkout)) + "\n"
        + "PURSERS_HOME_ACCEPTANCE_HOST_URL=" + shlex.quote(origin)
        + " PURSERS_HOME_ACCEPTANCE_BOARD=" + shlex.quote(args.board)
        + " PURSERS_HOME_ACCEPTANCE_EVIDENCE=" + shlex.quote(str(report))
        + " PURSERS_HOME_BROWSER_OBSERVER=" + shlex.quote(str(observer_dir))
        + " PURSERS_HOME_ACCEPTANCE_COMMIT=" + shlex.quote(args.commit)
        + " " + shlex.quote(sys.executable)
        + " -m pytest -q tools/aionui-extension/tests/home_acceptance/test_live_host.py\n"
    )
    cleanup_text = (
        "#!/bin/sh\nset -eu\n"
        "root=$(CDPATH= cd -- \"$(dirname \"$0\")\" && pwd)\n"
        "for name in helper.pid aioncore.pid; do\n"
        "  pid_file=$root/$name\n"
        "  [ -f \"$pid_file\" ] || continue\n"
        "  pid=$(cat \"$pid_file\")\n"
        "  case \"$pid\" in (*[!0-9]*|'') exit 1;; esac\n"
        "  command=$(ps -p \"$pid\" -o command=)\n"
        "  case \"$command\" in (*\"$root\"*) kill \"$pid\";; (*) echo \"refusing unrelated pid $pid\" >&2; exit 1;; esac\n"
        "  rm \"$pid_file\"\n"
        "done\n"
    )
    _write(root / "start-aioncore.sh", start_text(
        core_command, "aioncore.pid",
        "unset AIONCORE_BOOTSTRAP_SECRET\n"
        "export AIONUI_EXTENSIONS_PATH=" + shlex.quote(str(extensions)) + "\n",
    ), 0o700)
    _write(root / "start-helper.sh", start_text(helper_command, "helper.pid"), 0o700)
    _write(root / "reviewer-commands.sh", reviewer_text, 0o700)
    _write(root / "cleanup.sh", cleanup_text, 0o700)

    manifest = {
        "schema_version": 1,
        "board": args.board,
        "origin": origin,
        "page": page,
        "candidate": {"commit": args.commit, "zip": str(package), "zip_sha256": _sha256(package)},
        "approved_runtime": {"commit": args.runtime_commit},
        "installed_extension": str(installed),
        "approved_observer": {
            "runner": str(observer_runner), "runner_sha256": args.observer_sha256,
            "backend": str(observer_backend), "backend_sha256": args.observer_backend_sha256,
            "harness": str(observer_harness), "harness_sha256": args.observer_harness_sha256,
            "install_dir": str(observer_dir),
        },
        "signed_host": {
            "bundle": str(host_bundle), "version": args.host_version,
            "cdhash": args.host_cdhash, "identity_mode": args.identity_mode,
        },
        "operator_authority": {
            "status": "required",
            "action": "Sign in or pair the isolated AionUi sandbox and issue its sandbox-only door to the independent verifier.",
        },
        "secrets": {
            "helper_token": str(token),
            "value_recorded": False,
            "aionpro_bootstrap_secret": {
                "required": args.identity_mode == "aionpro",
                "source": "explicit-private-file" if aionpro_secret else "not-used",
                "path": str(aionpro_secret) if aionpro_secret else None,
                "value_recorded": False,
            },
        },
        "missing_capabilities": ["result_visibility", "seat_lifecycle", "team_lifecycle", "ticket_lifecycle"],
    }
    _write(root / "handoff.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n", 0o600)
    return {"ok": True, "handoff": str(root), "manifest": str(root / "handoff.json")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-checkout", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--candidate-zip", required=True)
    parser.add_argument("--sandbox-root", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--page-path", default="/api/extensions/pursers/assets/webui/index.html")
    parser.add_argument("--observer-runner", required=True)
    parser.add_argument("--observer-sha256", required=True)
    parser.add_argument("--observer-backend", required=True)
    parser.add_argument("--observer-backend-sha256", required=True)
    parser.add_argument("--observer-harness", required=True)
    parser.add_argument("--observer-harness-sha256", required=True)
    parser.add_argument("--observer-install-dir", required=True)
    parser.add_argument("--helper", required=True)
    parser.add_argument("--helper-sha256", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--bridge-bin", required=True)
    parser.add_argument("--aioncore-bin", required=True)
    parser.add_argument("--ego-browser", required=True)
    parser.add_argument("--host-bundle", required=True)
    parser.add_argument("--host-cdhash", required=True)
    parser.add_argument("--codesign", default="/usr/bin/codesign")
    parser.add_argument("--host-version", default="2.2.1")
    parser.add_argument("--identity-mode", choices=("webui", "aionpro"), required=True)
    parser.add_argument("--aionpro-bootstrap-secret-file")
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--core-version", default="0.2.1")
    parser.add_argument("--task-space", default="pursers-home-acceptance")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        result = prepare(build_parser().parse_args(argv))
    except (HandoffError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
