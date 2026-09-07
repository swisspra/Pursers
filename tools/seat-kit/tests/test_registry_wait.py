from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
import pursers_client.project_registry as registry_module  # noqa: E402
from pursers_client import (  # noqa: E402
    HELD_TICKET_KINDS,
    REVIEWER_WAIT_KINDS,
    REVIEW_LEASE_EXPIRED,
    REVIEW_LEASE_RELEASED,
    WORKER_WAIT_KINDS,
    wait_for_boards,
)

SPEC = importlib.util.spec_from_file_location("seat_registry", ROOT / "seat_new.py")
assert SPEC and SPEC.loader
seat_new = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seat_new)


REGISTRY = {
    "schema_version": 1,
    "projects": {
        "home": {"board_id": "pursers", "work_dir": "/repo/home", "status": "active"},
        "other": {"board_id": "fullplatts", "work_dir": "/repo/other", "status": "active"},
        "paused": {"board_id": "paused", "work_dir": "/repo/paused", "status": "paused"},
    },
}


def generated(role: str = "worker"):
    source = seat_new._board_python(role, None, 560)
    spec = importlib.util.spec_from_loader(f"board_{role}_registry", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, f"board_{role}.py", "exec"), module.__dict__)
    return module


def test_registry_cursor_map_round_trip_and_skipped_boards() -> None:
    module = generated()
    parsed = module._parser().parse_args(
        ["wait", "--since", '{"pursers": 4, "fullplatts": 7}']
    )
    calls = []

    async def fake_wait(_client, boards, since, timeout_s, **kwargs):
        calls.append((boards, since, timeout_s, kwargs))
        return {
            "new_seq": since,
            "events": [],
            "timed_out": True,
            "waited_s": 1.0,
            "boards": ["pursers"],
            "skipped_boards": {"fullplatts": "authorization denied"},
        }

    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            module._cmd_wait(
                SimpleNamespace(identity=SimpleNamespace(agent_id="AI-seat")),
                "pursers",
                parsed.since,
                parsed.timeout,
                boards=parsed.boards,
                registry=REGISTRY,
                active_registry_boards=lambda _registry, _home: ["fullplatts", "pursers"],
                registry_work_dirs=lambda _registry: {
                    "pursers": "/repo/home",
                    "fullplatts": "/repo/other",
                },
                registry_project_work_dirs=lambda _registry: {
                    "home": "/repo/home", "other": "/repo/other"
                },
                wait_for_boards=fake_wait,
            )
        )
    result = json.loads(output.getvalue())
    assert result["new_seq"] == {"pursers": 4, "fullplatts": 7}
    assert result["skipped_boards"] == {"fullplatts": "authorization denied"}
    assert calls[0][0] == ["fullplatts", "pursers"]
    assert calls[0][3]["allow_takeover"] is True


def test_registry_wait_fails_when_every_board_is_skipped() -> None:
    module = generated()

    async def fake_wait(_client, boards, since, timeout_s, **kwargs):
        raise RuntimeError(
            "all selected boards were skipped:\n"
            "fullplatts: authorization denied\n"
            "pursers: seat collision"
        )

    with pytest.raises(
        RuntimeError,
        match="all selected boards were skipped",
    ):
        asyncio.run(
            module._cmd_wait(
                SimpleNamespace(identity=SimpleNamespace(agent_id="AI-seat")),
                "pursers",
                0,
                1,
                boards="registry",
                registry=REGISTRY,
                active_registry_boards=lambda _registry, _home: [
                    "fullplatts", "pursers"
                ],
                registry_work_dirs=lambda _registry: {},
                registry_project_work_dirs=lambda _registry: {},
                wait_for_boards=fake_wait,
            )
        )


