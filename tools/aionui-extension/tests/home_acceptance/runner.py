#!/usr/bin/env python3
"""Reproducible runner for real AionUi Home acceptance evidence.

The runner is the caller side of the trust boundary: it never authors
screenshot bytes, accessibility trees, host identity, page URLs or timestamps.
Every such value comes from the verifier-owned observer
(``browser_observer.py``) that recorded it through a real browser channel, and
the same observer is replayed by ``harness.validate_evidence_report`` at
validation time.

Subcommands:

``install-observer``  copy the observer into a verifier-owned directory
``doctor``            report observed capability status, exit non-zero if blocked
``capture``           record one real browser observation into evidence
``validate``          validate an evidence report through the installed observer
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[3]
OBSERVER_SOURCE = HERE / "browser_observer.py"
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 7
EXIT_FAILED = 8


class RunnerError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _observer_command(observer_dir: Path) -> Path:
    command = (observer_dir / "browser_observer.py").resolve()
    if not command.is_file():
        raise RunnerError(EXIT_BLOCKED, f"observer is not installed at {command}")
    if not os.access(command, os.X_OK):
        raise RunnerError(EXIT_BLOCKED, "installed observer is not executable")
    if command.is_relative_to(REPOSITORY_ROOT.resolve()):
        raise RunnerError(
            EXIT_USAGE,
            "observer must be installed outside the checkout to stay verifier-owned",
        )
    return command


def _run_observer(argv: list[str], *, stdin_text: str | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        input=stdin_text if stdin_text is not None else "",
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()[-1:] or ["no stderr"]
        code = EXIT_BLOCKED if completed.returncode == EXIT_BLOCKED else EXIT_FAILED
        raise RunnerError(code, f"observer exited {completed.returncode}: {message[0][:300]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RunnerError(EXIT_FAILED, "observer returned no JSON capture") from None
    if not isinstance(payload, dict):
        raise RunnerError(EXIT_FAILED, "observer capture is not a JSON object")
    return payload


def install_observer(args: argparse.Namespace) -> int:
    destination = Path(args.dir).expanduser().resolve()
    if destination.is_relative_to(REPOSITORY_ROOT.resolve()):
        raise RunnerError(EXIT_USAGE, "observer directory must live outside the checkout")
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    command = destination / "browser_observer.py"
    shutil.copyfile(OBSERVER_SOURCE, command)
    os.chmod(command, 0o755)
    if args.backend_command:
        backend: dict[str, Any] = {"kind": "command", "command": list(args.backend_command)}
    else:
        backend = {"kind": "ego-browser", "command": str(Path(args.ego_browser).expanduser().resolve())}
    config_path = destination / "observer.json"
    existing_id = None
    if config_path.is_file() and not args.rotate_session:
        try:
            existing_id = json.loads(config_path.read_text(encoding="utf-8")).get("observer_id")
        except (OSError, json.JSONDecodeError):
            existing_id = None
    observer_id = existing_id or f"observer-{uuid.uuid4().hex[:16]}"
    config = {
        "schema_version": SCHEMA_VERSION,
        "observer_id": observer_id,
        "store_dir": "captures",
        "max_age_s": int(args.max_age_s),
        "repository_root": str(REPOSITORY_ROOT.resolve()),
        "backend": backend,
    }
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)
    store = destination / "captures"
    store.mkdir(parents=True, exist_ok=True)
    os.chmod(store, 0o700)
    json.dump(
        {
            "installed": str(command),
            "observer_id": observer_id,
            "backend_kind": backend["kind"],
            "max_age_s": config["max_age_s"],
        },
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return EXIT_OK


def _load_assertions(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        raise RunnerError(
            EXIT_USAGE,
            "--assertions is required: every observation needs verifiable assertions",
        )
    try:
        assertions = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_USAGE, "assertions file is not readable JSON") from None
    if not isinstance(assertions, list) or not assertions:
        raise RunnerError(EXIT_USAGE, "assertions file must hold a nonempty list")
    return assertions


def _artifact(evidence_root: Path, relative: str, data: bytes) -> dict[str, str]:
    path = evidence_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}


def doctor(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(HERE))
    import browser_observer as observer_module

    status: dict[str, Any] = {"target": args.target, "checks": {}}
    exit_code = EXIT_OK
    try:
        command = _observer_command(Path(args.observer).expanduser())
        status["checks"]["observer_installed"] = {"state": "ok", "path": str(command)}
    except RunnerError as error:
        status["checks"]["observer_installed"] = {"state": "blocked", "reason": str(error)}
        exit_code = EXIT_BLOCKED
    try:
        identity = observer_module.probe_host_identity(args.target.rstrip("/"))
        status["checks"]["host_identity"] = {"state": "ok", **identity}
    except observer_module.ObserverError as error:
        status["checks"]["host_identity"] = {"state": "blocked", "reason": str(error)}
        exit_code = EXIT_BLOCKED
    if args.probe_browser:
        try:
            command = _observer_command(Path(args.observer).expanduser())
            probe = _run_observer([str(command), "probe-browser", "--page", args.probe_browser])
            status["checks"]["browser_channel"] = {"state": "ok", **probe}
        except RunnerError as error:
            status["checks"]["browser_channel"] = {"state": "blocked", "reason": str(error)}
            exit_code = EXIT_BLOCKED
    status["acceptance_ready"] = exit_code == EXIT_OK
    json.dump(status, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return exit_code


def capture(args: argparse.Namespace) -> int:
    command = _observer_command(Path(args.observer).expanduser())
    evidence_root = Path(args.evidence).expanduser().resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": args.observation,
        "target": {"base_url": args.target.rstrip("/"), "board_id": args.board},
        "candidate_commit": args.commit,
        "page_url": args.page,
        "assertions": _load_assertions(args.assertions),
    }
    spec_dir = evidence_root / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{args.observation}.json"
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload = _run_observer([str(command), "capture", "--spec", str(spec_path)])
    screenshot = base64.b64decode(payload["screenshot_base64"], validate=True)
    screenshot_artifact = _artifact(
        evidence_root, f"artifacts/{args.observation}.png", screenshot
    )
    snapshot_document = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": payload["observation_id"],
        "page_url": payload["page_url"],
        "captured_at": payload["captured_at"],
        "snapshot": payload["snapshot"],
    }
    snapshot_artifact = _artifact(
        evidence_root,
        f"artifacts/{args.observation}.ax.json",
        json.dumps(snapshot_document, sort_keys=True).encode("utf-8"),
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": "browser_observation",
        "observation_id": payload["observation_id"],
        "target": payload["target"],
        "host": {"version": payload["host_version"], "build": payload["host_build"]},
        "candidate_commit": payload["candidate_commit"],
        "captured_at": payload["captured_at"],
        "page_url": payload["page_url"],
        "screenshot": screenshot_artifact,
        "accessibility_snapshot": snapshot_artifact,
        "assertions": spec["assertions"],
    }
    reference = f"observations/{args.observation}.json"
    receipt_path = evidence_root / reference
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    host_receipt = {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": "host_identity",
        "target": payload["target"],
        "product": payload["host_product"],
        "version": payload["host_version"],
        "build": payload["host_build"],
        "candidate_commit": payload["candidate_commit"],
        "captured_at": payload["captured_at"],
        "source": "host-api",
    }
    (evidence_root / "host-identity.json").write_text(
        json.dumps(host_receipt, sort_keys=True), encoding="utf-8"
    )
    json.dump(
        {
            "observation_id": payload["observation_id"],
            "evidence": reference,
            "host_identity_evidence": "host-identity.json",
            "page_url": payload["page_url"],
            "screenshot_sha256": screenshot_artifact["sha256"],
            "snapshot_sha256": snapshot_artifact["sha256"],
        },
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return EXIT_OK


def validate(args: argparse.Namespace) -> int:
    command = _observer_command(Path(args.observer).expanduser())
    sys.path.insert(0, str(HERE))
    import harness

    target = harness.validate_live_target(args.target.rstrip("/"), args.board)
    capabilities = harness.discover_repository_capabilities()
    try:
        summary = harness.validate_evidence_report(
            Path(args.report).expanduser().resolve(),
            target,
            capabilities,
            args.commit,
            browser_observer_command=command,
        )
    except harness.AcceptanceCapabilityUnavailable as error:
        json.dump(
            {"outcome": "blocked", "reason": str(error)}, sys.stdout, indent=2, sort_keys=True
        )
        sys.stdout.write("\n")
        return EXIT_BLOCKED
    except harness.AcceptanceError as error:
        json.dump(
            {"outcome": "failed", "reason": str(error)}, sys.stdout, indent=2, sort_keys=True
        )
        sys.stdout.write("\n")
        return EXIT_FAILED
    json.dump({"outcome": "passed", **summary}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install-observer", help="install the verifier-owned observer")
    install.add_argument("--dir", required=True, help="verifier-owned directory outside the checkout")
    install.add_argument("--ego-browser", default="ego-browser", help="absolute path to the ego-browser CLI")
    install.add_argument("--backend-command", nargs="+", help="alternative absolute capture backend command")
    install.add_argument("--max-age-s", type=int, default=43_200)
    install.add_argument("--rotate-session", action="store_true", help="mint a new observer_id")
    install.set_defaults(handler=install_observer)

    check = sub.add_parser("doctor", help="report observed capability status")
    check.add_argument("--observer", required=True)
    check.add_argument("--target", required=True)
    check.add_argument(
        "--probe-browser",
        help="page URL to observe through the real browser channel without writing evidence",
    )
    check.set_defaults(handler=doctor)

    shot = sub.add_parser("capture", help="record one real browser observation")
    shot.add_argument("--observer", required=True)
    shot.add_argument("--evidence", required=True)
    shot.add_argument("--observation", required=True)
    shot.add_argument("--target", required=True)
    shot.add_argument("--board", required=True)
    shot.add_argument("--commit", required=True)
    shot.add_argument("--page", required=True)
    shot.add_argument("--assertions", help="JSON file holding the observation assertions")
    shot.set_defaults(handler=capture)

    check_report = sub.add_parser("validate", help="validate an evidence report")
    check_report.add_argument("--observer", required=True)
    check_report.add_argument("--report", required=True)
    check_report.add_argument("--target", required=True)
    check_report.add_argument("--board", required=True)
    check_report.add_argument("--commit", required=True)
    check_report.set_defaults(handler=validate)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    try:
        return int(args.handler(args))
    except RunnerError as error:
        sys.stderr.write(f"runner: {error}\n")
        return error.code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
