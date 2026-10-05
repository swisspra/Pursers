#!/usr/bin/env python3
"""Serve the Fleet UI against deterministic public-safe acceptance data."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
import time
from pathlib import Path


def load_dashboard(repo: Path):
    dashboard_dir = repo / "tools/fleet-dashboard"
    sys.path.insert(0, str(dashboard_dir))
    spec = importlib.util.spec_from_file_location(
        "fleet_dashboard_acceptance", dashboard_dir / "fleet_dashboard.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Fleet Dashboard")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ticket(
    ticket_id: str,
    title: str,
    status: str,
    *,
    owner: str | None = None,
    updated: str = "2030-01-02T11:50:00Z",
) -> dict:
    return {
        "id": ticket_id,
        "ticket_id": ticket_id,
        "title": title,
        "description": "Synthetic display-ready acceptance record.",
        "status": status,
        "status_label": status.replace("_", " "),
        "claimed_by": owner,
        "updated_at": updated,
        "created_at": "2030-01-02T10:00:00Z",
        "ttl_s": 900 if owner else None,
        "tier": 2,
        "skills_required": [],
        "required_fields": ["test_output", "observations"],
        "rejection_count": 1 if ticket_id == "TK-review" else 0,
        "abandoned_count": 0,
        "review_label": "strict" if status == "submitted" else None,
    }


def activity(
    ticket_id: str,
    stage: str,
    state: str,
    *,
    attempt: int | None,
    actor: str | None,
    next_action: str,
    freshness: str = "fresh",
    boundary: str = "unknown",
    blocker: str | None = None,
) -> dict:
    value = {
        "schema_version": 1,
        "stage": stage,
        "state": state,
        "attempt_id": attempt,
        "actor_id": actor,
        "updated_at": "2030-01-02T11:58:00Z",
        "freshness": freshness,
        "evidence_refs": [
            f"board://fixture-board/ticket/{ticket_id}#acceptance-evidence"
        ],
        "next_action": next_action,
        "completion_boundary": boundary,
    }
    if blocker is not None:
        value["blocking_reason"] = blocker
    return value


class AcceptanceCache:
    def __init__(
        self,
        dashboard,
        mode: str = "populated",
        *,
        ticket_count: int = 6,
        detail_delay_ms: int = 0,
        overhead_delay_ms: int = 0,
    ) -> None:
        self.dashboard = dashboard
        self.mode = mode
        self.ticket_count = ticket_count
        self.detail_delay_ms = detail_delay_ms
        self.overhead_delay_ms = overhead_delay_ms
        self._detail_requests = 0
        self._overhead_requests = 0
        self._delay_lock = threading.Lock()
        self.revision = 0
        self.display_names: dict[str, dict[str, object]] = {
            "AI-synthetic-01": {"display_name": "Atlas", "revision": 0}
        }

    @staticmethod
    def labels() -> list[str]:
        return ["fixture"]

    @staticmethod
    def resolve_central(value: str | None) -> str:
        if value not in {None, "fixture"}:
            raise KeyError(value)
        return "fixture"

    def _tickets(self) -> list[dict]:
        rows = [
            ticket("TK-human", "Choose the safe rollout window", "needs_human"),
            ticket("TK-review", "Verify the submitted display evidence", "submitted", owner="reviewer-01"),
            ticket("TK-working", "Prepare the Fleet acceptance gallery", "claimed", owner="worker-01"),
            ticket("TK-stale", "Reconcile the expired work evidence", "claimed", owner="worker-03"),
            ticket("TK-open", "Document the next bounded action", "open"),
            ticket("TK-closed", "Preserve the source-backed route map", "closed", owner="worker-02"),
        ]
        records = {
            "TK-human": activity(
                "TK-human", "validation", "blocked", attempt=1,
                actor="AI-synthetic-03", next_action="Record the requested human decision.",
                blocker="A rollout window is required.",
            ),
            "TK-review": activity(
                "TK-review", "review", "waiting", attempt=1,
                actor=None, next_action="An independent reviewer must claim the submission.",
            ),
            "TK-working": activity(
                "TK-working", "work", "running", attempt=1,
                actor="AI-synthetic-01", next_action="Continue the current work attempt.",
            ),
            "TK-stale": activity(
                "TK-stale", "work", "stale", attempt=2,
                actor="AI-synthetic-03", next_action="Reconcile stale evidence before continuing.",
                freshness="stale", blocker="The last meaningful update is stale.",
            ),
            "TK-open": activity(
                "TK-open", "work", "retrying", attempt=2,
                actor=None, next_action="Claim the next attempt and address review feedback.",
            ),
            "TK-closed": activity(
                "TK-closed", "completed", "completed", attempt=1,
                actor="AI-synthetic-02", next_action="No further lifecycle action is required.",
                boundary="integration",
            ),
        }
        for row in rows:
            row["activity"] = records[row["id"]]
        for index in range(len(rows), self.ticket_count):
            rows.append(
                ticket(
                    f"TK-scale-{index:04d}",
                    f"Representative bounded ticket {index:04d}",
                    "open" if index % 3 else "closed",
                )
            )
        return rows

    def _agents(self) -> list[dict]:
        rows = []
        for index in range(35):
            working = index == 0
            stale = index == 34
            name = f"synthetic-worker-{index + 1:02d}"
            work = []
            if working:
                work = [
                    {
                        "board_id": "fixture-board",
                        "project": "Fixture Project",
                        "role": "worker",
                        "current_ticket_id": "TK-working",
                        "current_ticket_title": "Prepare the Fleet acceptance gallery",
                        "current_ticket_status": "claimed",
                        "lease_expires_at": "2030-01-02T12:15:00Z",
                    }
                ]
            agent_id = f"AI-synthetic-{index + 1:02d}"
            profile = self.display_names.get(
                agent_id, {"display_name": None, "revision": 0}
            )
            display_name = profile["display_name"]
            rows.append(
                {
                    "agent_name": name,
                    "agent_id": agent_id,
                    "principal_id": f"PR-synthetic-{index + 1:02d}",
                    "display_name": display_name,
                    "display_name_profiles": [
                        {
                            "board_id": "fixture-board",
                            "agent_id": agent_id,
                            "display_name": display_name,
                            "display_label": display_name or name,
                            "revision": profile["revision"],
                        }
                    ],
                    "pool_status": "busy" if working else "stale" if stale else "available",
                    "boards": ["fixture-board"],
                    "board_scope": ["fixture-board"],
                    "duplicate_name": False,
                    "last_seen": "2030-01-02T11:59:00Z" if not stale else "2030-01-02T10:00:00Z",
                    "seats": [
                        {
                            "agent_id": agent_id,
                            "board_id": "fixture-board",
                            "project": "Fixture Project",
                            "role": "worker",
                            "current_ticket_id": "TK-working" if working else None,
                            "current_ticket_title": "Prepare the Fleet acceptance gallery" if working else None,
                            "current_ticket_status": "claimed" if working else None,
                            "lease_expires_at": "2030-01-02T12:15:00Z" if working else None,
                            "capabilities": {
                                "tier_max": 2,
                                "host": "synthetic",
                                "can_work": True,
                                "can_review": False,
                            },
                            "profile": {
                                "display_name": display_name,
                                "display_label": display_name or name,
                                "revision": profile["revision"],
                            },
                        }
                    ],
                    "current_work": work,
                }
            )
        label_counts: dict[str, int] = {}
        for row in rows:
            if row["display_name"]:
                label_counts[row["display_name"]] = (
                    label_counts.get(row["display_name"], 0) + 1
                )
        for row in rows:
            row["duplicate_name"] = bool(
                row["display_name"]
                and label_counts.get(row["display_name"], 0) > 1
            )
        return rows

    def save_agent_display_name(
        self, board_id: str, payload: object, central: str | None = None
    ) -> dict:
        self.resolve_central(central)
        if board_id != "fixture-board" or not isinstance(payload, dict):
            raise ValueError("invalid display-name target")
        agent_id = payload.get("agent_id")
        display_name = payload.get("display_name")
        expected_revision = payload.get("expected_revision")
        if not isinstance(agent_id, str) or not agent_id.startswith("AI-synthetic-"):
            raise ValueError("target agent not found")
        current = self.display_names.get(
            agent_id, {"display_name": None, "revision": 0}
        )
        if expected_revision != current["revision"]:
            raise ValueError(
                "display-name revision conflict: "
                f"expected {expected_revision}, current {current['revision']}"
            )
        if display_name is not None and not isinstance(display_name, str):
            raise ValueError("display_name must be a string or null")
        normalized = display_name.strip() if isinstance(display_name, str) else None
        if display_name is not None and not normalized:
            raise ValueError("display_name must not be empty; use null to reset")
        changed = normalized != current["display_name"]
        revision = int(current["revision"]) + (1 if changed else 0)
        self.display_names[agent_id] = {
            "display_name": normalized,
            "revision": revision,
        }
        return {
            "ok": True,
            "changed": changed,
            "profile": {
                "agent_id": agent_id,
                "agent_name": agent_id.replace("AI-synthetic-", "synthetic-worker-"),
                "display_name": normalized,
                "display_label": normalized or agent_id.replace(
                    "AI-synthetic-", "synthetic-worker-"
                ),
                "revision": revision,
            },
        }

    def get(self, central: str | None = None) -> dict:
        self.resolve_central(central)
        if self.mode == "error":
            raise RuntimeError("synthetic acceptance source unavailable")
        self.revision += 1
        tickets = self._tickets()
        events = [
            {
                "seq": 104,
                "kind": "ticket_submitted",
                "ticket_id": "TK-review",
                "actor": "synthetic-worker-02",
                "actor_role": "worker",
                "status_from": "claimed",
                "status_to": "submitted",
                "occurred_at": "2030-01-02T11:56:00Z",
            },
            {
                "seq": 103,
                "kind": "ticket_claimed",
                "ticket_id": "TK-working",
                "actor": "synthetic-worker-01",
                "actor_role": "worker",
                "status_from": "open",
                "status_to": "claimed",
                "occurred_at": "2030-01-02T11:52:00Z",
            },
            {
                "seq": 102,
                "kind": "human_requested",
                "ticket_id": "TK-human",
                "actor": "synthetic-worker-03",
                "actor_role": "worker",
                "occurred_at": "2030-01-02T11:48:00Z",
            },
        ]
        counts: dict[str, int] = {}
        for row in tickets:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        result = {
            "central": "fixture",
            "generated_at": "2030-01-02T12:00:00Z",
            "fixture_revision": "display-ready-v1",
            "boards": [
                {
                    "board_id": "fixture-board",
                    "label": "Fixture Project",
                    "status": "ready",
                    "counts": counts,
                    "tickets": tickets,
                    "events": events,
                    "coordinator_heartbeat": "2030-01-02T11:59:30Z",
                    "snapshot_truncation": {"returned": 6, "total": 9},
                    "human_requests": [
                        {
                            "request_id": "HR-synthetic-01",
                            "ticket_id": "TK-human",
                            "kind": "decision",
                            "message": "Choose a public-safe maintenance window.",
                            "asked_by": "synthetic-worker-03",
                            "asked_at": "2030-01-02T11:48:00Z",
                            "form_safe": True,
                            "requested_schema": {
                                "type": "object",
                                "properties": {
                                    "window": {
                                        "type": "string",
                                        "title": "Maintenance window",
                                        "enum": ["Morning", "Afternoon"],
                                    }
                                },
                                "required": ["window"],
                            },
                        }
                    ],
                    "coordinator_findings": {
                        "items": [
                            {
                                "kind": "source_check",
                                "level": "warn",
                                "text": "Synthetic evidence is ready for independent inspection.",
                                "ticket_id": "TK-review",
                            }
                        ],
                        "butler_evaluations": [],
                        "agreement_by_question_kind": [],
                        "agreement_by_ticket": [],
                        "multi_question_tickets": [],
                    },
                }
            ],
            "agents": self._agents(),
            "inactive_agents": [],
            "pool_summary": {
                "online": 35,
                "busy": 1,
                "available": 33,
                "connected": 0,
                "stale": 1,
                "unknown_model": 35,
            },
        }
        if self.mode == "empty":
            result.update(
                {
                    "boards": [],
                    "agents": [],
                    "inactive_agents": [],
                    "pool_summary": {
                        "online": 0,
                        "busy": 0,
                        "available": 0,
                        "connected": 0,
                        "stale": 0,
                        "unknown_model": 0,
                    },
                }
            )
        return result

    def get_board(self, board_id: str, central: str | None = None) -> dict:
        self.resolve_central(central)
        if board_id != "fixture-board":
            raise KeyError(board_id)
        with self._delay_lock:
            self._detail_requests += 1
            cold_detail = self._detail_requests == 1
        if self.detail_delay_ms and cold_detail:
            time.sleep(self.detail_delay_ms / 1000)
        fleet = self.get(central)
        board = fleet["boards"][0]
        snapshot_tickets = []
        for row in board["tickets"]:
            projected = dict(row)
            projected["ticket_id"] = projected.pop("id")
            snapshot_tickets.append(projected)
        result = self.dashboard.project_board_detail(
            {
                "board_id": "fixture-board",
                "label": "Fixture Project",
                "snapshot": {
                    "tickets": snapshot_tickets,
                    "total_counts": {"tickets": 8},
                },
                "events": board["events"],
            }
        )
        result.update(
            {
                "central": "fixture",
                # Exercise timestamp-only refreshes without changing semantic data.
                "generated_at": f"2030-01-02T12:00:{min(self._detail_requests, 59):02d}Z",
            }
        )
        return result

    def get_config(self, central: str | None = None) -> dict:
        self.resolve_central(central)
        return {
            "config": {
                "board_butler": {
                    "schema_version": 1,
                    "global": {
                        "drafting": {
                            "endpoint_ref": "https://provider.example.invalid/v1",
                            "model": "synthetic-model",
                        }
                    },
                    "projects": {},
                    "boards": {},
                }
            },
            "expected_sha256": "a" * 64,
        }

    def get_overhead_thresholds(
        self, central: str | None = None
    ) -> dict[str, int | float]:
        self.resolve_central(central)
        with self._delay_lock:
            self._overhead_requests += 1
            cold_overhead = self._overhead_requests == 1
        if self.overhead_delay_ms and cold_overhead:
            time.sleep(self.overhead_delay_ms / 1000)
        return self.dashboard.context_pressure_thresholds(None)

    def get_project_registry(self, central: str | None = None) -> dict:
        self.resolve_central(central)
        return {
            "registry": {"schema_version": 1, "projects": {}},
            "expected_sha256": "b" * 64,
        }

    def get_managed_configuration(
        self, board_id: str, central: str | None = None
    ) -> dict:
        self.resolve_central(central)
        if board_id != "fixture-board":
            raise KeyError(board_id)
        principal = "PR-" + "a" * 64
        connector = {
            "schema_version": 1,
            "approved_connector_ids": ["connector:issues"],
            "connectors": [{
                "connector_id": "connector:issues", "enabled": False,
                "transport": "streamable_http", "protocol_revision": "2026-07-28",
                "endpoint_ref": "endpoint:issues", "secret_ref": "[redacted]",
                "tools": [{"name": "issues_list", "effect": "read_only", "replay": "never", "stable_call_id_field": None}],
                "resources": [], "risky_tools": [], "denied_tools": [],
                "limits": {"timeout_ms": 30000, "max_input_bytes": 65536, "max_output_bytes": 1000000, "max_concurrency": 2, "calls_per_minute": 60},
            }],
            "sources": [{
                "source_id": "issues", "connector_id": "connector:issues", "enabled": False,
                "list_tool": "issues_list", "fixed_args": {"status": "open"}, "items_path": "issues",
                "field_map": {"external_id": "id", "revision": "revision", "title": "title", "body": "body", "link": "url", "project_hint": "project"},
                "routing": {"project_hint_is_registry_key": True}, "mode": "ask", "content_type": "structured", "max_pages": 1,
            }],
        }
        onboarding = {"sources": {"issues": {"domain": "work", "projects_root": "[redacted]", "auto_onboard": False, "per_cycle_cap": 2, "retry_limit": 3, "retry_backoff_s": 60, "repositories": {"service": {"repository_url": "https://example.invalid/org/service.git", "integration_ref": "main"}}, "member_roles": {}, "activate_delivery_policy": False}}}
        return {
            "schema": "fleet_managed_config_v1", "board_id": board_id,
            "families": {
                "board_policy": {"status": "configurable", "values": {"review_policy": "strict", "stale_after_days": 3}, "expected_sha256": "1" * 64},
                "memberships": {"status": "configurable", "members": [{"principal_id": principal, "role": "admin", "agent_names": ["synthetic-admin"]}], "expected_sha256": "2" * 64},
                "central_retention": {"status": "configurable", "values": {"archive_after_days": 2, "inline_history_limit": 50, "invite_prune_after_days": 7, "journal_retention_days": 7, "journal_row_cap": 50000}, "defaults": {"archive_after_days": 2, "inline_history_limit": 50, "invite_prune_after_days": 7, "journal_retention_days": 7, "journal_row_cap": 50000}, "ranges": {"archive_after_days": {"minimum": 0, "maximum": 365}, "inline_history_limit": {"minimum": 1, "maximum": 500}, "invite_prune_after_days": {"minimum": 0, "maximum": 365}, "journal_retention_days": {"minimum": 0, "maximum": 365}, "journal_row_cap": {"minimum": 501, "maximum": 1000000}}, "expected_sha256": "3" * 64},
                "source_connectors": {"status": "configurable", "apply_mode": "restart-required", "desired": connector, "effective": connector, "expected_sha256": "4" * 64},
                "source_onboarding": {"status": "configurable", "apply_mode": "restart-required", "desired": onboarding, "effective": onboarding, "expected_sha256": "5" * 64},
            },
        }

    def get_dispatch(self, board_id: str, central: str | None = None) -> dict:
        self.resolve_central(central)
        if board_id != "fixture-board":
            raise KeyError(board_id)
        return {"claim_ttl_s": 900, "offer_ttl_s": 180, "broadcast_reoffer_s": 60, "second_opinion": True, "fallback_broadcast": True}

    def get_project_delivery_settings(self, central: str | None = None) -> dict:
        self.resolve_central(central)
        return {"projects": [{
            "name": "Fixture Project", "repository_configured": True, "integration_ref": "main",
            "delivery_policy_overrides": {"mode": "per_ticket_pr", "integration_branch": "pursers-integration", "mapped_base": "main", "release_trigger": {"kind": "ready"}, "validation": {"required_reviewers": 1, "test_commands": ["pytest -q"]}, "conflict_policy": "pause", "final_merge": "manual"},
            "delivery_policy": {"mode": "per_ticket_pr", "integration_branch": "pursers-integration", "mapped_base": "main", "snapshot_branch_prefix": "codex", "final_pr_target": "main"},
            "delivery_policy_provenance": {"mode": "repository"}, "delivery_policy_active": True,
            "delivery_runtime": {"ready": True, "blockers": []},
        }]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("populated", "empty", "error"), default="populated"
    )
    parser.add_argument("--ticket-count", type=int, default=6)
    parser.add_argument("--detail-delay-ms", type=int, default=0)
    parser.add_argument("--overhead-delay-ms", type=int, default=0)
    args = parser.parse_args()
    repo = args.repo.resolve()
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    dashboard = load_dashboard(repo)
    cache = AcceptanceCache(
        dashboard,
        args.mode,
        ticket_count=max(6, min(args.ticket_count, 2_000)),
        detail_delay_ms=max(0, min(args.detail_delay_ms, 30_000)),
        overhead_delay_ms=max(0, min(args.overhead_delay_ms, 30_000)),
    )
    handler = dashboard.make_handler(
        cache,
        stats_path=state_dir / "bridge-stats.json",
        worker_manager=dashboard.WorkerManager(state_dir / "workers"),
        butler_manager=dashboard.ButlerSettingsManager(state_dir / "butler"),
        deployment={"running_sha": "synthetic", "dirty": False},
    )
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
