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
        "extension.join",
        "extension.status",
        "extension.worker_presets",
        "extension.reviewer_presets",
        "personal.connection",
        "personal.search",
        "personal.today",
        "personal.work",
        "personal.agents",
        "personal.fleet",
        "personal.links",
        "personal.activity",
        "personal.health",
        "personal.current_work",
        "personal.latest_handoff",
        "personal.important_pinned_note",
        "personal.recent_activity",
        "personal.ticket_groups",
        "personal.agent_duplicate_stale",
        "personal.fleet_projects",
        "personal.fleet_pool",
        "personal.link_nodes_edges",
        "personal.empty_error_offline",
        "fleet.central_availability",
        "fleet.pool_counts",
        "fleet.board_cards",
        "fleet.active_tickets",
        "fleet.agent_pool",
        "fleet.overview",
        "fleet.boards_hub",
        "fleet.agents_hub",
        "fleet.operations_hub",
        "fleet.board_tickets",
        "fleet.board_timeline",
        "fleet.board_changes",
        "fleet.board_flow",
        "fleet.board_routes",
        "fleet.overhead",
        "fleet.coordinator_config",
        "fleet.workers",
        "fleet.seat_inventory",
        "fleet.seat_import",
        "fleet.bridge",
        "fleet.doctor",
        "fleet.dispatch",
        "fleet.registry_worktrees",
        "fleet.doors",
        "fleet.add_project",
        "fleet.release",
        "fleet.operations",
        "fleet.search",
        "fleet.keyboard_help",
        "fleet.empty_error_offline",
    }
)
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
) -> dict[str, Any]:
    require_mutation_opt_in(os.environ.get("PURSERS_HOME_ACCEPTANCE_MUTATE"))
    if target.board_id is None:
        raise AcceptanceError("sandbox board is required for mutation evidence")
    if capabilities.missing_mutation_capabilities:
        missing = ", ".join(capabilities.missing_mutation_capabilities)
        raise AcceptanceError(f"sibling interface capabilities unavailable: {missing}")
    raw = report_path.read_text(encoding="utf-8")
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
    steps = report.get("steps")
    if not isinstance(steps, list) or len(steps) != len(SEQUENCE):
        raise AcceptanceError("every acceptance step must pass exactly once")
    passed_steps = _passed_evidence_ids(steps, "acceptance step")
    if passed_steps != set(SEQUENCE):
        raise AcceptanceError("every acceptance step must pass exactly once")
    inventory = report.get("inventory")
    if not isinstance(inventory, list):
        raise AcceptanceError("dashboard inventory evidence is required")
    passed_inventory = _passed_evidence_ids(inventory, "dashboard inventory item")
    missing_inventory = REQUIRED_INVENTORY - passed_inventory
    if missing_inventory:
        raise AcceptanceError(
            "dashboard inventory is incomplete: " + ", ".join(sorted(missing_inventory))
        )
    suites = report.get("suites")
    if not isinstance(suites, list) or not suites:
        raise AcceptanceError("existing suite evidence is required")
    if any(not isinstance(suite, dict) or suite.get("status") != "passed" for suite in suites):
        raise AcceptanceError("every recorded suite must pass")
    if report.get("all_existing_suites_passed") is not True:
        raise AcceptanceError("all_existing_suites_passed must be true")
    return {
        "evidence_kind": report["evidence_kind"],
        "host_product": host["product"],
        "steps_passed": len(passed_steps),
        "inventory_passed": len(passed_inventory),
        "suites_passed": len(suites),
    }


def _passed_evidence_ids(items: list[Any], label: str) -> set[str]:
    identifiers: list[str] = []
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
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise AcceptanceError(f"duplicate {label} id")
    return set(identifiers)


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


def _verify_command(base_url: str, board_id: str, report: Path) -> int:
    target = validate_live_target(base_url, board_id)
    result = validate_evidence_report(report, target, discover_repository_capabilities())
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
    args = parser.parse_args(argv)
    if args.command == "discover":
        return _discover_command()
    if args.command == "probe-status":
        return _probe_command(args.host_url)
    return _verify_command(args.host_url, args.board, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
