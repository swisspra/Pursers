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
``prepare-aionui-typed`` install and plan the 21 AionUi typed-evidence rows
``record-aionui-typed`` execute one verifier-bound AionUi typed recorder
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from .pair_aionui import tool_sha256, validate_pairing_proof
except ImportError:  # Direct runner.py execution.
    from pair_aionui import tool_sha256, validate_pairing_proof

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[3]
OBSERVER_SOURCE = HERE / "browser_observer.py"
TYPED_SOURCE = HERE / "typed_evidence.py"
GAP_IMPLEMENTABILITY_MAP = REPOSITORY_ROOT / "planning/gap-implementability-map.json"
TYPED_PREDICATE_DELTA = (
    REPOSITORY_ROOT / "docs/design-home/context/typed-predicate-integration-delta.json"
)
SCHEMA_VERSION = 1
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,127}")
SURFACE_PRODUCTS = {
    "aionui": "AionUi",
    "fleet": "Pursers Fleet",
    "mcp-app": "Pursers Personal",
}

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 7
EXIT_FAILED = 8


class RunnerError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _state_recipe(
    before: list[dict[str, str]],
    actions: list[dict[str, Any]],
    after: list[dict[str, str]],
    *,
    settle_milliseconds: int = 250,
) -> dict[str, Any]:
    recipe = {
        "before": before,
        "actions": actions,
        "after": after,
        "settle_milliseconds": settle_milliseconds,
    }
    return {
        "adapter": "trusted_browser_state_v1",
        "recipe": recipe,
        "select_allowlist": sorted({
            row["path"] for row in (*before, *actions, *after)
        }),
    }


