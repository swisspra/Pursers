from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

EXTENSION_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_BOARDS = frozenset({"pursers", "fullplatts", "mi-mcp-prd"})
MUTATION_OPT_IN = "I_UNDERSTAND_SANDBOX_ONLY"
MAX_REPORT_BYTES = 1_000_000
MAX_EVIDENCE_FILE_BYTES = 10_000_000
MAX_EVIDENCE_TOTAL_BYTES = 100_000_000
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SEQUENCE = (
    "fresh_install",
    "door_connect",
    "team_setup",
    "six_workers_two_reviewers",
    "ticket_offer_claim",
    "ticket_submit_independent_review",
    "result_visible",
    "pause_resume_stop",
    "clean_reconnect_after_rotation",
)
CURRENT_OPERATOR_TIERS = {
    "gemini_worker": 1,
    "glm_worker": 1,
    "qwen_worker": 2,
    "codex_worker": 2,
    "reviewer": 2,
}
REQUIRED_MUTATION_CAPABILITIES = frozenset(
    {
        "door_join",
        "door_rotation",
        "team_lifecycle",
        "seat_lifecycle",
        "ticket_lifecycle",
        "result_visibility",
    }
)
REQUIRED_INVENTORY = frozenset(
    {
        # Exact surface/state IDs from approved inventory TK-f8a62bab8d05.
        "dashboard-ui.logic",
        "dashboard-ui.styles",
        "dashboard-ui.shell",
        "fleet-dashboard.surface",
        "extension-join.surface",
        "personal-mcp.surface",
        "dashboard-ui.state.empty-tickets",
        "dashboard-ui.state.loading",
        "dashboard-ui.state.error",
        "dashboard-ui.state.permission-denied",
        "dashboard-ui.state.stale",
        "dashboard-ui.state.search-empty",
        "fleet-dashboard.state.empty-centrals",
        "fleet-dashboard.state.loading-board",
        "fleet-dashboard.state.error-board",
        "fleet-dashboard.state.offline",
        "fleet-dashboard.state.bounded",
        "fleet-dashboard.state.truncated-tickets",
        "fleet-dashboard.state.edit-paused",
        "fleet-dashboard.state.empty-workers",
        "fleet-dashboard.state.empty-agents",
        "fleet-dashboard.state.routes-unavailable",
        "extension-join.state.initial",
        "extension-join.state.joining",
        "extension-join.state.joined",
        "extension-join.state.error",
        "extension-join.state.bridge-missing",
        "extension-join.state.status-loaded",
        "extension-join.state.status-empty",
        "personal-mcp.state.board-empty",
        "personal-mcp.state.not-onboarded",
        "personal-mcp.state.ticket-not-found",
        "personal-mcp.state.memory-empty",
        "personal-mcp.state.fleet-unavailable",
        "personal-mcp.state.links-empty",
        # Independently observable extension items.
        "extension.settings-navigation",
        "extension.join-form",
        "extension.join-progress",
        "extension.bounded-errors",
        "extension.redacted-status-card",
        "extension.worker-preset-codex",
        "extension.worker-preset-claude",
        "extension.reviewer-preset-codex",
        "extension.reviewer-preset-claude",
        "extension.environment-free-mcp-registration",
        "extension.idempotent-reconnect",
        # Personal dashboard shell and Today items.
        "personal.connection-banner",
        "personal.board-identity",
        "personal.data-provenance",
        "personal.health",
        "personal.refresh",
        "personal.theme",
        "personal.keyboard-tabs",
        "personal.search-results",
        "personal.search-no-results",
        "personal.today-status-metrics",
        "personal.today-current-work",
        "personal.today-active-agents",
        "personal.today-latest-handoff",
        "personal.today-important-pinned-note",
        "personal.today-recent-activity",
        # Personal Work items.
        "personal.work-total",
        "personal.work-status-groups",
        "personal.work-ownership",
        "personal.work-priority",
        "personal.work-lease",
        "personal.work-rejection",
        "personal.work-abandonment",
        "personal.work-review-readiness",
        "personal.work-no-ticket",
        # Personal Agents items.
        "personal.agents-total-live",
        "personal.agents-role",
        "personal.agents-platform",
        "personal.agents-focus",
        "personal.agents-current-ticket",
        "personal.agents-idle-lease",
        "personal.agents-duplicate-name",
        "personal.agents-duplicate-identity",
        "personal.agents-stale",
        "personal.agents-empty",
        # Personal Fleet, Links, Activity, and MCP data-source items.
        "personal.fleet-online-busy-available-stale",
        "personal.fleet-registry-warning",
        "personal.fleet-projects",
        "personal.fleet-project-ticket-counts",
        "personal.fleet-shared-pool",
        "personal.fleet-project-seats",
        "personal.fleet-truncation",
        "personal.fleet-unavailable",
        "personal.fleet-empty",
        "personal.links-source-label",
        "personal.links-node-edge-totals",
        "personal.links-edge-types",
        "personal.links-pinned",
        "personal.links-truncation",
        "personal.links-unavailable",
        "personal.links-empty",
        "personal.activity-scope",
        "personal.activity-bounded-feed",
        "personal.activity-cursor",
        "personal.activity-dropped-events",
        "personal.activity-has-more-resync",
        "personal.activity-stale",
        "personal.activity-error",
        "personal.activity-offline",
        "personal.activity-empty",
        "personal.source-board-snapshot",
        "personal.source-fleet-snapshot",
        "personal.source-link-snapshot",
        "personal.source-board-event-feed",
        # Fleet dashboard shell, hubs, and board-detail items.
        "fleet.central-availability-isolation",
        "fleet.updated-state",
        "fleet.search-results",
        "fleet.search-no-results",
        "fleet.theme",
        "fleet.density",
        "fleet.keyboard-help",
        "fleet.refresh-pause-resume",
        "fleet.pool-online",
        "fleet.pool-busy",
        "fleet.pool-available",
        "fleet.pool-stale",
        "fleet.board-cards",
        "fleet.ticket-counts",
        "fleet.active-ticket-rows",
        "fleet.agent-pool",
        "fleet.agent-current-claims",
        "fleet.agent-duplicate-names",
        "fleet.agent-retired-stale-drawer",
        "fleet.board-detail-metadata",
        "fleet.board-detail-activity",
        "fleet.board-detail-truncation",
        "fleet.hub-overview",
        "fleet.hub-boards",
        "fleet.hub-agents",
        "fleet.hub-operations",
        "fleet.tab-tickets",
        "fleet.tab-timeline",
        "fleet.tab-changes",
        "fleet.tab-flow",
        "fleet.tab-routes",
        "fleet.default-central-aliases",
        "fleet.unknown-route-recovery",
        "fleet.protocol-overhead",
        "fleet.coordinator-configuration",
        "fleet.worker-management",
        "fleet.intake",
        "fleet.findings",
        # Fleet configuration, doors, project, release, and operation items.
        "fleet.config-seat-inventory",
        "fleet.config-discovery-import-conflicts",
        "fleet.config-add-update-preview",
        "fleet.config-exact-diff-confirmation",
        "fleet.config-bridge-versions",
        "fleet.config-doctor",
        "fleet.config-tier-skill-role-capabilities",
        "fleet.config-current-offers",
        "fleet.config-dispatch-policy-gaps-history",
        "fleet.config-registry-worktrees",
        "fleet.doors-project-rows",
        "fleet.doors-key-id",
        "fleet.doors-expiry",
        "fleet.doors-connected-seats",
        "fleet.doors-copy",
        "fleet.doors-rotation-warning",
        "fleet.doors-disabled",
        "fleet.doors-unconfigured",
        "fleet.doors-error",
        "fleet.doors-secret-free-output",
        "fleet.add-project-registry",
        "fleet.add-project-board",
        "fleet.add-project-principals",
        "fleet.add-project-policy",
        "fleet.add-project-clone-steps",
        "fleet.add-project-idempotent-rerun",
        "fleet.add-project-one-time-doors",
        "fleet.add-project-partial-failure",
        "fleet.add-project-authorization-error",
        "fleet.release-manifest",
        "fleet.release-tag",
        "fleet.release-ci",
        "fleet.release-pypi",
        "fleet.release-github",
        "fleet.release-central",
        "fleet.release-restart-checklist",
        "fleet.release-immutable-confirmation-plan",
        "fleet.operations-job-progress",
        "fleet.operations-job-result",
        "fleet.operations-rollback-failure",
        "fleet.operations-disabled-controls",
        "fleet.operations-unavailable-services",
    }
)
REQUIRED_SUITES = {
    "repository-python": "python3 tools/ci_manifest.py run",
    "extension-routes-node": "node --test tools/aionui-extension/tests/routes.test.cjs",
    "extension-door-node": "node --test tools/aionui-extension/tests/door_adapter.test.cjs",
    "extension-team-node": "node --test tools/aionui-extension/tests/team_adapter.test.cjs",
    "dashboard-typecheck": "cd tools/dashboard-ui && NODE_ENV= npm run typecheck",
    "dashboard-build": "cd tools/dashboard-ui && NODE_ENV= npm run build",
    "repository-leak-scan": "python3 tools/leak_scan.py",
    "candidate-diff-check": "git diff --check",
}
SENSITIVE_KEY = re.compile(r"(?:authorization|bearer|cookie|door|jwt|secret|token)", re.I)
SECRET_VALUE = re.compile(
    r"(?:prs1\.[A-Za-z0-9._-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|Bearer\s+\S+)",
    re.I,
)
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)")
EVIDENCE_REFERENCE = re.compile(r"[A-Za-z0-9._/-]{1,240}")


