#!/usr/bin/env python3
"""Serve the Fleet UI against deterministic public-safe acceptance data."""

from __future__ import annotations

import argparse
import importlib.util
import sys
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
    def __init__(self, dashboard, mode: str = "populated") -> None:
        self.dashboard = dashboard
        self.mode = mode
        self.revision = 0

    @staticmethod
    def labels() -> list[str]:
        return ["fixture"]

    @staticmethod
    def resolve_central(value: str | None) -> str:
        if value not in {None, "fixture"}:
            raise KeyError(value)
        return "fixture"

    @staticmethod
    def _tickets() -> list[dict]:
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
        return rows

    @staticmethod
    def _agents() -> list[dict]:
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
            rows.append(
                {
                    "agent_name": name,
                    "agent_id": f"AI-synthetic-{index + 1:02d}",
                    "principal_id": f"PR-synthetic-{index + 1:02d}",
                    "pool_status": "busy" if working else "stale" if stale else "available",
                    "boards": ["fixture-board"],
                    "board_scope": ["fixture-board"],
                    "duplicate_name": False,
                    "last_seen": "2030-01-02T11:59:00Z" if not stale else "2030-01-02T10:00:00Z",
                    "seats": [
                        {
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
                        }
                    ],
                    "current_work": work,
                }
            )
        return rows

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
                "generated_at": fleet["generated_at"],
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

    def get_project_registry(self, central: str | None = None) -> dict:
        self.resolve_central(central)
        return {
            "registry": {"schema_version": 1, "projects": {}},
            "expected_sha256": "b" * 64,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("populated", "empty", "error"), default="populated"
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    dashboard = load_dashboard(repo)
    cache = AcceptanceCache(dashboard, args.mode)
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