def test_active_generated_stable_seat_resumes_registry_and_wakes_on_held_update(
    monkeypatch,
) -> None:
    module = generated()
    calls: list[tuple[str, dict[str, object]]] = []
    active_seats = {
        "pursers": "AI-home",
        "fullplatts": "AI-other",
    }

    def result(value: dict[str, object], *, error: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            is_error=error,
            structured_content={"result": value} if not error else None,
            content=[SimpleNamespace(text=str(value.get("error", "error")))],
        )

    class Raw:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, name, arguments, **_kwargs):
            calls.append((name, dict(arguments)))
            board_id = arguments["board_id"]
            if name == "board_join":
                if board_id in active_seats and not arguments.get("allow_takeover"):
                    return result(
                        {"error": "seat name already active under this principal"},
                        error=True,
                    )
                return result({
                    "agent_id": active_seats.setdefault(board_id, f"AI-{board_id}"),
                    "generation_token": f"gen-{board_id}",
                })
            if name == "ticket_get":
                return result({"ticket": {
                    "ticket_id": arguments["ticket_id"],
                    "status": "in_progress",
                    "target_url": "home/task",
                    "dispatch_state": {"state": "claimed"},
                    "claimed_by_agent_id": "AI-home",
                }})
            if name == "board_catchup":
                events = (
                    [{
                        "seq": 11,
                        "kind": "ticket_annotated",
                        "ticket_id": "TK-held",
                    }]
                    if board_id == "pursers" and arguments["cursor"] < 11
                    else []
                )
                return result({
                    "events": events,
                    "next_cursor": 11 if board_id == "pursers" else 0,
                    "has_more": False,
                })
            raise AssertionError(name)

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
        agent_name="worker-agent",
        allow_takeover=True,
        identity=SimpleNamespace(agent_id="AI-home"),
        generation_token="gen-home",
        _client=raw,
        _http=http,
        url="http://central.invalid/mcp",
    )
    monkeypatch.setattr(
        registry_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(registry_module, "Client", lambda *_args, **_kwargs: raw)

    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(module._cmd_wait(
            client,
            "pursers",
            {"pursers": 10, "fullplatts": 0},
            1,
            boards="registry",
            registry=REGISTRY,
            active_registry_boards=lambda _registry, _home: [
                "fullplatts", "pursers"
            ],
            registry_work_dirs=lambda _registry: {
                "pursers": "/repo/home", "fullplatts": "/repo/other"
            },
            registry_project_work_dirs=lambda _registry: {
                "home": "/repo/home", "other": "/repo/other"
            },
            wait_for_boards=wait_for_boards,
            dispatch_kinds=WORKER_WAIT_KINDS,
        ))

    response = json.loads(output.getvalue())
    assert response["boards"] == ["fullplatts", "pursers"]
    assert response["skipped_boards"] == {}
    assert response["reason"] == "held_ticket_update"
    assert response["events"][0]["kind"] == "ticket_annotated"
    join_calls = [arguments for name, arguments in calls if name == "board_join"]
    assert join_calls == [{
        "board_id": "fullplatts",
        "agent_name": "worker-agent",
        "capabilities": module._seat_capabilities(),
        "allow_takeover": True,
    }]


def test_boards_home_uses_legacy_scalar_wait() -> None:
    module = generated()
    parsed = module._parser().parse_args(
        ["wait", "--boards", "home", "--since", '{"pursers": 9}', "--timeout", "1"]
    )

    class HomeClient:
        identity = SimpleNamespace(agent_id="AI-seat")

        async def events(
            self, from_cursor=None, *, only_mine=True, kinds=None,
            resource_subscriptions=None, acknowledge=True, touch=None,
            cursor_callback=None,
        ):
            if cursor_callback:
                cursor_callback(from_cursor)
            if False:
                yield None

    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            module._cmd_wait(
                HomeClient(), "pursers", parsed.since, parsed.timeout,
                boards=parsed.boards,
            )
        )
    result = json.loads(output.getvalue())
    assert result["new_seq"] == 9
    assert result["timed_out"] is True


