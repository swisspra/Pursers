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
``prepare``           expand 198 core observations plus 3 final gates
``capture``           record one real browser observation into evidence
``assemble``          refuse incomplete captures and assemble the report
``validate``          validate an evidence report through the installed observer
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[3]
OBSERVER_SOURCE = HERE / "browser_observer.py"
SCHEMA_VERSION = 1
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SURFACE_PRODUCTS = {
    "aionui": "AionUi",
    "fleet": "Pursers Fleet",
    "personal": "Pursers Personal",
}

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


def _resolve_backend_executable(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    discovered = shutil.which(value)
    if not discovered:
        raise RunnerError(EXIT_BLOCKED, f"browser backend executable is unavailable: {value}")
    return Path(discovered).resolve()


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


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, text=True, capture_output=True,
        check=False, timeout=10,
    )
    if completed.returncode:
        raise RunnerError(EXIT_BLOCKED, "verification checkout git identity is unavailable")
    return completed.stdout.strip()


def _load_surface_manifest(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_USAGE, "surface manifest is not readable JSON") from None
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "candidate_commit", "surfaces"
    } or manifest["schema_version"] != SCHEMA_VERSION:
        raise RunnerError(EXIT_USAGE, "surface manifest fields do not match schema")
    candidate = manifest["candidate_commit"]
    if not isinstance(candidate, str) or not FULL_SHA.fullmatch(candidate):
        raise RunnerError(EXIT_USAGE, "surface manifest candidate_commit must be a full SHA")
    if _git("rev-parse", "HEAD") != candidate or _git("status", "--porcelain"):
        raise RunnerError(EXIT_BLOCKED, "verification checkout must be clean at candidate_commit")
    surfaces = manifest["surfaces"]
    if not isinstance(surfaces, dict) or set(surfaces) != set(SURFACE_PRODUCTS):
        raise RunnerError(EXIT_USAGE, "surface manifest needs exact aionui, fleet, personal entries")
    normalized: dict[str, Any] = {}
    expected_adapters = {
        "aionui": "signed-aionui",
        "fleet": "pinned-process-artifact",
        "personal": "pinned-signed-aionui-personal-mcp",
    }
    for surface_id, product in SURFACE_PRODUCTS.items():
        row = surfaces[surface_id]
        required = {"adapter", "target"} if surface_id == "aionui" else {
            "adapter", "target", "artifact"
        }
        if surface_id == "personal":
            required.add("runtime")
        if not isinstance(row, dict) or set(row) != required:
            raise RunnerError(EXIT_USAGE, f"surface {surface_id} fields do not match schema")
        if row["adapter"] != expected_adapters[surface_id]:
            raise RunnerError(EXIT_USAGE, f"surface {surface_id} adapter is invalid")
        target = row["target"]
        if not isinstance(target, dict) or set(target) != {"base_url", "board_id"}:
            raise RunnerError(EXIT_USAGE, f"surface {surface_id} target is invalid")
        from urllib.parse import urlsplit
        parsed = urlsplit(str(target["base_url"]))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or not isinstance(target["board_id"], str)
            or not re.fullmatch(r"(?:sandbox|test)-[A-Za-z0-9._-]{1,71}", target["board_id"])
        ):
            raise RunnerError(EXIT_USAGE, f"surface {surface_id} target is unsafe")
        normalized_row: dict[str, Any] = {
            "adapter": row["adapter"],
            "target": {"base_url": str(target["base_url"]).rstrip("/"), "board_id": target["board_id"]},
            "product": product,
            "candidate_commit": candidate,
        }
        if surface_id != "aionui":
            relative = row["artifact"]
            if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
                raise RunnerError(EXIT_USAGE, f"surface {surface_id} artifact path is invalid")
            artifact = (REPOSITORY_ROOT / relative).resolve()
            if not artifact.is_relative_to(REPOSITORY_ROOT.resolve()) or not artifact.is_file():
                raise RunnerError(EXIT_USAGE, f"surface {surface_id} artifact is unavailable")
            tracked = _git("ls-files", "--error-unmatch", relative)
            if tracked != relative:
                raise RunnerError(EXIT_USAGE, f"surface {surface_id} artifact is not exact tracked content")
            normalized_row.update({
                "artifact": relative,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "version": f"candidate-{candidate[:12]}",
            })
        if surface_id == "personal":
            runtime = row["runtime"]
            if not isinstance(runtime, dict) or set(runtime) != {
                "artifact", "pid_file", "receipt"
            }:
                raise RunnerError(EXIT_USAGE, "Personal runtime fields are invalid")
            runtime_artifact = runtime["artifact"]
            if (
                not isinstance(runtime_artifact, str)
                or runtime_artifact.startswith("/")
                or ".." in Path(runtime_artifact).parts
            ):
                raise RunnerError(EXIT_USAGE, "Personal runtime artifact is invalid")
            runtime_source = (REPOSITORY_ROOT / runtime_artifact).resolve()
            if (
                not runtime_source.is_relative_to(REPOSITORY_ROOT.resolve())
                or not runtime_source.is_file()
                or _git("ls-files", "--error-unmatch", runtime_artifact) != runtime_artifact
            ):
                raise RunnerError(EXIT_USAGE, "Personal runtime artifact is not exact tracked content")
            runtime_paths: dict[str, str] = {}
            for field in ("pid_file", "receipt"):
                value = runtime[field]
                if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
                    raise RunnerError(EXIT_USAGE, f"Personal runtime {field} must be absolute")
                resolved = Path(value).expanduser().resolve()
                if resolved.is_relative_to(REPOSITORY_ROOT.resolve()):
                    raise RunnerError(EXIT_USAGE, f"Personal runtime {field} must stay outside checkout")
                runtime_paths[field] = str(resolved)
            normalized_row["runtime"] = {
                "artifact": runtime_artifact,
                "artifact_sha256": hashlib.sha256(runtime_source.read_bytes()).hexdigest(),
                **runtime_paths,
            }
        normalized[surface_id] = normalized_row
    personal_meta = tomllib.loads(
        (REPOSITORY_ROOT / "packages/personal/pyproject.toml").read_text(encoding="utf-8")
    )
    personal_version = personal_meta.get("project", {}).get("version")
    if not isinstance(personal_version, str):
        raise RunnerError(EXIT_BLOCKED, "Personal package version is unavailable")
    normalized["personal"]["version"] = personal_version
    if normalized["fleet"]["target"]["base_url"] == normalized["aionui"]["target"]["base_url"]:
        raise RunnerError(EXIT_USAGE, "Fleet must use its actual distinct loopback origin")
    return normalized


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
        backend = {
            "kind": "ego-browser",
            "command": str(_resolve_backend_executable(args.ego_browser)),
            "task_space": args.task_space,
        }
    config_path = destination / "observer.json"
    existing_id = None
    if config_path.is_file() and not args.rotate_session:
        try:
            existing_id = json.loads(config_path.read_text(encoding="utf-8")).get("observer_id")
        except (OSError, json.JSONDecodeError):
            existing_id = None
    observer_id = existing_id or f"observer-{uuid.uuid4().hex[:16]}"
    surfaces = _load_surface_manifest(args.surface_manifest) if args.surface_manifest else None
    config = {
        "schema_version": SCHEMA_VERSION,
        "observer_id": observer_id,
        "store_dir": "captures",
        "max_age_s": int(args.max_age_s),
        "repository_root": str(REPOSITORY_ROOT.resolve()),
        "backend": backend,
        "surfaces": surfaces,
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
            "surfaces": {
                key: {
                    "target": value["target"],
                    "product": value["product"],
                    "candidate_commit": value["candidate_commit"],
                }
                for key, value in (surfaces or {}).items()
            },
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


def _load_observer_config(observer_dir: Path) -> dict[str, Any]:
    _observer_command(observer_dir)
    path = observer_dir.expanduser().resolve() / "observer.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_BLOCKED, "installed observer config is unreadable") from None
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError(EXIT_BLOCKED, "installed observer config is invalid")
    surfaces = config.get("surfaces")
    if not isinstance(surfaces, dict) or set(surfaces) != set(SURFACE_PRODUCTS):
        raise RunnerError(EXIT_BLOCKED, "installed observer lacks verifier-pinned surfaces")
    return config


def _acceptance_ids() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    sys.path.insert(0, str(HERE))
    import harness

    return (
        tuple(harness.SEQUENCE),
        tuple(sorted(harness.REQUIRED_INVENTORY)),
        tuple(harness.REQUIRED_FINAL_GATES),
    )


def _surface_for(identifier: str) -> str:
    sys.path.insert(0, str(HERE))
    import harness

    return str(harness._surface_for_identifier(identifier))


def prepare(args: argparse.Namespace) -> int:
    observer_dir = Path(args.observer).expanduser().resolve()
    config = _load_observer_config(observer_dir)
    try:
        manifest = json.loads(Path(args.manifest).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_USAGE, "observation manifest is not readable JSON") from None
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "operator_topology", "observations"
    } or manifest["schema_version"] != SCHEMA_VERSION:
        raise RunnerError(EXIT_USAGE, "observation manifest fields do not match schema")
    sys.path.insert(0, str(HERE))
    import harness

    try:
        operator_topology = harness._validate_operator_topology(
            manifest["operator_topology"]
        )
    except harness.AcceptanceError as error:
        raise RunnerError(EXIT_USAGE, str(error)) from None
    rows = manifest["observations"]
    sequence, inventory, final_gates = _acceptance_ids()
    required = {*sequence, *inventory, *final_gates}
    if not isinstance(rows, list) or len(rows) != len(required):
        raise RunnerError(EXIT_USAGE, f"observation manifest must contain exactly {len(required)} rows")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not {"id", "page_url", "assertions"}.issubset(row):
            raise RunnerError(EXIT_USAGE, "observation manifest row fields do not match schema")
        identifier = row["id"]
        if not isinstance(identifier, str) or identifier in by_id:
            raise RunnerError(EXIT_USAGE, "observation manifest IDs must be unique strings")
        if not isinstance(row["assertions"], list) or not row["assertions"]:
            raise RunnerError(EXIT_USAGE, f"observation {identifier} needs assertions")
        typed_conjuncts = harness._canonical_typed_conjuncts(identifier)
        expected_keys = {"id", "page_url", "assertions"}
        if typed_conjuncts:
            expected_keys.add("typed_evidence")
        if set(row) != expected_keys:
            raise RunnerError(EXIT_USAGE, "observation manifest row fields do not match schema")
        required_assertions = list(harness._canonical_browser_assertions(identifier))
        if required_assertions:
            if row["assertions"] != required_assertions:
                raise RunnerError(
                    EXIT_USAGE,
                    f"observation {identifier} assertions do not match its canonical visual fact",
                )
        elif any(
            not isinstance(assertion, dict)
            or not str(assertion.get("name", "")).startswith("browser context: ")
            for assertion in row["assertions"]
        ):
            raise RunnerError(
                EXIT_USAGE,
                f"observation {identifier} needs explicit browser context assertions",
            )
        if typed_conjuncts:
            try:
                harness._passed_evidence_items([{
                    "id": identifier,
                    "status": "passed",
                    "evidence": f"observations/{identifier}.json",
                    "typed_evidence": row["typed_evidence"],
                }], "observation")
            except harness.AcceptanceError as error:
                raise RunnerError(EXIT_USAGE, str(error)) from None
        by_id[identifier] = row
    if set(by_id) != required:
        raise RunnerError(EXIT_USAGE, "observation manifest IDs do not match the authoritative set")
    evidence = Path(args.evidence).expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    commands: list[list[str]] = []
    for identifier in (*sequence, *inventory, *final_gates):
        row = by_id[identifier]
        surface_id = _surface_for(identifier)
        surface = config["surfaces"][surface_id]
        target = surface["target"]
        from urllib.parse import urlsplit
        page = urlsplit(row["page_url"] if isinstance(row["page_url"], str) else "")
        origin = urlsplit(target["base_url"])
        if page.scheme != origin.scheme or page.netloc != origin.netloc or page.query or page.fragment:
            raise RunnerError(EXIT_USAGE, f"observation {identifier} page_url uses the wrong surface origin")
        assertion_path = evidence / "assertions" / f"{identifier}.json"
        assertion_path.parent.mkdir(parents=True, exist_ok=True)
        assertion_path.write_text(json.dumps(row["assertions"], indent=2, sort_keys=True) + "\n")
        commands.append([
            sys.executable, str(Path(__file__).resolve()), "capture",
            "--observer", str(observer_dir), "--evidence", str(evidence),
            "--observation", identifier, "--surface", surface_id,
            "--target", target["base_url"], "--board", target["board_id"],
            "--commit", surface["candidate_commit"], "--page", row["page_url"],
            "--assertions", str(assertion_path),
        ])
    plan = {
        "schema_version": SCHEMA_VERSION,
        "candidate_commit": config["surfaces"]["aionui"]["candidate_commit"],
        "operator_topology": operator_topology,
        "sequence": list(sequence),
        "inventory": list(inventory),
        "final_gates": list(final_gates),
        "typed_evidence": {
            identifier: by_id[identifier]["typed_evidence"]
            for identifier in (*sequence, *inventory, *final_gates)
            if harness._canonical_typed_conjuncts(identifier)
        },
        "commands": commands,
    }
    plan_path = evidence / "capture-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    json.dump({"plan": str(plan_path), "observations": len(commands)}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def assemble(args: argparse.Namespace) -> int:
    observer_dir = Path(args.observer).expanduser().resolve()
    config = _load_observer_config(observer_dir)
    evidence = Path(args.evidence).expanduser().resolve()
    try:
        suite_manifest = json.loads(Path(args.suite_manifest).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_USAGE, "suite manifest is not readable JSON") from None
    sys.path.insert(0, str(HERE))
    import harness

    try:
        plan = json.loads((evidence / "capture-plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_BLOCKED, "capture plan is missing or invalid") from None
    if not isinstance(plan, dict) or set(plan) != {
        "schema_version", "candidate_commit", "operator_topology",
        "sequence", "inventory", "final_gates", "typed_evidence", "commands",
    } or plan["schema_version"] != SCHEMA_VERSION:
        raise RunnerError(EXIT_FAILED, "capture plan fields do not match schema")
    candidate = config["surfaces"]["aionui"]["candidate_commit"]
    if plan.get("candidate_commit") != candidate:
        raise RunnerError(EXIT_FAILED, "capture plan candidate does not match observer")
    try:
        operator_topology = harness._validate_operator_topology(
            plan.get("operator_topology")
        )
    except harness.AcceptanceError as error:
        raise RunnerError(EXIT_FAILED, str(error)) from None

    if not isinstance(suite_manifest, dict) or set(suite_manifest) != {"schema_version", "suites"}:
        raise RunnerError(EXIT_USAGE, "suite manifest fields do not match schema")
    suite_refs = suite_manifest["suites"]
    if not isinstance(suite_refs, dict) or set(suite_refs) != set(harness.REQUIRED_SUITES):
        raise RunnerError(EXIT_USAGE, "suite manifest does not match the required suite set")
    sequence, inventory, final_gates = _acceptance_ids()
    observations: dict[str, dict[str, Any]] = {}
    typed_by_id = plan["typed_evidence"]
    expected_typed_ids = {
        identifier
        for identifier in (*sequence, *inventory, *final_gates)
        if harness._canonical_typed_conjuncts(identifier)
    }
    if not isinstance(typed_by_id, dict) or set(typed_by_id) != expected_typed_ids:
        raise RunnerError(EXIT_FAILED, "capture plan typed evidence is invalid")
    surface_runtime: dict[str, dict[str, Any]] = {}
    for identifier in (*sequence, *inventory, *final_gates):
        path = evidence / "observations" / f"{identifier}.json"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RunnerError(EXIT_BLOCKED, f"capture missing or invalid: {identifier}") from None
        surface_id = _surface_for(identifier)
        if receipt.get("observation_id") != identifier or receipt.get("surface_id") != surface_id:
            raise RunnerError(EXIT_FAILED, f"capture surface binding is invalid: {identifier}")
        runtime = receipt.get("runtime")
        if surface_id in surface_runtime and surface_runtime[surface_id] != runtime:
            raise RunnerError(EXIT_FAILED, f"surface runtime changed across captures: {surface_id}")
        surface_runtime[surface_id] = runtime
        observations[identifier] = receipt
        typed_conjuncts = harness._canonical_typed_conjuncts(identifier)
        if typed_conjuncts:
            typed_rows = typed_by_id.get(identifier)
            try:
                validated = harness._passed_evidence_items([{
                    "id": identifier,
                    "status": "passed",
                    "evidence": f"observations/{identifier}.json",
                    "typed_evidence": typed_rows,
                }], "observation")[identifier]
            except harness.AcceptanceError as error:
                raise RunnerError(EXIT_FAILED, str(error)) from None
            for typed in validated.typed:
                try:
                    typed_path = (evidence / typed["evidence"]).resolve(strict=True)
                    typed_path.relative_to(evidence)
                    if not typed_path.is_file():
                        raise FileNotFoundError(typed_path)
                except (FileNotFoundError, ValueError, RuntimeError):
                    raise RunnerError(
                        EXIT_BLOCKED,
                        f"typed evidence missing or invalid: {identifier}",
                    ) from None
    suites: list[dict[str, Any]] = []
    for name, command in harness.REQUIRED_SUITES.items():
        reference = suite_refs[name]
        try:
            receipt = json.loads((evidence / reference).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            raise RunnerError(EXIT_BLOCKED, f"suite receipt missing or invalid: {name}") from None
        if receipt.get("name") != name or receipt.get("command") != command or receipt.get("exit_code") != 0:
            raise RunnerError(EXIT_FAILED, f"suite receipt does not prove success: {name}")
        suites.append({
            "name": name, "command": command, "status": "passed",
            "commit": receipt.get("commit"), "evidence": reference,
        })
    aion = config["surfaces"]["aionui"]
    surfaces = {
        surface_id: {
            "target": config["surfaces"][surface_id]["target"],
            "runtime": surface_runtime[surface_id],
            "candidate_commit": candidate,
        }
        for surface_id in SURFACE_PRODUCTS
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": "real_browser_host", "mocked": False, "synthetic": False,
        "target": aion["target"],
        "host": {**surface_runtime["aionui"], "evidence": "host-identity-aionui.json"},
        "surfaces": surfaces,
        "operator_topology": operator_topology,
        "steps": [
            {
                "id": identifier,
                "status": "passed",
                "evidence": f"observations/{identifier}.json",
                **({"typed_evidence": typed_by_id[identifier]} if identifier in typed_by_id else {}),
            }
            for identifier in sequence
        ],
        "inventory": [
            {
                "id": identifier,
                "status": "passed",
                "evidence": f"observations/{identifier}.json",
                **({"typed_evidence": typed_by_id[identifier]} if identifier in typed_by_id else {}),
            }
            for identifier in inventory
        ],
        "final_gates": [
            {
                "id": identifier,
                "status": "passed",
                "evidence": f"observations/{identifier}.json",
                **({"typed_evidence": typed_by_id[identifier]} if identifier in typed_by_id else {}),
            }
            for identifier in final_gates
        ],
        "suites": suites,
        "all_existing_suites_passed": True,
    }
    report["host"].pop("identity_source", None)
    destination = Path(args.report).expanduser().resolve()
    if destination.parent != evidence:
        raise RunnerError(EXIT_USAGE, "assembled report must live directly in the evidence directory")
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    json.dump({"report": str(destination), "observations": len(observations)}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


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
        runtime = observer_module.probe_runtime_health(args.target.rstrip("/"))
        status["checks"]["runtime_health"] = {"state": "ok", **runtime}
    except observer_module.ObserverError as error:
        status["checks"]["runtime_health"] = {"state": "blocked", "reason": str(error)}
        exit_code = EXIT_BLOCKED
    if args.probe_browser:
        try:
            command = _observer_command(Path(args.observer).expanduser())
            probe = _run_observer([str(command), "probe-browser", "--page", args.probe_browser])
            status["checks"]["browser_channel"] = {"state": "ok", **probe}
            status["checks"]["host_identity"] = {
                "state": "ok",
                **probe["host"],
                "candidate_commit": probe["candidate_commit"],
                "selected_board": probe["selected_board"],
            }
        except RunnerError as error:
            status["checks"]["browser_channel"] = {"state": "blocked", "reason": str(error)}
            status["checks"]["host_identity"] = {
                "state": "blocked",
                "reason": "authenticated browser host/UI binding is unavailable",
            }
            exit_code = EXIT_BLOCKED
    else:
        status["checks"]["host_identity"] = {
            "state": "blocked",
            "reason": "--probe-browser is required for authenticated host/UI binding",
        }
        exit_code = EXIT_BLOCKED
    status["acceptance_ready"] = exit_code == EXIT_OK
    json.dump(status, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return exit_code


ATTESTATION_NONCE = re.compile(r"^[0-9a-f]{32,128}$")


def _attestation_nonce(args: argparse.Namespace) -> str:
    """Return the verifier-selected nonce for this capture's live challenge.

    The verifier owns nonce selection. For a Personal capture it passes the
    same nonce it already handed to the AionUi conversation, so the signed
    answer lands in this capture's accessibility snapshot; otherwise a fresh
    nonce is generated here and no stale value can be reused.
    """
    supplied = getattr(args, "attestation_nonce", None)
    if supplied is None:
        return secrets.token_hex(32)
    if not ATTESTATION_NONCE.fullmatch(supplied):
        raise RunnerError(
            EXIT_USAGE,
            "--attestation-nonce must be 32-128 lowercase hex characters",
        )
    return supplied


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
        "surface_id": args.surface,
        "attestation_nonce": _attestation_nonce(args),
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
        "surface_id": payload["surface_id"],
        "runtime": {
            "product": payload["host_product"],
            "version": payload["host_version"],
            "build": payload["host_build"],
            "identity_source": payload["host_identity_source"],
        },
        "candidate_commit": payload["candidate_commit"],
        "captured_at": payload["captured_at"],
        "page_url": payload["page_url"],
        "screenshot": screenshot_artifact,
        "accessibility_snapshot": snapshot_artifact,
        "assertions": spec["assertions"],
        "attestation": payload["attestation"],
        "attestation_nonce": payload["attestation_nonce"],
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
        "source": payload["host_identity_source"],
    }
    surface_host_reference = f"host-identity-{payload['surface_id']}.json"
    (evidence_root / surface_host_reference).write_text(
        json.dumps(host_receipt, sort_keys=True), encoding="utf-8"
    )
    json.dump(
        {
            "observation_id": payload["observation_id"],
            "evidence": reference,
            "host_identity_evidence": surface_host_reference,
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
    install.add_argument(
        "--task-space",
        default="pursers-home-acceptance",
        help="existing ego-browser task-space name or numeric id",
    )
    install.add_argument("--backend-command", nargs="+", help="alternative absolute capture backend command")
    install.add_argument("--max-age-s", type=int, default=43_200)
    install.add_argument(
        "--surface-manifest",
        help="verifier-authored exact AionUi, Fleet, and Personal runtime bindings",
    )
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

    plan = sub.add_parser("prepare", help="expand an exact verifier observation manifest")
    plan.add_argument("--observer", required=True)
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--evidence", required=True)
    plan.set_defaults(handler=prepare)

    shot = sub.add_parser("capture", help="record one real browser observation")
    shot.add_argument("--observer", required=True)
    shot.add_argument("--evidence", required=True)
    shot.add_argument("--observation", required=True)
    shot.add_argument("--surface", choices=tuple(SURFACE_PRODUCTS), default="aionui")
    shot.add_argument("--target", required=True)
    shot.add_argument("--board", required=True)
    shot.add_argument("--commit", required=True)
    shot.add_argument("--page", required=True)
    shot.add_argument("--assertions", help="JSON file holding the observation assertions")
    shot.add_argument(
        "--attestation-nonce",
        help="verifier-selected nonce already answered in the AionUi conversation",
    )
    shot.set_defaults(handler=capture)

    report = sub.add_parser("assemble", help="assemble a complete report from all captures")
    report.add_argument("--observer", required=True)
    report.add_argument("--evidence", required=True)
    report.add_argument("--suite-manifest", required=True)
    report.add_argument("--report", required=True)
    report.set_defaults(handler=assemble)

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
