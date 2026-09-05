from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import pursers_client.project_registry as registry_module
from pursers_client import (
    DISPATCH_KINDS,
    SUBMITTED_RELEVANT_KINDS,
    active_registry_boards,
    parse_project_registry,
    registry_operator_work_dirs,
    registry_project_operator_work_dirs,
    registry_project_work_dirs,
    registry_work_dirs,
)


def state(value: object) -> dict:
    return {"state": {"value": json.dumps(value)}}


def test_registry_parser_active_boards_and_work_dirs() -> None:
    parsed = parse_project_registry(state({
        "schema_version": 1,
        "projects": {
            "home": {"board_id": "pursers", "work_dir": "/repo/home", "status": "active"},
            "alias": {"board_id": "pursers", "work_dir": "/repo/alias", "status": "active"},
            "other": {"board_id": "fullplatts", "work_dir": "/repo/other", "status": "active"},
            "paused": {"board_id": "paused", "work_dir": "/repo/paused", "status": "paused"},
        },
    }))
    assert active_registry_boards(parsed, "pursers") == ["fullplatts", "pursers"]
    assert registry_work_dirs(parsed)["fullplatts"] == "/repo/other"


def test_registry_routes_seats_to_fleet_clone_and_retains_operator_checkout() -> None:
    parsed = parse_project_registry(state({
        "schema_version": 1,
        "projects": {
            "Alpha": {
                "board_id": "alpha",
                "work_dir": "/repo/operator",
                "work_dir_owner": "operator",
                "fleet_clone_dir": "/fleet/alpha",
                "status": "active",
            },
        },
    }))

    assert registry_work_dirs(parsed) == {"alpha": "/fleet/alpha"}
    assert registry_project_work_dirs(parsed)["alpha"] == "/fleet/alpha"
    assert registry_operator_work_dirs(parsed) == {"alpha": "/repo/operator"}
    assert registry_project_operator_work_dirs(parsed)["alpha"] == "/repo/operator"


@pytest.mark.parametrize("value", [
    {"schema_version": 2, "projects": {}},
    {"schema_version": 1, "projects": {"bad": {"board_id": "x", "work_dir": "relative", "status": "active"}}},
    {"schema_version": 1, "projects": {"bad": {"board_id": "x", "work_dir": "/repo/x", "work_dir_owner": "human", "status": "active"}}},
    {"schema_version": 1, "projects": {"bad": {"board_id": "x", "work_dir": "/repo/x", "fleet_clone_dir": "relative", "status": "active"}}},
])
def test_registry_parser_rejects_invalid_schema(value: object) -> None:
    with pytest.raises(ValueError, match="project_registry"):
        parse_project_registry(state(value))


def test_registry_wait_bounds_backlog_and_round_trips_cursor(monkeypatch) -> None:
    all_events = [
        {
            "seq": seq,
            "kind": "ticket_status_changed",
            "status_to": "claimed" if seq <= 20 else "submitted",
            "ticket_id": f"TK-{seq}",
            "recipient_identities": ["AI-reviewer"],
        }
        for seq in range(1, 25)
    ]
    catchup_calls = 0

    def result(value: dict) -> SimpleNamespace:
        return SimpleNamespace(
            is_error=False, structured_content={"result": value}, content=[]
        )

    class Raw:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, name, arguments, **_kwargs):
            nonlocal catchup_calls
            if name == "board_join":
                return result({"agent_id": "AI-reviewer", "generation_token": "gen"})
            if name == "ticket_get":
                return result({"ticket": {"target_url": "home/item"}})
            assert name == "board_catchup"
            catchup_calls += 1
            remaining = [
                event for event in all_events if event["seq"] > arguments["cursor"]
            ]
            page = remaining[:2]
            return result({
                "events": page,
                "next_cursor": page[-1]["seq"] if page else arguments["cursor"],
                "has_more": len(remaining) > len(page),
            })

        @asynccontextmanager
        async def listen(self, **_kwargs):
            async def empty():
                if False:
                    yield None

            yield empty()

    raw = Raw()

    @asynccontextmanager
    async def http():
        yield None

    client = SimpleNamespace(
        board_id="pursers",
        agent_name="reviewer",
        identity=SimpleNamespace(agent_id="AI-reviewer"),
        _client=raw,
        _http=http,
        url="http://central.invalid/mcp",
    )
    monkeypatch.setattr(registry_module, "streamable_http_client", lambda *_a, **_k: object())
    monkeypatch.setattr(registry_module, "Client", lambda *_a, **_k: raw)

    cursor: int | dict[str, int] = 0
    seen = []
    serialized_sizes = []
    first = asyncio.run(registry_module.wait_for_boards(
        client,
        ["pursers"],
        cursor,
        1,
        kinds={"ticket_status_changed"},
        submitted=True,
        project_work_dirs={"home": "/repo/home"},
    ))
    assert first["events"] == []
    assert first["new_seq"] == {"pursers": 16}
    assert first["timed_out"] is True
    assert catchup_calls == registry_module.MAX_CATCHUP_PAGES_PER_BOARD
    serialized_sizes.append(len(json.dumps(first)))
    cursor = first["new_seq"]

    for expected_seq in range(21, 25):
        response = asyncio.run(registry_module.wait_for_boards(
            client,
            ["pursers"],
            cursor,
            1,
            kinds={"ticket_status_changed"},
            submitted=True,
            project_work_dirs={"home": "/repo/home"},
        ))
        assert len(response["events"]) == registry_module.MAX_EVENTS_PER_BOARD
        assert response["events"][0]["seq"] == expected_seq
        assert response["new_seq"] == {"pursers": expected_seq}
        serialized_sizes.append(len(json.dumps(response)))
        seen.extend(event["seq"] for event in response["events"])
        cursor = response["new_seq"]

    assert seen == [21, 22, 23, 24]
    assert max(serialized_sizes) < 1_000