class AcceptanceError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryCapabilities:
    api_routes: tuple[str, ...]
    assistants: tuple[str, ...]
    personal_views: tuple[str, ...]
    capabilities: tuple[str, ...]
    missing_mutation_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class LiveTarget:
    base_url: str
    board_id: str | None = None


def _semantic_capabilities(routes: tuple[str, ...]) -> set[str]:
    lowered = {route.lower() for route in routes}
    capabilities = {"read_only_discovery"}
    if "/pursers/join" in lowered:
        capabilities.add("door_join")
    if "/pursers/status" in lowered:
        capabilities.add("seat_status")
    if "/pursers/onboarding/rotate" in lowered:
        capabilities.add("door_rotation")
    return capabilities


def discover_repository_capabilities(root: Path = EXTENSION_ROOT) -> RepositoryCapabilities:
    manifest = json.loads((root / "aion-extension.json").read_text(encoding="utf-8"))
    contributes = manifest.get("contributes", {})
    webui = contributes.get("webui", {})
    routes = tuple(
        sorted(
            route["path"]
            for route in webui.get("apiRoutes", [])
            if isinstance(route, dict) and isinstance(route.get("path"), str)
        )
    )
    assistants = tuple(
        sorted(
            assistant["id"]
            for assistant in contributes.get("assistants", [])
            if isinstance(assistant, dict) and isinstance(assistant.get("id"), str)
        )
    )
    dashboard_entry = root.parent / "dashboard-ui" / "dashboard-entry.html"
    dashboard_text = dashboard_entry.read_text(encoding="utf-8") if dashboard_entry.exists() else ""
    personal_views = tuple(sorted(set(re.findall(r'data-view="([a-z-]+)"', dashboard_text))))
    capabilities = _semantic_capabilities(routes)
    missing = tuple(sorted(REQUIRED_MUTATION_CAPABILITIES - capabilities))
    return RepositoryCapabilities(
        api_routes=routes,
        assistants=assistants,
        personal_views=personal_views,
        capabilities=tuple(sorted(capabilities)),
        missing_mutation_capabilities=missing,
    )


