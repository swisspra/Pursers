from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
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
            )
        )
    assert observed["submitted"] is True
    assert observed["kinds"] == frozenset({"ticket_status_changed"})


def test_all_routed_verbs_accept_board_flag() -> None:
    for role, commands in {
        "worker": [["list"], ["get", "TK-x"], ["claim", "TK-x"],
                   ["renew", "TK-x"], ["submit", "TK-x", "s", "n", "f"]],
        "reviewer": [["list"], ["list-all"], ["get", "TK-x"],
                     ["approve", "TK-x", "n"], ["reject", "TK-x", "n", "f"]],
    }.items():
        parser = generated(role)._parser()
        for command in commands:
            parsed = parser.parse_args([*command, "--board", "fullplatts"])
            assert parsed.board == "fullplatts"
