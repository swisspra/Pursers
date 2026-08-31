from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self

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
                        "lifecycle_status": "active",
                        "membership_role": "member",
                        "status": "working",
                    }
                ],
                "tickets": [
                    {
                        "ticket_id": "TK-current",
                        "title": "Current work",
                        "status": "claimed",
                        "claimed_by_agent_id": "AI-one",
                        "updated_at": recent,
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
                        "lifecycle_status": "active",
                        "status": "active",
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
    assert result["agents"][0]["seats"] == [
        {
            "board_id": "board-one",
            "project": "One",
            "role": "member",
            "current_ticket_id": "TK-current",
            "current_ticket_title": "Current work",
            "last_seen": recent,
        },
        {
            "board_id": "board-two",
            "project": "Two",
            "role": None,
            "current_ticket_id": None,
            "current_ticket_title": None,
            "last_seen": recent,
        },
    ]
    assert result["agents"][0]["duplicate_name"] is False
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


def test_agent_projection_marks_busy_when_claim_is_outside_ticket_window() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [
                        {
                            "principal_id": "PR-1",
                            "agent_name": "worker",
                            "agent_id": "AI-1",
                            "last_activity_at": now.isoformat(),
                            "lifecycle_status": "active",
                            "status": "working",
                        }
                    ],
                    "tickets": [
                        {
                            "ticket_id": f"TK-{index:04d}",
                            "status": "closed",
                        }
                        for index in range(50)
                    ],
                    "omitted_counts": {"tickets": 1},
                    "truncated": True,
                },
                "events": [],
            }
        ],
        stale_seconds=300,
        now=now,
    )

    assert result["agents"][0]["pool_status"] == "busy"
    assert result["pool_summary"]["busy"] == 1


@pytest.mark.parametrize("lifecycle_status", ["handed_off", "inactive"])
def test_non_active_lifecycle_is_not_busy(lifecycle_status: str) -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [
                        {
                            "principal_id": "PR-1",
                            "agent_name": "worker",
                            "last_activity_at": now.isoformat(),
                            "lifecycle_status": lifecycle_status,
                            "status": "working",
                        }
                    ],
                    "tickets": [],
                },
                "events": [],
            }
        ],
        stale_seconds=300,
        now=now,
    )

    assert result["agents"][0]["pool_status"] == "available"


def test_truncated_ticket_counts_are_rendered_as_lower_bounds() -> None:
    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {
                    "agents": [],
                    "tickets": [
                        {"ticket_id": "TK-open", "status": "open"},
                        {"ticket_id": "TK-claimed", "status": "claimed"},
                    ],
                    "omitted_counts": {"tickets": 3},
                    "truncated": True,
                },
                "events": [],
            }
        ],
        stale_seconds=300,
    )

    assert result["boards"][0]["counts"] == {
        "open": ">=1",
        "claimed": ">=1",
        "submitted": ">=0",
        "closed_today": ">=0",
    }


def test_ticket_table_filters_before_bounding_and_includes_submitted() -> None:
    tickets = [
        {"ticket_id": f"TK-closed-{index:03d}", "status": "closed"}
        for index in range(dashboard.SNAPSHOT_LIMIT)
    ]
    tickets.extend(
        [
            {"ticket_id": "TK-open", "status": "open"},
            {"ticket_id": "TK-submitted", "status": "submitted"},
            {"ticket_id": "TK-claimed", "status": "claimed"},
        ]
    )

    result = dashboard.aggregate_fleet(
        [
            {
                "label": "Board",
                "board_id": "board",
                "snapshot": {"agents": [], "tickets": tickets},
                "events": [],
            }
        ],
        stale_seconds=300,
    )

    rows = result["boards"][0]["tickets"]
    assert [row["id"] for row in rows] == [
        "TK-claimed",
        "TK-submitted",
        "TK-open",
    ]