def validate_live_target(base_url: str, board_id: str | None = None) -> LiveTarget:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise AcceptanceError("host URL must use http or https")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AcceptanceError("host URL must be loopback-only")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AcceptanceError("host URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise AcceptanceError("host URL must be an origin without a path")
    if board_id is not None:
        if board_id in PRODUCTION_BOARDS:
            raise AcceptanceError("production board refused")
        if not re.fullmatch(r"(?:sandbox|test)-[A-Za-z0-9._-]{1,71}", board_id):
            raise AcceptanceError("board must have a sandbox- or test- prefix")
    return LiveTarget(base_url=base_url.rstrip("/"), board_id=board_id)


def require_mutation_opt_in(value: str | None) -> None:
    if value != MUTATION_OPT_IN:
        raise AcceptanceError(
            f"mutation opt-in must equal {MUTATION_OPT_IN}"
        )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return SECRET_VALUE.sub("[REDACTED]", value)
    return value


def probe_extension_status(target: LiveTarget, timeout_s: float = 3.0) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}))
    endpoint = urljoin(f"{target.base_url}/", "pursers/status")
    with opener.open(endpoint, timeout=timeout_s) as response:
        final_target = validate_live_target(response.geturl().rsplit("/pursers/status", 1)[0])
        if final_target.base_url != target.base_url:
            raise AcceptanceError("status probe redirected to a different origin")
        payload = json.loads(response.read(1_048_577).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise AcceptanceError("status response is not the Pursers status contract")
    seats = payload.get("seats") if isinstance(payload.get("seats"), list) else []
    return {
        "ok": payload["ok"],
        "push_mode": payload.get("push_mode", "unknown"),
        "seat_count": len(seats),
        "roles": sorted(
            {
                seat.get("role")
                for seat in seats
                if isinstance(seat, dict) and isinstance(seat.get("role"), str)
            }
        ),
    }


def validate_evidence_report(
    report_path: Path,
    target: LiveTarget,
    capabilities: RepositoryCapabilities,
    candidate_commit: str,
) -> dict[str, Any]:
    require_mutation_opt_in(os.environ.get("PURSERS_HOME_ACCEPTANCE_MUTATE"))
    if target.board_id is None:
        raise AcceptanceError("sandbox board is required for mutation evidence")
    if (
        not isinstance(candidate_commit, str)
        or not FULL_SHA.fullmatch(candidate_commit)
    ):
        raise AcceptanceError("candidate commit must be a full lowercase 40-hex SHA")
    if capabilities.missing_mutation_capabilities:
        missing = ", ".join(capabilities.missing_mutation_capabilities)
        raise AcceptanceError(f"sibling interface capabilities unavailable: {missing}")
    evidence_root = report_path.parent.resolve(strict=True)
    resolved_report = _resolve_evidence_file(
        evidence_root, report_path.name, "evidence report", MAX_REPORT_BYTES
    )
    raw = resolved_report.read_text(encoding="utf-8")
    if SECRET_VALUE.search(raw) or PRIVATE_PATH.search(raw):
        raise AcceptanceError("evidence report contains a secret or private path")
    report = json.loads(raw)
    if report.get("schema_version") != 1:
        raise AcceptanceError("evidence schema_version must be 1")
    if report.get("evidence_kind") != "real_browser_host":
        raise AcceptanceError("evidence_kind must be real_browser_host")
    if report.get("mocked") is not False or report.get("synthetic") is not False:
        raise AcceptanceError("mocked or synthetic evidence cannot establish GUI acceptance")
    if report.get("target") != {"base_url": target.base_url, "board_id": target.board_id}:
        raise AcceptanceError("evidence target does not match the explicit sandbox target")
    host = report.get("host")
    if not isinstance(host, dict) or host.get("product") != "AionUi":
        raise AcceptanceError("evidence must identify the real AionUi host")
    _require_exact_text(host.get("version"), "host version")
    _require_exact_text(host.get("build"), "host build")
    steps = report.get("steps")
    if not isinstance(steps, list) or len(steps) != len(SEQUENCE):
        raise AcceptanceError("every acceptance step must pass exactly once")
    passed_steps = _passed_evidence_items(steps, "acceptance step")
    if set(passed_steps) != set(SEQUENCE):
        raise AcceptanceError("every acceptance step must pass exactly once")
    inventory = report.get("inventory")
    if not isinstance(inventory, list):
        raise AcceptanceError("dashboard inventory evidence is required")
    passed_inventory = _passed_evidence_items(inventory, "dashboard inventory item")
    inventory_ids = set(passed_inventory)
    missing_inventory = REQUIRED_INVENTORY - inventory_ids
    unexpected_inventory = inventory_ids - REQUIRED_INVENTORY
    if missing_inventory or unexpected_inventory:
        raise AcceptanceError(
            "dashboard inventory does not match the authoritative set: "
            f"missing={sorted(missing_inventory)}, unexpected={sorted(unexpected_inventory)}"
        )
    suites = report.get("suites")
    if not isinstance(suites, list) or not suites:
        raise AcceptanceError("existing suite evidence is required")
    seen_suites: set[str] = set()
    suite_references: list[str] = []
    for suite in suites:
        if not isinstance(suite, dict) or suite.get("status") != "passed":
            raise AcceptanceError("every recorded suite must pass")
        name = suite.get("name")
        command = suite.get("command")
        commit = suite.get("commit")
        reference = suite.get("evidence")
        if not isinstance(name, str) or not name.strip():
            raise AcceptanceError("every suite needs a nonempty exact name")
        if name in seen_suites:
            raise AcceptanceError("duplicate suite name")
        seen_suites.add(name)
        if command != REQUIRED_SUITES.get(name):
            raise AcceptanceError(
                f"suite {name} command does not match the required exact command"
            )
        if (
            commit != candidate_commit
            or not isinstance(commit, str)
            or not FULL_SHA.fullmatch(commit)
        ):
            raise AcceptanceError("every suite commit must equal the full candidate commit")
        if not isinstance(reference, str) or not EVIDENCE_REFERENCE.fullmatch(reference):
            raise AcceptanceError("every suite needs a bounded relative evidence reference")
        if reference.startswith("/") or ".." in Path(reference).parts:
            raise AcceptanceError("every suite evidence reference must stay relative")
        suite_references.append(reference)
    if seen_suites != set(REQUIRED_SUITES):
        raise AcceptanceError(
            "suite set does not match required suites: "
            f"missing={sorted(set(REQUIRED_SUITES) - seen_suites)}, "
            f"unexpected={sorted(seen_suites - set(REQUIRED_SUITES))}"
        )
    _validate_evidence_artifacts(
        evidence_root,
        [*passed_steps.values(), *passed_inventory.values(), *suite_references],
        excluded=resolved_report,
    )
    if report.get("all_existing_suites_passed") is not True:
        raise AcceptanceError("all_existing_suites_passed must be true")
    return {
        "evidence_kind": report["evidence_kind"],
        "host_product": host["product"],
        "host_version": host["version"],
        "host_build": host["build"],
        "candidate_commit": candidate_commit,
        "steps_passed": len(passed_steps),
        "inventory_passed": len(passed_inventory),
        "suites_passed": len(suites),
    }


def _require_exact_text(value: Any, label: str) -> str:
    rejected = {"unknown", "unset", "n/a", "exact_version", "exact_build"}
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 for character in value)
        or value.lower() in rejected
    ):
        raise AcceptanceError(f"{label} must be a nonempty exact value")
    return value


