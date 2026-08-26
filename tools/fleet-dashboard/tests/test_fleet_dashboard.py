from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_dashboard", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def registry(projects: dict) -> dict:
    return {"state": {"value": json.dumps({"schema_version": 1, "projects": projects})}}


def test_registry_includes_home_and_excludes_paused_projects() -> None:
    result = dashboard.parse_project_registry(
        registry(
            {
                "Active": {
                    "board_id": "board-active",
                    "status": "active",
                    "work_dir": "/tmp/a",
                },
                "Paused": {
                    "board_id": "board-paused",
                    "status": "paused",
                    "work_dir": "/tmp/b",
                },
                "Duplicate": {
                    "board_id": "home-board",
                    "status": "active",
                    "work_dir": "/tmp/c",
                },
            }
        ),
        "home-board",
    )

    assert result == [("home-board", "home-board"), ("Active", "board-active")]


def test_agents_group_by_principal_and_name_across_board_specific_ids() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=20)).isoformat()
    rows = [
        {
            "label": "One",
            "board_id": "board-one",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-a",
                        "agent_id": "AI-one",
                        "last_activity_at": recent,
                    }
                ],
                "tickets": [
                    {
                        "ticket_id": "TK-1",
                        "title": "Work",
                        "status": "claimed",
                        "claimed_by_agent_id": "AI-one",
                    }
                ],
            },
            "events": [],
        },
        {
            "label": "Two",
            "board_id": "board-two",
            "snapshot": {
                "agents": [
                    {
                        "principal_id": "PR-1",
                        "agent_name": "worker-a",
                        "agent_id": "AI-two",
                        "last_activity_at": recent,
                    }
                ],
                "tickets": [],
            },
            "events": [],
        },
    ]

    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)

    assert len(result["agents"]) == 1
    assert result["agents"][0]["boards"] == ["board-one", "board-two"]
    assert result["agents"][0]["pool_status"] == "busy"
    assert result["pool_summary"] == {
        "online": 1,
        "busy": 1,
        "available": 0,
        "stale": 0,
    }


def test_available_and_stale_classification() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    agents = [
        {
            "principal_id": "PR-1",
            "agent_name": "recent",
            "agent_id": "AI-1",
            "last_activity_at": (now - timedelta(seconds=299)).isoformat(),
        },
        {
            "principal_id": "PR-2",
            "agent_name": "old",
            "agent_id": "AI-2",
            "last_activity_at": (now - timedelta(seconds=301)).isoformat(),
        },
    ]
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {"agents": agents, "tickets": []},
                "events": [],
            }
        ],
        stale_seconds=300,
        now=now,
    )

    assert {row["agent_name"]: row["pool_status"] for row in result["agents"]} == {
        "recent": "available",
        "old": "stale",
    }


def test_output_rows_and_titles_are_bounded() -> None:
    tickets = [
        {
            "ticket_id": f"TK-{index}",
            "title": "x" * 500,
            "status": "open",
            "updated_at": "2030-01-01T00:00:00+00:00",
        }
        for index in range(80)
    ]
    events = [{"seq": index, "kind": "ticket_created"} for index in range(80)]
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {"agents": [], "tickets": tickets, "truncated": True},
                "events": events,
            }
        ],
        stale_seconds=300,
    )
    board = result["boards"][0]

    assert len(board["tickets"]) == dashboard.MAX_TICKET_ROWS
    assert len(board["events"]) == dashboard.MAX_EVENT_ROWS
    assert (
        max(len(row["title"]) for row in board["tickets"]) <= dashboard.MAX_TITLE_CHARS
    )
    assert board["truncated"] is True


def test_non_loopback_host_is_refused() -> None:
    with pytest.raises(SystemExit):
        dashboard.parse_args(["--host", "0.0.0.0"])
