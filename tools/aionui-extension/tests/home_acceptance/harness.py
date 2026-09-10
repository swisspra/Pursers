from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

try:
    from . import browser_observer
except ImportError:
    import browser_observer

EXTENSION_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = EXTENSION_ROOT.parents[1]
PRODUCTION_BOARDS = frozenset({"pursers", "fullplatts", "mi-mcp-prd"})
MUTATION_OPT_IN = "I_UNDERSTAND_SANDBOX_ONLY"
MAX_REPORT_BYTES = 1_000_000
MAX_EVIDENCE_FILE_BYTES = 10_000_000
MAX_EVIDENCE_TOTAL_BYTES = 100_000_000
MIN_SCREENSHOT_BYTES = 256
MIN_SCREENSHOT_WIDTH = 320
MIN_SCREENSHOT_HEIGHT = 180
MAX_SCREENSHOT_DIMENSION = 16_384
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
BUILD_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{5,127}")
SEQUENCE = (
    "fresh_install",
    "door_connect",
    "team_setup",
    "five_workers_three_reviewers",
    "ticket_offer_claim",
    "ticket_submit_independent_review",
    "result_visible",
    "pause_resume_stop",
    "clean_reconnect_after_rotation",
)
CURRENT_OPERATOR_TIERS = {
    "goose_worker": 1,
    "codex_worker": 2,
    "codex_reviewer": 2,
    "optional_opus_worker": 2,
}
CURRENT_OPERATOR_TOPOLOGY = {
    "goose_worker": {"count": 2, "model": "vertex_ai/gemini-3.8-flash", "tier_max": 1},
    "codex_worker": {"count": 3, "model": "sol-high-fast", "tier_max": 2},
    "codex_reviewer": {"count": 3, "model": "sol-high-fast", "tier_max": 2},
}
SURFACE_IDS = frozenset({"aionui", "fleet", "personal"})
SURFACE_PRODUCTS = {
    "aionui": "AionUi",
    "fleet": "Pursers Fleet",
    "personal": "Pursers Personal",
}
SURFACE_IDENTITY_SOURCES = frozenset(
    {
        "signed-aionui-webui-listener",
        "signed-aionui-aionpro-listener",
        "verifier-pinned-process-artifact",
        "verifier-pinned-signed-aionui-artifact",
    }
)
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
JWT_PREFIX = "e" + "yJ"
SECRET_VALUE = re.compile(
    rf"(?:prs1\.[A-Za-z0-9._-]{{12,}}|{JWT_PREFIX}[A-Za-z0-9_-]+\."
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|Bearer\s+\S+)",
    re.I,
)
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)")
EVIDENCE_REFERENCE = re.compile(r"[A-Za-z0-9._/-]{1,240}")
SUITE_LOG_MARKERS = {
    "repository-python": (
        "central:", "client:", "personal:", "wait-bridge:", "seat-kit:"
    ),
    "extension-routes-node": ("# pass ", "# fail 0"),
    "extension-door-node": ("# pass ", "# fail 0"),
    "extension-team-node": ("# pass ", "# fail 0"),
    "dashboard-typecheck": ("> typecheck",),
    "dashboard-build": ("> build", "built in"),
    "repository-leak-scan": ("leak_scan: clean",),
    "candidate-diff-check": (),
}


class AcceptanceError(ValueError):
    pass


class AcceptanceCapabilityUnavailable(AcceptanceError):
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


@dataclass(frozen=True)
class BrowserObservationRequest:
    observation_id: str
    target: LiveTarget
    host_version: str
    host_build: str
    candidate_commit: str
    captured_at: str
    page_url: str
    assertions: tuple[dict[str, Any], ...]
    surface_id: str = "aionui"
    runtime_product: str = "AionUi"
    runtime_identity_source: str = "signed-aionui-webui-listener"


@dataclass(frozen=True)
class TrustedBrowserCapture:
    observer_id: str
    observation_id: str
    target: LiveTarget
    host_product: str
    host_version: str
    host_build: str
    host_identity_source: str
    candidate_commit: str
    captured_at: str
    page_url: str
    screenshot: bytes
    snapshot: Any
    surface_id: str = "aionui"


@dataclass(frozen=True)
class _BrowserEvidence:
    request: BrowserObservationRequest
    screenshot: bytes
    snapshot: Any
    references: tuple[str, str]


class _TrustedBrowserObserver(Protocol):
    def capture(self, request: BrowserObservationRequest) -> TrustedBrowserCapture:
        """Capture the observation through a verifier-owned browser channel."""