CURRENT_AIONUI_TYPED_RECIPES = {
    "browser-team-setup": _state_recipe(
        [{"path": "/group_count_before", "selector": ".group-card", "property": "count"}],
        [{"kind": "click", "selector": "#refresh-groups", "path": "/refresh_groups"}],
        [
            {"path": "/group_count", "selector": ".group-card", "property": "count"},
            {"path": "/selected_group_count", "selector": '[data-selected-group="true"]', "property": "count"},
        ],
    ),
    "browser-five-workers-three-reviewers": _state_recipe(
        [{"path": "/member_count_before", "selector": ".roster-row", "property": "count"}],
        [{"kind": "click", "selector": "#refresh-roster", "path": "/refresh_roster"}],
        [
            {"path": "/worker_count", "selector": "#roster-list", "property": "attribute:data-worker-count"},
            {"path": "/reviewer_count", "selector": "#roster-list", "property": "attribute:data-reviewer-count"},
            {"path": "/unique_identity_count", "selector": "#roster-list", "property": "attribute:data-unique-identity-count"},
        ],
    ),
    "browser-ticket-offer-claim-live": _state_recipe(
        [{"path": "/live_offer_status_before", "selector": '.ticket-row:has([data-claim-offer="live"])', "property": "attribute:data-offer-status"}],
        [{"kind": "click", "selector": '[data-claim-offer="live"]', "path": "/claim_live_offer"}],
        [
            {"path": "/live_offer_status", "selector": '.ticket-row[data-offer-status="claimed"]', "property": "attribute:data-offer-status"},
            {"path": "/claimed_identity", "selector": '.ticket-row[data-offer-status="claimed"] [data-claimed-identity]', "property": "attribute:data-claimed-identity"},
        ],
    ),
    "browser-ticket-offer-claim-expired": _state_recipe(
        [{"path": "/expired_offer_status_before", "selector": '.ticket-row:has([data-claim-offer="expired"])', "property": "attribute:data-offer-status"}],
        [{"kind": "click", "selector": '[data-claim-offer="expired"]', "path": "/claim_expired_offer"}],
        [
            {"path": "/expired_offer_status", "selector": '.ticket-row:has([data-claim-offer="expired"])', "property": "attribute:data-offer-status"},
            {"path": "/claim_error", "selector": '.ticket-row:has([data-claim-offer="expired"]) [data-claim-error]', "property": "attribute:data-claim-error"},
        ],
    ),
    "browser-ticket-submit-independent-review": _state_recipe(
        [{"path": "/closed_result_count_before", "selector": "#submission-list", "property": "attribute:data-closed-result-count"}],
        [{"kind": "click", "selector": "#refresh-results", "path": "/refresh_results"}],
        [
            {"path": "/closed_result_count", "selector": "#submission-list", "property": "attribute:data-closed-result-count"},
            {"path": "/independent_review_count", "selector": "#submission-list", "property": "attribute:data-independent-review-count"},
            {"path": "/pending_approval_count", "selector": "#submission-list", "property": "attribute:data-pending-approval-count"},
        ],
    ),
    "browser-result-visible": _state_recipe(
        [{"path": "/cursor_before", "selector": "#submission-list", "property": "attribute:data-cursor"}],
        [{"kind": "click", "selector": "#refresh-results", "path": "/refresh_results"}],
        [
            {"path": "/closed_result_count", "selector": "#submission-list", "property": "attribute:data-closed-result-count"},
            {"path": "/review", "selector": '.submission-row[data-ticket-status="closed"] .review-outcome', "property": "text"},
            {"path": "/actor", "selector": '.submission-row[data-ticket-status="closed"] .result-actor', "property": "attribute:data-result-actor"},
            {"path": "/status_transition", "selector": '.submission-row[data-ticket-status="closed"] .result-transition', "property": "attribute:data-status-transition"},
            {"path": "/cursor", "selector": "#submission-list", "property": "attribute:data-cursor"},
        ],
    ),
    "browser-pause-resume-stop": _state_recipe(
        [{"path": "/running_count", "selector": "#roster-list", "property": "attribute:data-running-count"}],
        [
            {"kind": "click", "selector": '[data-seat-action="pause"]', "path": "/pause"},
            {"kind": "wait", "milliseconds": 500, "path": "/pause_settle"},
            {"kind": "click", "selector": '[data-seat-action="stop"]', "path": "/open_stop"},
            {"kind": "click", "selector": "#confirm-stop", "path": "/stop"},
        ],
        [
            {"path": "/pause_requested", "selector": "#roster-list", "property": "attribute:data-last-pause-requested"},
            {"path": "/stop_requested", "selector": "#roster-list", "property": "attribute:data-last-stop-requested"},
            {"path": "/resume_control_count", "selector": '[data-seat-action="resume"]', "property": "count"},
        ],
        settle_milliseconds=1_000,
    ),
    "browser-clean-reconnect-new-door": _state_recipe(
        [{"path": "/connection_before", "selector": "#connection-pill", "property": "text"}],
        [{"kind": "click", "selector": "#rotate-door", "path": "/replace_door"}],
        [
            {"path": "/connection_pill", "selector": "#connection-pill", "property": "text"},
            {"path": "/connection_message", "selector": "#connection-message", "property": "text"},
            {"path": "/stale_offer_count", "selector": "#connection-card", "property": "attribute:data-stale-offer-count"},
            {"path": "/duplicate_identity_count", "selector": "#connection-card", "property": "attribute:data-duplicate-identity-count"},
            {"path": "/orphan_registration_count", "selector": "#connection-card", "property": "attribute:data-orphan-registration-count"},
            {"path": "/door_value", "selector": "#door", "property": "value"},
        ],
        settle_milliseconds=1_000,
    ),
    "browser-extension-join-state-status-loaded": _state_recipe(
        [{"path": "/status_field_count_before", "selector": "#connection-card [data-field]", "property": "count"}],
        [{"kind": "click", "selector": "#refresh-all", "path": "/refresh_status"}],
        [
            {"path": "/board_text", "selector": '#connection-card [data-field="board"]', "property": "text"},
            {"path": "/status_field_count", "selector": "#connection-card [data-field]", "property": "count"},
        ],
    ),
    "browser-extension-idempotent-reconnect": _state_recipe(
        [{"path": "/connection_count", "selector": "#connection-card", "property": "attribute:data-connection-count"}],
        [{"kind": "click", "selector": "#recover-seat", "path": "/recover"}],
        [
            {"path": "/connection_count", "selector": "#connection-card", "property": "attribute:data-connection-count"},
            {"path": "/registration_error_count", "selector": "#connection-card", "property": "attribute:data-registration-error-count"},
        ],
    ),
    "browser-extension-settings-navigation": _state_recipe(
        [{"path": "/navigation_current", "selector": "#helper", "property": "attribute:data-navigation-current"}],
        [{"kind": "click", "selector": "#open-quickstart", "path": "/settings_navigation"}],
        [
            {"path": "/navigation_current", "selector": "#helper", "property": "attribute:data-navigation-current"},
            {"path": "/helper_heading", "selector": "#helper-title", "property": "text"},
        ],
    ),
    "browser-final-quickstart-candidate-flow": _state_recipe(
        [{"path": "/helper_heading_before", "selector": "#helper-title", "property": "text"}],
        [{"kind": "click", "selector": "#open-quickstart", "path": "/open_quickstart"}],
        [
            {"path": "/helper_action", "selector": "#connect-helper", "property": "text"},
            {"path": "/helper_heading", "selector": "#helper-title", "property": "text"},
            {"path": "/raw_json_count", "selector": "#submission-list", "property": "attribute:data-raw-json-count"},
        ],
    ),
}


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
        raise RunnerError(EXIT_USAGE, "surface manifest needs exact aionui, fleet, mcp-app entries")
    normalized: dict[str, Any] = {}
    expected_adapters = {
        "aionui": "signed-aionui",
        "fleet": "pinned-process-artifact",
        "mcp-app": "pinned-signed-aionui-personal-mcp",
    }
    for surface_id, product in SURFACE_PRODUCTS.items():
        row = surfaces[surface_id]
        required = {"adapter", "target"} if surface_id == "aionui" else {
            "adapter", "target", "artifact"
        }
        if surface_id == "mcp-app":
            required.update({"candidate_manifest_url", "runtime"})
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
        if surface_id == "mcp-app":
            candidate_url = row["candidate_manifest_url"]
            candidate_parsed = urlsplit(str(candidate_url))
            if (
                not isinstance(candidate_url, str)
                or candidate_parsed.scheme != parsed.scheme
                or candidate_parsed.netloc != parsed.netloc
                or candidate_parsed.username
                or candidate_parsed.password
                or candidate_parsed.query
                or candidate_parsed.fragment
                or not candidate_parsed.path.endswith("/candidate.json")
            ):
                raise RunnerError(
                    EXIT_USAGE,
                    "Personal candidate_manifest_url must name same-origin candidate.json",
                )
            normalized_row["candidate_manifest_url"] = candidate_url
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
        if surface_id == "mcp-app":
            runtime = row["runtime"]
            if not isinstance(runtime, dict) or set(runtime) != {
                "artifact", "challenge_key", "pid_file", "receipt"
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
            for field in ("challenge_key", "pid_file", "receipt"):
                value = runtime[field]
                if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
                    raise RunnerError(EXIT_USAGE, f"Personal runtime {field} must be absolute")
                configured = Path(value).expanduser()
                if field == "challenge_key" and configured.is_symlink():
                    raise RunnerError(
                        EXIT_USAGE, "Personal runtime challenge_key must not be a symlink"
                    )
                resolved = configured.resolve()
                if resolved.is_relative_to(REPOSITORY_ROOT.resolve()):
                    raise RunnerError(EXIT_USAGE, f"Personal runtime {field} must stay outside checkout")
                if field == "challenge_key":
                    try:
                        status = resolved.stat()
                        key = resolved.read_bytes()
                    except OSError:
                        raise RunnerError(
                            EXIT_USAGE, "Personal runtime challenge_key is unavailable"
                        ) from None
                    if (
                        not resolved.is_file()
                        or status.st_uid != os.getuid()
                        or status.st_mode & 0o077
                    ):
                        raise RunnerError(
                            EXIT_USAGE, "Personal runtime challenge_key must be private"
                        )
                    if len(key) < 32:
                        raise RunnerError(
                            EXIT_USAGE, "Personal runtime challenge_key is too short"
                        )
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
    normalized["mcp-app"]["version"] = personal_version
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


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RunnerError(EXIT_BLOCKED, f"{label} is not readable JSON") from None
    if not isinstance(value, dict):
        raise RunnerError(EXIT_FAILED, f"{label} is not a JSON object")
    return value


def _aionui_typed_producers() -> dict[str, dict[str, Any]]:
    """Return the reviewed recorder recipe for every typed AionUi conjunct."""
    sys.path.insert(0, str(HERE))
    import harness

    catalog: dict[str, dict[str, Any]] = {}
    gap_map = _load_json_object(GAP_IMPLEMENTABILITY_MAP, "typed recipe map")
    for row in gap_map.get("rows", []):
        if not isinstance(row, dict) or row.get("surface") != "aionui":
            continue
        predicate = row.get("canonical_predicate")
        request = row.get("executable_request_template")
        source = row.get("trust_source_recipe_fragment")
        if not isinstance(predicate, dict):
            continue
        source_id = predicate.get("source_id")
        if isinstance(source_id, str):
            if not isinstance(request, dict) or not isinstance(source, dict):
                source = CURRENT_AIONUI_TYPED_RECIPES.get(source_id)
                if source is None:
                    continue
                recipe = source["recipe"]
                request = {
                    "recorder": {
                        "source_id": source_id,
                        "before": recipe["before"],
                        "action": recipe["actions"],
                        "after": recipe["after"],
                    }
                }
            catalog[source_id] = {
                "observation_id": row.get("observation_id"),
                "predicate": predicate,
                "recorder": request.get("recorder"),
                "adapter": source,
            }

    delta = _load_json_object(TYPED_PREDICATE_DELTA, "typed predicate delta")
    for row in delta.get("proposals", []):
        if not isinstance(row, dict):
            continue
        adapter = row.get("adapter")
        predicate = row.get("canonical_predicate")
        request = row.get("executable_request")
        if (
            not isinstance(adapter, dict)
            or adapter.get("surface") != "aionui"
            or not isinstance(predicate, dict)
            or not isinstance(request, dict)
        ):
            continue
        source_id = predicate.get("source_id")
        if isinstance(source_id, str) and source_id not in catalog:
            catalog[source_id] = {
                "observation_id": row.get("id"),
                "predicate": predicate,
                "recorder": request.get("recorder"),
                "adapter": {
                    key: adapter[key]
                    for key in ("adapter", "recipe", "select_allowlist")
                    if key in adapter
                },
            }

    required: dict[str, tuple[str, dict[str, Any]]] = {}
    for identifier in (
        *harness.SEQUENCE,
        *sorted(harness.REQUIRED_INVENTORY),
        *harness.REQUIRED_FINAL_GATES,
    ):
        if harness._surface_for_identifier(identifier) != "aionui":
            continue
        for conjunct in harness._canonical_typed_conjuncts(identifier):
            source_id = conjunct.get("source_id")
            if not isinstance(source_id, str) or source_id in required:
                raise RunnerError(EXIT_FAILED, "AionUi typed source IDs are not unique")
            required[source_id] = (identifier, conjunct)
    if set(catalog) & set(required) != set(required):
        missing = ", ".join(sorted(set(required) - set(catalog)))
        raise RunnerError(EXIT_FAILED, f"AionUi typed producer recipes are missing: {missing}")
    catalog = {source_id: catalog[source_id] for source_id in required}
    for source_id, (identifier, conjunct) in required.items():
        row = catalog[source_id]
        if source_id in CURRENT_AIONUI_TYPED_RECIPES:
            row["predicate"] = conjunct
        recorder = row["recorder"]
        adapter = row["adapter"]
        if (
            row["observation_id"] != identifier
            or row["predicate"] != conjunct
            or not isinstance(recorder, dict)
            or recorder.get("source_id") != source_id
            or not isinstance(adapter, dict)
            or adapter.get("adapter") not in {
                "trusted_browser_state_v1", "aionui_assistant_binding_v1"
            }
            or set(adapter) != {"adapter", "recipe", "select_allowlist"}
        ):
            raise RunnerError(
                EXIT_FAILED, f"AionUi typed producer contract differs: {source_id}"
            )
    return catalog


def _capture_plan_page_urls(plan: dict[str, Any]) -> dict[str, str]:
    pages: dict[str, str] = {}
    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise RunnerError(EXIT_FAILED, "capture plan commands are invalid")
    for command in commands:
        if not isinstance(command, list):
            raise RunnerError(EXIT_FAILED, "capture plan command is invalid")
        try:
            identifier = command[command.index("--observation") + 1]
            page = command[command.index("--page") + 1]
        except (ValueError, IndexError):
            raise RunnerError(EXIT_FAILED, "capture plan command lacks page binding") from None
        if not isinstance(identifier, str) or not isinstance(page, str) or identifier in pages:
            raise RunnerError(EXIT_FAILED, "capture plan page bindings are invalid")
        pages[identifier] = page
    return pages


def _copy_typed_module(destination: Path) -> tuple[Path, str]:
    if destination.is_relative_to(REPOSITORY_ROOT.resolve()):
        raise RunnerError(EXIT_USAGE, "typed directory must live outside the checkout")
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    command = destination / "typed_evidence.py"
    temporary = destination / f".typed-evidence-{uuid.uuid4().hex}"
    shutil.copyfile(TYPED_SOURCE, temporary)
    os.chmod(temporary, 0o700)
    os.replace(temporary, command)
    return command, hashlib.sha256(command.read_bytes()).hexdigest()


def _installed_assistant_manifest(value: str) -> Path:
    raw = Path(value).expanduser()
    installed = raw.resolve()
    candidate = REPOSITORY_ROOT / "tools/aionui-extension/aion-extension.json"
    if (
        not raw.is_absolute()
        or raw.is_symlink()
        or not installed.is_file()
        or installed.is_relative_to(REPOSITORY_ROOT.resolve())
        or installed.read_bytes() != candidate.read_bytes()
    ):
        raise RunnerError(
            EXIT_USAGE, "installed assistant manifest must be an external exact candidate copy"
        )
    for context in ("worker.md", "reviewer.md"):
        installed_context = installed.parent / "contexts" / context
        candidate_context = candidate.parent / "contexts" / context
        if (
            installed_context.is_symlink()
            or not installed_context.is_file()
            or installed_context.read_bytes() != candidate_context.read_bytes()
        ):
            raise RunnerError(
                EXIT_USAGE, "installed assistant contexts must match the candidate"
            )
    return installed


def _bridge_artifact_provenance(command_value: str, wheel_value: str) -> dict[str, str]:
    command = Path(command_value).expanduser().resolve()
    wheel = Path(wheel_value).expanduser().resolve()
    root = REPOSITORY_ROOT.resolve()
    if (
        not command.is_file()
        or not os.access(command, os.X_OK)
        or command.is_relative_to(root)
        or command.stat().st_mode & 0o022
        or not wheel.is_file()
        or wheel.is_relative_to(root)
        or wheel.stat().st_mode & 0o022
    ):
        raise RunnerError(
            EXIT_USAGE,
            "bridge command and exact candidate wheel must be external verifier-owned files",
        )
    completed = subprocess.run(
        [str(command), "--version"], text=True, capture_output=True,
        check=False, timeout=10,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    version = completed.stdout.strip()
    if completed.returncode or not SAFE_VERSION.fullmatch(version):
        raise RunnerError(EXIT_BLOCKED, "bridge command version is unavailable")
    return {
        "version": version,
        "wheel_path": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "command": str(command),
        "command_sha256": hashlib.sha256(command.read_bytes()).hexdigest(),
    }


def prepare_aionui_typed(args: argparse.Namespace) -> int:
    """Install and wire verifier-owned recorders for the 21 AionUi typed rows."""
    observer_dir = Path(args.observer).expanduser().resolve()
    config = _load_observer_config(observer_dir)
    evidence = Path(args.evidence).expanduser().resolve()
    plan_path = evidence / "capture-plan.json"
    plan = _load_json_object(plan_path, "capture plan")
    aionui = config["surfaces"]["aionui"]
    if plan.get("candidate_commit") != aionui["candidate_commit"]:
        raise RunnerError(EXIT_FAILED, "capture plan candidate does not match observer")
    typed_by_id = plan.get("typed_evidence")
    if not isinstance(typed_by_id, dict):
        raise RunnerError(EXIT_FAILED, "capture plan typed evidence is invalid")
    pages = _capture_plan_page_urls(plan)
    producers = _aionui_typed_producers()
    sys.path.insert(0, str(HERE))
    import harness

    installed_manifest = _installed_assistant_manifest(args.installed_manifest)
    bridge_provenance = _bridge_artifact_provenance(
        args.bridge_command, args.bridge_wheel
    )
    typed_dir = Path(args.dir).expanduser().resolve()
    trust_path = typed_dir / "trust.json"
    typed_plan_path = evidence / "aionui-typed-plan.json"
    if trust_path.exists() or typed_plan_path.exists():
        raise RunnerError(
            EXIT_USAGE, "typed pipeline already exists; use a fresh verifier/evidence directory"
        )
    recorder, recorder_sha256 = _copy_typed_module(typed_dir)
    observer = _observer_command(observer_dir)
    observer_config = observer_dir / "observer.json"
    candidate_manifest = REPOSITORY_ROOT / "tools/aionui-extension/aion-extension.json"
    observer_id = config.get("observer_id")
    if not isinstance(observer_id, str) or not observer_id:
        raise RunnerError(EXIT_BLOCKED, "installed observer lacks observer_id")
    state_sources: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for source_id, producer in producers.items():
        identifier = producer["observation_id"]
        conjuncts = list(harness._canonical_typed_conjuncts(identifier))
        typed_rows = typed_by_id.get(identifier)
        if not isinstance(typed_rows, list) or len(typed_rows) != len(conjuncts):
            raise RunnerError(EXIT_FAILED, f"typed references differ: {identifier}")
        typed_index = next(
            index for index, conjunct in enumerate(conjuncts, start=1)
            if conjunct.get("source_id") == source_id
        )
        reference = typed_rows[typed_index - 1]
        adapter = producer["adapter"]
        source = {
            "adapter": adapter["adapter"],
            "provenance": "verifier-owned-aionui-browser",
            "runtime_id": f"{observer_id}-aionui",
            "surface": "aionui",
            "board_id": aionui["target"]["board_id"],
            "candidate_commit": aionui["candidate_commit"],
            "command": str(observer),
            "command_sha256": hashlib.sha256(observer.read_bytes()).hexdigest(),
            "config_path": str(observer_config),
            "config_sha256": hashlib.sha256(observer_config.read_bytes()).hexdigest(),
            "base_url": aionui["target"]["base_url"],
            "page_url": pages[identifier],
            "recipe": adapter["recipe"],
            "env": {},
            "timeout_seconds": 180,
            "select_allowlist": adapter["select_allowlist"],
            "bridge_provenance": bridge_provenance,
        }
        if adapter["adapter"] == "aionui_assistant_binding_v1":
            source.update({
                "candidate_manifest": str(candidate_manifest),
                "candidate_manifest_sha256": hashlib.sha256(
                    candidate_manifest.read_bytes()
                ).hexdigest(),
                "installed_manifest": str(installed_manifest),
            })
        state_sources[source_id] = source
        record_id = f"{identifier}-{typed_index}"
        records.append({
            "record_id": record_id,
            "observation_id": identifier,
            "typed_index": typed_index,
            "reference": reference,
            "conjunct": producer["predicate"],
            "recorder": producer["recorder"],
        })
    trust = {
        "schema_version": SCHEMA_VERSION,
        "verifier_id": observer_id,
        "trusted_module_path": str(recorder),
        "module_sha256": recorder_sha256,
        "candidate_checkout_root": str(REPOSITORY_ROOT.resolve()),
        "candidate_commit": aionui["candidate_commit"],
        "board_id": aionui["target"]["board_id"],
        "max_age_seconds": min(int(config.get("max_age_s", 300)), 86_400),
        "active_evidence_key": "aionui-run-key",
        "evidence_keys": {"aionui-run-key": secrets.token_hex(32)},
        "http_sources": {},
        "mcp_sources": {},
        "receipt_sources": {},
        "log_sources": {},
        "state_sources": state_sources,
        "replay_guard": {"path": str(typed_dir / "replay.log"), "consume": True},
    }
    trust_path.write_text(json.dumps(trust, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(trust_path, 0o600)
    typed_plan = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "aionui_typed_evidence_plan",
        "candidate_commit": aionui["candidate_commit"],
        "board_id": aionui["target"]["board_id"],
        "evidence_root": str(evidence),
        "recorder": str(recorder),
        "trust": str(trust_path),
        "records": sorted(records, key=lambda row: (row["observation_id"], row["typed_index"])),
    }
    typed_plan["commands"] = [
        [
            sys.executable, str(Path(__file__).resolve()), "record-aionui-typed",
            "--plan", str(typed_plan_path), "--record", row["record_id"],
        ]
        for row in typed_plan["records"]
    ]
    typed_plan_path.write_text(
        json.dumps(typed_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    json.dump(
        {
            "plan": str(typed_plan_path),
            "rows": len({row["observation_id"] for row in records}),
            "records": len(records),
            "adapters": {
                adapter: sum(source["adapter"] == adapter for source in state_sources.values())
                for adapter in ("trusted_browser_state_v1", "aionui_assistant_binding_v1")
            },
        },
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return EXIT_OK


def _bounded_evidence_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise RunnerError(EXIT_FAILED, "typed evidence path is not bounded")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise RunnerError(EXIT_FAILED, "typed evidence path is not bounded")
    return path


def record_aionui_typed(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    plan = _load_json_object(plan_path, "AionUi typed plan")
    if set(plan) != {
        "schema_version", "artifact_kind", "candidate_commit", "board_id",
        "evidence_root", "recorder", "trust", "records", "commands",
    } or plan.get("schema_version") != SCHEMA_VERSION or plan.get(
        "artifact_kind"
    ) != "aionui_typed_evidence_plan":
        raise RunnerError(EXIT_FAILED, "AionUi typed plan fields do not match schema")
    records = plan["records"]
    if not isinstance(records, list):
        raise RunnerError(EXIT_FAILED, "AionUi typed plan records are invalid")
    matches = [row for row in records if isinstance(row, dict) and row.get("record_id") == args.record]
    if len(matches) != 1:
        raise RunnerError(EXIT_USAGE, "typed record ID is not unique in the plan")
    row = matches[0]
    reference = row.get("reference")
    if not isinstance(reference, dict) or set(reference) != {
        "evidence", "run_id", "action_id", "entity", "causal_index"
    }:
        raise RunnerError(EXIT_FAILED, "typed reference fields do not match schema")
    evidence_root = Path(str(plan["evidence_root"])).resolve()
    if plan_path.parent != evidence_root:
        raise RunnerError(EXIT_FAILED, "typed plan is outside its evidence root")
    output = _bounded_evidence_path(evidence_root, reference["evidence"])
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RunnerError(EXIT_USAGE, "typed evidence already exists")
    recorder = Path(str(plan["recorder"])).resolve()
    trust_path = Path(str(plan["trust"])).resolve()
    trust = _load_json_object(trust_path, "typed trust")
    if (
        recorder != Path(str(trust.get("trusted_module_path"))).resolve()
        or not recorder.is_file()
        or not os.access(recorder, os.X_OK)
        or recorder.is_relative_to(REPOSITORY_ROOT.resolve())
        or hashlib.sha256(recorder.read_bytes()).hexdigest() != trust.get("module_sha256")
        or trust_path.stat().st_mode & 0o077
    ):
        raise RunnerError(EXIT_BLOCKED, "typed recorder or trust binding is invalid")
    request = {
        "schema_version": SCHEMA_VERSION,
        "kind": row["conjunct"]["kind"],
        "context": {
            "observation_id": row["observation_id"],
            "run_id": reference["run_id"],
            "action_id": reference["action_id"],
            "entity": reference["entity"],
            "surface": "aionui",
            "board_id": plan["board_id"],
            "candidate_commit": plan["candidate_commit"],
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "causal_index": reference["causal_index"],
        },
        "recorder": row["recorder"],
    }
    request_path = evidence_root / "typed-requests" / f"{row['record_id']}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(request_path, 0o600)
    completed = subprocess.run(
        [
            str(recorder), "record", "--request", str(request_path),
            "--trust", str(trust_path), "--output", str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
        cwd=recorder.parent,
        env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode:
        tail = completed.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise RunnerError(
            EXIT_BLOCKED, f"typed recorder exited {completed.returncode}: {tail[0][:300]}"
        )
    if not output.is_file() or output.stat().st_mode & 0o077:
        raise RunnerError(EXIT_FAILED, "typed recorder did not write private evidence")
    json.dump(
        {
            "record_id": row["record_id"],
            "observation_id": row["observation_id"],
            "typed_index": row["typed_index"],
            "evidence": reference["evidence"],
        },
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return EXIT_OK


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
        boundary = harness._catalogue_boundary_reason(identifier)
        if boundary is not None:
            raise RunnerError(
                EXIT_BLOCKED,
                f"catalogue boundary {identifier} cannot be captured: {boundary}",
            )
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
    pairing_proof = None
    if args.pairing_proof:
        if args.surface != "aionui":
            raise RunnerError(
                EXIT_USAGE, "--pairing-proof is valid only for the aionui surface"
            )
        try:
            proof_value = json.loads(
                Path(args.pairing_proof).expanduser().read_text(encoding="utf-8")
            )
            target_port = urlsplit(args.target).port
            pairing_proof = validate_pairing_proof(
                proof_value,
                expected_port=target_port,
                expected_tool_sha256=tool_sha256(),
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RunnerError(EXIT_USAGE, f"invalid --pairing-proof: {exc}") from None
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
    if pairing_proof is not None:
        receipt["pairing"] = pairing_proof
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
    summary = {
        "observation_id": payload["observation_id"],
        "evidence": reference,
        "host_identity_evidence": surface_host_reference,
        "page_url": payload["page_url"],
        "screenshot_sha256": screenshot_artifact["sha256"],
        "snapshot_sha256": snapshot_artifact["sha256"],
    }
    if pairing_proof is not None:
        summary["pairing"] = pairing_proof
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
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
            typed_evidence_evaluator_command=(
                Path(args.typed_evaluator).expanduser().resolve()
                if args.typed_evaluator else None
            ),
            typed_evidence_trust=(
                Path(args.typed_trust).expanduser().resolve()
                if args.typed_trust else None
            ),
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

    typed_plan = sub.add_parser(
        "prepare-aionui-typed",
        help="install and plan verifier-owned typed recorders for the 21 AionUi rows",
    )
    typed_plan.add_argument("--observer", required=True)
    typed_plan.add_argument("--evidence", required=True)
    typed_plan.add_argument(
        "--dir", required=True, help="fresh verifier-owned typed-evidence directory"
    )
    typed_plan.add_argument(
        "--installed-manifest",
        required=True,
        help="external installed aion-extension.json with matching contexts",
    )
    typed_plan.add_argument(
        "--bridge-command", required=True,
        help="external installed exact-candidate pursers-wait-bridge executable",
    )
    typed_plan.add_argument(
        "--bridge-wheel", required=True,
        help="external exact-candidate pursers-wait-bridge wheel",
    )
    typed_plan.set_defaults(handler=prepare_aionui_typed)

    typed_record = sub.add_parser(
        "record-aionui-typed", help="execute one record from a prepared AionUi typed plan"
    )
    typed_record.add_argument("--plan", required=True)
    typed_record.add_argument("--record", required=True)
    typed_record.set_defaults(handler=record_aionui_typed)

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
    shot.add_argument(
        "--pairing-proof",
        help="JSON proof printed by pair_aionui.py for this AionUi core",
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
    check_report.add_argument("--typed-evaluator")
    check_report.add_argument("--typed-trust")
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