def _resolve_evidence_file(
    evidence_root: Path,
    reference: str,
    label: str,
    max_bytes: int = MAX_EVIDENCE_FILE_BYTES,
) -> Path:
    if not EVIDENCE_REFERENCE.fullmatch(reference):
        raise AcceptanceError(f"{label} needs a bounded relative evidence reference")
    if reference.startswith("/") or ".." in Path(reference).parts:
        raise AcceptanceError(f"{label} evidence reference must stay relative")
    try:
        candidate = (evidence_root / reference).resolve(strict=True)
        candidate.relative_to(evidence_root)
    except (FileNotFoundError, RuntimeError, ValueError):
        raise AcceptanceError(f"{label} is missing or escapes the evidence directory") from None
    if not candidate.is_file():
        raise AcceptanceError(f"{label} must be a regular file")
    if candidate.stat().st_size > max_bytes:
        raise AcceptanceError(f"{label} exceeds {max_bytes} bytes")
    return candidate


def _validate_evidence_artifacts(
    evidence_root: Path, references: list[str], excluded: Path
) -> None:
    checked: set[Path] = set()
    total_bytes = 0
    for reference in references:
        candidate = _resolve_evidence_file(evidence_root, reference, "evidence artifact")
        if candidate == excluded:
            raise AcceptanceError("evidence report cannot reference itself as an artifact")
        if candidate in checked:
            continue
        checked.add(candidate)
        data = candidate.read_bytes()
        total_bytes += len(data)
        if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
            raise AcceptanceError(
                f"evidence artifacts exceed {MAX_EVIDENCE_TOTAL_BYTES} total bytes"
            )
        text = data.decode("utf-8", errors="replace")
        if SECRET_VALUE.search(text) or PRIVATE_PATH.search(text):
            raise AcceptanceError("evidence artifact contains a secret or private path")