class VerifierBrowserObserver:
    """Replay verifier-owned browser captures through a bounded executable."""

    def __init__(self, command: Path, evidence_root: Path) -> None:
        if not command.is_absolute():
            raise AcceptanceError("browser observer command must be an absolute path")
        try:
            resolved = command.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise AcceptanceCapabilityUnavailable(
                "trusted browser observer command is unavailable"
            ) from None
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise AcceptanceCapabilityUnavailable(
                "trusted browser observer command is not executable"
            )
        repository = REPOSITORY_ROOT.resolve()
        evidence = evidence_root.resolve()
        if resolved.is_relative_to(repository) or resolved.is_relative_to(evidence):
            raise AcceptanceError(
                "browser observer must be verifier-owned outside the checkout and evidence directory"
            )
        if resolved.stat().st_mode & 0o022:
            raise AcceptanceError("browser observer command must not be group/world writable")
        self.command = resolved

    def capture(self, request: BrowserObservationRequest) -> TrustedBrowserCapture:
        request_payload = {
            **asdict(request),
            "target": asdict(request.target),
            "assertions": list(request.assertions),
        }
        try:
            completed = subprocess.run(
                [str(self.command)],
                input=json.dumps(request_payload, sort_keys=True),
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
                cwd=self.command.parent,
                env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcceptanceError("trusted browser observer execution failed") from exc
        if completed.returncode != 0 or len(completed.stdout.encode()) > MAX_EVIDENCE_FILE_BYTES:
            raise AcceptanceError("trusted browser observer returned no valid capture")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise AcceptanceError("trusted browser observer returned invalid JSON") from None
        expected = {
            "observer_id", "observation_id", "target", "host_product",
            "host_version", "host_build", "host_identity_source", "candidate_commit", "captured_at",
            "page_url", "screenshot_base64", "snapshot", "surface_id",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise AcceptanceError("trusted browser observer capture fields do not match schema")
        try:
            screenshot = base64.b64decode(payload["screenshot_base64"], validate=True)
        except (TypeError, ValueError):
            raise AcceptanceError("trusted browser observer screenshot is not valid base64") from None
        if payload["target"] != asdict(request.target):
            raise AcceptanceError("trusted browser observer target does not match request")
        return TrustedBrowserCapture(
            observer_id=payload["observer_id"],
            observation_id=payload["observation_id"],
            target=request.target,
            host_product=payload["host_product"],
            host_version=payload["host_version"],
            host_build=payload["host_build"],
            host_identity_source=payload["host_identity_source"],
            candidate_commit=payload["candidate_commit"],
            captured_at=payload["captured_at"],
            page_url=payload["page_url"],
            screenshot=screenshot,
            snapshot=payload["snapshot"],
            surface_id=payload["surface_id"],
        )


def _semantic_capabilities(routes: tuple[str, ...]) -> set[str]:
    lowered = {route.lower() for route in routes}
    capabilities = {"read_only_discovery"}
    if "/pursers/join" in lowered:
        capabilities.add("door_join")
    if "/pursers/status" in lowered:
        capabilities.add("seat_status")
    if "/pursers/onboarding/rotate" in lowered:
        capabilities.add("door_rotation")
    if {
        "/pursers/groups",
        "/pursers/groups/status",
        "/pursers/groups/create",
        "/pursers/groups/update",
        "/pursers/groups/remove",
    }.issubset(lowered):
        capabilities.add("team_lifecycle")
    if {
        "/pursers/seat-lifecycle/join",
        "/pursers/seat-lifecycle/status",
        "/pursers/seat-lifecycle/disconnect",
    }.issubset(lowered):
        capabilities.add("seat_lifecycle")
    if {
        "/pursers/tickets",
        "/pursers/tickets/status",
        "/pursers/tickets/get",
        "/pursers/tickets/create",
        "/pursers/tickets/cancel",
    }.issubset(lowered):
        capabilities.add("ticket_lifecycle")
    if "/pursers/results" in lowered:
        capabilities.add("result_visibility")
    return capabilities


def discover_repository_capabilities(root: Path = EXTENSION_ROOT) -> RepositoryCapabilities:
    manifest = json.loads((root / "aion-extension.json").read_text(encoding="utf-8"))
    contributes = manifest.get("contributes", {})
    webui = contributes.get("webui", {})
    api_routes = webui.get("apiRoutes", []) if isinstance(webui, dict) else []
    routes = {
        route["path"]
        for route in api_routes
        if isinstance(route, dict) and isinstance(route.get("path"), str)
    }
    if not routes:
        helper_routes = root / "webui" / "routes.js"
        helper_text = helper_routes.read_text(encoding="utf-8") if helper_routes.exists() else ""
        routes.update(
            route
            for route in re.findall(r"url\.pathname === ['\"]([^'\"]+)['\"]", helper_text)
            if route.startswith("/pursers/")
        )
    discovered_routes = tuple(sorted(routes))
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
    capabilities = _semantic_capabilities(discovered_routes)
    missing = tuple(sorted(REQUIRED_MUTATION_CAPABILITIES - capabilities))
    return RepositoryCapabilities(
        api_routes=discovered_routes,
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


def _surface_for_identifier(identifier: str) -> str:
    if identifier.startswith(("fleet.", "fleet-dashboard.")):
        return "fleet"
    if identifier.startswith(("personal.", "personal-mcp.")):
        return "personal"
    return "aionui"


def _validate_surface_bindings(
    value: Any,
    primary: LiveTarget,
    host: dict[str, Any],
    candidate_commit: str,
) -> dict[str, dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != SURFACE_IDS:
        raise AcceptanceError(
            "surface bindings must contain exact aionui, fleet, and personal entries"
        )
    bindings: dict[str, dict[str, Any]] = {}
    for surface_id in sorted(SURFACE_IDS):
        row = value[surface_id]
        if not isinstance(row, dict) or set(row) != {
            "target", "runtime", "candidate_commit"
        }:
            raise AcceptanceError(f"surface {surface_id} fields do not match schema")
        raw_target = row["target"]
        if not isinstance(raw_target, dict) or set(raw_target) != {
            "base_url", "board_id"
        }:
            raise AcceptanceError(f"surface {surface_id} target fields do not match schema")
        surface_target = validate_live_target(
            raw_target["base_url"], raw_target["board_id"]
        )
        if surface_target.board_id != primary.board_id:
            raise AcceptanceError(
                "every surface must bind the same explicit sandbox board"
            )
        runtime = row["runtime"]
        if not isinstance(runtime, dict) or set(runtime) != {
            "product", "version", "build", "identity_source"
        }:
            raise AcceptanceError(
                f"surface {surface_id} runtime fields do not match schema"
            )
        if runtime["product"] != SURFACE_PRODUCTS[surface_id]:
            raise AcceptanceError(f"surface {surface_id} product identity is invalid")
        version = _require_exact_text(
            runtime.get("version"), f"surface {surface_id} runtime version"
        )
        build = _require_exact_text(
            runtime.get("build"), f"surface {surface_id} runtime build"
        )
        if (
            (surface_id == "aionui" and not SEMVER.fullmatch(version))
            or (surface_id != "aionui" and not BUILD_ID.fullmatch(version))
            or not BUILD_ID.fullmatch(build)
        ):
            raise AcceptanceError(f"surface {surface_id} runtime identity is invalid")
        if runtime["identity_source"] not in SURFACE_IDENTITY_SOURCES:
            raise AcceptanceError(
                f"surface {surface_id} identity source is unsupported"
            )
        if row["candidate_commit"] != candidate_commit:
            raise AcceptanceError(
                "every surface must bind the exact candidate commit"
            )
        bindings[surface_id] = {
            "target": surface_target,
            "runtime": dict(runtime),
            "candidate_commit": candidate_commit,
        }
    aionui = bindings["aionui"]
    if aionui["target"] != primary:
        raise AcceptanceError(
            "aionui surface must match the report primary target"
        )
    if (
        aionui["runtime"]["product"] != "AionUi"
        or aionui["runtime"]["version"] != host["version"]
        or aionui["runtime"]["build"] != host["build"]
        or not str(aionui["runtime"]["identity_source"]).startswith(
            "signed-aionui-"
        )
    ):
        raise AcceptanceError(
            "aionui surface must match the signed report host identity"
        )
    if bindings["fleet"]["target"].base_url == primary.base_url:
        raise AcceptanceError(
            "fleet surface must use its actual distinct loopback origin"
        )
    expected_sources = {
        "fleet": "verifier-pinned-process-artifact",
        "personal": "verifier-pinned-signed-aionui-artifact",
    }
    for surface_id, expected_source in expected_sources.items():
        if bindings[surface_id]["runtime"]["identity_source"] != expected_source:
            raise AcceptanceError(
                f"surface {surface_id} must use {expected_source} trust"
            )
    return bindings


def _validate_operator_topology(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        *CURRENT_OPERATOR_TOPOLOGY, "optional_opus_worker"
    }:
        raise AcceptanceError("operator topology fields do not match the current fleet")
    for kind, expected in CURRENT_OPERATOR_TOPOLOGY.items():
        if value[kind] != expected:
            raise AcceptanceError(f"operator topology {kind} is stale or invalid")
    optional = value["optional_opus_worker"]
    if (
        not isinstance(optional, dict)
        or set(optional) != {"enabled", "count", "model", "tier_max"}
        or type(optional["enabled"]) is not bool
        or optional["count"] != (1 if optional["enabled"] else 0)
        or optional["model"] != "opus"
        or optional["tier_max"] != 2
    ):
        raise AcceptanceError("optional Opus topology must be explicit and additional")
    return json.loads(json.dumps(value))


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


def _helper_transport_headers() -> dict[str, str]:
    """Credentials for the loopback helper transport, when the verifier supplies them.

    Under the static-only manifest the host serves extension assets but does not
    execute route handlers, so `pursers/status` is answered by the packaged
    loopback helper, which fails closed without both an exact allowed `Origin`
    and the `x-pursers-home-token` header. Returning an empty mapping keeps the
    historical unauthenticated probe behaviour byte for byte, so an unconfigured
    environment still reports the capability as unavailable rather than passing.
    """

    token_file = os.environ.get("PURSERS_HOME_ACCEPTANCE_TOKEN_FILE")
    origin = os.environ.get("PURSERS_HOME_ACCEPTANCE_ORIGIN")
    if not token_file or not origin:
        return {}
    path = Path(token_file)
    if not path.is_file():
        raise AcceptanceError("helper token file does not exist")
    if path.stat().st_mode & 0o077:
        raise AcceptanceError("helper token file is not mode 0600")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise AcceptanceError("helper token file is empty")
    return {"Origin": origin, "x-pursers-home-token": token}


def _read_extension_status(target: LiveTarget, timeout_s: float) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}))
    endpoint = urljoin(f"{target.base_url}/", "pursers/status")
    request = Request(endpoint, headers=_helper_transport_headers())
    try:
        with opener.open(request, timeout=timeout_s) as response:
            final_target = validate_live_target(
                response.geturl().rsplit("/pursers/status", 1)[0]
            )
            if final_target.base_url != target.base_url:
                raise AcceptanceError("status probe redirected to a different origin")
            payload = json.loads(response.read(1_048_577).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise AcceptanceCapabilityUnavailable(
            "real loopback Pursers status capability is unavailable"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise AcceptanceError("status response is not the Pursers status contract")
    return payload


def probe_extension_status(target: LiveTarget, timeout_s: float = 3.0) -> dict[str, Any]:
    payload = _read_extension_status(target, timeout_s)
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


def probe_host_identity(target: LiveTarget, timeout_s: float = 3.0) -> dict[str, str]:
    try:
        payload = _read_extension_status(target, timeout_s)
        host = payload.get("host")
    except AcceptanceCapabilityUnavailable:
        try:
            host = browser_observer.probe_host_identity(target.base_url, timeout_s)
        except browser_observer.ObserverError as exc:
            raise AcceptanceCapabilityUnavailable(
                "Pursers status lacks verifiable AionUi host version/build identity"
            ) from exc
    if not isinstance(host, dict):
        raise AcceptanceCapabilityUnavailable(
            "Pursers status lacks verifiable AionUi host version/build identity"
        )
    product = _require_exact_text(host.get("product"), "observed host product")
    version = _require_exact_text(host.get("version"), "observed host version")
    build = _require_exact_text(host.get("build"), "observed host build")
    if product != "AionUi" or not SEMVER.fullmatch(version) or not BUILD_ID.fullmatch(build):
        raise AcceptanceCapabilityUnavailable(
            "Pursers status lacks verifiable AionUi host version/build identity"
        )
    return {"product": product, "version": version, "build": build}


def validate_evidence_report(
    report_path: Path,
    target: LiveTarget,
    capabilities: RepositoryCapabilities,
    candidate_commit: str,
    browser_observer_command: Path | None = None,
) -> dict[str, Any]:
    configured = browser_observer_command or (
        Path(value) if (value := os.environ.get("PURSERS_HOME_BROWSER_OBSERVER")) else None
    )
    observer = (
        VerifierBrowserObserver(configured, report_path.parent)
        if configured is not None
        else None
    )
    return _validate_evidence_report(
        report_path,
        target,
        capabilities,
        candidate_commit,
        trusted_browser_observer=observer,
    )


def _validate_evidence_report(
    report_path: Path,
    target: LiveTarget,
    capabilities: RepositoryCapabilities,
    candidate_commit: str,
    *,
    trusted_browser_observer: _TrustedBrowserObserver | None,
) -> dict[str, Any]:
    require_mutation_opt_in(os.environ.get("PURSERS_HOME_ACCEPTANCE_MUTATE"))
    if target.board_id is None:
        raise AcceptanceError("sandbox board is required for mutation evidence")
    if (
        not isinstance(candidate_commit, str)
        or not FULL_SHA.fullmatch(candidate_commit)
    ):
        raise AcceptanceError("candidate commit must be a full lowercase 40-hex SHA")
    _verify_candidate_commit(candidate_commit)
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
    host_version = _require_exact_text(host.get("version"), "host version")
    host_build = _require_exact_text(host.get("build"), "host build")
    if not SEMVER.fullmatch(host_version):
        raise AcceptanceError("host version must be an exact semantic version")
    if not BUILD_ID.fullmatch(host_build):
        raise AcceptanceError("host build must be an exact build identifier")
    observed_host = probe_host_identity(target)
    if observed_host != {
        "product": "AionUi",
        "version": host_version,
        "build": host_build,
    }:
        raise AcceptanceError(
            "report host version/build does not match the active loopback host"
        )
    surface_bindings = _validate_surface_bindings(
        report.get("surfaces"), target, host, candidate_commit
    )
    if surface_bindings is not None:
        _validate_operator_topology(report.get("operator_topology"))
    host_reference = host.get("evidence")
    if not isinstance(host_reference, str):
        raise AcceptanceError("host identity needs an evidence receipt")
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
    suite_rows: list[dict[str, Any]] = []
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
        suite_rows.append(suite)
    if seen_suites != set(REQUIRED_SUITES):
        raise AcceptanceError(
            "suite set does not match required suites: "
            f"missing={sorted(set(REQUIRED_SUITES) - seen_suites)}, "
            f"unexpected={sorted(seen_suites - set(REQUIRED_SUITES))}"
        )
    primary_references = [
        host_reference,
        *passed_steps.values(),
        *passed_inventory.values(),
        *suite_references,
    ]
    if len(primary_references) != len(set(primary_references)):
        raise AcceptanceError("each host, observation, and suite needs a distinct receipt")
    _validate_host_receipt(
        evidence_root,
        host_reference,
        target,
        host_version,
        host_build,
        candidate_commit,
    )
    browser_evidence: list[_BrowserEvidence] = []
    for identifier, reference in {**passed_steps, **passed_inventory}.items():
        surface_id = _surface_for_identifier(identifier)
        browser_evidence.append(_validate_browser_receipt(
            evidence_root,
            reference,
            identifier,
            target,
            host_version,
            host_build,
            candidate_commit,
            None if surface_bindings is None else surface_bindings[surface_id],
            surface_id,
        ))
    browser_attachment_references = [
        reference
        for evidence in browser_evidence
        for reference in evidence.references
    ]
    if len(browser_attachment_references) != len(set(browser_attachment_references)):
        raise AcceptanceError(
            "each browser observation needs distinct screenshot and snapshot artifacts"
        )
    browser_digests = [
        hashlib.sha256(
            _resolve_evidence_file(
                evidence_root, reference, "browser observation artifact"
            ).read_bytes()
        ).hexdigest()
        for reference in browser_attachment_references
    ]
    if len(browser_digests) != len(set(browser_digests)):
        raise AcceptanceError(
            "each browser observation needs unique substantive screenshot and snapshot evidence"
        )
    suite_output_references: list[str] = []
    for suite in suite_rows:
        suite_output_references.append(_validate_suite_receipt(
            evidence_root, suite, target, candidate_commit
        ))
    _validate_evidence_artifacts(
        evidence_root,
        [
            *primary_references,
            *browser_attachment_references,
            *suite_output_references,
        ],
        excluded=resolved_report,
    )
    _validate_trusted_browser_observations(
        browser_evidence, trusted_browser_observer
    )
    for suite in suite_rows:
        _execute_required_suite(suite["name"], suite["command"], candidate_commit)
    if report.get("all_existing_suites_passed") is not True:
        raise AcceptanceError("all_existing_suites_passed must be true")
    return {
        "evidence_kind": report["evidence_kind"],
        "host_product": host["product"],
        "host_version": host_version,
        "host_build": host_build,
        "candidate_commit": candidate_commit,
        "steps_passed": len(passed_steps),
        "inventory_passed": len(passed_inventory),
        "suites_passed": len(suites),
    }


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise AcceptanceError("candidate commit is unavailable in this checkout")
    return result.stdout.strip()


def _verify_candidate_commit(candidate_commit: str) -> None:
    resolved = _git("rev-parse", "--verify", f"{candidate_commit}^{{commit}}")
    if resolved != candidate_commit:
        raise AcceptanceError("candidate commit does not resolve exactly")
    if _git("rev-parse", "HEAD") != candidate_commit:
        raise AcceptanceError("candidate commit must equal the verification checkout HEAD")


def _require_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise AcceptanceError(f"{label} must be an ISO-8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AcceptanceError(
            f"{label} must be an ISO-8601 timestamp with timezone"
        ) from None
    if parsed.tzinfo is None:
        raise AcceptanceError(f"{label} must be an ISO-8601 timestamp with timezone")
    return parsed


def _load_receipt(evidence_root: Path, reference: str, label: str) -> dict[str, Any]:
    path = _resolve_evidence_file(evidence_root, reference, label)
    raw = path.read_text(encoding="utf-8")
    if SECRET_VALUE.search(raw) or PRIVATE_PATH.search(raw):
        raise AcceptanceError(f"{label} contains a secret or private path")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise AcceptanceError(f"{label} must be a structured JSON receipt") from None
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be a structured JSON receipt")
    return value


def _validate_host_receipt(
    evidence_root: Path,
    reference: str,
    target: LiveTarget,
    version: str,
    build: str,
    candidate_commit: str,
) -> None:
    receipt = _load_receipt(evidence_root, reference, "host identity receipt")
    expected_keys = {
        "schema_version", "evidence_kind", "target", "product", "version",
        "build", "candidate_commit", "captured_at", "source",
    }
    if set(receipt) != expected_keys:
        raise AcceptanceError("host identity receipt fields do not match schema")
    if (
        receipt["schema_version"] != 1
        or receipt["evidence_kind"] != "host_identity"
        or receipt["target"] != asdict(target)
        or receipt["product"] != "AionUi"
        or receipt["version"] != version
        or receipt["build"] != build
        or receipt["candidate_commit"] != candidate_commit
        or receipt["source"] not in {
            "aionui-about",
            "host-api",
            "signed-aionui-webui-listener",
            "signed-aionui-aionpro-listener",
        }
    ):
        raise AcceptanceError("host identity receipt does not match the report")
    _require_timestamp(receipt["captured_at"], "host captured_at")


def _artifact_descriptor(
    evidence_root: Path, value: Any, label: str
) -> tuple[Path, bytes]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise AcceptanceError(f"{label} must contain exact path and sha256 fields")
    if not isinstance(value["path"], str):
        raise AcceptanceError(f"{label} path must be a bounded relative reference")
    path = _resolve_evidence_file(evidence_root, value["path"], label)
    data = path.read_bytes()
    digest = value["sha256"]
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise AcceptanceError(f"{label} sha256 must be a lowercase digest")
    if hashlib.sha256(data).hexdigest() != digest:
        raise AcceptanceError(f"{label} sha256 does not match the artifact")
    text = data.decode("utf-8", errors="replace")
    if SECRET_VALUE.search(text) or PRIVATE_PATH.search(text):
        raise AcceptanceError(f"{label} contains a secret or private path")
    return path, data


def _validate_browser_receipt(
    evidence_root: Path,
    reference: str,
    identifier: str,
    target: LiveTarget,
    version: str,
    build: str,
    candidate_commit: str,
    surface_binding: dict[str, Any] | None = None,
    surface_id: str = "aionui",
) -> _BrowserEvidence:
    receipt = _load_receipt(evidence_root, reference, "browser observation receipt")
    expected_keys = (
        {
            "schema_version", "evidence_kind", "observation_id", "target", "host",
            "candidate_commit", "captured_at", "page_url", "screenshot",
            "accessibility_snapshot", "assertions",
        }
        if surface_binding is None
        else {
            "schema_version", "evidence_kind", "observation_id", "surface_id",
            "target", "runtime", "candidate_commit", "captured_at", "page_url",
            "screenshot", "accessibility_snapshot", "assertions",
        }
    )
    if set(receipt) != expected_keys:
        raise AcceptanceError("browser observation receipt fields do not match schema")
    expected_target = target
    expected_version = version
    expected_build = build
    expected_product = "AionUi"
    expected_source = "signed-aionui-webui-listener"
    receipt_binding_matches = receipt.get("host") == {
        "version": version, "build": build
    }
    if surface_binding is not None:
        expected_target = surface_binding["target"]
        runtime = surface_binding["runtime"]
        expected_product = runtime["product"]
        expected_version = runtime["version"]
        expected_build = runtime["build"]
        expected_source = runtime["identity_source"]
        receipt_binding_matches = (
            receipt.get("surface_id") == surface_id
            and receipt.get("runtime") == runtime
        )
    if (
        receipt["schema_version"] != 1
        or receipt["evidence_kind"] != "browser_observation"
        or receipt["observation_id"] != identifier
        or receipt["target"] != asdict(expected_target)
        or not receipt_binding_matches
        or receipt["candidate_commit"] != candidate_commit
    ):
        raise AcceptanceError(
            f"browser observation receipt does not bind {identifier!r} to the report"
        )
    _require_timestamp(receipt["captured_at"], "observation captured_at")
    page = urlsplit(receipt["page_url"] if isinstance(receipt["page_url"], str) else "")
    origin = urlsplit(expected_target.base_url)
    if (
        page.scheme != origin.scheme
        or page.netloc != origin.netloc
        or page.username is not None
        or page.password is not None
        or page.query
        or page.fragment
    ):
        raise AcceptanceError("browser observation page_url must use the target origin")
    _screenshot_path, screenshot = _artifact_descriptor(
        evidence_root, receipt["screenshot"], "browser screenshot"
    )
    _validate_png(screenshot)
    _snapshot_path, snapshot_data = _artifact_descriptor(
        evidence_root,
        receipt["accessibility_snapshot"],
        "accessibility snapshot",
    )
    try:
        snapshot = json.loads(snapshot_data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceError("accessibility snapshot must be JSON") from None
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema_version",
        "observation_id",
        "page_url",
        "captured_at",
        "snapshot",
    }:
        raise AcceptanceError("accessibility snapshot fields do not match schema")
    if (
        snapshot["schema_version"] != 1
        or snapshot["observation_id"] != identifier
        or snapshot["page_url"] != receipt["page_url"]
        or snapshot["captured_at"] != receipt["captured_at"]
        or not isinstance(snapshot["snapshot"], (dict, list))
        or _structured_node_count(snapshot["snapshot"]) < 5
        or len(json.dumps(snapshot["snapshot"], sort_keys=True)) < 128
    ):
        raise AcceptanceError("accessibility snapshot does not bind the observation id")
    assertions = receipt["assertions"]
    if not isinstance(assertions, list) or not assertions:
        raise AcceptanceError("browser observation needs explicit assertions")
    for assertion in assertions:
        if (
            not isinstance(assertion, dict)
            or set(assertion) != {"name", "path", "operator", "expected"}
            or not isinstance(assertion["name"], str)
            or not assertion["name"].strip()
            or assertion["operator"] not in {"equals", "contains"}
            or not isinstance(assertion["path"], list)
            or not assertion["path"]
            or len(json.dumps(assertion["expected"])) > 1_000
        ):
            raise AcceptanceError("browser observation assertion is not verifiable")
    _evaluate_browser_assertions(snapshot["snapshot"], assertions)
    return _BrowserEvidence(
        request=BrowserObservationRequest(
            observation_id=identifier,
            target=expected_target,
            host_version=expected_version,
            host_build=expected_build,
            candidate_commit=candidate_commit,
            captured_at=receipt["captured_at"],
            page_url=receipt["page_url"],
            assertions=tuple(assertions),
            surface_id=surface_id,
            runtime_product=expected_product,
            runtime_identity_source=expected_source,
        ),
        screenshot=screenshot,
        snapshot=snapshot["snapshot"],
        references=(
            receipt["screenshot"]["path"],
            receipt["accessibility_snapshot"]["path"],
        ),
    )


def _validate_trusted_browser_observations(
    evidence_rows: list[_BrowserEvidence],
    observer: _TrustedBrowserObserver | None,
) -> None:
    if observer is None:
        raise AcceptanceCapabilityUnavailable(
            "trusted browser observer is unavailable; report artifacts cannot establish GUI acceptance"
        )
    observer_ids: set[str] = set()
    for evidence in evidence_rows:
        request = evidence.request
        capture = observer.capture(request)
        if not isinstance(capture, TrustedBrowserCapture):
            raise AcceptanceError("trusted browser observer returned an invalid capture")
        observer_ids.add(_require_exact_text(capture.observer_id, "browser observer id"))
        if (
            capture.observation_id != request.observation_id
            or capture.target != request.target
            or capture.surface_id != request.surface_id
            or capture.host_product != request.runtime_product
            or capture.host_version != request.host_version
            or capture.host_build != request.host_build
            or capture.host_identity_source != request.runtime_identity_source
            or capture.candidate_commit != request.candidate_commit
            or capture.captured_at != request.captured_at
            or capture.page_url != request.page_url
        ):
            raise AcceptanceError(
                "trusted browser capture does not bind the report observation"
            )
        if capture.screenshot != evidence.screenshot:
            raise AcceptanceError(
                "browser screenshot does not match the trusted observer capture"
            )
        if capture.snapshot != evidence.snapshot:
            raise AcceptanceError(
                "accessibility snapshot does not match the trusted observer capture"
            )
        _evaluate_browser_assertions(capture.snapshot, list(request.assertions))
    if len(observer_ids) != 1:
        raise AcceptanceError(
            "all browser observations must come from one trusted observer session"
        )


def _evaluate_browser_assertions(
    snapshot: Any, assertions: list[dict[str, Any]]
) -> None:
    for assertion in assertions:
        actual = _resolve_snapshot_path(snapshot, assertion["path"])
        if assertion["operator"] == "equals":
            passed = actual == assertion["expected"]
        else:
            passed = isinstance(actual, (str, list)) and assertion["expected"] in actual
        if not passed:
            raise AcceptanceError("browser observation assertion failed against snapshot")


def _validate_suite_receipt(
    evidence_root: Path,
    suite: dict[str, Any],
    target: LiveTarget,
    candidate_commit: str,
) -> str:
    receipt = _load_receipt(evidence_root, suite["evidence"], "suite run receipt")
    expected_keys = {
        "schema_version", "evidence_kind", "name", "command", "commit",
        "target", "started_at", "finished_at", "exit_code", "output",
    }
    if set(receipt) != expected_keys:
        raise AcceptanceError("suite run receipt fields do not match schema")
    if (
        receipt["schema_version"] != 1
        or receipt["evidence_kind"] != "suite_run"
        or receipt["name"] != suite["name"]
        or receipt["command"] != suite["command"]
        or receipt["commit"] != candidate_commit
        or receipt["target"] != asdict(target)
        or receipt["exit_code"] != 0
    ):
        raise AcceptanceError("suite run receipt does not match the claimed suite")
    started = _require_timestamp(receipt["started_at"], "suite started_at")
    finished = _require_timestamp(receipt["finished_at"], "suite finished_at")
    if finished < started:
        raise AcceptanceError("suite finished_at cannot precede started_at")
    _output_path, output = _artifact_descriptor(
        evidence_root, receipt["output"], "suite output"
    )
    text = output.decode("utf-8", errors="replace")
    for marker in SUITE_LOG_MARKERS[suite["name"]]:
        if marker not in text:
            raise AcceptanceError(
                f"suite output for {suite['name']} lacks success marker {marker!r}"
            )
    if suite["name"].startswith("extension-"):
        match = re.search(r"(?m)^# pass (\d+)$", text)
        if match is None or int(match.group(1)) < 1:
            raise AcceptanceError("Node suite output must report at least one pass")
    if suite["name"] == "candidate-diff-check":
        result = subprocess.run(
            ["git", "diff", "--check", f"{candidate_commit}^", candidate_commit],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode or result.stdout or result.stderr:
            raise AcceptanceError("candidate commit fails git diff --check")
    return receipt["output"]["path"]


def _validate_png(data: bytes) -> None:
    if len(data) < MIN_SCREENSHOT_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AcceptanceError("browser screenshot must be a substantive PNG image")
    offset = 8
    width = height = 0
    saw_idat = saw_iend = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise AcceptanceError("browser screenshot PNG is truncated")
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            raise AcceptanceError("browser screenshot PNG checksum is invalid")
        if chunk_type == b"IHDR":
            if length != 13:
                raise AcceptanceError("browser screenshot PNG header is invalid")
            width = int.from_bytes(payload[:4], "big")
            height = int.from_bytes(payload[4:8], "big")
        elif chunk_type == b"IDAT":
            saw_idat = saw_idat or bool(payload)
        elif chunk_type == b"IEND":
            saw_iend = True
            if end != len(data):
                raise AcceptanceError("browser screenshot PNG has trailing data")
            break
        offset = end
    if (
        width < MIN_SCREENSHOT_WIDTH
        or height < MIN_SCREENSHOT_HEIGHT
        or width > MAX_SCREENSHOT_DIMENSION
        or height > MAX_SCREENSHOT_DIMENSION
        or not saw_idat
        or not saw_iend
    ):
        raise AcceptanceError("browser screenshot must be a substantive PNG image")


def _structured_node_count(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(_structured_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_structured_node_count(item) for item in value)
    return 1


def _resolve_snapshot_path(snapshot: Any, path: list[Any]) -> Any:
    current = snapshot
    for part in path:
        if isinstance(current, dict) and isinstance(part, str) and part in current:
            current = current[part]
        elif (
            isinstance(current, list)
            and type(part) is int
            and 0 <= part < len(current)
        ):
            current = current[part]
        else:
            raise AcceptanceError("browser assertion path is absent from snapshot")
    return current


def _execute_required_suite(name: str, command: str, candidate_commit: str) -> None:
    if command != REQUIRED_SUITES.get(name):
        raise AcceptanceError("independent suite command is not authorized")
    if name == "repository-python":
        arguments = ["python3", "tools/ci_manifest.py", "run"]
        cwd = REPOSITORY_ROOT
        environment = None
    elif name.startswith("extension-"):
        test_file = {
            "extension-routes-node": "routes.test.cjs",
            "extension-door-node": "door_adapter.test.cjs",
            "extension-team-node": "team_adapter.test.cjs",
        }[name]
        arguments = ["node", "--test", f"tools/aionui-extension/tests/{test_file}"]
        cwd = REPOSITORY_ROOT
        environment = None
    elif name in {"dashboard-typecheck", "dashboard-build"}:
        arguments = ["npm", "run", name.removeprefix("dashboard-")]
        cwd = REPOSITORY_ROOT / "tools" / "dashboard-ui"
        environment = {**os.environ, "NODE_ENV": ""}
    elif name == "repository-leak-scan":
        arguments = ["python3", "tools/leak_scan.py"]
        cwd = REPOSITORY_ROOT
        environment = None
    elif name == "candidate-diff-check":
        arguments = [
            "git", "diff", "--check", f"{candidate_commit}^", candidate_commit
        ]
        cwd = REPOSITORY_ROOT
        environment = None
    else:
        raise AcceptanceError("independent suite name is not authorized")
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=1_800,
    )
    if completed.returncode:
        raise AcceptanceError(f"independent suite execution failed: {name}")


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
    if not isinstance(reference, str) or not EVIDENCE_REFERENCE.fullmatch(reference):
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
    base_url: str, board_id: str, report: Path, candidate_commit: str,
    browser_observer: Path | None,
) -> int:
    target = validate_live_target(base_url, board_id)
    result = validate_evidence_report(
        report, target, discover_repository_capabilities(), candidate_commit,
        browser_observer,
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
    verify.add_argument("--browser-observer", type=Path)
    args = parser.parse_args(argv)
    if args.command == "discover":
        return _discover_command()
    if args.command == "probe-status":
        return _probe_command(args.host_url)
    return _verify_command(
        args.host_url, args.board, args.report, args.candidate_commit,
        args.browser_observer,
    )


if __name__ == "__main__":
    raise SystemExit(main())