def test_reviewer_submitted_wait_fans_out_registry() -> None:
    module = generated("reviewer")
    observed = {}

    async def fake_wait(_client, boards, since, timeout_s, **kwargs):
        observed.update(kwargs)
        return {"new_seq": {board: 0 for board in boards}, "events": [], "timed_out": True,
                "waited_s": 0.0, "boards": boards, "skipped_boards": {}}

    with redirect_stdout(io.StringIO()):
        asyncio.run(
            module._cmd_wait(
                SimpleNamespace(identity=SimpleNamespace(agent_id="AI-reviewer")),
                "pursers", 0, 1, submitted=True, boards="registry", registry=REGISTRY,
                active_registry_boards=lambda _registry, _home: ["fullplatts", "pursers"],
                registry_work_dirs=lambda _registry: {},
                registry_project_work_dirs=lambda _registry: {},
                wait_for_boards=fake_wait,
                submitted_relevant_kinds=REVIEWER_WAIT_KINDS,
            )
        )
    assert observed["submitted"] is True
    assert observed["kinds"] == REVIEWER_WAIT_KINDS


def test_reviewer_home_wait_wakes_on_release_and_expiry() -> None:
    for sequence, kind in enumerate(
        (REVIEW_LEASE_RELEASED, REVIEW_LEASE_EXPIRED), start=10
    ):
        module = generated("reviewer")

        class HomeClient:
            identity = SimpleNamespace(agent_id="AI-reviewer")

            async def events(
                self, from_cursor=None, *, only_mine=True, kinds=None,
                resource_subscriptions=None, acknowledge=True, touch=None,
                cursor_callback=None,
            ):
                yield {
                    "id": f"EV-{sequence}",
                    "seq": sequence,
                    "kind": kind,
                    "ticket_id": "TK-review",
                    "status_to": "submitted",
                }

        output = io.StringIO()
        with redirect_stdout(output):
            asyncio.run(
                module._cmd_wait(
                    HomeClient(), "pursers", sequence - 1, 1,
                    submitted=True,
                    submitted_relevant_kinds=REVIEWER_WAIT_KINDS,
                )
            )
        result = json.loads(output.getvalue())
        assert result["timed_out"] is False
        assert result["events"][0]["kind"] == kind


def test_worker_home_wait_returns_only_its_dispatch_offer() -> None:
    module = generated("worker")

    class HomeClient:
        identity = SimpleNamespace(agent_id="AI-worker-b")

        async def ticket_get(self, ticket_id):
            offered_to = "AI-worker-a" if ticket_id == "TK-other" else "AI-worker-b"
            return {"ticket": {
                "ticket_id": ticket_id,
                "status": "open",
                "dispatch_state": {"state": "offered"},
                "work_offer": {"agent_id": offered_to, "expires_at": "later"},
                "tier": 1 if ticket_id == "TK-mine" else 3,
                "skills_required": ["python"],
            }}

        async def events(
            self, from_cursor=None, *, only_mine=True, kinds=None,
            resource_subscriptions=None, acknowledge=True, touch=None,
            cursor_callback=None,
        ):
            yield {"seq": 1, "kind": "ticket_created", "ticket_id": "TK-mine"}
            yield {"seq": 2, "kind": "ticket_offered", "ticket_id": "TK-other"}
            yield {"seq": 3, "kind": "ticket_offered", "ticket_id": "TK-mine"}

    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(module._cmd_wait(
            HomeClient(), "pursers", 0, 1,
            dispatch_kinds=frozenset({"ticket_offered"}),
        ))

    result = json.loads(output.getvalue())
    assert result["reason"] == "offer"
    assert [event["ticket_id"] for event in result["events"]] == ["TK-mine"]
    assert result["events"][0]["offer"]["skills_required"] == ["python"]


def test_reviewer_home_wait_suppresses_another_reviewers_offer() -> None:
    module = generated("reviewer")

    class HomeClient:
        identity = SimpleNamespace(agent_id="AI-reviewer-b")

        async def ticket_get(self, ticket_id):
            offered_to = (
                "AI-reviewer-a" if ticket_id == "TK-other" else "AI-reviewer-b"
            )
            return {"ticket": {
                "ticket_id": ticket_id,
                "status": "submitted",
                "dispatch_state": {"state": "offered"},
                "review_offer": {"agent_id": offered_to, "expires_at": "later"},
            }}

        async def events(
            self, from_cursor=None, *, only_mine=True, kinds=None,
            resource_subscriptions=None, acknowledge=True, touch=None,
            cursor_callback=None,
        ):
            yield {
                "seq": 1, "kind": "ticket_status_changed",
                "status_to": "submitted", "ticket_id": "TK-mine",
            }
            yield {"seq": 2, "kind": "review_offered", "ticket_id": "TK-other"}
            yield {"seq": 3, "kind": "review_offered", "ticket_id": "TK-mine"}

    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(module._cmd_wait(
            HomeClient(), "pursers", 0, 1, submitted=True,
            submitted_relevant_kinds=REVIEWER_WAIT_KINDS,
        ))

    result = json.loads(output.getvalue())
    assert result["reason"] == "offer"
    assert [event["ticket_id"] for event in result["events"]] == ["TK-mine"]