def test_fetcher_requests_central_max_snapshot_bounds() -> None:
    requested: dict[str, int | None] = {}

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_snapshot(
            self, *, limit: int | None = None, max_bytes: int | None = None
        ) -> dict:
            requested.update(limit=limit, max_bytes=max_bytes)
            return {"latest_seq": 0, "agents": [], "tickets": []}

        async def board_catchup(self, **_kwargs: object) -> dict:
            return {"events": []}

    config = dashboard.Config(
        url="https://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5.0,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())

    asyncio.run(fetcher._read_board("Board", "board"))

    assert requested == {"limit": 1_000, "max_bytes": 300_000}


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


def test_detail_projection_is_byte_bounded_and_drops_unbounded_history() -> None:
    tickets = [
        {
            "ticket_id": f"TK-{index:04d}",
            "title": "title " + "x" * 500,
            "description": "d" * 20_000,
            "status": "closed" if index % 2 else "claimed",
            "required_fields": ["field-" + "r" * 200 for _ in range(40)],
            "updated_at": f"2030-01-01T00:{index % 60:02d}:00+00:00",
            "submission_history": [
                {"summary": "s" * 20_000, "notes": "n" * 100_000}
            ],
            "review_label": "independent-review",
        }
        for index in range(dashboard.SNAPSHOT_LIMIT)
    ]
    detail = dashboard.project_board_detail(
        {
            "label": "Board",
            "board_id": "board",
            "snapshot": {
                "tickets": tickets,
                "total_counts": {"tickets": len(tickets)},
            },
            "events": [
                {"seq": index, "kind": "ticket_status_changed", "ticket_id": "TK-0000"}
                for index in range(150)
            ],
        }
    )
    body = dashboard._json_bytes(detail)

    assert len(body) <= dashboard.API_MAX_BYTES
    assert detail["ticket_omitted"] > 0
    assert detail["ticket_returned"] == len(detail["tickets"])
    assert len(detail["events"]) <= dashboard.DETAIL_EVENT_SCAN_LIMIT
    assert [event["seq"] for event in detail["events"]] == sorted(
        event["seq"] for event in detail["events"]
    )
    assert b"submission_history" not in body
    assert b'"notes"' not in body


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/board/board-1", "board-1"),
        ("/api/board/board-1?fresh=1", "board-1"),
        ("/api/board/", None),
        ("/api/board/board/child", None),
        ("/api/board/%2Fetc", None),
        ("/api/board/board%20name", None),
        ("/other/board-1", None),
    ],
)
def test_detail_route_fallback_is_safe(path: str, expected: str | None) -> None:
    assert dashboard.board_id_from_api_path(path) == expected


def test_unknown_board_does_not_accumulate_detail_cache_entries() -> None:
    class Fetcher:
        async def fetch(self) -> dict:
            return {}

        async def fetch_board(self, board_id: str) -> dict:
            raise KeyError(board_id)

    cache = dashboard.DashboardCache(Fetcher(), 5.0)

    with pytest.raises(KeyError):
        cache.get_board("unknown-board")

    assert cache._details == {}


def test_fetch_board_uses_bounded_snapshot_and_catchup() -> None:
    calls: list[tuple[str, dict]] = []

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_state_get(self, **kwargs: object) -> dict:
            calls.append(("board_state_get", dict(kwargs)))
            return registry(
                {
                    "Board": {
                        "board_id": "board-detail",
                        "status": "active",
                        "work_dir": "/tmp/board",
                    }
                }
            )

        async def board_snapshot(self, **kwargs: object) -> dict:
            calls.append(("board_snapshot", dict(kwargs)))
            return {"latest_seq": 9, "agents": [], "tickets": []}

        async def board_catchup(self, **kwargs: object) -> dict:
            calls.append(("board_catchup", dict(kwargs)))
            return {"events": []}

    config = dashboard.Config(
        url="https://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="home-board",
        agent_name="viewer",
        stale_seconds=300,
        cache_seconds=5.0,
    )
    fetcher = dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: Client())

    result = asyncio.run(fetcher.fetch_board("board-detail"))

    assert result["board"]["board_id"] == "board-detail"
    assert ("board_snapshot", {"limit": 1_000, "max_bytes": 300_000}) in calls
    assert (
        "board_catchup",
        {
            "cursor": 0,
            "limit": 100,
            "ack": False,
            "max_events": 100,
            "max_bytes": 100_000,
        },
    ) in calls