@pytest.mark.parametrize(
    ("submitted", "event_kind", "offer_key", "mine", "other"),
    [
        (False, "ticket_offered", "work_offer", "AI-worker-b", "AI-worker-a"),
        (True, "review_offered", "review_offer", "AI-reviewer-b", "AI-reviewer-a"),
    ],
)
def test_registry_wait_returns_only_this_seats_dispatch_offer(
    monkeypatch, submitted, event_kind, offer_key, mine, other
) -> None:
    calls: list[tuple[str, dict]] = []
    tickets = {
        "TK-other": {
            "ticket_id": "TK-other",
            "status": "submitted" if submitted else "open",
            "target_url": "home/item",
            "dispatch_state": {"state": "offered"},
            offer_key: {"agent_id": other, "expires_at": "later"},
        },
        "TK-mine": {
            "ticket_id": "TK-mine",
            "status": "submitted" if submitted else "open",
            "target_url": "home/item",
            "dispatch_state": {"state": "offered"},
            offer_key: {"agent_id": mine, "expires_at": "later"},
            "tier": 3,
            "skills_required": ["dispatch"],
        },
    }

    def result(value: dict) -> SimpleNamespace:
        return SimpleNamespace(
            is_error=False, structured_content={"result": value}, content=[]
        )

    class Raw:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, name, arguments, **_kwargs):
            calls.append((name, arguments))
            if name == "board_join":
                return result({"agent_id": mine, "generation_token": "gen"})
            if name == "ticket_get":
                return result({"ticket": tickets[arguments["ticket_id"]]})
            if name == "board_catchup":
                return result({
                    "events": [
                        {
                            "seq": 1,
                            "kind": "ticket_status_changed" if submitted else "ticket_created",
                            "status_to": "submitted" if submitted else "open",
                            "ticket_id": "TK-mine",
                        },
                        {"seq": 2, "kind": event_kind, "ticket_id": "TK-other"},
                        {"seq": 3, "kind": event_kind, "ticket_id": "TK-mine"},
                    ],
                    "next_cursor": 3,
                    "has_more": False,
                })
            raise AssertionError(name)

        @asynccontextmanager
        async def listen(self, **_kwargs):
            async def empty():
                if False:
                    yield None

            yield empty()

    @asynccontextmanager
    async def http():
        yield None

    raw = Raw()
    monkeypatch.setattr(
        registry_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(registry_module, "Client", lambda *_args, **_kwargs: raw)
    client = SimpleNamespace(
        board_id="pursers",
        agent_name="seat-b",
        identity=SimpleNamespace(agent_id=mine),
        _client=raw,
        _http=http,
        url="http://central.invalid/mcp",
    )
    capabilities = {"tier_max": 3, "skills": ["dispatch"]}
    response = asyncio.run(registry_module.wait_for_boards(
        client,
        ["pursers"],
        0,
        1,
        kinds=(
            SUBMITTED_RELEVANT_KINDS
            if submitted
            else DISPATCH_KINDS | {"ticket_created", "ticket_status_changed"}
        ),
        submitted=submitted,
        work_dirs={"pursers": "/repo/home"},
        project_work_dirs={"home": "/repo/home"},
        capabilities=capabilities,
    ))

    assert [event["ticket_id"] for event in response["events"]] == ["TK-mine"]
    assert response["reason"] == "offer"
    assert response["events"][0]["offer"] == {
        "ticket_id": "TK-mine",
        "board_id": "pursers",
        "expires_at": "later",
        "tier": 3,
        "skills_required": ["dispatch"],
    }
    join = next(arguments for name, arguments in calls if name == "board_join")
    assert join["capabilities"] == capabilities