def test_worker_and_reviewer_held_event_wake_matrix() -> None:
    async def exercise() -> None:
        for role in ("worker", "reviewer"):
            module = generated(role)
            submitted = role == "reviewer"
            mine = f"AI-{role}"
            for kind in sorted(HELD_TICKET_KINDS):
                event = {
                    "kind": kind,
                    "ticket_id": f"TK-{role}-{kind}",
                    "reviewer_agent_id": mine,
                }
                held_ticket = {
                    "ticket_id": event["ticket_id"],
                    "status": "submitted" if submitted else "in_progress",
                    "dispatch_state": {"state": "claimed"},
                    "review_lease": {"reviewer_agent_id": mine},
                    "claimed_by_agent_id": mine,
                }
                other_ticket = {
                    **held_ticket,
                    "review_lease": {"reviewer_agent_id": "AI-other"},
                    "claimed_by_agent_id": "AI-other",
                }
                other_event = {**event, "reviewer_agent_id": "AI-other"}

                class Client:
                    identity = SimpleNamespace(agent_id=mine)

                    def __init__(self, ticket):
                        self.ticket = ticket

                    async def ticket_get(self, _ticket_id):
                        return {"ticket": self.ticket}

                selected = await module._event_for_seat(
                    Client(held_ticket), event, submitted=submitted, board_id="pursers"
                )
                assert selected is not None, (role, kind)
                assert selected["reason"] == "held_ticket_update"
                assert await module._event_for_seat(
                    Client(other_ticket),
                    other_event,
                    submitted=submitted,
                    board_id="pursers",
                ) is None, (role, kind)

        worker = generated("worker")
        reviewer = generated("reviewer")

        class BroadcastClient:
            identity = SimpleNamespace(agent_id="AI-seat")

            def __init__(self, status):
                self.status = status

            async def ticket_get(self, ticket_id):
                return {"ticket": {
                    "ticket_id": ticket_id,
                    "status": self.status,
                    "dispatch_state": {"state": "broadcast"},
                }}

        worker_event = {
            "kind": "ticket_status_changed", "ticket_id": "TK-work",
            "status_to": "open",
        }
        reviewer_event = {
            "kind": "ticket_status_changed", "ticket_id": "TK-review",
            "status_to": "submitted",
        }
        assert (await worker._event_for_seat(
            BroadcastClient("open"), worker_event,
            submitted=False, board_id="pursers",
        ))["reason"] == "broadcast"
        assert (await reviewer._event_for_seat(
            BroadcastClient("submitted"), reviewer_event,
            submitted=True, board_id="pursers",
        ))["reason"] == "broadcast"

    asyncio.run(exercise())


def test_all_routed_verbs_accept_board_flag() -> None:
    for role, commands in {
        "worker": [["list"], ["get", "TK-x"], ["claim", "TK-x"],
                   ["renew", "TK-x"], ["submit", "TK-x", "s", "n", "f"]],
        "reviewer": [["list"], ["list-all"], ["get", "TK-x"],
                     ["review-claim", "TK-x"], ["renew", "TK-x"],
                     ["review-release", "TK-x", "handoff"],
                     ["approve", "TK-x", "n"], ["reject", "TK-x", "n", "f"]],
    }.items():
        parser = generated(role)._parser()
        for command in commands:
            parsed = parser.parse_args([*command, "--board", "fullplatts"])
            assert parsed.board == "fullplatts"
