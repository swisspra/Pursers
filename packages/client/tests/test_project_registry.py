from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import pursers_client.project_registry as registry_module
from pursers_client import active_registry_boards, parse_project_registry, registry_work_dirs


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


@pytest.mark.parametrize("value", [
    {"schema_version": 2, "projects": {}},
    {"schema_version": 1, "projects": {"bad": {"board_id": "x", "work_dir": "relative", "status": "active"}}},
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