def _passed_evidence_items(items: list[Any], label: str) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "passed":
            raise AcceptanceError(f"every {label} must pass")
        identifier = item.get("id")
        reference = item.get("evidence")
        if not isinstance(identifier, str):
            raise AcceptanceError(f"every {label} needs an id")
        if not isinstance(reference, str) or not EVIDENCE_REFERENCE.fullmatch(reference):
            raise AcceptanceError(f"every {label} needs a bounded relative evidence reference")
        if reference.startswith("/") or ".." in Path(reference).parts:
            raise AcceptanceError(f"every {label} evidence reference must stay relative")
        if identifier in evidence:
            raise AcceptanceError(f"duplicate {label} id")
        evidence[identifier] = reference
    return evidence


def _discover_command() -> int:
    capabilities = discover_repository_capabilities()
    print(json.dumps(asdict(capabilities), indent=2, sort_keys=True))
    if capabilities.missing_mutation_capabilities:
        print(
            "SKIP full acceptance: sibling interface capabilities unavailable: "
            + ", ".join(capabilities.missing_mutation_capabilities),
            file=sys.stderr,
        )
    return 0


def _probe_command(base_url: str) -> int:
    target = validate_live_target(base_url)
    print(json.dumps(probe_extension_status(target), indent=2, sort_keys=True))
    return 0


def _verify_command(
    base_url: str, board_id: str, report: Path, candidate_commit: str
) -> int:
    target = validate_live_target(base_url, board_id)
    result = validate_evidence_report(
        report, target, discover_repository_capabilities(), candidate_commit
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pursers Home acceptance capability harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover")
    probe = subparsers.add_parser("probe-status")
    probe.add_argument("--host-url", required=True)
    verify = subparsers.add_parser("verify-evidence")
    verify.add_argument("--host-url", required=True)
    verify.add_argument("--board", required=True)
    verify.add_argument("--report", required=True, type=Path)
    verify.add_argument("--candidate-commit", required=True)
    args = parser.parse_args(argv)
    if args.command == "discover":
        return _discover_command()
    if args.command == "probe-status":
        return _probe_command(args.host_url)
    return _verify_command(
        args.host_url, args.board, args.report, args.candidate_commit
    )


if __name__ == "__main__":
    raise SystemExit(main())