def test_non_loopback_host_is_refused() -> None:
    with pytest.raises(SystemExit):
        dashboard.parse_args(["--host", "0.0.0.0"])


@pytest.mark.parametrize(
    ("contents", "expected_status"),
    [(None, "missing"), ("not-json", "malformed")],
)
def test_overhead_endpoint_returns_empty_for_missing_or_malformed_stats(
    contents: str | None, expected_status: str
) -> None:
    class Cache:
        def get(self) -> dict:
            return {}

        def get_board(self, _board_id: str) -> dict:
            return {}

    with tempfile.TemporaryDirectory() as raw:
        stats = Path(raw) / "stats.json"
        if contents is not None:
            stats.write_text(contents, encoding="utf-8")
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.make_handler(Cache(), stats)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/overhead"
            ) as response:
                result = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    assert result["source_status"] == expected_status
    assert result["seats"] == []
    assert result["note"] == "protocol overhead (estimated), not provider billing"


def test_overhead_projection_aggregates_today_and_seven_day_tools() -> None:
    now = datetime(2030, 1, 7, 12, tzinfo=timezone.utc)
    document = {
        "schema_version": 1,
        "days": {
            "2030-01-01": {
                "seats": {
                    "old": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "request_bytes": 40,
                        "response_bytes": 60,
                        "calls": {
                            "ticket_list": {
                                "count": 1,
                                "request_bytes": 40,
                                "response_bytes": 60,
                            }
                        },
                    }
                }
            },
            "2030-01-07": {
                "seats": {
                    "today": {
                        "board_id": "board",
                        "agent_name": "worker",
                        "request_bytes": 100,
                        "response_bytes": 300,
                        "calls": {
                            "board_catchup": {
                                "count": 2,
                                "request_bytes": 100,
                                "response_bytes": 300,
                            }
                        },
                    }
                }
            },
        },
    }
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = dashboard.read_overhead_stats(path, now=now)

    seat = result["seats"][0]
    assert seat["today_bytes"] == 400
    assert seat["today_estimated_tokens"] == 100
    assert seat["seven_day_bytes"] == 500
    assert seat["seven_day_estimated_tokens"] == 125
    assert seat["seven_day_calls"] == 3
    assert [tool["tool"] for tool in seat["top_tools"]] == [
        "board_catchup",
        "ticket_list",
    ]


def test_findings_panel_projection_handles_absent_present_and_truncated_state() -> None:
    absent = dashboard.project_board_detail(
        {
            "label": "Board",
            "board_id": "board",
            "snapshot": {"tickets": [], "state": {}},
            "events": [],
        }
    )
    assert absent["coordinator_findings"] is None

    findings = [
        {
            "kind": f"finding-{index}",
            "level": "critical" if index == 0 else "warn",
            "message": "x" * 700,
            "ticket_id": "TK-one",
        }
        for index in range(dashboard.MAX_FINDINGS + 3)
    ]
    present = dashboard.project_board_detail(
        {
            "label": "Board",
            "board_id": "board",
            "snapshot": {
                "tickets": [],
                "state": {
                    "coordinator_findings": {
                        "value": json.dumps(
                            {"findings": findings, "truncated_count": 2}
                        )
                    }
                },
            },
            "events": [],
        }
    )["coordinator_findings"]

    assert len(present["items"]) == dashboard.MAX_FINDINGS
    assert present["truncated_count"] == 5
    assert present["items"][0]["level"] == "critical"
    assert len(present["items"][0]["text"]) <= dashboard.MAX_FINDING_CHARS
    assert "Coordinator findings" in dashboard.HTML
    assert "/api/overhead" in dashboard.HTML
